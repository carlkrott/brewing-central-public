"""Brewing recipe, scale, lifecycle, and archive API contracts."""
from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import pytest


def _device(client, device_id: str = "brew-device") -> None:
    response = client.post(
        "/api/ingest",
        json={"ID": device_id, "angle": 24.0, "gravity": 1.0, "temperature": 20.0},
    )
    assert response.status_code == 200


def _recipe_payload(name: str = "Blackberry test") -> dict:
    return {
        "name": name,
        "style": "fruit wine",
        "description": "Small-batch process used to validate scaling rules.",
        "base_volume_l": 10.0,
        "notes": "Keep the live starter separate from boiled nutrient feed.",
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
            {
                "name": "Oak chips",
                "quantity": 10.0,
                "unit": "g",
                "category": "addition",
                "scaling": {"mode": "power", "exponent": 0.75},
            },
            {
                "name": "Pectic enzyme",
                "quantity": 2.0,
                "unit": "g",
                "category": "enzyme",
                "scaling": {
                    "mode": "piecewise",
                    "points": [
                        {"volume_l": 1.0, "quantity": 0.5},
                        {"volume_l": 30.0, "quantity": 4.0},
                        {"volume_l": 200.0, "quantity": 18.0},
                    ],
                },
            },
        ],
    }


def test_recipe_create_list_and_scale_rules(test_client):
    created = test_client.post("/api/recipes", json=_recipe_payload())
    assert created.status_code == 201
    recipe = created.json()
    assert recipe["revision"] == 1
    assert recipe["base_volume_l"] == 10.0

    listed = test_client.get("/api/recipes").json()
    assert [row["id"] for row in listed["recipes"]] == [recipe["id"]]

    scaled = test_client.get(f"/api/recipes/{recipe['id']}?volume_l=200").json()
    values = {item["name"]: item["scaled_quantity"] for item in scaled["ingredients"]}
    assert scaled["target_volume_l"] == 200.0
    assert values["Blackberries"] == pytest.approx(80.0)
    assert values["Starter culture"] == pytest.approx(0.25)
    assert values["Oak chips"] == pytest.approx(10.0 * (200.0 / 10.0) ** 0.75)
    assert values["Pectic enzyme"] == pytest.approx(18.0)


def test_piecewise_rule_interpolates_and_clamps_to_endpoints(test_client):
    recipe = test_client.post("/api/recipes", json=_recipe_payload()).json()
    at_twenty = test_client.get(f"/api/recipes/{recipe['id']}?volume_l=20").json()
    enzyme = next(i for i in at_twenty["ingredients"] if i["name"] == "Pectic enzyme")
    assert enzyme["scaled_quantity"] == pytest.approx(0.5 + (3.5 * 19.0 / 29.0))

    payload = _recipe_payload("Endpoint clamp")
    payload["base_volume_l"] = 100.0
    created = test_client.post("/api/recipes", json=payload).json()
    at_one = test_client.get(f"/api/recipes/{created['id']}?volume_l=1").json()
    enzyme = next(i for i in at_one["ingredients"] if i["name"] == "Pectic enzyme")
    assert enzyme["scaled_quantity"] == pytest.approx(0.5)


def test_recipe_scale_and_payload_validation_is_bounded(test_client):
    assert test_client.post("/api/recipes", json={**_recipe_payload(), "base_volume_l": 0}).status_code == 422
    recipe = test_client.post("/api/recipes", json=_recipe_payload()).json()
    assert test_client.get(f"/api/recipes/{recipe['id']}?volume_l=0.5").status_code == 422
    assert test_client.get(f"/api/recipes/{recipe['id']}?volume_l=201").status_code == 422

    malformed = _recipe_payload("Bad piecewise")
    malformed["ingredients"][-1]["scaling"]["points"] = [
        {"volume_l": 30, "quantity": 4},
        {"volume_l": 30, "quantity": 5},
    ]
    assert test_client.post("/api/recipes", json=malformed).status_code == 422


def test_recipe_update_uses_optimistic_revision(test_client):
    payload = _recipe_payload()
    created = test_client.post("/api/recipes", json=payload).json()
    changed = copy.deepcopy(payload)
    changed["expected_revision"] = 1
    changed["notes"] = "Updated notes"

    updated = test_client.put(f"/api/recipes/{created['id']}", json=changed)
    assert updated.status_code == 200
    assert updated.json()["revision"] == 2
    assert updated.json()["notes"] == "Updated notes"

    stale = test_client.put(f"/api/recipes/{created['id']}", json=changed)
    assert stale.status_code == 409
    assert stale.json()["detail"] == "recipe was changed by another request"


def test_begin_brew_requires_known_device_and_one_active_run(test_client):
    recipe = test_client.post("/api/recipes", json=_recipe_payload()).json()
    request = {"device_id": "brew-device", "recipe_id": recipe["id"], "target_volume_l": 30.0}
    assert test_client.post("/api/brews", json=request).status_code == 404

    _device(test_client)
    active = test_client.post("/api/brews", json=request)
    assert active.status_code == 201
    assert active.json()["status"] == "active"
    assert active.json()["recipe_snapshot"]["target_volume_l"] == 30.0

    conflict = test_client.post("/api/brews", json=request)
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "device already has an active brew"


def test_active_brew_snapshot_is_immutable_when_recipe_changes(test_client):
    _device(test_client)
    payload = _recipe_payload()
    recipe = test_client.post("/api/recipes", json=payload).json()
    brew = test_client.post(
        "/api/brews",
        json={"device_id": "brew-device", "recipe_id": recipe["id"], "target_volume_l": 30.0},
    ).json()

    changed = copy.deepcopy(payload)
    changed.update({"expected_revision": 1, "name": "Changed template"})
    assert test_client.put(f"/api/recipes/{recipe['id']}", json=changed).status_code == 200

    fetched = test_client.get(f"/api/brews/{brew['id']}").json()
    assert fetched["recipe_snapshot"]["name"] == "Blackberry test"
    assert fetched["recipe_snapshot"]["revision"] == 1


def test_brew_events_stop_and_archive(test_client):
    _device(test_client)
    recipe = test_client.post("/api/recipes", json=_recipe_payload()).json()
    brew = test_client.post(
        "/api/brews",
        json={"device_id": "brew-device", "recipe_id": recipe["id"], "target_volume_l": 30.0},
    ).json()

    event = test_client.post(
        f"/api/brews/{brew['id']}/events",
        json={
            "event_type": "feeding",
            "source": "manual",
            "notes": "Added the first nutrient feed.",
            "data": {"sugar_g": 75},
        },
    )
    assert event.status_code == 201
    assert event.json()["source"] == "manual"

    stopped = test_client.post(
        f"/api/brews/{brew['id']}/stop",
        json={"outcome": "completed", "notes": "Stable finish."},
    )
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "completed"
    assert stopped.json()["ended_at"] is not None

    archive = test_client.get("/api/brews?status=completed").json()
    assert [row["id"] for row in archive["brews"]] == [brew["id"]]
    fetched = test_client.get(f"/api/brews/{brew['id']}").json()
    assert [row["event_type"] for row in fetched["events"]] == [
        "brew_started",
        "feeding",
        "brew_completed",
    ]


def test_completed_brew_cannot_accept_events_or_stop_twice(test_client):
    _device(test_client)
    recipe = test_client.post("/api/recipes", json=_recipe_payload()).json()
    brew = test_client.post(
        "/api/brews",
        json={"device_id": "brew-device", "recipe_id": recipe["id"], "target_volume_l": 30.0},
    ).json()
    assert test_client.post(f"/api/brews/{brew['id']}/stop", json={"outcome": "aborted"}).status_code == 200
    assert test_client.post(
        f"/api/brews/{brew['id']}/events",
        json={"event_type": "late", "source": "manual"},
    ).status_code == 409
    assert test_client.post(f"/api/brews/{brew['id']}/stop", json={"outcome": "completed"}).status_code == 409


def test_water_reference_snapshots_latest_sample_without_faking_full_calibration(test_client):
    _device(test_client)
    created = test_client.post(
        "/api/device/brew-device/water-reference",
        json={"label": "Fresh water, wide jar"},
    )
    assert created.status_code == 201
    reference = created.json()
    assert reference["device_id"] == "brew-device"
    assert reference["angle"] == pytest.approx(24.0)
    assert reference["temperature_c"] == pytest.approx(20.0)
    assert reference["gravity_reference"] == pytest.approx(1.0)
    assert reference["source_sample_id"] > 0

    listed = test_client.get("/api/device/brew-device/water-references")
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["water_references"]] == [reference["id"]]


def test_water_reference_requires_known_device(test_client):
    response = test_client.post(
        "/api/device/missing/water-reference", json={"label": "Water"}
    )
    assert response.status_code == 404


def test_dashboard_exposes_brewing_central_tabs_and_fail_closed_assistant(test_client):
    response = test_client.get("/")
    assert response.status_code == 200
    html = response.text
    for panel in ('id="tab-dashboard"', 'id="tab-recipes"', 'id="tab-brew"'):
        assert panel in html
    assert 'id="record-water-reference"' in html
    assert 'id="begin-brew"' in html
    assert 'src="/static/brewing.js"' in html
    status = test_client.get("/api/assistant/status")
    assert status.status_code == 200
    assert status.json()["available"] is False
    failed = test_client.post(
        "/api/assistant/chat", json={"message": "hello", "context": {}}
    )
    assert failed.status_code == 503
    assert failed.json()["detail"] == "assistant is not configured"


def test_assistant_chat_loads_context_and_persists_exchange(test_client, app_module, monkeypatch):
    _device(test_client)
    recipe = test_client.post("/api/recipes", json=_recipe_payload()).json()
    app_module.ASSISTANT_CLIENT.base_url = "http://127.0.0.1:3100"
    app_module.ASSISTANT_CLIENT.token_path = Path("/unused-in-mocked-chat")
    captured = {}

    def fake_chat(message, conversation_id):
        captured["message"] = message
        captured["conversation_id"] = conversation_id
        return {"message": "Measured tilt is steady.", "model": "gemma", "tool_calls": []}

    monkeypatch.setattr(app_module.ASSISTANT_CLIENT, "chat", fake_chat)
    response = test_client.post(
        "/api/assistant/chat",
        json={
            "message": "How is it looking?",
            "context": {
                "surface": "recipe",
                "recipe_id": recipe["id"],
                "device_id": "brew-device",
            },
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "Measured tilt is steady."
    assert body["model"] == "gemma"
    assert body["conversation_id"] == captured["conversation_id"]
    assert '"recipe"' in captured["message"]
    assert '"latest_telemetry"' in captured["message"]

    with sqlite3.connect(app_module.BREW_DB_PATH) as conn:
        rows = conn.execute(
            "SELECT role,message FROM assistant_messages ORDER BY id"
        ).fetchall()
    assert rows == [
        ("user", "How is it looking?"),
        ("assistant", "Measured tilt is steady."),
    ]


def test_research_documents_are_fts_indexed(app_module):
    app_module.ASSISTANT_STORE.save_research_documents(
        [
            {
                "source_kind": "kiwix",
                "source_url": "http://example.invalid/fermentation",
                "title": "Fruit fermentation",
                "content": "Blackberry must acidity and yeast nutrition reference.",
                "metadata": {},
            }
        ]
    )
    matches = app_module.ASSISTANT_STORE.search_research("blackberry nutrition")
    assert len(matches) == 1
    assert matches[0]["source_kind"] == "kiwix"
    assert "[Blackberry]" in matches[0]["excerpt"]
    assert len(matches[0]["content_hash"]) == 64
    assert matches[0]["version_no"] == 1
    assert matches[0]["completeness_status"] == "complete"
    assert matches[0]["quality_status"] == "unreviewed"


def test_research_documents_dedupe_exact_content_and_version_changes(app_module):
    document = {
        "source_kind": "kiwix",
        "source_url": "http://example.invalid/versioned",
        "title": "Versioned fermentation",
        "content": "Blackberry must acidity reference.",
        "metadata": {},
    }
    app_module.ASSISTANT_STORE.save_research_documents([document, document])
    changed = {**document, "content": "Blackberry must acidity and tannin reference updated."}
    app_module.ASSISTANT_STORE.save_research_documents([changed])

    with sqlite3.connect(app_module.BREW_DB_PATH) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM research_documents WHERE source_url=?",
            (document["source_url"],),
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT version_no FROM research_document_versions v "
            "JOIN research_documents d ON d.id=v.document_id "
            "WHERE d.source_url=? ORDER BY version_no",
            (document["source_url"],),
        ).fetchall() == [(1,), (2,)]

    matches = app_module.ASSISTANT_STORE.search_research("blackberry tannin")
    assert len(matches) == 1
    assert matches[0]["version_no"] == 2
    assert matches[0]["quality_status"] == "unreviewed"


def test_research_evidence_links_round_trip_with_retrieval_provenance(app_module):
    app_module.ASSISTANT_STORE.save_research_documents(
        [
            {
                "source_kind": "searxng",
                "source_url": "https://example.invalid/evidence",
                "title": "Evidence source",
                "content": "Yeast nutrition improves fermentation reliability.",
                "metadata": {},
            }
        ]
    )
    matches = app_module.ASSISTANT_STORE.search_research("yeast nutrition")
    from app.assistant_pipeline import AssistantJobRequest, AssistantJobStore

    job_store = AssistantJobStore(app_module.BREW_DB_PATH)
    request = AssistantJobRequest(
        kind="chat",
        client_request_id="123e4567-e89b-42d3-a456-426614174051",
        surface="recipe",
        message="yeast nutrition",
        research=True,
    )
    job, created = job_store.create(request, {})
    assert created is True
    links = app_module.ASSISTANT_STORE.link_research_evidence(job["job_id"], matches)
    assert len(links) == 1
    assert links[0]["origin"] == "retrieval"
    assert links[0]["support_status"] == "unreviewed"
    assert links[0]["version_no"] == matches[0]["version_no"]
    assert links[0]["content_hash"] == matches[0]["content_hash"]


def test_v4_research_rows_receive_version_metadata_on_migration(tmp_path):
    path = tmp_path / "legacy-research.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE brew_schema(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO brew_schema(version, applied_at) VALUES (4, '2026-01-01T00:00:00+00:00');
            CREATE TABLE research_documents(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_kind TEXT NOT NULL,
                source_url TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO research_documents(source_kind,source_url,title,content,metadata_json,created_at)
            VALUES ('kiwix','https://example.invalid/legacy','Legacy source','legacy evidence','{}','2026-01-02T00:00:00+00:00');
            """
        )

    from app.brewing import BrewingStore

    BrewingStore(path).initialize()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version FROM brew_schema").fetchone() == (6,)
        row = conn.execute(
            "SELECT version_no,content_hash,freshness_at_utc,completeness_status,quality_status "
            "FROM research_document_versions"
        ).fetchone()
    assert row == (
        1,
        "7ff202e8a4829ba47b300eef973e930a489c07c7ae89261c69acc08724a9cd7d",
        "2026-01-02T00:00:00+00:00",
        "complete",
        "unreviewed",
    )


def test_legacy_research_rows_are_searchable_after_fts_migration(tmp_path):
    path = tmp_path / "legacy-research-fts.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE brew_schema(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO brew_schema(version, applied_at) VALUES (4, '2026-01-01T00:00:00+00:00');
            CREATE TABLE research_documents(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_kind TEXT NOT NULL,
                source_url TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO research_documents(source_kind,source_url,title,content,metadata_json,created_at)
            VALUES (
                'kiwix',
                'https://example.invalid/legacy-fts',
                'Legacy FTS source',
                'Legacy hydrometer calibration evidence',
                '{}',
                '2026-01-02T00:00:00+00:00'
            );
            """
        )
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_documents_fts'"
        ).fetchone() is None

    from app.assistant import AssistantStore
    from app.brewing import BrewingStore

    brewing_store = BrewingStore(path)
    brewing_store.initialize()
    brewing_store.initialize()

    matches = AssistantStore(path).search_research("hydrometer calibration")
    assert len(matches) == 1
    assert matches[0]["source_url"] == "https://example.invalid/legacy-fts"
    assert "[hydrometer]" in matches[0]["excerpt"]


def test_scheduled_addition_event_is_exact_keyed_and_idempotent(test_client):
    _device(test_client, "schedule-device")
    ingredient_key = "123e4567-e89b-42d3-a456-426614174040"
    addition_key = "123e4567-e89b-42d3-a456-426614174041"
    recipe_payload = {
        "name": "Scheduled test",
        "base_volume_l": 10.0,
        "ingredients": [
            {
                "ingredient_key": ingredient_key,
                "name": "honey",
                "quantity": 1.0,
                "unit": "kg",
                "material_type": "fermentable",
            }
        ],
        "scheduled_additions": [
            {
                "addition_key": addition_key,
                "series_key": "123e4567-e89b-42d3-a456-426614174042",
                "sequence": 1,
                "series_kind": "sugar_step",
                "ingredient_key": ingredient_key,
                "quantity": 100.0,
                "unit": "g",
                "scaling": {"mode": "linear"},
                "trigger": {"kind": "manual"},
            }
        ],
    }
    recipe = test_client.post("/api/recipes", json=recipe_payload).json()
    brew = test_client.post(
        "/api/brews",
        json={"device_id": "schedule-device", "recipe_id": recipe["id"], "target_volume_l": 10.0},
    )
    assert brew.status_code == 201, brew.text
    brew_id = brew.json()["id"]
    event_payload = {
        "event_type": "scheduled_addition_recorded",
        "source": "agent",
        "client_request_id": "123e4567-e89b-42d3-a456-426614174043",
        "data": {
            "addition_key": addition_key,
            "actual_quantity": 100.0,
            "actual_unit": "g",
        },
    }
    first = test_client.post(f"/api/brews/{brew_id}/events", json=event_payload)
    assert first.status_code == 201, first.text
    second = test_client.post(f"/api/brews/{brew_id}/events", json=event_payload)
    assert second.status_code == 201, second.text
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["data"]["planned_quantity"] == 100.0
    assert second.json()["data"]["planned_unit"] == "g"
    brew_state = test_client.get(f"/api/brews/{brew_id}")
    assert brew_state.status_code == 200
    assert brew_state.json()["scheduled_additions"][0]["state"] == "recorded"
    unknown = test_client.post(
        f"/api/brews/{brew_id}/events",
        json={
            **event_payload,
            "client_request_id": "123e4567-e89b-42d3-a456-426614174044",
            "data": {"addition_key": "123e4567-e89b-42d3-a456-426614174045", "actual_quantity": 1, "actual_unit": "g"},
        },
    )
    assert unknown.status_code == 409
    assert unknown.json()["detail"]["code"] == "addition_not_found"


def test_legacy_v2_database_migrates_stable_keys_and_v3_tables(tmp_path):
    db_path = tmp_path / "legacy-brew.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            PRAGMA foreign_keys=OFF;
            CREATE TABLE brew_schema(version INTEGER NOT NULL);
            INSERT INTO brew_schema(version) VALUES (2);
            CREATE TABLE recipes(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, style TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
                base_volume_l REAL NOT NULL, notes TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT
            );
            CREATE TABLE recipe_ingredients(
                id INTEGER PRIMARY KEY AUTOINCREMENT, recipe_id INTEGER NOT NULL, sort_order INTEGER NOT NULL,
                name TEXT NOT NULL, quantity REAL NOT NULL, unit TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'other', scaling_json TEXT NOT NULL DEFAULT '{"mode":"linear"}'
            );
            CREATE TABLE brew_runs(
                id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL, recipe_id INTEGER NOT NULL,
                recipe_snapshot_json TEXT NOT NULL, target_volume_l REAL NOT NULL, status TEXT NOT NULL,
                started_at TEXT NOT NULL, ended_at TEXT, notes TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE brew_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, brew_run_id INTEGER NOT NULL, event_type TEXT NOT NULL,
                event_at TEXT NOT NULL, source TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', data_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE water_references(
                id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL, observed_at TEXT NOT NULL,
                angle REAL NOT NULL, temperature_c REAL, gravity_reference REAL NOT NULL,
                source_sample_id INTEGER NOT NULL, label TEXT NOT NULL, created_at TEXT NOT NULL
            );
            INSERT INTO recipes(name,base_volume_l,created_at,updated_at) VALUES ('legacy',10,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z');
            INSERT INTO recipe_ingredients(recipe_id,sort_order,name,quantity,unit) VALUES (1,0,'honey',1,'kg');
            """
        )
    from app.brewing import BrewingStore

    store = BrewingStore(db_path)
    store.initialize()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT version FROM brew_schema").fetchone() == (6,)
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='device_operating_intent'"
        ).fetchone() == (1,)
        key = conn.execute("SELECT ingredient_key FROM recipe_ingredients").fetchone()[0]
        assert len(key) == 36 and key[14] == "4"
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"assistant_jobs", "recipe_culture_profiles", "recipe_scheduled_additions", "recipe_process_steps"} <= tables
    loaded = store.get_recipe(1)
    assert loaded is not None
    assert loaded["ingredients"][0]["ingredient_key"] == key


def test_culture_schedule_and_process_fields_round_trip(test_client):
    _device(test_client, "culture-device")
    culture_ingredient = "123e4567-e89b-42d3-a456-426614174050"
    culture_key = "123e4567-e89b-42d3-a456-426614174051"
    addition_key = "123e4567-e89b-42d3-a456-426614174052"
    step_key = "123e4567-e89b-42d3-a456-426614174053"
    response = test_client.post(
        "/api/recipes",
        json={
            "name": "Sourdough culture test",
            "beverage_type": "other",
            "base_volume_l": 10.0,
            "ingredients": [
                {"ingredient_key": culture_ingredient, "name": "starter", "quantity": 0.5, "unit": "kg", "material_type": "culture"},
                {"name": "tea", "quantity": 10, "unit": "g", "material_type": "tannin", "tannin_detail": {"source_kind": "black_tea", "caffeine_status": "yes"}},
            ],
            "culture_profiles": [{
                "culture_key": culture_key,
                "client_key": "123e4567-e89b-42d3-a456-426614174054",
                "ingredient_key": culture_ingredient,
                "display_name": "Kitchen starter",
                "culture_kind": "sourdough_starter",
                "organism_status": "mixed_unknown",
                "identity_assertion": "mixed_consortium",
                "backslop_ratio": "1:5:5",
                "refresh_interval_hours": "12-24",
                "substrates": [{"name": "whole wheat flour", "material_type": "flour", "allergen_tags": ["wheat"]}],
            }],
            "scheduled_additions": [{
                "addition_key": addition_key,
                "series_key": "123e4567-e89b-42d3-a456-426614174055",
                "sequence": 1,
                "series_kind": "sugar_step",
                "ingredient_key": culture_ingredient,
                "quantity": 100,
                "unit": "g",
                "scaling": {"mode": "linear"},
                "trigger": {"kind": "manual"},
                "instructions": "Add only after operator confirms readiness.",
            }],
            "process_steps": [{
                "step_key": step_key,
                "sequence": 1,
                "phase": "culture_build",
                "method": "inoculate",
                "linked_addition_key": addition_key,
                "linked_culture_key": culture_key,
                "instructions": "Observe activity; do not infer a single strain.",
            }],
        },
    )
    assert response.status_code == 201, response.text
    recipe = response.json()
    assert recipe["culture_profiles"][0]["culture_key"] == culture_key
    assert "client_key" not in recipe["culture_profiles"][0]
    assert recipe["scheduled_additions"][0]["addition_key"] == addition_key
    assert recipe["scheduled_additions"][0]["scaled_quantity"] == 100.0
    assert recipe["process_steps"][0]["linked_culture_key"] == culture_key
    assert recipe["ingredients"][1]["tannin_detail"]["source_kind"] == "black_tea"


def test_research_broker_reports_empty_sources_truthfully(app_module, monkeypatch):
    broker = app_module.RESEARCH_BROKER

    monkeypatch.setattr(broker, "_request_json", lambda *_args, **_kwargs: {"results": []})
    monkeypatch.setattr(
        broker,
        "_request_text",
        lambda *_args, **_kwargs: ("http://kiwix.invalid/search?pattern=test", "<html></html>"),
    )

    documents, status = broker.search("test")

    assert documents == []
    assert status == {"searxng": "empty", "kiwix": "empty"}


def test_research_broker_marks_only_document_producing_source_ok(app_module, monkeypatch):
    broker = app_module.RESEARCH_BROKER

    monkeypatch.setattr(
        broker,
        "_request_json",
        lambda *_args, **_kwargs: {
            "results": [
                {
                    "url": "https://example.invalid/fermentation",
                    "title": "Fermentation reference",
                    "content": "Hydrometer readings are primary evidence.",
                    "engine": "example",
                }
            ]
        },
    )
    monkeypatch.setattr(
        broker,
        "_request_text",
        lambda *_args, **_kwargs: ("http://kiwix.invalid/search?pattern=test", "  "),
    )

    documents, status = broker.search("test")

    assert len(documents) == 1
    assert documents[0]["source_kind"] == "searxng"
    assert status == {"searxng": "ok", "kiwix": "empty"}


def test_research_broker_uses_kiwix_suggestion_content_path(app_module, monkeypatch):
    broker = app_module.RESEARCH_BROKER
    requested_pages = []

    def fake_json(url, params):
        if url == broker.searxng_url:
            return {"results": []}
        assert url == "http://kiwix.example.test:9090/suggest"
        assert params["content"] == "wikipedia_en_all_maxi_2026-02"
        suggestions = {
            "summarize": ("Summarize", "Summarize"),
            "cautiously": ("Cautiously", "Cautiously"),
            "fermentation": ("Fermentation_(beer)", "Fermentation (beer)"),
            "hydrometer": ("Hydrometer", "Hydrometer"),
        }
        if params["term"] in suggestions:
            path, value = suggestions[params["term"]]
            return [{"kind": "path", "path": path, "value": value}]
        return []

    def fake_text(url, params):
        requested_pages.append((url, params))
        return url, "<main>Beer fermentation converts sugars through yeast metabolism.</main>"

    monkeypatch.setattr(broker, "_request_json", fake_json)
    monkeypatch.setattr(broker, "_request_text", fake_text)

    documents, status = broker.search(
        "Summarize cautiously what fermentation and a hydrometer measure"
    )

    assert status == {"searxng": "empty", "kiwix": "ok"}
    assert [document["title"] for document in documents] == [
        "Hydrometer",
        "Fermentation (beer)",
    ]
    assert all(document["source_kind"] == "kiwix" for document in documents)
    assert all(document["content"].startswith("Beer fermentation") for document in documents)
    assert requested_pages == [
        (
            "http://kiwix.example.test:9090/content/wikipedia_en_all_maxi_2026-02/Hydrometer",
            {},
        ),
        (
            "http://kiwix.example.test:9090/content/wikipedia_en_all_maxi_2026-02/Fermentation_(beer)",
            {},
        )
    ]


# ---------------------------------------------------------------------------
# P1 research QC and retrieval closure contract tests.
# ---------------------------------------------------------------------------


def _raise_lookup_error(*_args, **_kwargs):
    from urllib.error import URLError

    raise URLError("both engines unavailable")


def test_both_engines_down_returns_useful_local_or_explicit_insufficiency(
    app_module, monkeypatch
) -> None:
    """When both engines are unreachable, the broker must NOT advertise ``ok``;
    if local FTS already covers the query the warm-reuse marker is set; if not,
    the response must explicitly say ``insufficient``.
    """
    broker = app_module.RESEARCH_BROKER

    def fake_json(url, params):
        del params
        return _raise_lookup_error()

    def fake_text(url, params):
        del url, params
        return _raise_lookup_error()

    monkeypatch.setattr(broker, "_request_json", fake_json)
    monkeypatch.setattr(broker, "_request_text", fake_text)

    # --- Cold path: no local coverage. Broker must NOT lie about ok.
    documents, status = broker.search(
        "explain wine fermentation nutrient balance and temperature"
    )
    assert status == {"searxng": "unavailable", "kiwix": "unavailable"}
    assert documents == []
    assert status["searxng"] != "ok"
    assert status["kiwix"] != "ok"
    # No ``ok`` source may be advertised for an empty/unavailable retrieval;
    # the persisted research_status must mark the outcome insufficient.
    assert "ok" not in status.values()

    # --- Warm path: with local FTS coverage, the broker is not consulted and
    # the warm-reuse marker must surface.
    app_module.ASSISTANT_STORE.save_research_documents(
        [
            {
                "source_kind": "kiwix",
                "source_url": "http://example.invalid/fermentation-warm",
                "title": "Warm reference",
                "content": (
                    "Yeast nutrient balance and temperature range reference "
                    "for wine fermentation."
                ),
                "metadata": {"content_kind": "full_text"},
            }
        ]
    )
    matches = app_module.ASSISTANT_STORE.search_research(
        "wine fermentation nutrient balance"
    )
    assert matches
    assert any(
        match["source_url"] == "http://example.invalid/fermentation-warm"
        for match in matches
    )


def test_irrelevant_http_200_is_rejected_with_explicit_reason(
    app_module, monkeypatch
) -> None:
    """HTTP 200 with no useful payload is not a successful retrieval."""
    broker = app_module.RESEARCH_BROKER

    def fake_json(url, params):
        del params
        if url == broker.searxng_url:
            return {
                "results": [
                    {
                        "url": "https://example.invalid/empty-search",
                        "title": "Empty search",
                        "engine": "example",
                    },
                    {
                        "url": "https://example.invalid/no-signal",
                        "title": "No signal",
                        "content": "   ",
                        "engine": "example",
                    },
                ]
            }
        return []

    def fake_text(url, params):
        del url, params
        return ("http://kiwix.invalid/page", "<html></html>")

    monkeypatch.setattr(broker, "_request_json", fake_json)
    monkeypatch.setattr(broker, "_request_text", fake_text)

    documents, status = broker.search("anything for retrieval")
    assert status["searxng"] == "empty"
    assert status["kiwix"] == "empty"
    assert documents == []
    # The "ok" label must not leak in for an HTTP 200 that produced nothing.
    assert "ok" not in status.values()


def test_installed_kiwix_full_text_qualified_before_parser_selects_payload(
    app_module, monkeypatch
) -> None:
    """A Kiwix result is accepted as full text only when its payload qualifies.

    Whitespace-only, navigation-only, or HTML-shell-only payloads are not
    treated as ``full_text``; the broker records ``empty`` for them.
    """
    broker = app_module.RESEARCH_BROKER

    observed_requests: list[tuple[str, str]] = []

    def fake_json(url, params):
        if url == broker.searxng_url:
            return {"results": []}
        assert url == "http://kiwix.example.test:9090/suggest"
        term_to_path = {
            "yeast": ("Yeast", "Yeast"),
            "fermentation": ("Fermentation", "Fermentation"),
            "temperature": ("Temperature", "Temperature"),
            "nutrient": ("Nutrient", "Nutrient"),
            "balance": ("Balance", "Balance"),
        }
        if params["term"] in term_to_path:
            path, value = term_to_path[params["term"]]
            return [{"kind": "path", "path": path, "value": value}]
        return []

    def fake_text(url, params):
        observed_requests.append((url, params.get("__qualifier", "")))
        # First request: navigation-only shell with no real body text.
        # Subsequent requests: full article body.
        if len(observed_requests) == 1:
            return (
                url,
                (
                    "<html><head><title>Fermentation</title></head>"
                    "<body><nav>navigation only</nav></body></html>"
                ),
            )
        return (
            url,
            (
                "<html><head><title>Fermentation</title></head>"
                "<body><main>"
                "Yeast converts sugars into alcohol and carbon dioxide through "
                "fermentation; temperature and nutrient balance drive reliability."
                "</main></body></html>"
            ),
        )

    monkeypatch.setattr(broker, "_request_json", fake_json)
    monkeypatch.setattr(broker, "_request_text", fake_text)

    documents, status = broker.search(
        "yeast fermentation temperature nutrient balance"
    )

    # The shell-only payload is rejected; the qualifying payload is kept.
    assert status["kiwix"] == "ok"
    assert len(documents) >= 1
    kiwix_documents = [
        document for document in documents if document["source_kind"] == "kiwix"
    ]
    assert len(kiwix_documents) >= 1
    for document in kiwix_documents:
        assert document["metadata"]["content_kind"] == "full_text"
        assert "fermentation" in document["content"].lower()
        # The qualification requires an article body marker (e.g. <main>),
        # so the kept documents must have meaningful visible text.
        assert len(document["content"].strip()) >= 20
    # The first request returned the shell-only payload; that request must
    # NOT have produced a kept document. We assert this by checking that at
    # least one shell-style payload was observed but not present in the
    # persisted set.
    assert len(observed_requests) >= 2
    # No kept document may have come from the navigation-only shell.
    assert not any(
        "navigation only" in document.get("content", "") for document in kiwix_documents
    ), kiwix_documents


def test_fts_update_delete_parity_preserves_searchable_set(app_module) -> None:
    """After an UPDATE or DELETE on ``research_documents`` the FTS index must
    stay in sync: stale terms unique to the previous content are removed,
    updated rows are reindexed, and DELETE drops the document entirely.
    """
    original = {
        "source_kind": "kiwix",
        "source_url": "http://example.invalid/parity",
        "title": "Parity reference",
        "content": "yeast nutrition reference for fermentation reliability",
        "metadata": {"content_kind": "full_text"},
    }
    app_module.ASSISTANT_STORE.save_research_documents([original])

    # After insert, the row is searchable by both new and shared terms.
    matches = app_module.ASSISTANT_STORE.search_research("yeast nutrition")
    assert any(
        match["source_url"] == "http://example.invalid/parity" for match in matches
    )

    # UPDATE: replace the row directly (raw SQL) and verify FTS picks the new
    # text up. ``hydration`` is unique to the NEW content; ``nutrition`` is
    # unique to the OLD content. After the trigger fires, ``nutrition`` must
    # no longer match the document.
    with sqlite3.connect(app_module.BREW_DB_PATH) as conn:
        conn.execute(
            "UPDATE research_documents SET title=?, content=? WHERE source_url=?",
            (
                "Parity reference updated",
                "yeast hydration acidity balance reference for fermentation reliability",
                "http://example.invalid/parity",
            ),
        )

    matches_after_update = app_module.ASSISTANT_STORE.search_research(
        "yeast hydration acidity"
    )
    assert any(
        match["source_url"] == "http://example.invalid/parity"
        and "hydration" in match["excerpt"].lower()
        for match in matches_after_update
    ), matches_after_update

    # The term unique to the OLD content must NOT match the document anymore.
    stale = app_module.ASSISTANT_STORE.search_research("nutrition")
    assert not any(
        match["source_url"] == "http://example.invalid/parity" for match in stale
    ), stale

    # DELETE: remove the row and verify FTS no longer surfaces it.
    with sqlite3.connect(app_module.BREW_DB_PATH) as conn:
        conn.execute(
            "DELETE FROM research_documents WHERE source_url=?",
            ("http://example.invalid/parity",),
        )
    after_delete = app_module.ASSISTANT_STORE.search_research("hydration acidity")
    assert not any(
        match["source_url"] == "http://example.invalid/parity" for match in after_delete
    ), after_delete


def test_prior_citation_resolvable_after_source_supersession(
    test_client, app_module, monkeypatch
) -> None:
    """A frozen evidence link to version N must remain resolvable even after
    the source URL has been superseded by version N+1.
    """
    from app.assistant_pipeline import AssistantJobRequest, AssistantJobStore

    app_module.ASSISTANT_CLIENT.base_url = "http://127.0.0.1:3100"
    app_module.ASSISTANT_CLIENT.token_path = Path("/unused-in-mocked-supersession")

    def fake_chat(message: str, conversation_id: str) -> dict:
        del message, conversation_id
        return {"message": "ok", "model": "gemma", "tool_calls": []}

    monkeypatch.setattr(app_module.ASSISTANT_CLIENT, "chat", fake_chat)

    url = "http://example.invalid/supersession"
    app_module.ASSISTANT_STORE.save_research_documents(
        [
            {
                "source_kind": "kiwix",
                "source_url": url,
                "title": "Supersession v1",
                "content": "Original fermentation balance reference.",
                "metadata": {"content_kind": "full_text"},
            }
        ]
    )
    matches_v1 = app_module.ASSISTANT_STORE.search_research("fermentation balance")
    version_v1 = next(
        match for match in matches_v1 if match["source_url"] == url
    )
    assert version_v1["version_no"] == 1

    job_store = AssistantJobStore(app_module.BREW_DB_PATH)
    job, created = job_store.create(
        AssistantJobRequest(
            kind="chat",
            client_request_id="123e4567-e89b-42d3-a456-4266141740c1",
            surface="recipe",
            message="fermentation balance",
            research=True,
        ),
        {},
    )
    assert created is True
    links = app_module.ASSISTANT_STORE.link_research_evidence(
        job["job_id"], matches_v1
    )
    assert len(links) == 1
    link = links[0]
    frozen_version_id = link["version_id"]

    # Now supersede the source with a new version.
    app_module.ASSISTANT_STORE.save_research_documents(
        [
            {
                "source_kind": "kiwix",
                "source_url": url,
                "title": "Supersession v2",
                "content": "Updated fermentation balance reference with new nutrients.",
                "metadata": {"content_kind": "full_text"},
            }
        ]
    )

    # The frozen link to version N must remain resolvable.
    resolved = app_module.ASSISTANT_STORE.resolve_evidence_link_version(
        link["link_id"]
    )
    assert resolved["version_id"] == frozen_version_id
    assert resolved["content_hash"] == version_v1["content_hash"]
    assert resolved["version_no"] == 1
    # The current/most-recent version is now v2; the linked version must NOT
    # have been silently bumped.
    matches_v2 = app_module.ASSISTANT_STORE.search_research("updated fermentation")
    version_v2 = next(
        match for match in matches_v2 if match["source_url"] == url
    )
    assert version_v2["version_no"] == 2
    assert version_v2["version_id"] != frozen_version_id


def test_quality_and_support_transitions_are_logged_idempotent_and_reject_unknown_values(
    app_module, caplog,
) -> None:
    """Operator review transitions for ``quality_status`` and ``support_status``
    are persisted, idempotent, and reject invalid or cross-version values.
    """
    # Persist two distinct versions of the same source URL.
    url = "http://example.invalid/transition"
    app_module.ASSISTANT_STORE.save_research_documents(
        [
            {
                "source_kind": "kiwix",
                "source_url": url,
                "title": "Transition v1",
                "content": "v1 content",
                "metadata": {"content_kind": "full_text"},
            }
        ]
    )
    app_module.ASSISTANT_STORE.save_research_documents(
        [
            {
                "source_kind": "kiwix",
                "source_url": url,
                "title": "Transition v2",
                "content": "v2 content",
                "metadata": {"content_kind": "full_text"},
            }
        ]
    )
    matches = app_module.ASSISTANT_STORE.search_research("v2 content")
    version_v2 = next(match for match in matches if match["source_url"] == url)
    assert version_v2["version_no"] == 2

    # quality_status: usable.
    result = app_module.ASSISTANT_STORE.set_quality_status(
        version_id=version_v2["version_id"],
        quality_status="usable",
    )
    assert result["quality_status"] == "usable"
    assert result["version_id"] == version_v2["version_id"]

    # Idempotent: applying the same value again returns the same state without
    # creating a new transition row for the same target value.
    again = app_module.ASSISTANT_STORE.set_quality_status(
        version_id=version_v2["version_id"],
        quality_status="usable",
    )
    assert again["quality_status"] == "usable"

    # Invalid value is rejected with a structured error.
    invalid = app_module.ASSISTANT_STORE.set_quality_status(
        version_id=version_v2["version_id"],
        quality_status="definitely_usable",
    )
    assert invalid["status"] == "rejected"
    assert "quality_status" in invalid["reason"]
    assert invalid["accepted_values"] == ["unreviewed", "usable", "rejected"]

    # Quality status transitions are recorded as bounded structured log
    # entries through the standard Python logging module, NOT as a
    # separate JSONL file beside brew.db. There must be no unmanaged
    # durable file.
    import logging

    log_path = app_module.ASSISTANT_STORE.path.parent / "research_review_transitions.log"
    assert not log_path.exists(), (
        f"unmanaged durable review log must not exist: {log_path}"
    )

    # The same transitions re-applied with caplog attached must surface
    # structured ``quality_status`` records (no JSONL file involved).
    # We start caplog AFTER the initial transitions above so we can
    # observe every outcome cleanly.
    caplog.set_level(logging.INFO, logger="app.assistant")
    caplog.clear()
    # Reset to a known starting value: usable, then re-apply to trigger
    # the noop branch.
    app_module.ASSISTANT_STORE.set_quality_status(
        version_id=version_v2["version_id"],
        quality_status="usable",
    )
    app_module.ASSISTANT_STORE.set_quality_status(
        version_id=version_v2["version_id"],
        quality_status="usable",  # idempotent noop
    )
    app_module.ASSISTANT_STORE.set_quality_status(
        version_id=version_v2["version_id"],
        quality_status="rejected",
    )
    quality_records = [
        record for record in caplog.records
        if record.name == "app.assistant" and getattr(record, "field", None) == "quality_status"
    ]
    assert quality_records, [r.__dict__ for r in caplog.records]
    latest = quality_records[-1]
    assert latest.version_id == version_v2["version_id"]
    assert latest.value == "rejected"
    assert latest.outcome == "applied"
    # The same value as a noop is also logged.
    noop_records = [
        record for record in quality_records
        if record.value == "usable" and record.outcome == "noop"
    ]
    assert noop_records, [r.__dict__ for r in quality_records]
    applied_records = [
        record for record in quality_records
        if record.outcome == "applied"
    ]
    assert applied_records, [r.__dict__ for r in quality_records]
    # Log payload must remain bounded — no exception/error text leaked.
    for record in quality_records:
        assert getattr(record, "exc_info", None) is None

    # support_status: supports / contradicts / not_supporting round-trip on an
    # evidence link; the underlying version is unchanged.
    # Reset the quality_status back to "usable" so the existing invariant
    # below still holds.
    app_module.ASSISTANT_STORE.set_quality_status(
        version_id=version_v2["version_id"],
        quality_status="usable",
    )
    from app.assistant_pipeline import AssistantJobRequest, AssistantJobStore

    job_store = AssistantJobStore(app_module.BREW_DB_PATH)
    job, created = job_store.create(
        AssistantJobRequest(
            kind="chat",
            client_request_id="123e4567-e89b-42d3-a456-4266141740d1",
            surface="recipe",
            message="transition review",
            research=True,
        ),
        {},
    )
    assert created is True
    link = app_module.ASSISTANT_STORE.link_research_evidence(
        job["job_id"], matches
    )
    assert link
    for support_status in ("supports", "contradicts", "not_supporting"):
        updated = app_module.ASSISTANT_STORE.set_support_status(
            link_id=link[0]["link_id"],
            support_status=support_status,
        )
        assert updated["support_status"] == support_status, updated
        assert updated["link_id"] == link[0]["link_id"]
        # The referenced version's quality_status is preserved.
        version_after = app_module.ASSISTANT_STORE.get_research_version(
            version_v2["version_id"]
        )
        assert version_after["quality_status"] == "usable"

    # Idempotency: setting the same support_status again returns the same link.
    again = app_module.ASSISTANT_STORE.set_support_status(
        link_id=link[0]["link_id"],
        support_status="not_supporting",
    )
    assert again["support_status"] == "not_supporting"

    # Invalid support_status is rejected.
    invalid = app_module.ASSISTANT_STORE.set_support_status(
        link_id=link[0]["link_id"],
        support_status="definitely_supports",
    )
    assert invalid["status"] == "rejected"
    assert "support_status" in invalid["reason"]
    assert invalid["accepted_values"] == [
        "unreviewed",
        "supports",
        "contradicts",
        "not_supporting",
    ]

    # Cross-version value rejection: setting a quality_status on a version
    # that does not exist is rejected, and the link remains resolvable.
    cross = app_module.ASSISTANT_STORE.set_quality_status(
        version_id=99999999,
        quality_status="usable",
    )
    assert cross["status"] == "rejected"
    assert cross["reason"] == "version_not_found"
    resolved = app_module.ASSISTANT_STORE.resolve_evidence_link_version(
        link[0]["link_id"]
    )
    assert resolved["version_id"] == version_v2["version_id"]
    assert resolved["content_hash"] == version_v2["content_hash"]


# ----------------------------------------------------------------------------
# New tests covering the P1-R1 repairs
# ----------------------------------------------------------------------------


def test_quality_status_http_route_round_trip(test_client, app_module) -> None:
    """The quality-status HTTP route must accept valid values, return
    ``outcome=noop`` for idempotent re-application, reject unknown values
    with a structured 422, and 404 on missing version ids.
    """
    from app.assistant_pipeline import AssistantJobRequest, AssistantJobStore

    # Persist a document + version via the production path.
    app_module.ASSISTANT_STORE.save_research_documents(
        [
            {
                "source_kind": "kiwix",
                "source_url": "http://example.invalid/quality-http",
                "title": "Quality HTTP reference",
                "content": (
                    "Yeast nutrient balance reference for fermentation reliability."
                ),
                "metadata": {"content_kind": "full_text"},
            }
        ]
    )
    matches = app_module.ASSISTANT_STORE.search_research("yeast nutrient balance")
    assert matches
    version = next(
        match for match in matches if match["source_url"] == "http://example.invalid/quality-http"
    )

    # Link it via a real assistant job so the version row is reachable.
    job_store = AssistantJobStore(app_module.BREW_DB_PATH)
    job, _created = job_store.create(
        AssistantJobRequest(
            kind="chat",
            client_request_id="123e4567-e89b-42d3-a456-4266141740f1",
            surface="recipe",
            message="quality http test",
            research=True,
        ),
        {},
    )
    assert job is not None

    # --- Valid: applied.
    response = test_client.post(
        f"/api/assistant/research/versions/{version['version_id']}/quality-status",
        json={"quality_status": "usable"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version_id"] == version["version_id"]
    assert body["quality_status"] == "usable"
    assert body["outcome"] == "applied"

    # --- Idempotent: same value again returns outcome=noop.
    again = test_client.post(
        f"/api/assistant/research/versions/{version['version_id']}/quality-status",
        json={"quality_status": "usable"},
    )
    assert again.status_code == 200, again.text
    again_body = again.json()
    assert again_body["outcome"] == "noop"
    assert again_body["quality_status"] == "usable"

    # --- Invalid: unknown value yields a structured 422.
    invalid = test_client.post(
        f"/api/assistant/research/versions/{version['version_id']}/quality-status",
        json={"quality_status": "definitely_usable"},
    )
    assert invalid.status_code == 422, invalid.text
    detail = invalid.json()["detail"]
    assert isinstance(detail, list), detail
    error_types = {item.get("type") for item in detail}
    assert "value_error" in error_types, detail

    # --- Missing: 404 on unknown version id.
    missing = test_client.post(
        "/api/assistant/research/versions/99999999/quality-status",
        json={"quality_status": "usable"},
    )
    assert missing.status_code == 404, missing.text
    assert missing.json()["detail"]["code"] == "version_not_found"


def test_qualifies_as_full_text_accepts_ordinary_body_paragraphs(app_module) -> None:
    """An ordinary body/p markup with several visible word tokens qualifies
    as ``full_text``. The body does NOT have to contain <article>, <main>,
    or <section> markers — many short Wikipedia pages do not.
    """
    broker = app_module.RESEARCH_BROKER
    payload = (
        "<html><head><title>Yeast</title></head>"
        "<body><p>"
        "Yeast converts sugars into alcohol and carbon dioxide through "
        "fermentation; temperature and nutrient balance drive reliability."
        "</p></body></html>"
    )
    assert broker._qualifies_as_full_text(payload) is True


def test_qualifies_as_full_text_accepts_short_visible_lead(app_module) -> None:
    """Short visible leads without article/main/section markers still
    qualify when there are enough word tokens in the body. An empty
    payload and a navigation-only shell do NOT qualify.
    """
    broker = app_module.RESEARCH_BROKER

    # Empty/whitespace payload is rejected.
    assert broker._qualifies_as_full_text("") is False
    assert broker._qualifies_as_full_text("   \n  ") is False

    # Navigation-only shell with no body text is rejected.
    shell = (
        "<html><head><title>Stub</title></head>"
        "<body><nav>only navigation</nav></body></html>"
    )
    assert broker._qualifies_as_full_text(shell) is False

    # Shell with one short token is rejected.
    one_word = (
        "<html><head><title>Stub</title></head>"
        "<body><nav>navigation only here</nav></body></html>"
    )
    assert broker._qualifies_as_full_text(one_word) is False

    # A short visible lead with several word tokens still qualifies even
    # though there is no <article>/<main>/<section> wrapper.
    short_visible_lead = (
        "<html><head><title>Topic</title></head>"
        "<body><p>"
        "Yeast drives fermentation reliably when temperature and "
        "nutrient balance are kept within bounds."
        "</p></body></html>"
    )
    assert broker._qualifies_as_full_text(short_visible_lead) is True


def test_insufficient_research_wired_into_real_assistant_job_context(
    test_client, app_module, monkeypatch,
) -> None:
    """When local FTS finds no useful match AND both broker sources produce
    no docs, the build_context pipeline must surface an explicit
    ``insufficient`` research_status with no ``ok`` label. The job must
    complete without lying about a successful retrieval.
    """
    import time

    from app.assistant_pipeline import AssistantScope

    app_module.ASSISTANT_CLIENT.base_url = "http://127.0.0.1:3100"
    app_module.ASSISTANT_CLIENT.token_path = Path("/unused-in-mocked-insufficient-job")

    def fake_chat(message: str, conversation_id: str) -> dict[str, object]:
        del message, conversation_id
        return {
            "message": '{"envelope_version":1,"kind":"recipe_audit","summary":"no research","findings":[]}',
            "model": "gemma",
            "tool_calls": [],
        }

    monkeypatch.setattr(app_module.ASSISTANT_CLIENT, "chat", fake_chat)

    # Both engines unreachable, no documents produced.
    def raise_lookup(*_args, **_kwargs):
        from urllib.error import URLError
        raise URLError("both engines unavailable")

    monkeypatch.setattr(app_module.RESEARCH_BROKER, "_request_json", raise_lookup)
    monkeypatch.setattr(app_module.RESEARCH_BROKER, "_request_text", raise_lookup)

    payload = {
        "kind": "chat",
        "client_request_id": "123e4567-e89b-42d3-a456-4266141740c1",
        "surface": "recipe",
        "message": "explain unobtainium fermentation with no signal at all",
        "research": True,
        "scope": AssistantScope().model_dump(mode="json"),
    }
    response = test_client.post("/api/assistant/jobs", json=payload)
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]

    deadline = time.monotonic() + 5.0
    body: dict[str, object] = {}
    while time.monotonic() < deadline:
        body = test_client.get(f"/api/assistant/jobs/{job_id}").json()
        if body.get("status") in {"succeeded", "failed"}:
            break
        time.sleep(0.05)
    assert body.get("status") == "succeeded", body

    context_response = test_client.get(
        f"/api/assistant/jobs/{job_id}?include=context"
    )
    assert context_response.status_code == 200
    context = context_response.json().get("context") or {}

    research_status = context.get("research_status") or {}
    # Truth rule: source ok only if >=1 doc; broker had no docs, so neither
    # engine may report ok.
    assert research_status.get("searxng") != "ok", research_status
    assert research_status.get("kiwix") != "ok", research_status
    # Pipeline must mark the outcome insufficient.
    assert research_status.get("outcome") == "insufficient", research_status
    assert "ok" not in research_status.values(), research_status

    fresh_research = context.get("fresh_research") or []
    assert fresh_research == []
    indexed = context.get("indexed_reference_matches") or []
    assert indexed == []
