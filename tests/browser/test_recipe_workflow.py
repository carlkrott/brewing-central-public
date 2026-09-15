"""Live-browser qualification for versioned Recipe Book workflows."""

from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from app.assistant_pipeline import (
    Finding,
    RecipeProposal,
    SuggestionBasisRecord,
)


@pytest.mark.requires_browser
def test_recipe_book_create_edit_isolation_and_stale_revision(
    browser_page, live_server: str
) -> None:
    """Create, revise, isolate, and reject stale recipe edits end-to-end."""
    page = browser_page
    page.set_default_timeout(5_000)
    recipes_url = f"{live_server}/api/recipes"

    def api_response(path: str, method: str):
        return lambda response: (
            urlsplit(response.url).path == path
            and response.request.method == method
        )

    def wait_for_recipe_list(minimum: int = 1) -> None:
        page.wait_for_function(
            "minimum => document.querySelectorAll('#recipe-list .recipe-list-item').length >= minimum",
            arg=minimum,
            timeout=5_000,
        )

    def select_recipe(recipe_id: int) -> None:
        page.locator(f"#recipe-list .recipe-list-item[data-recipe-id='{recipe_id}']").click()
        page.wait_for_function(
            "id => document.getElementById('recipe-id').value === String(id)",
            arg=recipe_id,
            timeout=5_000,
        )

    def fill_recipe(name: str, style: str, ingredient: str, quantity: str) -> None:
        page.locator("#recipe-name").fill(name)
        page.locator("#recipe-style").fill(style)
        page.locator("#recipe-base-volume").fill("20")
        page.locator("#recipe-beverage-type").select_option("beer")
        page.locator("#recipe-notes").fill(f"Notes for {name}")
        row = page.locator("#recipe-ingredients .ingredient-row").first
        row.locator(".ingredient-name").fill(ingredient)
        row.locator(".ingredient-quantity").fill(quantity)
        row.locator(".ingredient-unit").fill("g")
        row.locator(".ingredient-category").fill("hops")

    page.goto(f"{live_server}/#recipes", wait_until="domcontentloaded")
    page.locator("#tab-button-recipes").click()
    page.wait_for_function(
        "() => document.body.dataset.activeTab === 'recipes' && !document.getElementById('tab-recipes').hidden",
        timeout=5_000,
    )
    page.locator("#recipe-form").wait_for()

    # The initial form has one client-generated UUID ingredient row.
    assert page.locator("#recipe-ingredients .ingredient-row").count() == 1
    fill_recipe("Browser Saison", "Saison", "Cascade", "50")
    with page.expect_response(api_response("/api/recipes", "POST")) as response_info:
        page.locator("#recipe-save").click()
    response = response_info.value
    assert response.status == 201
    first = response.json()
    first_id = int(first["id"])
    assert first["revision"] == 1
    page.wait_for_function(
        "() => document.getElementById('recipe-status').textContent.includes('Saved revision 1')",
        timeout=5_000,
    )
    wait_for_recipe_list()
    first_item = page.locator(f"#recipe-list .recipe-list-item[data-recipe-id='{first_id}']")
    assert first_item.count() == 1
    assert "Browser Saison" in first_item.inner_text()

    # Reloading must repopulate the saved recipe and its form values.
    page.reload(wait_until="domcontentloaded")
    page.locator("#tab-button-recipes").click()
    wait_for_recipe_list()
    select_recipe(first_id)
    assert page.locator("#recipe-name").input_value() == "Browser Saison"
    assert page.locator("#recipe-revision").input_value() == "1"

    # An in-page edit must append revision 2 rather than overwrite revision 1.
    page.locator("#recipe-name").fill("Browser Saison Revised")
    page.locator("#recipe-notes").fill("Revision two notes")
    with page.expect_response(api_response(f"/api/recipes/{first_id}", "PUT")) as response_info:
        page.locator("#recipe-save").click()
    response = response_info.value
    assert response.status == 200
    revised = response.json()
    assert revised["id"] == first_id
    assert revised["revision"] == 2
    page.wait_for_function(
        "() => document.getElementById('recipe-status').textContent.includes('Saved revision 2')",
        timeout=5_000,
    )
    # saveRecipe sets the status before awaiting loadRecipes/selectRecipe, which
    # populates #recipe-revision in fillRecipeForm. Wait for that stable DOM
    # signal before asserting so we do not race the form refresh.
    page.wait_for_function(
        "() => document.getElementById('recipe-revision').value === '2'",
        timeout=5_000,
    )
    assert page.locator("#recipe-revision").input_value() == "2"

    # A second recipe must remain distinct in both the list and persisted form.
    page.locator("#new-recipe").click()
    page.wait_for_function(
        "() => document.getElementById('recipe-id').value === ''",
        timeout=5_000,
    )
    fill_recipe("Browser Wit", "Witbier", "Coriander", "25")
    with page.expect_response(api_response("/api/recipes", "POST")) as response_info:
        page.locator("#recipe-save").click()
    response = response_info.value
    assert response.status == 201
    second = response.json()
    second_id = int(second["id"])
    assert second_id != first_id
    assert second["revision"] == 1
    wait_for_recipe_list(2)
    assert page.locator("#recipe-list .recipe-list-item").count() == 2

    page.reload(wait_until="domcontentloaded")
    page.locator("#tab-button-recipes").click()
    wait_for_recipe_list(2)
    select_recipe(first_id)
    assert page.locator("#recipe-name").input_value() == "Browser Saison Revised"
    assert page.locator("#recipe-revision").input_value() == "2"
    assert page.locator(".ingredient-name").first.input_value() == "Cascade"
    select_recipe(second_id)
    assert page.locator("#recipe-name").input_value() == "Browser Wit"
    assert page.locator("#recipe-style").input_value() == "Witbier"
    assert page.locator(".ingredient-name").first.input_value() == "Coriander"
    assert page.locator("#recipe-revision").input_value() == "1"

    # Reload first so the browser holds revision 2, then advance the live store
    # externally. Both the direct stale PUT and the browser retry cross Uvicorn.
    page.reload(wait_until="domcontentloaded")
    page.locator("#tab-button-recipes").click()
    wait_for_recipe_list(2)
    select_recipe(first_id)
    with httpx.Client(timeout=3.0) as client:
        current_response = client.get(f"{recipes_url}/{first_id}")
        assert current_response.status_code == 200
        current = current_response.json()
        assert current["revision"] == 2
        update_payload = {
            "name": "Browser Saison External",
            "style": current["style"],
            "description": current["description"],
            "base_volume_l": current["base_volume_l"],
            "beverage_type": current["beverage_type"],
            "initial_fermenter_volume_l": current["initial_fermenter_volume_l"],
            "target_metrics": current["target_metrics"],
            "notes": current["notes"],
            "ingredients": [
                {
                    key: ingredient[key]
                    for key in (
                        "ingredient_key", "client_key", "name", "quantity", "unit",
                        "unit_other", "category", "material_type", "purpose",
                        "addition_stage", "addition_timing", "scaling", "product",
                        "provenance", "allergen_tags", "other_allergen",
                        "sensitivity_tags", "other_sensitivity", "tannin_detail",
                        "nutrient_detail", "must_preparation", "preparation_other",
                        "schedule_allocation",
                    )
                }
                for ingredient in current["ingredients"]
            ],
            "culture_profiles": current["culture_profiles"],
            "scheduled_additions": current["scheduled_additions"],
            "process_steps": current["process_steps"],
            "expected_revision": 2,
        }
        external = client.put(f"{recipes_url}/{first_id}", json=update_payload)
        assert external.status_code == 200
        assert external.json()["revision"] == 3
        stale = client.put(f"{recipes_url}/{first_id}", json=update_payload)
        assert stale.status_code == 409

    # The browser still carries revision 2. Its stale PUT must be visible in the
    # recipe status region, and a reload must show the external revision 3.
    page.locator("#recipe-name").fill("Browser Saison Stale Attempt")
    with page.expect_response(api_response(f"/api/recipes/{first_id}", "PUT")) as response_info:
        page.locator("#recipe-save").click()
    response = response_info.value
    assert response.status == 409
    page.wait_for_function(
        "() => document.getElementById('recipe-status').textContent.includes('changed elsewhere')",
        timeout=5_000,
    )
    assert page.locator("#recipe-status").is_visible()
    assert "Reload before overwriting" in page.locator("#recipe-status").inner_text()

    page.reload(wait_until="domcontentloaded")
    page.locator("#tab-button-recipes").click()
    wait_for_recipe_list(2)
    select_recipe(first_id)
    assert page.locator("#recipe-name").input_value() == "Browser Saison External"
    assert page.locator("#recipe-revision").input_value() == "3"
    select_recipe(second_id)
    assert page.locator("#recipe-name").input_value() == "Browser Wit"
    assert page.locator("#recipe-revision").input_value() == "1"


# ----- P2/W4 recipe workflow helper fixtures ---------------------------------


def _enable_fake_assistant(app_module, monkeypatch, request, responses):
    """Configure the ZeroClawClient to appear configured and reply with a
    deterministic queue of model responses. Each call pops the next entry.
    """
    queue = list(responses)
    app_module.ASSISTANT_CLIENT.base_url = "http://127.0.0.1:65535"
    app_module.ASSISTANT_CLIENT.token_path = Path("/tmp/ispindel-fake-token")
    # The pipeline uses _client_for_job(kind, client, structured_client) which
    # prefers structured_client when configured. For non-chat kinds we route
    # through whichever is "configured" first; here we make ZeroClawClient
    # appear configured so recipe_audit/rewrite land on our fake.
    monkeypatch.setattr(app_module.ASSISTANT_CLIENT, "chat", _FailingChat(queue), raising=False)

    def restore():
        app_module.ASSISTANT_CLIENT.base_url = ""
        app_module.ASSISTANT_CLIENT.token_path = None

    request.addfinalizer(restore)
    return queue


class _FailingChat:
    """Callable that returns the next queued response; errors when exhausted."""

    def __init__(self, queue):
        self._queue = list(queue)

    def __call__(self, message, conversation_id):
        if not self._queue:
            raise AssertionError("fake assistant queue exhausted")
        return self._queue.pop(0)


def _audit_result(findings):
    return {
        "message": json.dumps({
            "envelope_version": 1,
            "kind": "recipe_audit",
            "summary": "audit fixture",
            "findings": [finding.model_dump(mode="json") for finding in findings],
        }),
        "model": "fake",
        "tool_calls": [],
    }


def _rewrite_result(parent_job_id, approved_finding_ids, proposal, change_basis=None):
    payload = {
        "envelope_version": 1,
        "kind": "recipe_rewrite",
        "summary": "rewrite fixture",
        "parent_job_id": parent_job_id,
        "approved_finding_ids": list(approved_finding_ids),
        "proposal": proposal.model_dump(mode="json"),
        "change_basis": [basis.model_dump(mode="json") for basis in (change_basis or [])],
    }
    return {
        "message": json.dumps(payload),
        "model": "fake",
        "tool_calls": [],
    }


def _base_recipe_payload(name):
    # The recipe API normalises empty object fields to {} on read so the
    # JS-side draft (recipeDraftForAssistant) carries product={} etc. The
    # proposal validator compares every sensitive leaf in the draft against
    # the proposal; matching the empty-collection representation keeps the
    # diff focused on /style and avoids spurious missing_suggestion_basis
    # errors for unchanged optional leaves. unit_other (recipe-save API)
    # is the canonical storage field here; recipeDraftForAssistant renames
    # it to custom_unit only when handing the same draft to the assistant.
    return {
        "name": name,
        "style": "Saison",
        "base_volume_l": 20.0,
        "beverage_type": "beer",
        "notes": "fixture",
        "ingredients": [
            {
                "ingredient_key": "123e4567-e89b-42d3-a456-426614174001",
                "name": "Cascade",
                "quantity": 50.0,
                "unit": "g",
                "category": "hops",
                "material_type": "other",
                "scaling": {"mode": "fixed"},
                "product": {},
                "tannin_detail": None,
                "nutrient_detail": None,
                "allergen_tags": [],
                "sensitivity_tags": [],
            }
        ],
    }


# ----- P2/W4 new nodes ------------------------------------------------------


@pytest.mark.requires_browser
def test_recipe_workflow_chain_full_journey(browser_page, live_server, monkeypatch, request) -> None:
    """End-to-end P2/W4 recipe workflow: create+save via the page controls,
    set a manual field that survives the rewrite, audit, approve a subset,
    rewrite, apply only the intended diff, save, and reload. The reload
    must surface the persisted review panel via GET on the rewrite job id
    without re-submitting any audit/rewrite. The manual field that the
    operator typed before audit must survive apply because the proposal
    preserves it and only the style diff is ticked.
    """
    import app.main as app_module  # local import: conftest reloads app_module

    manual_name = "Manual override"

    page = browser_page
    page.set_default_timeout(5000)
    page.goto(f"{live_server}/#recipes", wait_until="domcontentloaded")
    page.locator("#tab-button-recipes").click()
    page.wait_for_function(
        "() => document.body.dataset.activeTab === 'recipes'",
        timeout=5000,
    )
    page.locator("#recipe-form").wait_for()

    # 0a. Create + save the initial recipe through the page controls
    # (requirement #6: not a direct POST). The form starts with one
    # client-generated UUID ingredient row, so we only fill the form
    # fields and click Save.
    page.locator("#recipe-name").fill("Workflow fixture")
    page.locator("#recipe-style").fill("Saison")
    page.locator("#recipe-base-volume").fill("20")
    page.locator("#recipe-beverage-type").select_option("beer")
    page.locator("#recipe-notes").fill("fixture")
    row = page.locator("#recipe-ingredients .ingredient-row").first
    row.locator(".ingredient-name").fill("Cascade")
    row.locator(".ingredient-quantity").fill("50")
    row.locator(".ingredient-unit").fill("g")
    row.locator(".ingredient-category").fill("hops")

    with httpx.Client(base_url=live_server, timeout=5.0) as client:
        recipes_before = len(client.get("/api/recipes").json()["recipes"])
        with page.expect_response(
            lambda response: urlsplit(response.url).path == "/api/recipes"
            and response.request.method == "POST"
        ) as response_info:
            page.locator("#recipe-save").click()
        seed = response_info.value.json()
        assert seed["revision"] == 1
        recipe_id = int(seed["id"])
        recipes_after = len(client.get("/api/recipes").json()["recipes"])
        assert recipes_after == recipes_before + 1, "save must create one row"

    # The save handler re-selects the recipe and the page restores from the
    # snapshot (which is empty for a fresh recipe). Wait for the recipe id
    # to land in the form.
    page.wait_for_function(
        "id => document.getElementById('recipe-id').value === String(id)",
        arg=recipe_id,
        timeout=5000,
    )

    # 0b. Operator types a MANUAL override into the name field BEFORE the
    # audit. The deterministic proposal below must preserve this exact
    # value while changing the approved /style field. We will only tick
    # /style on apply, so the manual value must survive end-to-end.
    page.locator("#recipe-name").fill(manual_name)

    # The draft's ingredient carries a UUID generated by the page at form
    # load. Capture it (and its scaling mode) BEFORE the audit so the
    # proposal can match the saved draft exactly and the diff computation
    # produces a single /style entry.
    draft_ingredient = page.evaluate(
        """() => {
          const row = document.querySelector('#recipe-ingredients .ingredient-row');
          return {
            ingredient_key: row?.dataset?.ingredientKey || '',
            scaling_mode: row?.querySelector('.ingredient-mode')?.value || '',
          };
        }"""
    )
    assert draft_ingredient["ingredient_key"], "draft ingredient must have an UUID key"

    finding = Finding(
        finding_id="F-style",
        origin="model",
        field_path="/style",
        severity="advisory",
        category="completeness",
        domain="general",
        current_value="Saison",
        suggested_value="Farmhouse Saison",
        rationale="narrow the style label",
        evidence=[],
    )
    proposal = RecipeProposal.model_validate({
        "name": manual_name,
        "style": "Farmhouse Saison",
        "base_volume_l": 20.0,
        "beverage_type": "beer",
        "target_metrics": {"sweetness": "unknown"},
        "notes": "fixture",
        "ingredients": [{
            "ingredient_key": draft_ingredient["ingredient_key"],
            "name": "Cascade",
            "quantity": 50.0,
            "unit": "g",
            "category": "hops",
            "material_type": "other",
            "scaling": {"mode": draft_ingredient["scaling_mode"] or "linear"},
        }],
    })
    _enable_fake_assistant(
        app_module,
        monkeypatch,
        request,
        [
            _audit_result([finding]),
            _rewrite_result(
                parent_job_id="placeholder",
                approved_finding_ids=["F-style"],
                proposal=proposal,
                change_basis=[SuggestionBasisRecord(
                    field_path="/style",
                    basis="inference",
                    evidence=[],
                    uncertainty="model inference only",
                )],
            ),
        ],
    )


    # 1. Audit the recipe through the page.
    audit_button = page.locator(
        '.assistant-panel[data-assistant-kind="recipe"] '
        '[data-assistant-action="recipe_audit"]'
    ).first
    audit_button.click()
    page.wait_for_function(
        "() => document.querySelector(\"input[data-finding-id='F-style']\") !== null",
        timeout=15000,
    )

    # 2. Approve the subset (just our one finding).
    page.locator("input[data-finding-id='F-style']").check()
    approve_button = page.locator(
        '.assistant-panel[data-assistant-kind="recipe"] '
        "button:has-text('Approve selected findings')"
    )
    approve_button.click()
    page.wait_for_function(
        "() => document.querySelector(\"button[data-assistant-rewrite='true']\")?.disabled === false",
        timeout=5000,
    )

    # 3. Trigger the rewrite.
    rewrite_button = page.locator(
        '.assistant-panel[data-assistant-kind="recipe"] '
        "button[data-assistant-rewrite='true']"
    )
    rewrite_button.click()
    page.wait_for_function(
        "() => document.querySelector(\".assistant-diff-entry input[data-diff-path]\") !== null",
        timeout=15000,
    )

    # The manual name MUST NOT appear in the diff (the deterministic
    # proposal preserves it). /style MUST appear because that's the only
    # approved change. Other diff entries (e.g. /target_metrics when the
    # form has no target metrics yet) are tolerated as long as they don't
    # touch the manual field.
    diff_paths = page.evaluate(
        "() => Array.from(document.querySelectorAll('.assistant-diff-entry input[data-diff-path]')).map(input => input.dataset.diffPath)"
    )
    assert "/style" in diff_paths, (
        f"/style must be in the diff so the operator can apply the only approved change; got {diff_paths!r}"
    )
    assert "/name" not in diff_paths, (
        f"/name must NOT be in the diff when the proposal preserves the manual override; got {diff_paths!r}"
    )

    # 4. Atomic opt-in apply: tick the style diff only, click Apply.
    page.locator(".assistant-diff-entry input[data-diff-path]").first.check()
    apply_button = page.locator(
        '.assistant-panel[data-assistant-kind="recipe"] '
        "button[data-assistant-action='apply']"
    )
    apply_button.click()
    page.wait_for_function(
        "() => document.getElementById('recipe-style').value === 'Farmhouse Saison'",
        timeout=5000,
    )
    # Manual name must still be intact — we never ticked /name.
    assert page.locator("#recipe-name").input_value() == manual_name

    # 5. Explicit save through the page.
    with httpx.Client(base_url=live_server, timeout=5.0) as client:
        recipes_before_save = len(client.get("/api/recipes").json()["recipes"])
        with page.expect_response(
            lambda response: urlsplit(response.url).path
            == f"/api/recipes/{recipe_id}"
            and response.request.method == "PUT"
        ) as response_info:
            page.locator("#recipe-save").click()
        body = response_info.value.json()
        assert body["revision"] == 2
        recipes_after_save = len(client.get("/api/recipes").json()["recipes"])
        assert recipes_after_save == recipes_before_save

    # 6. Reload. The reload path must refetch the persisted rewrite job
    # via GET (only network mutation), render the diff panel without a
    # fresh POST, and the saved form values must still match.
    submitted_jobs: list[str] = []
    fetched_jobs: list[str] = []
    page.on(
        "request",
        lambda req: submitted_jobs.append(req.url)
        if (req.method == "POST" and req.url.endswith("/api/assistant/jobs"))
        else None,
    )
    page.on(
        "request",
        lambda req: fetched_jobs.append(urlsplit(req.url).path)
        if (req.method == "GET"
            and urlsplit(req.url).path.startswith("/api/assistant/jobs/")
            and urlsplit(req.url).path != "/api/assistant/jobs")
        else None,
    )

    page.reload(wait_until="domcontentloaded")
    page.locator("#tab-button-recipes").click()
    page.wait_for_function(
        "() => document.body.dataset.activeTab === 'recipes'",
        timeout=5000,
    )
    page.wait_for_function(
        f"() => document.querySelectorAll('#recipe-list .recipe-list-item').length >= 1",
        timeout=5000,
    )
    page.locator(f"#recipe-list .recipe-list-item[data-recipe-id='{recipe_id}']").click()
    page.wait_for_function(
        "id => document.getElementById('recipe-id').value === String(id)",
        arg=recipe_id,
        timeout=5000,
    )
    # The review panel must be restored: the /style diff entry is visible,
    # AND the manual name is in the form.
    page.wait_for_function(
        "() => document.querySelector(\"input[data-diff-path='/style']\") !== null",
        timeout=10000,
    )
    page.wait_for_function("() => true", timeout=200)
    assert submitted_jobs == [], (
        "reload must not POST a new /api/assistant/jobs; "
        f"observed {submitted_jobs!r}"
    )
    assert fetched_jobs, "reload must GET the persisted /api/assistant/jobs/{id}"
    assert page.locator("#recipe-name").input_value() == manual_name
    assert page.locator("#recipe-revision").input_value() == "2"
    assert page.locator("#recipe-style").input_value() == "Farmhouse Saison"
    # Persisted diff content survives reload. /style must be present;
    # /name (the manual override) must still be absent.
    reloaded_diff_paths = page.evaluate(
        "() => Array.from(document.querySelectorAll('.assistant-diff-entry input[data-diff-path]')).map(input => input.dataset.diffPath)"
    )
    assert "/style" in reloaded_diff_paths, (
        f"reloaded diff must include the persisted /style entry; got {reloaded_diff_paths!r}"
    )
    assert "/name" not in reloaded_diff_paths, (
        f"reloaded diff must not touch the manual name override; got {reloaded_diff_paths!r}"
    )


@pytest.mark.requires_browser
def test_workflow_reload_restores_state_without_rerun(browser_page, live_server, monkeypatch, request) -> None:
    """Reload after a succeeded audit must restore the review surface from
    sessionStorage WITHOUT issuing a new POST to /api/assistant/jobs. The
    page's reload path refetches the persisted job via GET against
    /api/assistant/jobs/{id} (the only network mutation allowed). The
    sessionStorage snapshot must contain only bounded identifiers and the
    audit scope — never the result, evidence, model output, prompt, draft,
    pre-apply snapshot, or binding token.
    """
    import app.main as app_module  # local import: conftest reloads app_module

    base = _base_recipe_payload("Reload fixture")
    with httpx.Client(base_url=live_server, timeout=5.0) as client:
        seed = client.post("/api/recipes", json=base).json()
        recipe_id = int(seed["id"])
        finding = Finding(
            finding_id="F-style",
            origin="model",
            field_path="/style",
            severity="info",
            category="completeness",
            domain="general",
            current_value="Saison",
            suggested_value="Farmhouse Saison",
            rationale="narrow style label",
            evidence=[],
        )
        _enable_fake_assistant(
            app_module,
            monkeypatch,
            request,
            [_audit_result([finding])],
        )

    page = browser_page
    page.set_default_timeout(5000)
    page.goto(f"{live_server}/#recipes", wait_until="domcontentloaded")
    page.locator("#tab-button-recipes").click()
    page.wait_for_function(
        "() => document.body.dataset.activeTab === 'recipes'",
        timeout=5000,
    )
    page.locator(f"#recipe-list .recipe-list-item[data-recipe-id='{recipe_id}']").click()
    page.wait_for_function(
        "id => document.getElementById('recipe-id').value === String(id)",
        arg=recipe_id,
        timeout=5000,
    )
    submitted_jobs: list[str] = []
    page.on(
        "request",
        lambda req: submitted_jobs.append(req.url)
        if (req.method == "POST" and req.url.endswith("/api/assistant/jobs"))
        else None,
    )
    page.locator(
        '.assistant-panel[data-assistant-kind="recipe"] '
        '[data-assistant-action="recipe_audit"]'
    ).first.click()
    page.wait_for_function(
        "() => document.querySelector(\"input[data-finding-id='F-style']\") !== null",
        timeout=10000,
    )
    assert submitted_jobs, "audit click did not POST a job"
    submitted_jobs.clear()

    workflow_raw = page.evaluate(
        "() => window.sessionStorage.getItem('ispindel.recipe.workflow')"
    )
    assert workflow_raw is not None, "expected sessionStorage write on audit success"
    parsed = json.loads(workflow_raw)
    snapshot = parsed[str(recipe_id)]
    # Bounded identifiers MUST be present.
    assert snapshot.get("recipeId") == str(recipe_id)
    assert snapshot.get("recipeRevision") == "1"
    assert snapshot.get("assistantJobId"), "assistant job id must be persisted"
    # Audit-lineage keys are populated only when the operator approves
    # findings; they may legitimately be empty after a bare audit.
    lineage_keys = (
        "assistantAuditJobId", "assistantApprovedFindingIds",
        "assistantAuditScopeRecipeId", "assistantAuditScopeRecipeRevision",
    )
    for key in lineage_keys:
        assert key in snapshot, f"snapshot must carry {key!r} key (may be empty)"
    # Result / evidence / model output / prompt / draft / pre-apply / token MUST NOT be persisted.
    forbidden_keys = {
        "jobResult", "jobKind", "jobStatus",
        "result", "applicable_diff", "proposal", "findings",
        "evidence", "context", "model", "tool_calls",
        "prompt", "message", "client_request_id",
        "draft", "preApplyDraft", "assistantPreApplyDraft",
        "assistantBindingToken",
    }
    leaked = sorted(set(snapshot.keys()) & forbidden_keys)
    assert not leaked, (
        f"workflow snapshot must not persist {leaked!r}; observed {sorted(snapshot.keys())}"
    )
    raw_blob = json.dumps(snapshot)
    for forbidden_token in (
        "applicable_diff", "proposal", "findings", "evidence",
        "draft", "preApplyDraft", "assistantBindingToken", "tool_calls",
    ):
        assert forbidden_token not in raw_blob, (
            f"workflow snapshot must not mention {forbidden_token!r}"
        )

    # Reload. The reload path must refetch the persisted job via GET and
    # never POST a new audit. The GET only fires for the persisted job id.
    submitted_jobs.clear()
    fetched_jobs: list[str] = []
    page.on(
        "request",
        lambda req: submitted_jobs.append(req.url)
        if (req.method == "POST" and req.url.endswith("/api/assistant/jobs"))
        else None,
    )
    page.on(
        "request",
        lambda req: fetched_jobs.append(urlsplit(req.url).path)
        if (req.method == "GET"
            and urlsplit(req.url).path.startswith("/api/assistant/jobs/")
            and urlsplit(req.url).path != "/api/assistant/jobs")
        else None,
    )

    page.reload(wait_until="domcontentloaded")
    page.locator("#tab-button-recipes").click()
    page.wait_for_function(
        "() => document.body.dataset.activeTab === 'recipes'",
        timeout=5000,
    )
    page.locator(f"#recipe-list .recipe-list-item[data-recipe-id='{recipe_id}']").click()
    page.wait_for_function(
        "id => document.getElementById('recipe-id').value === String(id)",
        arg=recipe_id,
        timeout=5000,
    )
    # Restore must surface a review panel without re-submitting.
    page.wait_for_function(
        "() => document.querySelector(\"input[data-finding-id='F-style']\") !== null",
        timeout=10000,
    )
    page.wait_for_function("() => true", timeout=200)
    assert submitted_jobs == [], (
        "reload must not re-submit /api/assistant/jobs; "
        f"observed {submitted_jobs!r}"
    )
    assert fetched_jobs, "reload must GET the persisted /api/assistant/jobs/{id}"
    expected_get = f"/api/assistant/jobs/{snapshot['assistantJobId']}"
    assert expected_get in fetched_jobs, (
        f"expected GET {expected_get!r}; observed {fetched_jobs!r}"
    )


@pytest.mark.requires_browser
def test_stale_form_or_revision_rejected_with_rebase_offer(browser_page, live_server) -> None:
    """A stale expected_revision must surface a rebase/reload offer instead
    of silently overwriting the saved recipe.
    """
    base = _base_recipe_payload("Stale revision fixture")
    with httpx.Client(base_url=live_server, timeout=5.0) as client:
        seed = client.post("/api/recipes", json=base).json()
        recipe_id = int(seed["id"])

    page = browser_page
    page.set_default_timeout(5000)
    page.goto(f"{live_server}/#recipes", wait_until="domcontentloaded")
    page.locator("#tab-button-recipes").click()
    page.wait_for_function(
        "() => document.body.dataset.activeTab === 'recipes'",
        timeout=5000,
    )
    page.locator(f"#recipe-list .recipe-list-item[data-recipe-id='{recipe_id}']").click()
    page.wait_for_function(
        "id => document.getElementById('recipe-id').value === String(id)",
        arg=recipe_id,
        timeout=5000,
    )
    # Confirm the browser is sitting on revision 1 before we advance the
    # saved revision externally.
    page.wait_for_function(
        "() => document.getElementById('recipe-revision').value === '1'",
        timeout=5000,
    )
    # Advance the saved revision externally so the browser's revision-1 form
    # becomes stale.
    with httpx.Client(base_url=live_server, timeout=5.0) as client:
        advance = json.loads(json.dumps(base))
        advance["expected_revision"] = 1
        advance["name"] = "External update"
        external = client.put(f"/api/recipes/{recipe_id}", json=advance)
        assert external.status_code == 200
        assert external.json()["revision"] == 2
    # Browser still carries the *stale* revision-1 form.
    page.locator("#recipe-name").fill("Stale browser save")
    with page.expect_response(
        lambda response: urlsplit(response.url).path
        == f"/api/recipes/{recipe_id}"
        and response.request.method == "PUT"
    ) as response_info:
        page.locator("#recipe-save").click()
    response = response_info.value
    assert response.status == 409
    page.wait_for_function(
        "() => /Reload before overwriting|rebase the audit/.test("
        "document.getElementById('recipe-status').textContent)",
        timeout=5000,
    )
    status_text = page.locator("#recipe-status").inner_text()
    assert "Reload" in status_text
    assert "rebase" in status_text
    assert "is-error" in (page.locator("#recipe-status").get_attribute("class") or "")


@pytest.mark.requires_browser
def test_double_click_save_produces_one_persisted_row(browser_page, live_server) -> None:
    """Triple-clicking Save must produce exactly one persisted recipe row,
    not three. The guard lives in saveRecipe as a dataset flag flipped
    synchronously around the POST.
    """
    base = _base_recipe_payload("Double click fixture")
    with httpx.Client(base_url=live_server, timeout=5.0) as client:
        seed = client.post("/api/recipes", json=base).json()
        recipe_id = int(seed["id"])

    page = browser_page
    page.set_default_timeout(5000)
    page.goto(f"{live_server}/#recipes", wait_until="domcontentloaded")
    page.locator("#tab-button-recipes").click()
    page.wait_for_function(
        "() => document.body.dataset.activeTab === 'recipes'",
        timeout=5000,
    )
    page.locator(f"#recipe-list .recipe-list-item[data-recipe-id='{recipe_id}']").click()
    page.wait_for_function(
        "id => document.getElementById('recipe-id').value === String(id)",
        arg=recipe_id,
        timeout=5000,
    )
    save_button = page.locator("#recipe-save")
    save_button.wait_for()
    with httpx.Client(base_url=live_server, timeout=5.0) as client:
        before_recipes = client.get("/api/recipes").json()["recipes"]
        rows_before = len(before_recipes)
        before_revision = next(
            (row["revision"] for row in before_recipes if row["id"] == recipe_id),
            None,
        )
        assert before_revision == 1
        # Fire three clicks in the SAME JS tick so Playwright's actionability
        # wait does not serialise them. The page's saveRecipe busy flag must
        # swallow the duplicates and yield exactly one persisted revision.
        submitted: list[str] = []
        page.on(
            "request",
            lambda req: submitted.append(req.url)
            if (req.method == "PUT" and req.url.endswith(f"/api/recipes/{recipe_id}"))
            else None,
        )
        page.evaluate(
            """() => {
              const btn = document.getElementById('recipe-save');
              btn.click();
              btn.click();
              btn.click();
            }"""
        )
        # Allow the in-flight POST + loadRecipes to settle.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if submitted and len(client.get("/api/recipes").json()["recipes"]) == rows_before:
                # Give any trailing duplicate a moment to land so the
                # assertion below sees the terminal revision.
                time.sleep(0.5)
                break
            time.sleep(0.05)
        assert len(submitted) == 1, (
            f"double-click guard must collapse rapid clicks into one POST; "
            f"observed {len(submitted)}: {submitted!r}"
        )
        rows_after = len(client.get("/api/recipes").json()["recipes"])
        assert rows_after == rows_before, (
            f"double-click guard must not create extra rows; got {rows_after - rows_before} new"
        )
        final = client.get(f"/api/recipes/{recipe_id}").json()
        assert final["revision"] == 2, (
            f"double-click guard must not advance revision twice; got {final['revision']}"
        )


@pytest.mark.requires_browser
def test_bad_pointer_in_proposed_change_is_rejected_before_mutation(
    browser_page, live_server, monkeypatch, request
) -> None:
    """A checked diff entry with a pointer that cannot be applied must abort
    the entire apply BEFORE any field mutates. Verified by capturing the
    form's pre-apply state and re-reading it after the failed apply.

    The test exercises the REAL rendered Apply button (no test hooks,
    no global exports). We intercept the page's poll GET against
    /api/assistant/jobs/{id} AFTER the deterministic rewrite succeeds,
    replacing result.applicable_diff with one valid and one invalid pointer.
    Both rendered checkboxes are ticked and the real Apply button is clicked.
    """
    import app.main as app_module  # local import: conftest reloads app_module

    base = _base_recipe_payload("Bad pointer fixture")
    with httpx.Client(base_url=live_server, timeout=5.0) as client:
        seed = client.post("/api/recipes", json=base).json()
        recipe_id = int(seed["id"])

    finding = Finding(
        finding_id="F-style",
        origin="model",
        field_path="/style",
        severity="advisory",
        category="completeness",
        domain="general",
        current_value="Saison",
        suggested_value="Farmhouse Saison",
        rationale="narrow the style label",
        evidence=[],
    )
    proposal = RecipeProposal.model_validate({
        "name": "Bad pointer fixture",
        "style": "Farmhouse Saison",
        "base_volume_l": 20.0,
        "beverage_type": "beer",
        "notes": "fixture",
        "ingredients": [{
            "ingredient_key": "123e4567-e89b-42d3-a456-426614174001",
            "name": "Cascade",
            "quantity": 50.0,
            "unit": "g",
            "category": "hops",
            "scaling": {"mode": "fixed"},
        }],
    })
    _enable_fake_assistant(
        app_module,
        monkeypatch,
        request,
        [
            _audit_result([finding]),
            _rewrite_result(
                parent_job_id="placeholder",
                approved_finding_ids=["F-style"],
                proposal=proposal,
                change_basis=[SuggestionBasisRecord(
                    field_path="/style",
                    basis="inference",
                    evidence=[],
                    uncertainty="model inference only",
                )],
            ),
        ],
    )

    page = browser_page
    page.set_default_timeout(5000)
    page.goto(f"{live_server}/#recipes", wait_until="domcontentloaded")
    page.locator("#tab-button-recipes").click()
    page.wait_for_function(
        "() => document.body.dataset.activeTab === 'recipes'",
        timeout=5000,
    )
    page.locator(f"#recipe-list .recipe-list-item[data-recipe-id='{recipe_id}']").click()
    page.wait_for_function(
        "id => document.getElementById('recipe-id').value === String(id)",
        arg=recipe_id,
        timeout=5000,
    )

    # Install the route BEFORE clicking rewrite: every page GET against
    # /api/assistant/jobs/{id} flows through here. We forward the upstream
    # response, and only when it's a succeeded recipe_rewrite do we replace
    # result.applicable_diff with one valid + one invalid pointer. The
    # audit poll response has no applicable_diff and passes through
    # untouched.
    valid_pointer = {"path": "/style", "before": "Saison", "after": "Farmhouse Saison"}
    invalid_pointer = {"path": "/nonexistent/leaf/value", "before": None, "after": "Bad"}

    def _intercept_get(route, request):
        response = route.fetch()
        try:
            payload = response.json()
        except ValueError:
            return route.fulfill(response=response)
        if (
            isinstance(payload, dict)
            and payload.get("kind") == "recipe_rewrite"
            and payload.get("status") == "succeeded"
            and isinstance(payload.get("result"), dict)
            and "applicable_diff" in payload["result"]
        ):
            new_result = dict(payload["result"])
            new_result["applicable_diff"] = [valid_pointer, invalid_pointer]
            new_payload = dict(payload)
            new_payload["result"] = new_result
            return route.fulfill(response=response, json=new_payload)
        return route.fulfill(response=response)

    page.route("**/api/assistant/jobs/*", _intercept_get)

    # Audit -> approve -> rewrite through the page.
    page.locator(
        '.assistant-panel[data-assistant-kind="recipe"] '
        '[data-assistant-action="recipe_audit"]'
    ).first.click()
    page.wait_for_function(
        "() => document.querySelector(\"input[data-finding-id='F-style']\") !== null",
        timeout=15000,
    )
    page.locator("input[data-finding-id='F-style']").check()
    page.locator(
        '.assistant-panel[data-assistant-kind="recipe"] '
        "button:has-text('Approve selected findings')"
    ).click()
    page.wait_for_function(
        "() => document.querySelector(\"button[data-assistant-rewrite='true']\")?.disabled === false",
        timeout=5000,
    )
    page.locator(
        '.assistant-panel[data-assistant-kind="recipe"] '
        "button[data-assistant-rewrite='true']"
    ).click()

    # The page must render TWO real checkboxes (one for the valid /style
    # pointer and one for the invalid pointer) — proof that our route
    # interception fired AND that the diff renderer is what the test
    # drives, not a stub.
    page.wait_for_function(
        "() => document.querySelectorAll('.assistant-diff-entry input[data-diff-path]').length === 2",
        timeout=15000,
    )
    rendered_paths = page.evaluate(
        "() => Array.from(document.querySelectorAll('.assistant-diff-entry input[data-diff-path]')).map(input => input.dataset.diffPath)"
    )
    assert rendered_paths == ["/style", "/nonexistent/leaf/value"], (
        f"unexpected rendered diff paths: {rendered_paths!r}"
    )

    # Capture the form snapshot BEFORE the apply attempt. Any successful
    # pointer application would mutate at least one of these fields.
    snapshot = page.evaluate(
        """() => ({
          name: document.getElementById('recipe-name').value,
          style: document.getElementById('recipe-style').value,
          notes: document.getElementById('recipe-notes').value,
        })"""
    )

    # Tick BOTH real checkboxes and click the real Apply button. The atomic
    # apply guard must refuse because the second pointer has no parent in
    # the pre-apply draft (applyDiffValue returns false), so no field may
    # mutate.
    page.locator(".assistant-diff-entry input[data-diff-path='/style']").check()
    page.locator(
        ".assistant-diff-entry input[data-diff-path='/nonexistent/leaf/value']"
    ).check()
    page.locator(
        '.assistant-panel[data-assistant-kind="recipe"] '
        "button[data-assistant-action='apply']"
    ).click()
    page.wait_for_function(
        "() => /No form changes were made|could not be applied completely|"
        "Select at least one diff entry|No matching diff entries|"
        "One or more checked diff entries/"
        ".test(document.getElementById('recipe-status').textContent)",
        timeout=5000,
    )
    after = page.evaluate(
        """() => ({
          name: document.getElementById('recipe-name').value,
          style: document.getElementById('recipe-style').value,
          notes: document.getElementById('recipe-notes').value,
        })"""
    )
    assert after == snapshot, (
        f"atomic apply must refuse the whole batch when one pointer is invalid; "
        f"snapshot {snapshot!r} -> after {after!r}"
    )
    status_text = page.locator("#recipe-status").inner_text()
    assert "No form changes" in status_text or "could not be applied completely" in status_text, (
        f"expected refusal message; got {status_text!r}"
    )


@pytest.mark.requires_browser
def test_recipe_44px_controls_remain_usable(browser_page, live_server) -> None:
    """At 320x568 the recipe tab must expose controls with computed
    width/height >= 44px so taps remain reachable. Desktop behaviour must
    be preserved on wider viewports.
    """
    base = _base_recipe_payload("44px fixture")
    with httpx.Client(base_url=live_server, timeout=5.0) as client:
        client.post("/api/recipes", json=base)

    page = browser_page
    page.set_viewport_size({"width": 320, "height": 568})
    page.set_default_timeout(5000)
    page.goto(f"{live_server}/#recipes", wait_until="domcontentloaded")
    page.locator("#tab-button-recipes").click()
    page.wait_for_function(
        "() => document.body.dataset.activeTab === 'recipes'",
        timeout=5000,
    )
    page.locator("#recipe-name").wait_for()

    select_controls = ["#recipe-save", "#new-recipe", "#add-ingredient"]
    selectors = select_controls + ["recipe-audit-action"]
    sizes = page.evaluate(
        """(selectors) => {
          const out = {};
          for (const sel of selectors) {
            if (sel === 'recipe-audit-action') {
              const btns = document.querySelectorAll(
                ".assistant-panel[data-assistant-kind='recipe'] button.assistant-action"
              );
              out[sel] = Array.from(btns).map(b => {
                const rect = b.getBoundingClientRect();
                return { label: b.textContent.trim(), width: rect.width, height: rect.height };
              });
            } else {
              const el = document.querySelector(sel);
              if (!el) { out[sel] = null; continue; }
              const rect = el.getBoundingClientRect();
              out[sel] = { width: rect.width, height: rect.height };
            }
          }
          return out;
        }""",
        selectors,
    )
    for sel in select_controls:
        size = sizes[sel]
        assert size is not None, f"missing control {sel}"
        assert size["height"] >= 44, f"{sel} height {size['height']} < 44"
        assert size["width"] >= 44, f"{sel} width {size['width']} < 44"
    for entry in sizes["recipe-audit-action"]:
        assert entry["height"] >= 44, f"audit action {entry!r} too small"

    # Desktop behaviour must be preserved on a wider viewport.
    page.set_viewport_size({"width": 1024, "height": 768})
    page.wait_for_function(
        "() => document.body.dataset.activeTab === 'recipes'",
        timeout=5000,
    )
    page.locator("#recipe-name").wait_for()
    desktop = page.evaluate(
        """() => {
          const el = document.querySelector('#recipe-save');
          const rect = el.getBoundingClientRect();
          return { width: rect.width, height: rect.height };
        }"""
    )
    # Desktop buttons remain usable but the global 44px floor only applies
    # under the @media (max-width:700px) block. The desktop height must not
    # collapse below the pre-existing baseline.
    assert desktop["height"] >= 24, "desktop recipe save height regressed"
