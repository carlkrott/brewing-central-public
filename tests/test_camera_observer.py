from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app import camera_observer


def config(tmp_path: Path, *, device_id: str | None = None) -> camera_observer.ObserverConfig:
    return camera_observer.ObserverConfig(
        dashboard_url="http://127.0.0.1:8098",
        dashboard_host="phone.example.test",
        camera_url="http://127.0.0.1:8080/shot.jpg",
        model_url="http://gemma.example.test:8090/v1/chat/completions",
        model="gemma",
        image_dir=tmp_path / "camera",
        device_id=device_id,
        retention_days=30,
    )


def test_analysis_prompt_treats_empty_vessel_as_valid_and_checks_gross_anomalies() -> None:
    prompt = camera_observer._analysis_prompt(
        {"id": 7, "device_id": "ispindel-7", "status": "active"},
        {"angle": 24.2},
    )

    assert "may intentionally be empty" in prompt
    assert "Do not assume liquid" in prompt
    assert "fallen over" in prompt
    assert "overflow, spill, or leak" in prompt
    assert "blocked or unusable camera view" in prompt
    assert "Do not claim bubbles" in prompt
    assert "does not prove that liquid or fermentation exists" in prompt


def test_run_once_skips_capture_without_active_brew(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(camera_observer, "_get_json", lambda *_: {"brews": []})
    monkeypatch.setattr(
        camera_observer,
        "fetch_snapshot",
        lambda *_: pytest.fail("camera must not run without an active brew"),
    )

    result = camera_observer.run_once(config(tmp_path))

    assert result["status"] == "not_required_no_active_brew"
    assert not (tmp_path / "camera").exists()


def test_select_brew_requires_unambiguous_mapping(tmp_path: Path) -> None:
    brews = [{"id": 1, "device_id": "a"}, {"id": 2, "device_id": "b"}]

    with pytest.raises(camera_observer.CameraObserverError, match="ambiguous"):
        camera_observer._select_brew(config(tmp_path), brews)

    assert camera_observer._select_brew(config(tmp_path, device_id="b"), brews) == brews[1]
    assert camera_observer._select_brew(config(tmp_path, device_id="missing"), brews) is None


def test_run_once_hashes_evidence_and_binds_camera_event(
    monkeypatch, tmp_path: Path
) -> None:
    image = b"\xff\xd8camera-evidence\xff\xd9"
    brew = {
        "id": 7,
        "device_id": "ispindel-7",
        "status": "active",
        "target_volume_l": 30,
        "recipe_snapshot": {"name": "Blackberry"},
    }
    latest = {"ts": "2026-09-12T13:00:00+00:00", "angle": 24.2}
    posted: dict[str, Any] = {}

    def fake_get(_config: camera_observer.ObserverConfig, path: str) -> dict[str, Any]:
        if path == "/api/brews?status=active":
            return {"brews": [brew]}
        if path == "/api/device/ispindel-7/operating-intent":
            return {"mode": "brewing", "camera_policy": "active_brew_structural"}
        assert path == "/api/device/ispindel-7/samples?hours=2"
        return {"samples": [{"angle": 23.9}, latest]}

    def fake_analyze(
        _config: camera_observer.ObserverConfig,
        supplied_image: bytes,
        supplied_brew: dict[str, Any],
        supplied_latest: dict[str, Any] | None,
    ) -> tuple[str, str | None]:
        assert supplied_image == image
        assert supplied_brew == brew
        assert supplied_latest == latest
        return "Vessel visible; no overflow visible.", "gemma-vision"

    def fake_post(
        _config: camera_observer.ObserverConfig,
        path: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        posted.update({"path": path, "payload": payload})
        return {"id": 42}

    monkeypatch.setattr(camera_observer, "_get_json", fake_get)
    monkeypatch.setattr(camera_observer, "fetch_snapshot", lambda *_: image)
    monkeypatch.setattr(camera_observer, "analyze_snapshot", fake_analyze)
    monkeypatch.setattr(camera_observer, "_post_json", fake_post)

    result = camera_observer.run_once(config(tmp_path))

    expected_hash = hashlib.sha256(image).hexdigest()
    assert result == {
        "status": "ok",
        "brew_id": 7,
        "event_id": 42,
        "capture_sha256": expected_hash,
        "capture_bytes": len(image),
        "resolved_model": "gemma-vision",
        "pruned_images": 0,
    }
    assert posted["path"] == "/api/brews/7/events"
    payload = posted["payload"]
    assert payload["event_type"] == "camera_observation"
    assert payload["source"] == "camera"
    assert payload["notes"] == "Vessel visible; no overflow visible."
    assert payload["data"]["capture_sha256"] == expected_hash
    stored = list((tmp_path / "camera").rglob("*.jpg"))
    assert len(stored) == 1
    assert stored[0].read_bytes() == image
    assert os.stat(stored[0]).st_mode & 0o777 == 0o600


def test_prune_images_removes_only_expired_jpegs(tmp_path: Path) -> None:
    observer_config = config(tmp_path)
    observer_config.image_dir.mkdir(parents=True)
    old = observer_config.image_dir / "old.jpg"
    recent = observer_config.image_dir / "recent.jpg"
    unrelated = observer_config.image_dir / "keep.txt"
    for path in (old, recent, unrelated):
        path.write_bytes(b"evidence")
    now = datetime.now(timezone.utc)
    old_timestamp = (now - timedelta(days=31)).timestamp()
    os.utime(old, (old_timestamp, old_timestamp))

    removed = camera_observer.prune_images(observer_config, now)

    assert removed == 1
    assert not old.exists()
    assert recent.exists()
    assert unrelated.exists()


def test_run_once_records_analysis_failure_without_fabricating_observation(
    monkeypatch, tmp_path: Path
) -> None:
    image = b"\xff\xd8failed-analysis\xff\xd9"
    brew = {"id": 9, "device_id": "ispindel-9", "status": "active"}
    posted: dict[str, Any] = {}

    def fake_get(_config: camera_observer.ObserverConfig, path: str) -> dict[str, Any]:
        if path == "/api/brews?status=active":
            return {"brews": [brew]}
        if path == "/api/device/ispindel-9/operating-intent":
            return {"mode": "brewing", "camera_policy": "active_brew_structural"}
        return {"samples": []}

    def fail_analysis(*_args) -> tuple[str, str | None]:
        raise camera_observer.CameraObserverError("provider unavailable")

    def fake_post(
        _config: camera_observer.ObserverConfig,
        path: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        posted.update({"path": path, "payload": payload})
        return {"id": 43}

    monkeypatch.setattr(camera_observer, "_get_json", fake_get)
    monkeypatch.setattr(camera_observer, "fetch_snapshot", lambda *_: image)
    monkeypatch.setattr(camera_observer, "analyze_snapshot", fail_analysis)
    monkeypatch.setattr(camera_observer, "_post_json", fake_post)

    result = camera_observer.run_once(config(tmp_path))

    assert result["status"] == "analysis_failed"
    assert result["event_id"] == 43
    assert posted["path"] == "/api/brews/9/events"
    assert posted["payload"]["event_type"] == "camera_analysis_failed"
    assert posted["payload"]["source"] == "camera"
    assert "provider unavailable" not in posted["payload"]["notes"]
