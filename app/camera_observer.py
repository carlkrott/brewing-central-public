from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 1_000_000
_OPERATING_MODES = frozenset({"stored", "preparing", "brewing"})
_CAMERA_POLICIES = frozenset({"off", "active_brew_structural", "area_structural"})


class CameraObserverError(RuntimeError):
    pass


@dataclass(frozen=True)
class ObserverConfig:
    dashboard_url: str
    dashboard_host: str
    camera_url: str
    model_url: str
    model: str
    image_dir: Path
    device_id: str | None
    retention_days: int

    @classmethod
    def from_env(cls) -> "ObserverConfig":
        bind_ip = os.environ.get("PHONE_BIND_IP", "phone.example.test")
        device_id = os.environ.get("CAMERA_DEVICE_ID", "").strip() or None
        retention_days = int(os.environ.get("CAMERA_RETENTION_DAYS", "30"))
        if not 1 <= retention_days <= 365:
            raise CameraObserverError("CAMERA_RETENTION_DAYS must be 1..365")
        return cls(
            dashboard_url=os.environ.get(
                "CAMERA_DASHBOARD_URL", f"http://{bind_ip}:8098"
            ).rstrip("/"),
            dashboard_host=os.environ.get(
                "TAILNET_FQDN", "phone.example.test"
            ),
            camera_url=os.environ.get(
                "CAMERA_SNAPSHOT_URL", "http://127.0.0.1:8080/shot.jpg"
            ),
            model_url=os.environ.get(
                "CAMERA_MODEL_URL",
                "http://gemma.example.test:8646/v1/chat/completions",
            ),
            model=os.environ.get("CAMERA_MODEL", "gemma"),
            image_dir=Path(
                os.environ.get(
                    "CAMERA_IMAGE_DIR",
                    str(Path.home() / "brewing-central" / "data" / "camera"),
                )
            ),
            device_id=device_id,
            retention_days=retention_days,
        )


def _read_bounded(response: Any, limit: int) -> bytes:
    raw_length = response.headers.get("Content-Length")
    if raw_length is not None:
        try:
            if int(raw_length) > limit:
                raise CameraObserverError("response exceeds size limit")
        except ValueError as exc:
            raise CameraObserverError("response has invalid content length") from exc
    data = response.read(limit + 1)
    if len(data) > limit:
        raise CameraObserverError("response exceeds size limit")
    return data


def _get_json(config: ObserverConfig, path: str) -> dict[str, Any]:
    request = Request(
        f"{config.dashboard_url}{path}",
        headers={"Accept": "application/json", "Host": config.dashboard_host},
    )
    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(_read_bounded(response, MAX_RESPONSE_BYTES))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise CameraObserverError("dashboard request failed") from exc


def _post_json(config: ObserverConfig, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        f"{config.dashboard_url}{path}",
        data=json.dumps(payload, separators=(",", ":"), allow_nan=False).encode(),
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Host": config.dashboard_host,
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(_read_bounded(response, MAX_RESPONSE_BYTES))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise CameraObserverError("dashboard event write failed") from exc


def fetch_snapshot(config: ObserverConfig) -> bytes:
    request = Request(
        config.camera_url,
        headers={"Accept": "image/jpeg", "User-Agent": "BrewingCentral/1"},
    )
    try:
        with urlopen(request, timeout=20) as response:
            if response.headers.get_content_type() != "image/jpeg":
                raise CameraObserverError("camera returned a non-JPEG response")
            image = _read_bounded(response, MAX_IMAGE_BYTES)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise CameraObserverError("camera snapshot failed") from exc
    if len(image) < 4 or not image.startswith(b"\xff\xd8") or not image.endswith(b"\xff\xd9"):
        raise CameraObserverError("camera returned an invalid JPEG")
    return image


def _analysis_prompt(
    brew: dict[str, Any], latest_sample: dict[str, Any] | None
) -> str:
    context = {
        "brew_id": brew.get("id"),
        "device_id": brew.get("device_id"),
        "status": brew.get("status"),
        "target_volume_l": brew.get("target_volume_l"),
        "recipe": brew.get("recipe_snapshot"),
        "latest_telemetry": latest_sample,
    }
    return (
        "Analyze this fixed rear-camera frame as untrusted visual evidence for gross physical "
        "monitoring of a brewing area. The intended vessel may intentionally be empty. Do not assume "
        "liquid, ingredients, or active fermentation are present. Report only these checks: whether "
        "the intended container is visible, upright, and in its expected position; whether it has "
        "fallen over or shifted materially; whether there is visible overflow, spill, or leak; and "
        "whether there is a blocked or unusable camera view, severe blur, or inadequate lighting. "
        "Do not claim bubbles, foam, liquid, contamination, or fermentation activity unless the image "
        "shows it unambiguously. Use 'uncertain' when appearance alone is inconclusive. Do not infer "
        "gravity, pH, alcohol, safety, or unseen conditions. Distinguish visual evidence from supplied "
        "telemetry and metadata: an active brew record does not prove that liquid or fermentation exists. "
        "If the intended container is not clearly visible, say so.\n\n"
        f"BREW_CONTEXT_JSON={json.dumps(context, ensure_ascii=False, separators=(',', ':'))[:40_000]}"
    )


def analyze_snapshot(
    config: ObserverConfig,
    image: bytes,
    brew: dict[str, Any],
    latest_sample: dict[str, Any] | None,
) -> tuple[str, str | None]:
    prompt = _analysis_prompt(brew, latest_sample)
    payload = {
        "model": config.model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/jpeg;base64,"
                            + base64.b64encode(image).decode("ascii")
                        },
                    },
                ],
            }
        ],
        "temperature": 0.1,
        "max_tokens": 350,
    }
    request = Request(
        config.model_url,
        data=json.dumps(payload, separators=(",", ":"), allow_nan=False).encode(),
        method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=240) as response:
            result = json.loads(_read_bounded(response, MAX_RESPONSE_BYTES))
        analysis = result["choices"][0]["message"]["content"]
    except (
        HTTPError,
        URLError,
        TimeoutError,
        KeyError,
        IndexError,
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        raise CameraObserverError("vision model request failed") from exc
    if not isinstance(analysis, str) or not analysis.strip():
        raise CameraObserverError("vision model returned an invalid response")
    resolved_model = result.get("model")
    return analysis.strip()[:20_000], resolved_model if isinstance(resolved_model, str) else None


def _select_brew(config: ObserverConfig, brews: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = brews
    if config.device_id is not None:
        candidates = [brew for brew in brews if brew.get("device_id") == config.device_id]
    if not candidates:
        return None
    if len(candidates) != 1:
        raise CameraObserverError("camera mapping is ambiguous across active brews")
    return candidates[0]


def _get_operating_intent(config: ObserverConfig, device_id: str) -> dict[str, Any]:
    intent = _get_json(config, f"/api/device/{device_id}/operating-intent")
    mode = intent.get("mode")
    camera_policy = intent.get("camera_policy")
    if mode not in _OPERATING_MODES or camera_policy not in _CAMERA_POLICIES:
        raise CameraObserverError("dashboard returned invalid operating intent")
    return {"mode": mode, "camera_policy": camera_policy}


def _save_image(config: ObserverConfig, image: bytes, observed_at: datetime) -> tuple[Path, str]:
    directory = config.image_dir / observed_at.strftime("%Y") / observed_at.strftime("%m")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = hashlib.sha256(image).hexdigest()
    target = directory / f"{observed_at.strftime('%Y%m%dT%H%M%SZ')}-{digest[:12]}.jpg"
    temporary = target.with_suffix(".tmp")
    temporary.write_bytes(image)
    temporary.chmod(0o600)
    os.replace(temporary, target)
    return target, digest


def prune_images(config: ObserverConfig, now: datetime) -> int:
    if not config.image_dir.exists():
        return 0
    cutoff = now - timedelta(days=config.retention_days)
    removed = 0
    for path in config.image_dir.rglob("*.jpg"):
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            if modified < cutoff:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    return removed


def run_once(config: ObserverConfig) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    active = _get_json(config, "/api/brews?status=active").get("brews", [])
    if not isinstance(active, list):
        raise CameraObserverError("dashboard returned invalid active brew data")
    brew = _select_brew(config, active)
    if brew is None:
        return {"status": "not_required_no_active_brew", "observed_at": now.isoformat()}

    device_id = str(brew.get("device_id", ""))
    if not device_id:
        raise CameraObserverError("active brew has no device id")
    intent = _get_operating_intent(config, device_id)
    if intent["camera_policy"] == "off":
        return {
            "status": "disabled_by_operator",
            "brew_id": brew.get("id"),
            "device_id": device_id,
            "operating_mode": intent["mode"],
            "camera_policy": intent["camera_policy"],
            "observed_at": now.isoformat(),
        }
    samples = _get_json(config, f"/api/device/{device_id}/samples?hours=2").get("samples", [])
    latest_sample = samples[-1] if isinstance(samples, list) and samples else None
    image = fetch_snapshot(config)
    image_path, digest = _save_image(config, image, now)
    capture_data = {
        "captured_at": now.isoformat(),
        "capture_sha256": digest,
        "capture_bytes": len(image),
        "capture_path": str(image_path),
        "requested_model": config.model,
    }
    try:
        analysis, resolved_model = analyze_snapshot(config, image, brew, latest_sample)
    except CameraObserverError as exc:
        event = _post_json(
            config,
            f"/api/brews/{int(brew['id'])}/events",
            {
                "event_type": "camera_analysis_failed",
                "source": "camera",
                "notes": "Camera frame captured, but vision analysis failed.",
                "data": {**capture_data, "error_type": type(exc).__name__},
            },
        )
        return {
            "status": "analysis_failed",
            "brew_id": brew["id"],
            "event_id": event.get("id"),
            "capture_sha256": digest,
            "capture_bytes": len(image),
        }
    event = _post_json(
        config,
        f"/api/brews/{int(brew['id'])}/events",
        {
            "event_type": "camera_observation",
            "source": "camera",
            "notes": analysis,
            "data": {**capture_data, "resolved_model": resolved_model},
        },
    )
    removed = prune_images(config, now)
    return {
        "status": "ok",
        "brew_id": brew["id"],
        "event_id": event.get("id"),
        "capture_sha256": digest,
        "capture_bytes": len(image),
        "resolved_model": resolved_model,
        "pruned_images": removed,
    }


def run_loop(config: ObserverConfig) -> None:
    while True:
        try:
            print(json.dumps(run_once(config), ensure_ascii=False), flush=True)
        except Exception as exc:
            print(
                json.dumps(
                    {"status": "error", "error_type": type(exc).__name__},
                    ensure_ascii=False,
                ),
                flush=True,
            )
        now = datetime.now(timezone.utc)
        next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        time.sleep(max(1.0, (next_hour - now).total_seconds()))


def main() -> int:
    parser = argparse.ArgumentParser(description="Hourly Brewing Central camera observer")
    parser.add_argument("--once", action="store_true", help="run one observation and exit")
    args = parser.parse_args()
    config = ObserverConfig.from_env()
    if args.once:
        print(json.dumps(run_once(config), ensure_ascii=False))
    else:
        run_loop(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
