from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

import pytest


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_PATH = ROOT / "scripts" / "deploy" / "deploy.py"
CONTRACT = ROOT / "contracts" / "ispindel-predecessor-expected-missing-amendment-v3.json"
PREDECESSOR = ROOT / "descriptors" / "predecessor-production.json"
VERSION = "ispindel-predecessor-expected-missing/2026-08-06-v3"
EXPECTED_PATH = "/tmp/ispindel-phase06a1-exact-build-20260730T023218Z-114bd2aa2a0dfe0943295766b177da0f/compose.override.yml"


@pytest.fixture(scope="module")
def deploy() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ispindel_deploy_amendment", DEPLOY_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rewrite_contract(tmp_path: Path, mutate: Callable[[dict[str, Any]], None]) -> Path:
    value: dict[str, Any] = json.loads(CONTRACT.read_text())
    mutate(value)
    unsigned = {key: item for key, item in value.items() if key != "contract_sha256"}
    value["contract_sha256"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    target = tmp_path / "amendment.json"
    target.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    return target


def _load(deploy: ModuleType, path: Path = CONTRACT) -> tuple[Path, dict[str, object], list[str]]:
    predecessor = json.loads(PREDECESSOR.read_text())
    return deploy.load_expected_missing_amendment(path, PREDECESSOR, predecessor)


def test_amendment_is_self_digesting_and_exactly_bound(deploy: ModuleType) -> None:
    path, value, expected = _load(deploy)
    assert path == CONTRACT.resolve()
    assert value["contract_version"] == VERSION
    assert expected == []
    assert value["contract_sha256"] == "282eab89407ae44410d5a3b5cca8f9c0eee7f1fc3073456c33a989a21b50736b"


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", "wrong/v1"),
        ("contract_version", "wrong-version"),
        ("source_release.release_id", "20260803T162826Z-000000000000"),
        ("source_release.manifest_sha256", "0" * 64),
        ("predecessor_descriptor.sha256", "0" * 64),
    ],
)
def test_closed_contract_bindings_reject_drift(
    tmp_path: Path, deploy: ModuleType, field: str, value: str,
) -> None:
    def mutate(payload: dict[str, Any]) -> None:
        cursor: dict[str, Any] = payload
        parts = field.split(".")
        for part in parts[:-1]:
            cursor = cursor[part]
        cursor[parts[-1]] = value

    with pytest.raises(deploy.DeploymentError):
        _load(deploy, _rewrite_contract(tmp_path, mutate))


@pytest.mark.parametrize(
    "field,value",
    [
        ("activation", {"allow_flag": "--unsafe", "version_flag": "--expected-missing-contract-version"}),
        ("snapshot_policy", {"complete_labelled_inventory_required": False}),
        ("rollback_policy", {"compose_inputs": "all-labelled-paths"}),
        ("prohibitions", ["changed"]),
        ("self_excluding_digest_construction", {"algorithm": "sha256"}),
    ],
)
def test_closed_policy_bindings_reject_semantic_drift(
    tmp_path: Path, deploy: ModuleType, field: str, value: object,
) -> None:
    target = _rewrite_contract(tmp_path, lambda payload: payload.__setitem__(field, value))
    with pytest.raises(deploy.DeploymentError):
        _load(deploy, target)


@pytest.mark.parametrize(
    "paths",
    [
        [EXPECTED_PATH + ".bak"],
        [EXPECTED_PATH, "/tmp/other"],
        [EXPECTED_PATH.replace("compose.override.yml", "compose")],
    ],
)
def test_expected_missing_set_cannot_be_widened_or_fuzzed(
    tmp_path: Path, deploy: ModuleType, paths: list[str],
) -> None:
    target = _rewrite_contract(tmp_path, lambda value: value.__setitem__("expected_missing_paths", paths))
    with pytest.raises(deploy.DeploymentError, match="expected-missing"):
        _load(deploy, target)


def test_malformed_self_digest_is_rejected(tmp_path: Path, deploy: ModuleType) -> None:
    value = json.loads(CONTRACT.read_text())
    value["contract_sha256"] = "0" * 64
    target = tmp_path / "amendment.json"
    target.write_text(json.dumps(value, sort_keys=True) + "\n")
    with pytest.raises(deploy.DeploymentError, match="self-digest"):
        _load(deploy, target)


def test_unknown_contract_key_is_rejected(tmp_path: Path, deploy: ModuleType) -> None:
    target = _rewrite_contract(tmp_path, lambda value: value.__setitem__("unexpected", True))
    with pytest.raises(deploy.DeploymentError, match="keys"):
        _load(deploy, target)


def test_amendment_path_rejects_symlink(tmp_path: Path, deploy: ModuleType) -> None:
    link = tmp_path / "amendment.json"
    link.symlink_to(CONTRACT)
    with pytest.raises(deploy.DeploymentError, match="regular non-symlink"):
        _load(deploy, link)


@pytest.mark.parametrize(
    "allow,version",
    [
        (False, None),
        (True, None),
        (False, VERSION),
        (True, "wrong-version"),
    ],
)
def test_missing_observation_requires_both_exact_activation_keys(
    deploy: ModuleType, allow: bool, version: str | None,
) -> None:
    _, value, _ = _load(deploy)
    with pytest.raises(deploy.DeploymentError, match="two-key"):
        deploy.require_expected_missing_activation(value, allow=allow, version=version)


def test_both_exact_activation_keys_are_accepted(deploy: ModuleType) -> None:
    _, value, _ = _load(deploy)
    deploy.require_expected_missing_activation(value, allow=True, version=VERSION)


def test_observed_absence_must_equal_exact_contract_set(deploy: ModuleType) -> None:
    rows = {
        "/srv/ispindel/docker-compose.yml": {"present": True},
        EXPECTED_PATH: {"present": False},
    }
    present, missing = deploy.require_compose_observation(rows, list(rows), [EXPECTED_PATH])
    assert present == ["/srv/ispindel/docker-compose.yml"]
    assert missing == [EXPECTED_PATH]
    rows["/srv/ispindel/docker-compose.yml"] = {"present": False}
    with pytest.raises(deploy.DeploymentError, match="exactly match"):
        deploy.require_compose_observation(rows, list(rows), [EXPECTED_PATH])


def test_expected_missing_path_becoming_present_is_drift(deploy: ModuleType) -> None:
    rows = {
        "/srv/ispindel/docker-compose.yml": {"present": True},
        EXPECTED_PATH: {"present": True},
    }
    with pytest.raises(deploy.DeploymentError, match="exactly match"):
        deploy.require_compose_observation(rows, list(rows), [EXPECTED_PATH])
