"""Local-first research reuse contract.

Two independent sequential assistant jobs using the same research message:

* Job 1 — no local FTS hit before submission → broker invoked exactly once,
  one document persisted, completed research_status names the fetched source.
* Job 2 — distinct client_request_id against the same isolated SQLite store →
  local FTS hit present, broker must NOT be invoked again, completed
  research_status contains a stable local-reuse marker, and the matched
  document/version (with content_hash) is exposed in the job context.

The test relies on the globally installed ``app_module`` fixture (isolated
SQLite store per test) and stubs ``RESEARCH_BROKER.search`` plus
``ASSISTANT_CLIENT.chat`` so no model/research network call happens. A bounded
terminal poll helper waits for each job to land in ``succeeded``/``failed``
without busy-spinning.
"""

from __future__ import annotations

import time
from pathlib import Path

from app.assistant_pipeline import AssistantScope


LOCAL_REUSE_KEY = "local_reuse"
LOCAL_REUSE_MARKER = "matched"


def _wait_for_terminal(test_client, job_id: str, deadline_s: float = 10.0) -> dict:
    deadline = time.monotonic() + deadline_s
    body: dict = {}
    while time.monotonic() < deadline:
        body = test_client.get(f"/api/assistant/jobs/{job_id}").json()
        if body.get("status") in {"succeeded", "failed"}:
            return body
        time.sleep(0.05)
    return body


def _post_chat_job(test_client, *, client_request_id: str, message: str, research: bool) -> dict:
    payload = {
        "kind": "chat",
        "client_request_id": client_request_id,
        "surface": "recipe",
        "message": message,
        "research": research,
        "scope": AssistantScope().model_dump(mode="json"),
    }
    response = test_client.post("/api/assistant/jobs", json=payload)
    assert response.status_code == 202, response.text
    return response.json()


def test_research_reuse_skips_broker_when_local_fts_match_exists(
    test_client, app_module, monkeypatch
) -> None:
    # Wire a deterministic model fake so kind="chat" jobs complete without
    # touching the real phone agent.
    app_module.ASSISTANT_CLIENT.base_url = "http://127.0.0.1:3100"
    app_module.ASSISTANT_CLIENT.token_path = Path("/unused-in-mocked-research-reuse")

    def fake_chat(message: str, conversation_id: str) -> dict:
        del message, conversation_id
        return {"message": "ok", "model": "gemma", "tool_calls": []}

    monkeypatch.setattr(app_module.ASSISTANT_CLIENT, "chat", fake_chat)

    # Deterministic local ResearchBroker fake: invoked exactly when the
    # production path reaches ``research.search(query)``. We track call
    # count + persist the document bundle via the real AssistantStore so the
    # FTS index sees the same payload as production.
    search_calls: list[str] = []

    def fake_search(query: str) -> tuple[list[dict], dict[str, str]]:
        search_calls.append(query)
        return (
            [
                {
                    "source_kind": "kiwix",
                    "source_url": "http://example.invalid/blackberry-fermentation",
                    "title": "Blackberry fermentation",
                    "content": (
                        "Blackberry must acidity, tannin balance, and yeast "
                        "nutrition reference for fermentation reliability."
                    ),
                    "metadata": {"content_kind": "full_text"},
                }
            ],
            {"searxng": "empty", "kiwix": "ok"},
        )

    monkeypatch.setattr(app_module.RESEARCH_BROKER, "search", fake_search)

    research_message = (
        "Explain blackberry must acidity, tannin balance, and yeast nutrition "
        "for fermentation reliability."
    )

    # --- Job 1: no local FTS hit; expect exactly one broker call + one doc.
    job_one = _post_chat_job(
        test_client,
        client_request_id="123e4567-e89b-42d3-a456-4266141740a1",
        message=research_message,
        research=True,
    )
    finished_one = _wait_for_terminal(test_client, job_one["job_id"])
    assert finished_one.get("status") == "succeeded", finished_one
    assert search_calls == [research_message], (
        f"broker.search calls = {search_calls!r}"
    )

    # Exactly one persisted document (URL-unique) on the isolated store.
    import sqlite3

    with sqlite3.connect(app_module.BREW_DB_PATH) as conn:
        document_rows = conn.execute(
            "SELECT id FROM research_documents WHERE source_url=?",
            ("http://example.invalid/blackberry-fermentation",),
        ).fetchall()
    assert len(document_rows) == 1

    # Job 1 context carries the fetched source in research_status.
    finished_one_ctx = test_client.get(
        f"/api/assistant/jobs/{finished_one['job_id']}?include=context"
    ).json()
    assert finished_one_ctx.get("status") == "succeeded", finished_one_ctx
    context_one = finished_one_ctx.get("context") or {}
    research_status_one = context_one.get("research_status") or {}
    assert research_status_one.get("kiwix") == "ok", research_status_one
    assert LOCAL_REUSE_KEY not in research_status_one, research_status_one
    indexed_one = context_one.get("indexed_reference_matches") or []
    assert indexed_one, "job 1 should have at least one persisted FTS match"
    assert indexed_one[0].get("content_hash"), indexed_one[0]
    fresh_one = context_one.get("fresh_research") or []
    assert len(fresh_one) == 1, fresh_one

    # --- Job 2: distinct client_request_id, local FTS hit available,
    # broker MUST NOT be called again, and the marker must be present.
    job_two = _post_chat_job(
        test_client,
        client_request_id="123e4567-e89b-42d3-a456-4266141740a2",
        message=research_message,
        research=True,
    )
    assert job_two["job_id"] != job_one["job_id"]
    finished_two = _wait_for_terminal(test_client, job_two["job_id"])
    assert finished_two.get("status") == "succeeded", finished_two
    assert search_calls == [research_message], (
        f"broker.search was re-invoked: calls = {search_calls!r}"
    )

    finished_two_ctx = test_client.get(
        f"/api/assistant/jobs/{finished_two['job_id']}?include=context"
    ).json()
    assert finished_two_ctx.get("status") == "succeeded", finished_two_ctx
    context_two = finished_two_ctx.get("context") or {}
    research_status_two = context_two.get("research_status") or {}
    assert research_status_two.get(LOCAL_REUSE_KEY) == LOCAL_REUSE_MARKER, (
        f"missing local-reuse marker in research_status: {research_status_two!r}"
    )
    # The fetched-source semantics from job 1 should NOT bleed into job 2;
    # broker was bypassed, so no fresh kiwix/searxng status is recorded.
    assert "kiwix" not in research_status_two, research_status_two
    assert "searxng" not in research_status_two, research_status_two

    fresh_two = context_two.get("fresh_research") or []
    assert fresh_two == [], fresh_two

    indexed_two = context_two.get("indexed_reference_matches") or []
    assert len(indexed_two) == 1, indexed_two
    assert indexed_two[0].get("content_hash") == indexed_one[0].get("content_hash"), (
        "job 2 must surface the same persisted match/content_hash as job 1"
    )
    assert (
        indexed_two[0].get("document_id") == indexed_one[0].get("document_id")
    ), indexed_two

    # Still exactly one persisted document on the isolated store.
    with sqlite3.connect(app_module.BREW_DB_PATH) as conn:
        document_rows_two = conn.execute(
            "SELECT id FROM research_documents WHERE source_url=?",
            ("http://example.invalid/blackberry-fermentation",),
        ).fetchall()
    assert len(document_rows_two) == 1


def test_cold_fetch_then_network_free_warm_reuse(
    test_client, app_module, monkeypatch
) -> None:
    """Cold fetch persists documents; the second job must reuse them without
    touching the broker or the network at all, even when both engines are
    otherwise unavailable.
    """
    app_module.ASSISTANT_CLIENT.base_url = "http://127.0.0.1:3100"
    app_module.ASSISTANT_CLIENT.token_path = Path("/unused-in-mocked-cold-warm")

    def fake_chat(message: str, conversation_id: str) -> dict:
        del message, conversation_id
        return {"message": "ok", "model": "gemma", "tool_calls": []}

    monkeypatch.setattr(app_module.ASSISTANT_CLIENT, "chat", fake_chat)

    search_calls: list[str] = []

    def fake_search(query: str) -> tuple[list[dict], dict[str, str]]:
        search_calls.append(query)
        return (
            [
                {
                    "source_kind": "kiwix",
                    "source_url": "http://example.invalid/cold-warm-reference",
                    "title": "Cold warm reference",
                    "content": (
                        "Yeast nutrient and temperature balance reference for "
                        "reliable wine fermentation."
                    ),
                    "metadata": {"content_kind": "full_text"},
                }
            ],
            {"searxng": "empty", "kiwix": "ok"},
        )

    monkeypatch.setattr(app_module.RESEARCH_BROKER, "search", fake_search)

    research_message = (
        "Explain yeast nutrient and temperature balance for reliable wine "
        "fermentation."
    )

    # --- Job 1: cold fetch. Broker is invoked exactly once.
    job_one = _post_chat_job(
        test_client,
        client_request_id="123e4567-e89b-42d3-a456-4266141740b1",
        message=research_message,
        research=True,
    )
    finished_one = _wait_for_terminal(test_client, job_one["job_id"])
    assert finished_one.get("status") == "succeeded", finished_one
    assert search_calls == [research_message]

    # --- Job 2: warm reuse. Even if the broker were to raise, the broker
    # must NOT be touched because the indexed FTS match already covers the
    # query. Force the broker to raise to prove it is never consulted.
    def _raise_if_called(query: str) -> tuple[list[dict], dict[str, str]]:
        raise AssertionError(
            f"broker.search was re-invoked during warm reuse: {query!r}"
        )

    monkeypatch.setattr(app_module.RESEARCH_BROKER, "search", _raise_if_called)

    job_two = _post_chat_job(
        test_client,
        client_request_id="123e4567-e89b-42d3-a456-4266141740b2",
        message=research_message,
        research=True,
    )
    assert job_two["job_id"] != job_one["job_id"]
    finished_two = _wait_for_terminal(test_client, job_two["job_id"])
    assert finished_two.get("status") == "succeeded", finished_two

    finished_two_ctx = test_client.get(
        f"/api/assistant/jobs/{finished_two['job_id']}?include=context"
    ).json()
    assert finished_two_ctx.get("status") == "succeeded", finished_two_ctx
    context_two = finished_two_ctx.get("context") or {}
    research_status_two = context_two.get("research_status") or {}
    assert research_status_two.get(LOCAL_REUSE_KEY) == LOCAL_REUSE_MARKER, (
        f"missing local-reuse marker in research_status: {research_status_two!r}"
    )
    # No fresh broker output bled into job 2.
    assert context_two.get("fresh_research") == []
    # The cold-fetched document is surfaced as the persisted match.
    indexed_two = context_two.get("indexed_reference_matches") or []
    assert len(indexed_two) == 1
    assert indexed_two[0]["source_url"] == "http://example.invalid/cold-warm-reference"
