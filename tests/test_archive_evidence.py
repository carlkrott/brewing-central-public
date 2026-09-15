"""W5 archive backend vertical slice: frozen evidence, append-only annotations, recipe forks."""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest


def _device(client, device_id: str = "brew-device") -> None:
    response = client.post(
        "/api/ingest",
        json={"ID": device_id, "angle": 24.0, "gravity": 1.0, "temperature": 20.0},
    )
    assert response.status_code == 200


def _recipe_payload(name: str = "Archive fixture recipe") -> dict:
    return {
        "name": name,
        "style": "fruit wine",
        "description": "Frozen snapshot fixture for W5.",
        "base_volume_l": 10.0,
        "notes": "Initial notes.",
        "ingredients": [
            {
                "name": "Blackberries",
                "quantity": 4.0,
                "unit": "kg",
                "category": "fruit",
                "scaling": {"mode": "linear"},
            },
            {
                "name": "Starter culture",
                "quantity": 0.25,
                "unit": "kg",
                "category": "culture",
                "scaling": {"mode": "fixed"},
            },
        ],
    }


def _completed_brew(client) -> dict:
    _device(client)
    recipe = client.post("/api/recipes", json=_recipe_payload()).json()
    brew = client.post(
        "/api/brews",
        json={
            "device_id": "brew-device",
            "recipe_id": recipe["id"],
            "target_volume_l": 30.0,
        },
    ).json()
    client.post(
        f"/api/brews/{brew['id']}/events",
        json={
            "event_type": "feeding",
            "source": "manual",
            "notes": "First nutrient feed.",
            "data": {"sugar_g": 75},
        },
    )
    client.post(f"/api/brews/{brew['id']}/stop", json={"outcome": "completed", "notes": "OK"})
    client.post(f"/api/brews/{brew['id']}/archive-evidence")
    return client.get(f"/api/brews/{brew['id']}").json()


def _completed_brew_unfrozen(client) -> dict:
    _device(client)
    recipe = client.post("/api/recipes", json=_recipe_payload()).json()
    brew = client.post(
        "/api/brews",
        json={
            "device_id": "brew-device",
            "recipe_id": recipe["id"],
            "target_volume_l": 30.0,
        },
    ).json()
    client.post(f"/api/brews/{brew['id']}/stop", json={"outcome": "completed", "notes": "OK"})
    return client.get(f"/api/brews/{brew['id']}").json()


# ---------- freeze_archive_evidence -----------------------------------------


def test_freeze_archive_evidence_is_idempotent_and_deterministic(test_client):
    brew = _completed_brew(test_client)
    first = test_client.post(f"/api/brews/{brew['id']}/archive-evidence")
    assert first.status_code == 200, first.text
    payload = first.json()
    assert payload["brew_run_id"] == brew["id"]
    assert payload["evidence_hash"] == hashlib.sha256(
        (payload["source_snapshot_json"] + "|" + payload["source_events_json"]).encode("utf-8")
    ).hexdigest()
    # Round-trip snapshot matches brew_events exactly.
    stored_events = json.loads(payload["source_events_json"])
    expected_events = [
        {
            "id": row["id"],
            "brew_run_id": row["brew_run_id"],
            "event_type": row["event_type"],
            "event_at": row["event_at"],
            "source": row["source"],
            "notes": row["notes"],
            "data": row["data"],
            "client_request_id": row["client_request_id"],
        }
        for row in brew["events"]
    ]
    assert stored_events == expected_events
    # Source snapshot equals the brew's immutable snapshot.
    assert json.loads(payload["source_snapshot_json"]) == brew["recipe_snapshot"]
    # Freeze again returns the same bundle and hash.
    second = test_client.post(f"/api/brews/{brew['id']}/archive-evidence")
    assert second.status_code == 200
    assert second.json()["id"] == payload["id"]
    assert second.json()["evidence_hash"] == payload["evidence_hash"]


def test_freeze_archive_evidence_survives_recipe_updates(test_client, app_module):
    brew = _completed_brew(test_client)
    frozen = test_client.post(f"/api/brews/{brew['id']}/archive-evidence").json()
    frozen_snapshot = json.loads(frozen["source_snapshot_json"])
    # Mutate the source recipe after the freeze.
    payload = copy.deepcopy(_recipe_payload("Archive fixture recipe"))
    payload["expected_revision"] = 1
    payload["name"] = "Archive fixture recipe v2"
    assert test_client.put(f"/api/recipes/{brew['recipe_id']}", json=payload).status_code == 200
    # Frozen bundle remains byte-identical and unchanged.
    with sqlite3.connect(app_module.BREW_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT source_snapshot_json, evidence_hash FROM archive_evidence_bundles WHERE id=?",
            (frozen["id"],),
        ).fetchone()
    assert row["source_snapshot_json"] == frozen["source_snapshot_json"]
    assert row["evidence_hash"] == frozen["evidence_hash"]
    # And the snapshot is exactly the historical recipe content, not today's revision.
    assert json.loads(row["source_snapshot_json"])["name"] == frozen_snapshot["name"]


def test_freeze_archive_evidence_rejects_active_and_missing_brew(test_client):
    _device(test_client)
    recipe = test_client.post("/api/recipes", json=_recipe_payload()).json()
    brew = test_client.post(
        "/api/brews",
        json={"device_id": "brew-device", "recipe_id": recipe["id"], "target_volume_l": 30.0},
    ).json()
    active = test_client.post(f"/api/brews/{brew['id']}/archive-evidence")
    assert active.status_code == 409
    assert active.json()["detail"]["code"] == "brew_not_terminal"

    missing = test_client.post("/api/brews/999999/archive-evidence")
    assert missing.status_code == 404


# ---------- annotations -----------------------------------------------------


def test_archive_annotations_append_only_with_revision_chain(test_client):
    brew = _completed_brew(test_client)
    test_client.post(f"/api/brews/{brew['id']}/archive-evidence")

    first = test_client.post(
        f"/api/brews/{brew['id']}/archive-annotations",
        json={
            "classification": "operator_post_brew",
            "origin": "operator",
            "content": {"notes": "First tasting note."},
        },
    )
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["revision_no"] == 1
    assert body["parent_annotation_id"] is None

    second = test_client.post(
        f"/api/brews/{brew['id']}/archive-annotations",
        json={
            "classification": "tasting_outcome",
            "origin": "operator",
            "content": {"notes": "Refined note."},
            "parent_annotation_id": body["id"],
        },
    )
    assert second.status_code == 201, second.text
    body2 = second.json()
    assert body2["revision_no"] == 2
    assert body2["parent_annotation_id"] == body["id"]

    third = test_client.post(
        f"/api/brews/{brew['id']}/archive-annotations",
        json={
            "classification": "hypothesis",
            "origin": "operator",
            "content": {"notes": "Third."},
            "parent_annotation_id": body2["id"],
        },
    )
    assert third.status_code == 201
    assert third.json()["revision_no"] == 3

    listed = test_client.get(f"/api/brews/{brew['id']}/archive-annotations").json()
    revisions = [row["revision_no"] for row in listed["annotations"]]
    assert revisions == [1, 2, 3]
    parents = [row["parent_annotation_id"] for row in listed["annotations"]]
    assert parents == [None, body["id"], body2["id"]]


def test_archive_annotation_rejects_invalid_origin_parent_and_classification(test_client):
    brew = _completed_brew(test_client)
    test_client.post(f"/api/brews/{brew['id']}/archive-evidence")

    # Invalid classification -> 422.
    bad_class = test_client.post(
        f"/api/brews/{brew['id']}/archive-annotations",
        json={
            "classification": "not_real",
            "origin": "operator",
            "content": {"notes": "x"},
        },
    )
    assert bad_class.status_code == 422

    # Model origin must be rejected (operator endpoint only).
    bad_origin = test_client.post(
        f"/api/brews/{brew['id']}/archive-annotations",
        json={
            "classification": "operator_post_brew",
            "origin": "model",
            "content": {"notes": "x"},
        },
    )
    assert bad_origin.status_code == 422

    # Parent from another brew must be rejected.
    other = _completed_brew(test_client)
    other_first = test_client.post(
        f"/api/brews/{other['id']}/archive-annotations",
        json={
            "classification": "operator_post_brew",
            "origin": "operator",
            "content": {"notes": "x"},
        },
    ).json()

    cross = test_client.post(
        f"/api/brews/{brew['id']}/archive-annotations",
        json={
            "classification": "operator_post_brew",
            "origin": "operator",
            "content": {"notes": "x"},
            "parent_annotation_id": other_first["id"],
        },
    )
    assert cross.status_code == 409

    # Parent that is not the immediately previous revision must be rejected.
    created = test_client.post(
        f"/api/brews/{brew['id']}/archive-annotations",
        json={
            "classification": "operator_post_brew",
            "origin": "operator",
            "content": {"notes": "x"},
        },
    ).json()
    test_client.post(
        f"/api/brews/{brew['id']}/archive-annotations",
        json={
            "classification": "tasting_outcome",
            "origin": "operator",
            "content": {"notes": "y"},
            "parent_annotation_id": created["id"],
        },
    )
    skip_parent = test_client.post(
        f"/api/brews/{brew['id']}/archive-annotations",
        json={
            "classification": "hypothesis",
            "origin": "operator",
            "content": {"notes": "z"},
            "parent_annotation_id": created["id"],  # not the immediately previous revision
        },
    )
    assert skip_parent.status_code == 409


def test_archive_annotation_requires_frozen_bundle(test_client):
    brew = _completed_brew_unfrozen(test_client)
    # Without freeze, an annotation request must fail.
    response = test_client.post(
        f"/api/brews/{brew['id']}/archive-annotations",
        json={
            "classification": "operator_post_brew",
            "origin": "operator",
            "content": {"notes": "x"},
        },
    )
    assert response.status_code == 409


def test_archive_annotation_history_is_immutable(test_client):
    brew = _completed_brew(test_client)
    test_client.post(f"/api/brews/{brew['id']}/archive-evidence")
    first = test_client.post(
        f"/api/brews/{brew['id']}/archive-annotations",
        json={
            "classification": "operator_post_brew",
            "origin": "operator",
            "content": {"notes": "Original"},
        },
    ).json()
    test_client.post(
        f"/api/brews/{brew['id']}/archive-annotations",
        json={
            "classification": "tasting_outcome",
            "origin": "operator",
            "content": {"notes": "Second"},
            "parent_annotation_id": first["id"],
        },
    )
    listed = test_client.get(f"/api/brews/{brew['id']}/archive-annotations").json()
    assert listed["annotations"][0]["content"] == first["content"]
    assert listed["annotations"][0]["content_hash"] == first["content_hash"]


# ---------- recipe fork + lineage -------------------------------------------


def test_fork_recipe_from_archive_creates_disconnected_recipe(test_client, app_module):
    brew = _completed_brew(test_client)
    fork = test_client.post(
        "/api/archive/fork-recipe",
        json={
            "source_recipe_id": brew["recipe_id"],
            "source_recipe_revision": brew["recipe_snapshot"]["revision"],
            "source_brew_run_id": brew["id"],
            "new_name": "Forked draft recipe",
        },
    )
    assert fork.status_code == 201, fork.text
    body = fork.json()
    assert body["id"] != brew["recipe_id"]
    assert body["name"] == "Forked draft recipe"
    # Snapshot child must mirror the historical snapshot's ingredient material identity.
    source_keys = {"name", "quantity", "unit", "category", "material_type", "scaling"}
    by_name_fork = {item["name"]: item for item in body["ingredients"]}
    for source_item in brew["recipe_snapshot"]["ingredients"]:
        fork_item = by_name_fork[source_item["name"]]
        for key in source_keys:
            assert fork_item[key] == source_item[key]
    # New recipe should have fresh UUID keys, not source keys.
    source_ingredient_keys = {
        ing["ingredient_key"] for ing in brew["recipe_snapshot"]["ingredients"]
    }
    fork_ingredient_keys = {ing["ingredient_key"] for ing in body["ingredients"]}
    assert source_ingredient_keys.isdisjoint(fork_ingredient_keys)
    # Child starts at revision 1.
    assert body["revision"] == 1

    # Lineage row is recorded.
    lineage = test_client.get(f"/api/recipes/{body['id']}/lineage").json()
    assert lineage["child_recipe_id"] == body["id"]
    assert lineage["source_recipe_id"] == brew["recipe_id"]
    assert lineage["source_recipe_revision"] == brew["recipe_snapshot"]["revision"]
    assert lineage["source_brew_run_id"] == brew["id"]
    assert lineage["fork_kind"] == "archive_improved_draft"


def test_fork_recipe_keeps_source_history_unchanged(test_client, app_module):
    brew = _completed_brew(test_client)
    frozen = test_client.post(f"/api/brews/{brew['id']}/archive-evidence").json()
    before_snapshot = json.loads(frozen["source_snapshot_json"])

    fork = test_client.post(
        "/api/archive/fork-recipe",
        json={
            "source_recipe_id": brew["recipe_id"],
            "source_recipe_revision": brew["recipe_snapshot"]["revision"],
            "source_brew_run_id": brew["id"],
            "new_name": "Forked draft",
        },
    ).json()

    # Brew history and evidence bundle are unchanged after the fork.
    brew_after = test_client.get(f"/api/brews/{brew['id']}").json()
    assert brew_after["recipe_snapshot"] == brew["recipe_snapshot"]
    after_frozen = test_client.post(f"/api/brews/{brew['id']}/archive-evidence").json()
    assert after_frozen["source_snapshot_json"] == frozen["source_snapshot_json"]
    assert json.loads(after_frozen["source_snapshot_json"]) == before_snapshot

    # Source recipe was not mutated.
    with sqlite3.connect(app_module.BREW_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT name, revision, updated_at FROM recipes WHERE id=?",
            (brew["recipe_id"],),
        ).fetchone()
        assert row["name"] == brew["recipe_snapshot"]["name"]

    # No new events on the source brew.
    events = brew_after["events"]
    assert len(events) == len(brew["events"])


def test_fork_recipe_rejects_invalid_inputs(test_client):
    brew = _completed_brew(test_client)
    base = {
        "source_recipe_id": brew["recipe_id"],
        "source_recipe_revision": brew["recipe_snapshot"]["revision"],
        "source_brew_run_id": brew["id"],
        "new_name": "Bad fork",
    }

    # Missing source_recipe_id
    bad = test_client.post("/api/archive/fork-recipe", json={**base, "source_recipe_id": 0})
    assert bad.status_code == 422

    # Wrong source_recipe_revision
    wrong_rev = test_client.post(
        "/api/archive/fork-recipe",
        json={**base, "source_recipe_revision": brew["recipe_snapshot"]["revision"] + 99},
    )
    assert wrong_rev.status_code == 409

    # Unknown source_brew_run_id
    missing_brew = test_client.post(
        "/api/archive/fork-recipe",
        json={**base, "source_brew_run_id": 999999},
    )
    assert missing_brew.status_code == 404

    # Duplicate child lineage (replay with the same source) is rejected.
    first = test_client.post("/api/archive/fork-recipe", json=base)
    assert first.status_code == 201
    duplicate = test_client.post(
        "/api/archive/fork-recipe",
        json={**base, "new_name": "Different name still conflicts"},
    )
    assert duplicate.status_code == 409


def test_fork_recipe_rejects_active_brew(test_client):
    _device(test_client)
    recipe = test_client.post("/api/recipes", json=_recipe_payload()).json()
    brew = test_client.post(
        "/api/brews",
        json={"device_id": "brew-device", "recipe_id": recipe["id"], "target_volume_l": 30.0},
    ).json()
    response = test_client.post(
        "/api/archive/fork-recipe",
        json={
            "source_recipe_id": recipe["id"],
            "source_recipe_revision": 1,
            "source_brew_run_id": brew["id"],
            "new_name": "Should not work",
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "brew_not_terminal"


def test_empty_archive_lists_are_well_formed(test_client):
    # No brews yet: empty evidence + empty annotations.
    brew = _completed_brew(test_client)
    other_id = brew["id"] + 9999  # non-existent
    assert test_client.post(f"/api/brews/{other_id}/archive-evidence").status_code == 404
    empty_annotations = test_client.get(f"/api/brews/{other_id}/archive-annotations")
    assert empty_annotations.status_code == 404
    # Valid brew with no annotations yet returns an empty list (after freeze).
    test_client.post(f"/api/brews/{brew['id']}/archive-evidence")
    empty_list = test_client.get(f"/api/brews/{brew['id']}/archive-annotations").json()
    assert empty_list["annotations"] == []


# ---------- W5 concurrency + ValidationError routing -----------------------


def test_freeze_archive_evidence_concurrent_store_calls_share_one_bundle(
    test_client, app_module
):
    """Concurrent direct store freezes must never raise IntegrityError.

    A ThreadPoolExecutor + barrier hammers freeze_archive_evidence from
    multiple threads; every caller must return the same bundle (id, hash).
    """
    brew = _completed_brew_unfrozen(test_client)
    brew_id = brew["id"]
    store = app_module.BREWING_STORE

    workers = 8
    barrier = threading.Barrier(workers)
    bundles: list[dict] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def call_freeze() -> None:
        barrier.wait(timeout=5.0)
        try:
            bundle = store.freeze_archive_evidence(brew_id)
        except BaseException as exc:  # noqa: BLE001 - capture for assertion
            with lock:
                errors.append(exc)
            return
        with lock:
            bundles.append(bundle)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(call_freeze) for _ in range(workers)]
        for future in futures:
            future.result(timeout=10.0)

    assert errors == [], f"concurrent freezes raised: {errors!r}"
    assert len(bundles) == workers
    first = bundles[0]
    assert all(b["id"] == first["id"] for b in bundles)
    assert all(b["evidence_hash"] == first["evidence_hash"] for b in bundles)
    # Exactly one bundle row exists for this brew.
    with sqlite3.connect(app_module.BREW_DB_PATH) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM archive_evidence_bundles WHERE brew_run_id=?",
            (brew_id,),
        ).fetchone()[0]
    assert count == 1


def test_fork_recipe_returns_422_for_malformed_persisted_snapshot(
    test_client, app_module
):
    """A persisted terminal brew snapshot that fails _snapshot_to_fork_payload
    must surface as HTTP 422 with stable code ``fork_source_snapshot_invalid``.
    """
    brew = _completed_brew_unfrozen(test_client)
    brew_id = brew["id"]

    # Mutate the persisted recipe snapshot to an invalid structure:
    # base_volume_l is below the ge=1.0 constraint on RecipePayload, which
    # causes RecipePayload.model_validate to raise pydantic.ValidationError.
    malformed_snapshot = {
        "name": "Forked draft",
        "style": "",
        "description": "",
        "base_volume_l": 0.1,  # invalid: ge=1.0
        "beverage_type": None,
        "initial_fermenter_volume_l": None,
        "target_metrics": {},
        "notes": "",
        "revision": 1,
        "ingredients": [
            {
                "ingredient_key": str(uuid.uuid4()),
                "name": "Broken ingredient",
                "quantity": 1.0,
                "unit": "kg",
                "category": "fruit",
                "scaling": {"mode": "linear"},
            }
        ],
        "culture_profiles": [],
        "scheduled_additions": [],
        "process_steps": [],
    }

    with sqlite3.connect(app_module.BREW_DB_PATH) as conn:
        conn.execute(
            "UPDATE brew_runs SET recipe_snapshot_json=? WHERE id=?",
            (json.dumps(malformed_snapshot, ensure_ascii=False), brew_id),
        )
        conn.commit()

    response = test_client.post(
        "/api/archive/fork-recipe",
        json={
            "source_recipe_id": brew["recipe_id"],
            "source_recipe_revision": 1,
            "source_brew_run_id": brew_id,
            "new_name": "Should not be created",
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "fork_source_snapshot_invalid"

    # And no child recipe was written.
    with sqlite3.connect(app_module.BREW_DB_PATH) as conn:
        child_count = conn.execute(
            "SELECT COUNT(*) FROM recipe_lineage WHERE source_brew_run_id=?",
            (brew_id,),
        ).fetchone()[0]
    assert child_count == 0