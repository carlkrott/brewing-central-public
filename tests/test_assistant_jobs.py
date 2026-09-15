from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from threading import Event
from typing import Any
from urllib.error import URLError

import pytest

from app.assistant import (
    CombinedGemmaClient,
    ResearchBroker,
    ZeroClawClient,
    _client_for_job,
    _filter_grounded_findings,
    _result_contract_json,
    _structured_prompt,
)
from app.assistant_pipeline import (
    MAX_STAGE_BYTES,
    ArchiveCompareResultV1,
    AssistantScope,
    BrewAnalyzeResultV1,
    BrewEventDraftResultV1,
    Finding,
    RecipeAuditResultV1,
    RecipeAutofillResultV1,
    RecipeProposal,
    RecipeRewriteResultV1,
)


def test_research_content_kind_is_explicit_and_indexed(app_module, monkeypatch) -> None:
    broker = ResearchBroker()

    def fake_json(url: str, params: dict[str, str]) -> object:
        if url == broker.searxng_url:
            return {
                "results": [
                    {
                        "url": "https://example.invalid/search-result",
                        "title": "Search result",
                        "content": "SearXNG snippet reference for fermentation.",
                        "engine": "example",
                    }
                ]
            }
        assert url == f"{broker.kiwix_base_url}/suggest"
        return [{"kind": "path", "path": "Fermentation_(beer)", "value": "Fermentation"}]

    monkeypatch.setattr(broker, "_request_json", fake_json)
    monkeypatch.setattr(
        broker,
        "_request_text",
        lambda url, params: (url, "<main>Kiwix full text reference for fermentation.</main>"),
    )

    documents, status = broker.search("fermentation reference")

    assert status == {"searxng": "ok", "kiwix": "ok"}
    assert [document["metadata"]["content_kind"] for document in documents] == [
        "snippet",
        "full_text",
    ]

    app_module.ASSISTANT_STORE.save_research_documents(documents)
    matches = app_module.ASSISTANT_STORE.search_research("reference")
    assert {match["source_kind"]: match["content_kind"] for match in matches} == {
        "searxng": "snippet",
        "kiwix": "full_text",
    }


def _draft_json() -> dict[str, object]:
    return {
        "name": "HTTP audit recipe",
        "beverage_type": "mead",
        "base_volume_l": 10.0,
        "ingredients": [
            {
                "ingredient_key": "123e4567-e89b-42d3-a456-426614174030",
                "name": "honey",
                "quantity": 1.0,
                "unit": "kg",
                "material_type": "fermentable",
            }
        ],
    }


@pytest.mark.parametrize(
    ("kind", "model_type"),
    [
        ("recipe_autofill", RecipeAutofillResultV1),
        ("recipe_audit", RecipeAuditResultV1),
        ("recipe_rewrite", RecipeRewriteResultV1),
        ("brew_analyze", BrewAnalyzeResultV1),
        ("brew_event_draft", BrewEventDraftResultV1),
        ("archive_compare", ArchiveCompareResultV1),
    ],
)
def test_result_contract_matches_the_runtime_validator(kind: str, model_type: type) -> None:
    contract_json = _result_contract_json(kind)
    contract = json.loads(contract_json)
    validator = model_type.model_json_schema()
    assert set(contract["properties"]) == set(validator["properties"])
    assert contract.get("required", []) == validator.get("required", [])
    for definition in contract.get("$defs", {}):
        assert set(contract["$defs"][definition]["properties"]) == set(
            validator["$defs"][definition]["properties"]
        )
        assert contract["$defs"][definition].get("required", []) == (
            validator["$defs"][definition].get("required", [])
        )
    assert len(contract_json.encode("utf-8")) <= 6 * 1024


def test_structured_prompt_projects_large_context_within_stage_cap() -> None:
    prompt = _structured_prompt(
        "recipe_autofill",
        {
            "capabilities": {"writes_require_operator": True},
            "scope": {},
            "draft": {"notes": "x" * 100_000},
        },
        "Return exactly one JSON object matching RESULT_CONTRACT_JSON.",
        user_message="Fill the empty fields.",
    )
    assert len(prompt.encode("utf-8")) <= MAX_STAGE_BYTES
    assert "RESULT_CONTRACT_JSON=" in prompt
    assert "JSON_OBJECT_RULE=" in prompt
    assert "FIELD_PATH_RULE=" in prompt
    assert "context_projection" in prompt


def test_full_audit_stage_drops_only_out_of_scope_findings() -> None:
    valid = Finding(
        finding_id="materials-valid",
        origin="model",
        field_path="/ingredients/123e4567-e89b-42d3-a456-426614174030/category",
        severity="advisory",
        category="consistency",
        domain="general",
        rationale="The category is stage-local.",
    )
    out_of_scope = Finding(
        finding_id="materials-out-of-scope",
        origin="model",
        field_path="/base_volume_l",
        severity="advisory",
        category="scaling",
        domain="general",
        rationale="Base volume is outside the materials stage.",
    )
    result = RecipeAuditResultV1(summary="materials", findings=[valid, out_of_scope])
    filtered, dropped = _filter_grounded_findings(
        result,
        {
            "draft": {
                "ingredients": [
                    {
                        "ingredient_key": "123e4567-e89b-42d3-a456-426614174030",
                        "category": "fermentable",
                    }
                ]
            }
        },
    )
    assert [finding.finding_id for finding in filtered.findings] == ["materials-valid"]
    assert dropped == 1


def test_zeroclaw_post_retries_once_with_the_same_idempotency_key(
    monkeypatch,
    tmp_path: Path,
) -> None:
    token_path = tmp_path / "token"
    token_path.write_text("test-token", encoding="utf-8")
    client = ZeroClawClient()
    client.base_url = "http://127.0.0.1:3100"
    client.token_path = token_path
    seen_keys: list[str | None] = []

    class Response:
        headers: dict[str, str] = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size: int = -1) -> bytes:
            return b'{"response":"ok"}'

    def fake_urlopen(request, timeout):
        del timeout
        seen_keys.append(request.headers.get("X-idempotency-key"))
        if len(seen_keys) == 1:
            raise URLError("transient")
        return Response()

    monkeypatch.setattr("app.assistant.urlopen", fake_urlopen)
    monkeypatch.setattr("app.assistant.time.sleep", lambda _seconds: None)
    assert client._request("/webhook", {"message": "test"}) == {"response": "ok"}
    assert len(seen_keys) == 2
    assert seen_keys[0]
    assert seen_keys[0] == seen_keys[1]


def test_combined_gemma_client_uses_tool_free_openai_completion(monkeypatch) -> None:
    requests: list[object] = []

    class Response:
        headers: dict[str, str] = {}

        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size: int = -1) -> bytes:
            return self.payload

    def fake_urlopen(request, timeout):
        del timeout
        requests.append(request)
        if request.full_url.endswith("/v1/models"):
            return Response({"data": [{"id": "combined-gemma-test"}]})
        body = json.loads(request.data)
        assert body["model"] == "combined-gemma-test"
        assert body["messages"] == [{"role": "user", "content": "structured prompt"}]
        assert body["max_tokens"] == 4096
        assert body["temperature"] == 0
        assert body["stream"] is False
        assert "tools" not in body
        return Response({
            "choices": [{"message": {"content": '{"kind":"recipe_audit"}'}}],
            "model": "combined-gemma-test",
        })

    monkeypatch.setattr("app.assistant.urlopen", fake_urlopen)
    client = CombinedGemmaClient()
    client.base_url = "http://gemma.example.test:8095"
    response = client.chat("structured prompt", "conversation-1")
    assert response == {
        "message": '{"kind":"recipe_audit"}',
        "model": "combined-gemma-test",
        "tool_calls": [],
    }
    assert len(requests) == 2


def test_structured_jobs_use_direct_client_without_moving_chat() -> None:
    zeroclaw = ZeroClawClient()
    direct = CombinedGemmaClient()
    direct.base_url = "http://gemma.example.test:8095"
    assert _client_for_job("chat", zeroclaw, direct) is zeroclaw
    assert _client_for_job("recipe_audit", zeroclaw, direct) is direct
    assert _client_for_job("recipe_autofill", zeroclaw, direct) is direct
    direct.base_url = ""
    assert _client_for_job("recipe_audit", zeroclaw, direct) is zeroclaw


def test_assistant_job_http_round_trip(test_client, app_module, monkeypatch) -> None:
    app_module.ASSISTANT_CLIENT.base_url = "http://127.0.0.1:3100"
    app_module.ASSISTANT_CLIENT.token_path = Path("/unused-in-mocked-job")
    captured: dict[str, str] = {}

    def fake_chat(message: str, conversation_id: str) -> dict[str, object]:
        captured["message"] = message
        return {
            "message": '{"envelope_version":1,"kind":"recipe_audit","summary":"ok","findings":[]}',
            "model": "gemma",
            "tool_calls": [],
        }

    monkeypatch.setattr(app_module.ASSISTANT_CLIENT, "chat", fake_chat)
    response = test_client.post(
        "/api/assistant/jobs",
        json={
            "kind": "recipe_audit",
            "client_request_id": "123e4567-e89b-42d3-a456-426614174031",
            "surface": "recipe",
            "message": "audit this recipe",
            "scope": AssistantScope().model_dump(mode="json"),
            "draft": _draft_json(),
        },
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert job_id

    deadline = time.monotonic() + 5.0
    body: dict[str, object] = {}
    while time.monotonic() < deadline:
        body = test_client.get(f"/api/assistant/jobs/{job_id}").json()
        if body.get("status") in {"succeeded", "failed"}:
            break
        time.sleep(0.05)
    assert body["status"] == "succeeded", body
    assert "CONTEXT_JSON" in captured["message"]
    assert f"JOB_ID={job_id}" in captured["message"]
    assert "Do not call tools" in captured["message"]
    assert "<tool_call>" in captured["message"]
    contract_line = next(
        line for line in captured["message"].splitlines()
        if line.startswith("RESULT_CONTRACT_JSON=")
    )
    contract = json.loads(contract_line.removeprefix("RESULT_CONTRACT_JSON="))
    assert contract["properties"]["kind"]["const"] == "recipe_audit"
    enum_line = next(
        line for line in captured["message"].splitlines()
        if line.startswith("RESULT_ENUM_RULES_JSON=")
    )
    enum_rules = json.loads(enum_line.removeprefix("RESULT_ENUM_RULES_JSON="))
    assert "process" in enum_rules["Finding.category"]
    assert "process" not in enum_rules["Finding.domain"]
    assert "measurement" in enum_rules["Finding.category"]
    assert "measurement" not in enum_rules["Finding.domain"]
    output_line = next(
        line for line in captured["message"].splitlines()
        if line.startswith("OUTPUT_LIMITS_JSON=")
    )
    assert json.loads(output_line.removeprefix("OUTPUT_LIMITS_JSON=")) == {
        "evidence_max_items": 4,
        "findings_max_items": 4,
        "rationale_max_sentences": 2,
        "summary_max_sentences": 2,
        "uncertainties_max_items": 4,
    }
    finding = contract["$defs"]["Finding"]
    required_finding_fields = {
        "finding_id",
        "origin",
        "field_path",
        "severity",
        "category",
        "domain",
        "rationale",
    }
    assert required_finding_fields <= set(finding["properties"])
    assert required_finding_fields <= set(finding["required"])
    result = body.get("result")
    assert isinstance(result, dict)
    assert result["kind"] == "recipe_audit"
    assert isinstance(result["findings"], list)
    assert body["model"] == "gemma"
    assert "context" not in body
    context_response = test_client.get(f"/api/assistant/jobs/{job_id}?include=context")
    assert context_response.status_code == 200
    context_body = context_response.json()
    assert context_body["context"]["draft"]["name"] == "HTTP audit recipe"
    assert context_body["context"]["dirty_diff"] == []
    assert context_body["context_hash"] == body["context_hash"]
    calls = body.get("model_calls")
    assert isinstance(calls, list) and calls
    assert isinstance(calls[0], dict)
    assert calls[0]["stage"] == "ordinary"
    assert calls[0]["status"] == "response_received"


def test_research_is_admitted_once_and_persisted_as_a_stage(test_client, app_module, monkeypatch) -> None:
    app_module.ASSISTANT_CLIENT.base_url = "http://127.0.0.1:3100"
    app_module.ASSISTANT_CLIENT.token_path = Path("/unused-in-mocked-research-job")
    research_entered = Event()
    release_research = Event()
    calls: list[str] = []

    def slow_search(query: str) -> tuple[list[dict[str, object]], dict[str, str]]:
        calls.append(query)
        research_entered.set()
        assert release_research.wait(5.0)
        return [], {"searxng": "empty", "kiwix": "empty"}

    def fake_chat(message: str, conversation_id: str) -> dict[str, object]:
        del message, conversation_id
        return {
            "message": '{"envelope_version":1,"kind":"recipe_audit","summary":"ok","findings":[]}',
            "model": "gemma",
            "tool_calls": [],
        }

    monkeypatch.setattr(app_module.RESEARCH_BROKER, "search", slow_search)
    monkeypatch.setattr(app_module.ASSISTANT_CLIENT, "chat", fake_chat)
    payload = {
        "kind": "recipe_audit",
        "client_request_id": "123e4567-e89b-42d3-a456-426614174034",
        "surface": "recipe",
        "message": "research before audit",
        "research": True,
        "scope": AssistantScope().model_dump(mode="json"),
        "draft": _draft_json(),
    }
    response = test_client.post("/api/assistant/jobs", json=payload)
    assert response.status_code == 202, response.text
    first = response.json()
    job_id = first["job_id"]
    assert research_entered.wait(2.0)
    running = test_client.get(f"/api/assistant/jobs/{job_id}").json()
    assert running["status"] == "running"
    running_stages = running.get("stages")
    assert isinstance(running_stages, list)
    assert next(stage for stage in running_stages if isinstance(stage, dict) and stage["stage"] == "research")["status"] == "running"

    duplicate = test_client.post("/api/assistant/jobs", json=payload)
    assert duplicate.status_code == 202, duplicate.text
    assert duplicate.json()["job_id"] == job_id
    assert duplicate.json()["created"] is False
    assert calls == ["research before audit"]

    release_research.set()
    deadline = time.monotonic() + 5.0
    finished: dict[str, object] = {}
    while time.monotonic() < deadline:
        finished = test_client.get(f"/api/assistant/jobs/{job_id}").json()
        if finished.get("status") in {"succeeded", "failed"}:
            break
        time.sleep(0.05)
    assert finished["status"] == "succeeded", finished
    assert [stage["status"] for stage in finished["stages"]] == [
        "succeeded", "succeeded", "succeeded", "succeeded"
    ]
    # Both engines reported empty payloads — the pipeline must surface
    # that outcome as an explicit insufficient result, not as a silent
    # success.
    research_status_raw = finished["research_status"]
    research_status: dict[str, str] = (
        dict(research_status_raw) if isinstance(research_status_raw, dict) else {}
    )
    assert research_status["searxng"] == "empty", research_status
    assert research_status["kiwix"] == "empty", research_status
    assert research_status["outcome"] == "insufficient", research_status
    assert "ok" not in research_status.values(), research_status


def test_assistant_pipeline_is_started_and_stopped_by_app_lifespan(app_module) -> None:
    from fastapi.testclient import TestClient

    pipeline = app_module.app.state.assistant_pipeline
    assert pipeline is not None
    with TestClient(app_module.app) as client:
        assert client.get("/health").status_code == 200
        assert pipeline._thread is not None
        assert pipeline._thread.is_alive()
    assert pipeline._thread is not None
    assert not pipeline._thread.is_alive()


def test_full_recipe_audit_persists_four_scopes_and_synthesis(test_client, app_module, monkeypatch) -> None:
    app_module.ASSISTANT_CLIENT.base_url = "http://127.0.0.1:3100"
    app_module.ASSISTANT_CLIENT.token_path = Path("/unused-in-mocked-full-audit")
    messages: list[str] = []

    def fake_chat(message: str, conversation_id: str) -> dict[str, object]:
        messages.append(message)
        return {
            "message": '{"envelope_version":1,"kind":"recipe_audit","summary":"scope ok","findings":[]}',
            "model": "gemma",
            "tool_calls": [],
        }

    monkeypatch.setattr(app_module.ASSISTANT_CLIENT, "chat", fake_chat)
    response = test_client.post(
        "/api/assistant/jobs",
        json={
            "kind": "recipe_audit",
            "audit_profile": "full",
            "client_request_id": "123e4567-e89b-42d3-a456-426614174032",
            "surface": "recipe",
            "message": "run the complete audit",
            "scope": AssistantScope().model_dump(mode="json"),
            "draft": _draft_json(),
        },
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    deadline = time.monotonic() + 5.0
    body: dict[str, object] = {}
    while time.monotonic() < deadline:
        body = test_client.get(f"/api/assistant/jobs/{job_id}").json()
        if body.get("status") in {"succeeded", "failed"}:
            break
        time.sleep(0.05)
    assert body["status"] == "succeeded", body
    assert len(messages) == 5
    assert all("RESULT_CONTRACT_JSON=" in message for message in messages)
    assert [
        next(line for line in message.splitlines() if line.startswith("JOB_ID="))
        for message in messages
    ] == [
        f"JOB_ID={job_id}:culture_and_targets",
        f"JOB_ID={job_id}:materials",
        f"JOB_ID={job_id}:scheduled_additions",
        f"JOB_ID={job_id}:process_and_balance",
        f"JOB_ID={job_id}:synthesis",
    ]
    assert "FINDING_ID_RULE=" in messages[-1]
    assert all(len(message.encode("utf-8")) <= MAX_STAGE_BYTES for message in messages)
    calls = body.get("model_calls")
    assert isinstance(calls, list) and len(calls) == 5
    assert [call["stage"] for call in calls if isinstance(call, dict)] == [
        "culture_and_targets", "materials", "scheduled_additions", "process_and_balance", "synthesis"
    ]


def test_empty_only_autofill_excludes_nonblank_changes(test_client, app_module, monkeypatch) -> None:
    app_module.ASSISTANT_CLIENT.base_url = "http://127.0.0.1:3100"
    app_module.ASSISTANT_CLIENT.token_path = Path("/unused-in-mocked-autofill")
    proposal = RecipeProposal.model_validate({**_draft_json(), "name": "Model replacement"})

    def fake_chat(message: str, conversation_id: str) -> dict[str, object]:
        return {
            "message": __import__("json").dumps({
                "envelope_version": 1,
                "kind": "recipe_autofill",
                "proposal": proposal.model_dump(mode="json"),
            }),
            "model": "gemma",
            "tool_calls": [],
        }

    monkeypatch.setattr(app_module.ASSISTANT_CLIENT, "chat", fake_chat)
    response = test_client.post(
        "/api/assistant/jobs",
        json={
            "kind": "recipe_autofill",
            "client_request_id": "123e4567-e89b-42d3-a456-426614174033",
            "surface": "recipe",
            "message": "fill empty fields only",
            "scope": AssistantScope().model_dump(mode="json"),
            "draft": _draft_json(),
        },
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    deadline = time.monotonic() + 5.0
    body: dict[str, object] = {}
    while time.monotonic() < deadline:
        body = test_client.get(f"/api/assistant/jobs/{job_id}").json()
        if body.get("status") in {"succeeded", "failed"}:
            break
        time.sleep(0.05)
    assert body["status"] == "succeeded", body
    result = body.get("result")
    assert isinstance(result, dict)
    assert result["excluded_change_count"] >= 1


def test_research_search_skips_searxng_results_with_only_a_title(app_module, monkeypatch) -> None:
    broker = ResearchBroker()

    def fake_json(url: str, params: dict[str, str]) -> object:
        if url == broker.searxng_url:
            return {
                "results": [
                    {
                        "url": "https://example.invalid/title-only",
                        "title": "Title-only result with no snippet",
                        "engine": "example",
                    },
                    {
                        "url": "https://example.invalid/blank-snippet",
                        "title": "Whitespace-only content",
                        "content": "   ",
                        "engine": "example",
                    },
                    {
                        "url": "https://example.invalid/usable-snippet",
                        "title": "Usable snippet",
                        "content": "Real snippet text from SearXNG.",
                        "engine": "example",
                    },
                ]
            }
        assert url == f"{broker.kiwix_base_url}/suggest"
        return [{"kind": "path", "path": "Title_only_test", "value": "Title only"}]

    monkeypatch.setattr(broker, "_request_json", fake_json)
    monkeypatch.setattr(
        broker,
        "_request_text",
        lambda url, params: (url, "<main>Kiwix fallback text.</main>"),
    )

    documents, status = broker.search("title-only test")

    assert status["searxng"] == "ok"
    assert status["kiwix"] == "ok"
    searxng_documents = [document for document in documents if document["source_kind"] == "searxng"]
    assert [document["source_url"] for document in searxng_documents] == [
        "https://example.invalid/usable-snippet"
    ]
    assert [document["metadata"]["content_kind"] for document in searxng_documents] == ["snippet"]
    kiwix_documents = [document for document in documents if document["source_kind"] == "kiwix"]
    assert kiwix_documents
    assert kiwix_documents[0]["metadata"]["content_kind"] == "full_text"


def test_research_search_skips_when_searxng_returns_only_blank_content(app_module, monkeypatch) -> None:
    broker = ResearchBroker()

    def fake_json(url: str, params: dict[str, str]) -> object:
        if url == broker.searxng_url:
            return {
                "results": [
                    {
                        "url": "https://example.invalid/blank-1",
                        "title": "Blank snippet one",
                        "content": "",
                        "engine": "example",
                    },
                    {
                        "url": "https://example.invalid/blank-2",
                        "title": "Blank snippet two",
                        "content": "   \n  ",
                        "engine": "example",
                    },
                ]
            }
        assert url == f"{broker.kiwix_base_url}/suggest"
        return [{"kind": "path", "path": "Blank_content_only", "value": "Blank content only"}]

    monkeypatch.setattr(broker, "_request_json", fake_json)
    monkeypatch.setattr(
        broker,
        "_request_text",
        lambda url, params: (url, "<main>Kiwix fallback text.</main>"),
    )

    documents, status = broker.search("blank content only")

    assert status["searxng"] == "empty"
    assert status["kiwix"] == "ok"
    searxng_documents = [document for document in documents if document["source_kind"] == "searxng"]
    assert searxng_documents == []
    kiwix_documents = [document for document in documents if document["source_kind"] == "kiwix"]
    assert kiwix_documents
    assert kiwix_documents[0]["metadata"]["content_kind"] == "full_text"


def _audit_finding_json(finding_id: str, field_path: str = "/ingredients/123e4567-e89b-42d3-a456-426614174030/category") -> dict[str, object]:
    return {
        "finding_id": finding_id,
        "origin": "model",
        "field_path": field_path,
        "severity": "advisory",
        "category": "consistency",
        "domain": "general",
        "rationale": "stage-local finding used only to seed approval",
    }


def _wait_for_terminal(test_client, job_id: str, deadline_s: float = 5.0) -> dict[str, object]:
    deadline = time.monotonic() + deadline_s
    body: dict[str, object] = {}
    while time.monotonic() < deadline:
        body = test_client.get(f"/api/assistant/jobs/{job_id}").json()
        if body.get("status") in {"succeeded", "failed"}:
            break
        time.sleep(0.05)
    return body


def _run_audit_and_approve(
    test_client,
    app_module,
    monkeypatch,
    *,
    client_request_id: str,
    scope: dict[str, object],
    draft: dict[str, object],
    approved_decision: str | None,
    approved_finding_ids: list[str],
    chat_finding_id: str,
) -> str:
    app_module.ASSISTANT_CLIENT.base_url = "http://127.0.0.1:3100"
    app_module.ASSISTANT_CLIENT.token_path = Path("/unused-in-mocked-rewrite-test")

    audit_payload = json.dumps({
        "envelope_version": 1,
        "kind": "recipe_audit",
        "summary": "audit for rewrite test",
        "findings": [_audit_finding_json(chat_finding_id)],
    })

    def fake_chat(message: str, conversation_id: str) -> dict[str, object]:
        del message, conversation_id
        return {"message": audit_payload, "model": "gemma", "tool_calls": []}

    monkeypatch.setattr(app_module.ASSISTANT_CLIENT, "chat", fake_chat)

    response = test_client.post(
        "/api/assistant/jobs",
        json={
            "kind": "recipe_audit",
            "client_request_id": client_request_id,
            "surface": "recipe",
            "message": "audit before rewrite",
            "scope": scope,
            "draft": draft,
        },
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    body = _wait_for_terminal(test_client, job_id)
    assert body.get("status") == "succeeded", body
    assert any(
        isinstance(item, dict) and item.get("finding_id") == chat_finding_id
        for item in (body.get("result", {}) or {}).get("findings", [])
    ), body

    if approved_decision is not None:
        approve_response = test_client.post(
            f"/api/assistant/jobs/{job_id}/approve",
            json={"decision": approved_decision, "finding_ids": approved_finding_ids},
        )
        assert approve_response.status_code == 200, approve_response.text
        approved = approve_response.json()
        assert approved.get("approval_decision") == approved_decision
        assert approved.get("approved_finding_ids") == approved_finding_ids
    return job_id


def test_submit_rewrite_rejects_missing_audit_parent(test_client, app_module, monkeypatch) -> None:
    app_module.ASSISTANT_CLIENT.base_url = "http://127.0.0.1:3100"
    app_module.ASSISTANT_CLIENT.token_path = Path("/unused-in-mocked-missing-parent")

    def fake_chat(message: str, conversation_id: str) -> dict[str, object]:
        del message, conversation_id
        return {
            "message": json.dumps({
                "envelope_version": 1,
                "kind": "recipe_rewrite",
                "summary": "should not run",
                "parent_job_id": "00000000-0000-4000-8000-000000000000",
                "approved_finding_ids": ["missing-parent-1"],
                "proposal": _draft_json(),
            }),
            "model": "gemma",
            "tool_calls": [],
        }

    monkeypatch.setattr(app_module.ASSISTANT_CLIENT, "chat", fake_chat)

    response = test_client.post(
        "/api/assistant/jobs",
        json={
            "kind": "recipe_rewrite",
            "client_request_id": "123e4567-e89b-42d3-a456-426614174040",
            "surface": "recipe",
            "message": "rewrite without an audit parent",
            "scope": AssistantScope().model_dump(mode="json"),
            "draft": _draft_json(),
            "parent_job_id": "00000000-0000-4000-8000-000000000000",
            "approved_finding_ids": ["missing-parent-1"],
        },
    )
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail == {
        "code": "audit_parent_unavailable",
        "message": "a succeeded recipe audit is required",
    }


def test_recipe_rewrite_retries_once_on_nested_model_shape_error(
    test_client, app_module, monkeypatch
) -> None:
    chat_finding_id = "rewrite-repair-1"
    draft = copy.deepcopy(_draft_json())
    draft["ingredients"].append({
        "ingredient_key": "123e4567-e89b-42d3-a456-426614174031",
        "name": "black tea",
        "quantity": 10.0,
        "unit": "g",
        "material_type": "tannin",
        "tannin_detail": {"source_kind": "black_tea"},
    })
    parent_job_id = _run_audit_and_approve(
        test_client,
        app_module,
        monkeypatch,
        client_request_id="123e4567-e89b-42d3-a456-426614174051",
        scope=AssistantScope().model_dump(mode="json"),
        draft=draft,
        approved_decision="approved",
        approved_finding_ids=[chat_finding_id],
        chat_finding_id=chat_finding_id,
    )

    app_module.ASSISTANT_STRUCTURED_CLIENT.base_url = "http://127.0.0.1:3100"
    bad_proposal: dict[str, Any] = copy.deepcopy(draft)
    bad_proposal["ingredients"][1]["tannin_detail"] = "black tea leaves"
    valid_message = json.dumps({
        "envelope_version": 1,
        "kind": "recipe_rewrite",
        "summary": "rewrite repaired",
        "parent_job_id": "123e4567-e89b-42d3-a456-426614174096",
        "approved_finding_ids": [chat_finding_id],
        "proposal": draft,
    })
    responses = iter([
        {"message": json.dumps({
            "envelope_version": 1,
            "kind": "recipe_rewrite",
            "summary": "bad nested shape",
            "parent_job_id": parent_job_id,
            "approved_finding_ids": [chat_finding_id],
            "proposal": bad_proposal,
        }), "model": "gemma", "tool_calls": []},
        {"message": valid_message, "model": "gemma", "tool_calls": []},
    ])
    prompts: list[str] = []

    def fake_structured_chat(message: str, conversation_id: str) -> dict[str, object]:
        del conversation_id
        prompts.append(message)
        return next(responses)

    monkeypatch.setattr(
        app_module.ASSISTANT_STRUCTURED_CLIENT,
        "chat",
        fake_structured_chat,
    )

    response = test_client.post(
        "/api/assistant/jobs",
        json={
            "kind": "recipe_rewrite",
            "client_request_id": "123e4567-e89b-42d3-a456-426614174052",
            "surface": "recipe",
            "message": "repair the approved rewrite",
            "scope": AssistantScope().model_dump(mode="json"),
            "draft": draft,
            "parent_job_id": parent_job_id,
            "approved_finding_ids": [chat_finding_id],
        },
    )
    assert response.status_code == 202, response.text
    body = _wait_for_terminal(test_client, response.json()["job_id"])
    assert body["status"] == "succeeded", body
    assert len(prompts) == 2
    assert f"JOB_ID={response.json()['job_id']}" in prompts[1]
    assert "CHANGE_BASIS_RULE=" in prompts[0]
    assert "one basis record for each populated leaf" in prompts[0]
    assert "RECIPE_SCHEMA_RULE=" in prompts[0]
    assert "REPAIR_NOTE=" in prompts[1]
    assert "canonical RFC 6901 leaf paths" in prompts[1]
    calls = body["model_calls"]
    assert isinstance(calls, list) and len(calls) == 2
    first_call, second_call = calls
    assert isinstance(first_call, dict) and isinstance(second_call, dict)
    assert first_call["status"] == "parse_failed"
    assert second_call["status"] == "parsed"
    result = body["result"]
    assert isinstance(result, dict)
    assert result["kind"] == "recipe_rewrite"
    assert result["parent_job_id"] == parent_job_id
    assert result["approved_finding_ids"] == [chat_finding_id]
    assert body["error_code"] is None


def test_recipe_rewrite_does_not_retry_unresolved_evidence(
    test_client, app_module, monkeypatch
) -> None:
    chat_finding_id = "rewrite-grounding-1"
    draft = copy.deepcopy(_draft_json())
    parent_job_id = _run_audit_and_approve(
        test_client,
        app_module,
        monkeypatch,
        client_request_id="123e4567-e89b-42d3-a456-426614174053",
        scope=AssistantScope().model_dump(mode="json"),
        draft=draft,
        approved_decision="approved",
        approved_finding_ids=[chat_finding_id],
        chat_finding_id=chat_finding_id,
    )

    app_module.ASSISTANT_STRUCTURED_CLIENT.base_url = "http://127.0.0.1:3100"
    prompts: list[str] = []
    rewrite = {
        "envelope_version": 1,
        "kind": "recipe_rewrite",
        "summary": "unresolved evidence",
        "parent_job_id": parent_job_id,
        "approved_finding_ids": [chat_finding_id],
        "proposal": draft,
        "evidence": [{
            "pointer": "https://attacker.example/not-in-context",
            "source_url": "https://attacker.example/not-in-context",
        }],
    }

    def fake_structured_chat(message: str, conversation_id: str) -> dict[str, object]:
        del conversation_id
        prompts.append(message)
        return {"message": json.dumps(rewrite), "model": "gemma", "tool_calls": []}

    monkeypatch.setattr(app_module.ASSISTANT_STRUCTURED_CLIENT, "chat", fake_structured_chat)
    response = test_client.post(
        "/api/assistant/jobs",
        json={
            "kind": "recipe_rewrite",
            "client_request_id": "123e4567-e89b-42d3-a456-426614174054",
            "surface": "recipe",
            "message": "reject unresolved evidence",
            "scope": AssistantScope().model_dump(mode="json"),
            "draft": draft,
            "parent_job_id": parent_job_id,
            "approved_finding_ids": [chat_finding_id],
        },
    )
    assert response.status_code == 202, response.text
    body = _wait_for_terminal(test_client, response.json()["job_id"])
    assert body["status"] == "failed", body
    assert len(prompts) == 1
    calls = body["model_calls"]
    assert isinstance(calls, list) and len(calls) == 1
    parse_errors = body["parse_errors"]
    assert isinstance(parse_errors, list) and parse_errors[0]["type"] == "context_pointer_invalid"


def test_submit_rewrite_rejects_unaudited_parent(test_client, app_module, monkeypatch) -> None:
    chat_finding_id = "unaudited-parent-1"
    parent_job_id = _run_audit_and_approve(
        test_client,
        app_module,
        monkeypatch,
        client_request_id="123e4567-e89b-42d3-a456-426614174041",
        scope=AssistantScope().model_dump(mode="json"),
        draft=_draft_json(),
        approved_decision=None,
        approved_finding_ids=[],
        chat_finding_id=chat_finding_id,
    )

    app_module.ASSISTANT_CLIENT.base_url = "http://127.0.0.1:3100"
    app_module.ASSISTANT_CLIENT.token_path = Path("/unused-in-mocked-unaudited-parent")

    def fake_chat(message: str, conversation_id: str) -> dict[str, object]:
        del message, conversation_id
        return {
            "message": json.dumps({
                "envelope_version": 1,
                "kind": "recipe_rewrite",
                "summary": "should not run",
                "parent_job_id": parent_job_id,
                "approved_finding_ids": [chat_finding_id],
                "proposal": _draft_json(),
            }),
            "model": "gemma",
            "tool_calls": [],
        }

    monkeypatch.setattr(app_module.ASSISTANT_CLIENT, "chat", fake_chat)

    response = test_client.post(
        "/api/assistant/jobs",
        json={
            "kind": "recipe_rewrite",
            "client_request_id": "123e4567-e89b-42d3-a456-426614174042",
            "surface": "recipe",
            "message": "rewrite on unaudited parent",
            "scope": AssistantScope().model_dump(mode="json"),
            "draft": _draft_json(),
            "parent_job_id": parent_job_id,
            "approved_finding_ids": [chat_finding_id],
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {
        "code": "audit_not_approved",
        "message": "approve audit findings before rewrite",
    }


def test_submit_rewrite_rejects_mismatched_approval(test_client, app_module, monkeypatch) -> None:
    chat_finding_id = "approval-mismatch-1"
    parent_job_id = _run_audit_and_approve(
        test_client,
        app_module,
        monkeypatch,
        client_request_id="123e4567-e89b-42d3-a456-426614174043",
        scope=AssistantScope().model_dump(mode="json"),
        draft=_draft_json(),
        approved_decision="approved",
        approved_finding_ids=[chat_finding_id],
        chat_finding_id=chat_finding_id,
    )

    app_module.ASSISTANT_CLIENT.base_url = "http://127.0.0.1:3100"
    app_module.ASSISTANT_CLIENT.token_path = Path("/unused-in-mocked-approval-mismatch")

    def fake_chat(message: str, conversation_id: str) -> dict[str, object]:
        del message, conversation_id
        return {
            "message": json.dumps({
                "envelope_version": 1,
                "kind": "recipe_rewrite",
                "summary": "should not run",
                "parent_job_id": parent_job_id,
                "approved_finding_ids": [chat_finding_id, "approval-mismatch-extra"],
                "proposal": _draft_json(),
            }),
            "model": "gemma",
            "tool_calls": [],
        }

    monkeypatch.setattr(app_module.ASSISTANT_CLIENT, "chat", fake_chat)

    response = test_client.post(
        "/api/assistant/jobs",
        json={
            "kind": "recipe_rewrite",
            "client_request_id": "123e4567-e89b-42d3-a456-426614174044",
            "surface": "recipe",
            "message": "rewrite with mismatched approval",
            "scope": AssistantScope().model_dump(mode="json"),
            "draft": _draft_json(),
            "parent_job_id": parent_job_id,
            "approved_finding_ids": [chat_finding_id, "approval-mismatch-extra"],
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {
        "code": "approval_mismatch",
        "message": "rewrite findings do not match the recorded approval",
    }


def test_submit_rewrite_rejects_draft_changed_since_audit(test_client, app_module, monkeypatch) -> None:
    chat_finding_id = "draft-changed-1"
    parent_draft = _draft_json()
    parent_job_id = _run_audit_and_approve(
        test_client,
        app_module,
        monkeypatch,
        client_request_id="123e4567-e89b-42d3-a456-426614174045",
        scope=AssistantScope().model_dump(mode="json"),
        draft=parent_draft,
        approved_decision="approved",
        approved_finding_ids=[chat_finding_id],
        chat_finding_id=chat_finding_id,
    )

    app_module.ASSISTANT_CLIENT.base_url = "http://127.0.0.1:3100"
    app_module.ASSISTANT_CLIENT.token_path = Path("/unused-in-mocked-draft-changed")

    modified_draft = copy.deepcopy(parent_draft)
    modified_draft["description"] = "A descriptive note that mutates the draft hash."

    def fake_chat(message: str, conversation_id: str) -> dict[str, object]:
        del message, conversation_id
        proposal = RecipeProposal.model_validate(copy.deepcopy(modified_draft))
        return {
            "message": json.dumps({
                "envelope_version": 1,
                "kind": "recipe_rewrite",
                "summary": "should not run",
                "parent_job_id": parent_job_id,
                "approved_finding_ids": [chat_finding_id],
                "proposal": proposal.model_dump(mode="json"),
            }),
            "model": "gemma",
            "tool_calls": [],
        }

    monkeypatch.setattr(app_module.ASSISTANT_CLIENT, "chat", fake_chat)

    response = test_client.post(
        "/api/assistant/jobs",
        json={
            "kind": "recipe_rewrite",
            "client_request_id": "123e4567-e89b-42d3-a456-426614174046",
            "surface": "recipe",
            "message": "rewrite after draft changed",
            "scope": AssistantScope().model_dump(mode="json"),
            "draft": modified_draft,
            "parent_job_id": parent_job_id,
            "approved_finding_ids": [chat_finding_id],
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {
        "code": "draft_changed_since_audit",
        "message": "the browser draft changed; run a new audit",
    }


def test_submit_rewrite_rejects_saved_revision_changed_since_audit(
    test_client, app_module, monkeypatch
) -> None:
    from app.brewing import RecipePayload, RecipeUpdatePayload

    chat_finding_id = "saved-revision-1"
    saved_recipe = app_module.BREWING_STORE.create_recipe(
        RecipePayload.model_validate({
            "name": "Saved rewrite recipe",
            "style": "english-ipa",
            "description": "",
            "base_volume_l": 10.0,
            "beverage_type": "beer",
            "notes": "",
            "ingredients": [
                {
                    "ingredient_key": "123e4567-e89b-42d3-a456-426614174050",
                    "name": "pale malt",
                    "quantity": 2.0,
                    "unit": "kg",
                    "material_type": "fermentable",
                }
            ],
        })
    )
    recipe_id = int(saved_recipe["id"])
    revision_at_audit = int(saved_recipe["revision"])

    parent_job_id = _run_audit_and_approve(
        test_client,
        app_module,
        monkeypatch,
        client_request_id="123e4567-e89b-42d3-a456-426614174047",
        scope=AssistantScope(recipe_id=recipe_id, recipe_revision=revision_at_audit).model_dump(
            mode="json"
        ),
        draft=_draft_json(),
        approved_decision="approved",
        approved_finding_ids=[chat_finding_id],
        chat_finding_id=chat_finding_id,
    )

    update_payload = RecipeUpdatePayload.model_validate({
        "name": "Saved rewrite recipe (edited)",
        "style": "english-ipa",
        "description": "second revision",
        "base_volume_l": 10.0,
        "beverage_type": "beer",
        "notes": "",
        "expected_revision": revision_at_audit,
        "ingredients": [
            {
                "ingredient_key": "123e4567-e89b-42d3-a456-426614174050",
                "name": "pale malt",
                "quantity": 2.0,
                "unit": "kg",
                "material_type": "fermentable",
            }
        ],
    })
    updated = app_module.BREWING_STORE.update_recipe(recipe_id, update_payload)
    assert updated is not None
    assert int(updated["revision"]) != revision_at_audit

    app_module.ASSISTANT_CLIENT.base_url = "http://127.0.0.1:3100"
    app_module.ASSISTANT_CLIENT.token_path = Path("/unused-in-mocked-saved-revision")

    def fake_chat(message: str, conversation_id: str) -> dict[str, object]:
        del message, conversation_id
        return {
            "message": json.dumps({
                "envelope_version": 1,
                "kind": "recipe_rewrite",
                "summary": "should not run",
                "parent_job_id": parent_job_id,
                "approved_finding_ids": [chat_finding_id],
                "proposal": _draft_json(),
            }),
            "model": "gemma",
            "tool_calls": [],
        }

    monkeypatch.setattr(app_module.ASSISTANT_CLIENT, "chat", fake_chat)

    response = test_client.post(
        "/api/assistant/jobs",
        json={
            "kind": "recipe_rewrite",
            "client_request_id": "123e4567-e89b-42d3-a456-426614174048",
            "surface": "recipe",
            "message": "rewrite after saved revision changed",
            "scope": AssistantScope(recipe_id=recipe_id).model_dump(mode="json"),
            "draft": _draft_json(),
            "parent_job_id": parent_job_id,
            "approved_finding_ids": [chat_finding_id],
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {
        "code": "saved_revision_changed_since_audit",
        "message": "the saved recipe revision changed; run a new audit",
    }


def test_evidence_link_supports_contradicts_not_supporting_round_trip(
    test_client, app_module
) -> None:
    """Operator review endpoints must accept supports/contradicts/not_supporting
    transitions and reject invalid values; the underlying frozen link stays
    resolvable after the source URL is superseded.
    """
    # Persist a document + version, then link it via a real assistant job so
    # the link row's FOREIGN KEY (job_id REFERENCES assistant_jobs) holds.
    from app.assistant_pipeline import AssistantJobRequest, AssistantJobStore

    app_module.ASSISTANT_STORE.save_research_documents(
        [
            {
                "source_kind": "kiwix",
                "source_url": "http://example.invalid/round-trip",
                "title": "Round trip reference",
                "content": "Yeast nutrient balance reference for fermentation reliability.",
                "metadata": {"content_kind": "full_text"},
            }
        ]
    )
    matches = app_module.ASSISTANT_STORE.search_research("yeast nutrient balance")
    assert matches

    job_store = AssistantJobStore(app_module.BREW_DB_PATH)
    job, _created = job_store.create(
        AssistantJobRequest(
            kind="chat",
            client_request_id="123e4567-e89b-42d3-a456-4266141740e1",
            surface="recipe",
            message="round trip review",
            research=True,
        ),
        {},
    )
    links = app_module.ASSISTANT_STORE.link_research_evidence(
        job["job_id"], matches
    )
    assert links
    link_id = links[0]["link_id"]

    # supports / contradicts / not_supporting round-trip via API.
    for support_status in ("supports", "contradicts", "not_supporting"):
        response = test_client.post(
            f"/api/assistant/research/links/{link_id}/support-status",
            json={"support_status": support_status},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["link_id"] == link_id
        assert body["support_status"] == support_status
        assert body["version_id"] == links[0]["version_id"]

    # Invalid support_status is rejected with a structured 422. Pydantic
    # performs the field validation at request-parse time; the error surface
    # is the standard FastAPI validation envelope.
    invalid = test_client.post(
        f"/api/assistant/research/links/{link_id}/support-status",
        json={"support_status": "definitely_supports"},
    )
    assert invalid.status_code == 422, invalid.text
    detail = invalid.json()["detail"]
    assert isinstance(detail, list), detail
    error_types = {item.get("type") for item in detail}
    assert "value_error" in error_types or "value_error.any_str.max_length" in error_types, detail

    # Unknown link id is rejected with a structured 404.
    missing = test_client.post(
        "/api/assistant/research/links/99999999/support-status",
        json={"support_status": "supports"},
    )
    assert missing.status_code == 404, missing.text
    assert missing.json()["detail"]["code"] == "link_not_found"

    # Frozen link stays resolvable after supersession: a second document for
    # the same URL creates v2, but the linked version remains v1.
    app_module.ASSISTANT_STORE.save_research_documents(
        [
            {
                "source_kind": "kiwix",
                "source_url": "http://example.invalid/round-trip",
                "title": "Round trip reference v2",
                "content": "Updated yeast nutrient balance reference with new nutrients.",
                "metadata": {"content_kind": "full_text"},
            }
        ]
    )
    resolved = test_client.get(
        f"/api/assistant/research/links/{link_id}"
    ).json()
    assert resolved["version_id"] == links[0]["version_id"]
    assert resolved["version_no"] == 1
    assert resolved["content_hash"] == links[0]["content_hash"]
