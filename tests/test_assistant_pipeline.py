from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app.assistant_pipeline import (
    AssistantJobRequest,
    AssistantJobStore,
    AssistantPipeline,
    AssistantScope,
    BrewEventDraftResultV1,
    ContractError,
    RecipeAuditResultV1,
    RecipeAutofillResultV1,
    RecipeRewriteResultV1,
    ManualTrigger,
    RecipeFormDraft,
    RecipeIngredientDraft,
    RecipeTargetMetricsDraft,
    ScheduledAdditionDraft,
    TanninDetailDraft,
    PipelineOutcome,
    canonical_hash,
    compute_recipe_diff,
    parse_model_json,
    recipe_contract_errors,
    validate_sensitive_change_basis,
)


def _client_id(seed: str = "1") -> str:
    return f"123e4567-e89b-42d3-a456-4266141740{seed}"


def _recipe() -> RecipeFormDraft:
    ingredient = RecipeIngredientDraft(
        ingredient_key="123e4567-e89b-42d3-a456-426614174010",
        name="honey",
        quantity=1.0,
        unit="kg",
        material_type="fermentable",
    )
    addition = ScheduledAdditionDraft(
        addition_key="123e4567-e89b-42d3-a456-426614174011",
        series_key="123e4567-e89b-42d3-a456-426614174012",
        ingredient_key=ingredient.ingredient_key,
        quantity=100.0,
        unit="g",
        series_kind="sugar_step",
        trigger=ManualTrigger(),
    )
    return RecipeFormDraft(
        name="Test mead",
        beverage_type="mead",
        base_volume_l=10.0,
        target_metrics=RecipeTargetMetricsDraft(abv_percent=8.0),
        ingredients=[ingredient],
        scheduled_additions=[addition],
    )


def test_recipe_keys_and_sugar_schedule_are_stable() -> None:
    recipe = _recipe()
    assert recipe.scheduled_additions[0].addition_key == "123e4567-e89b-42d3-a456-426614174011"
    assert recipe_contract_errors(recipe) == []
    assert canonical_hash(recipe) == canonical_hash(recipe.model_dump(mode="json"))


def test_recipe_diff_uses_logical_keys_not_sqlite_rowids() -> None:
    before = _recipe().model_dump(mode="json")
    after = _recipe().model_copy(deep=True)
    after.ingredients[0].quantity = 2.0
    diff = compute_recipe_diff(before, after.model_dump(mode="json"))
    assert any(item["path"].endswith("/quantity") for item in diff)
    assert all("rowid" not in item["path"] for item in diff)


def test_sensitive_basis_rejects_new_ingredient_without_evidence() -> None:
    recipe = _recipe()
    proposal = recipe.model_dump(mode="json", exclude_none=False)
    proposal.pop("target_volume_l", None)
    proposal["ingredients"].append(
        {
            "ingredient_key": "123e4567-e89b-42d3-a456-426614174013",
            "name": "caffeine concentrate",
            "quantity": 100.0,
            "unit": "g",
            "material_type": "other",
        }
    )
    result = RecipeAutofillResultV1.model_validate({
        "proposal": proposal,
        "change_basis": [],
    })
    with pytest.raises(ContractError) as exc_info:
        validate_sensitive_change_basis(result, recipe)
    assert any(
        error.startswith("/ingredients/123e4567-e89b-42d3-a456-426614174013/")
        and error.endswith(":missing_suggestion_basis")
        for error in exc_info.value.errors
    )


def test_sensitive_basis_does_not_allow_parent_row_to_authorize_new_ingredient() -> None:
    recipe = _recipe()
    ingredient_key = "123e4567-e89b-42d3-a456-426614174013"
    proposal = recipe.model_dump(mode="json", exclude_none=False)
    proposal.pop("target_volume_l", None)
    proposal["ingredients"].append(
        {
            "ingredient_key": ingredient_key,
            "name": "caffeine concentrate",
            "quantity": 100.0,
            "unit": "g",
            "material_type": "other",
        }
    )
    result = RecipeAutofillResultV1.model_validate({
        "proposal": proposal,
        "change_basis": [{
            "field_path": f"/ingredients/{ingredient_key}",
            "basis": "research",
            "evidence": ["untrusted source text"],
        }],
    })
    with pytest.raises(ContractError) as exc_info:
        validate_sensitive_change_basis(result, recipe)
    assert any(error.endswith(":missing_suggestion_basis") for error in exc_info.value.errors)


def test_sensitive_basis_rejects_forged_user_value_for_changed_quantity() -> None:
    recipe = _recipe()
    ingredient_key = recipe.ingredients[0].ingredient_key
    proposal = recipe.model_dump(mode="json", exclude_none=False)
    proposal.pop("target_volume_l", None)
    proposal["ingredients"][0]["quantity"] = 100.0
    result = RecipeAutofillResultV1.model_validate({
        "proposal": proposal,
        "change_basis": [{
            "field_path": f"/ingredients/{ingredient_key}/quantity",
            "basis": "user_value",
        }],
    })
    with pytest.raises(ContractError) as exc_info:
        validate_sensitive_change_basis(result, recipe)
    assert f"/ingredients/{ingredient_key}/quantity:user_value_provenance_unavailable" in exc_info.value.errors


def test_sensitive_basis_passes_new_ingredient_with_only_populated_leaves_evidenced() -> None:
    # Behavioral regression: a newly inserted row whose optional fields were
    # omitted (None/blank/empty-collection defaults) must not demand evidence
    # for those absent leaves; only typed leaves carrying an actual proposed
    # value require exact evidence. Valid zero/False values still count as
    # populated and demand evidence like any other typed leaf.
    recipe = _recipe()
    ingredient_key = "123e4567-e89b-42d3-a456-426614174013"
    proposal = recipe.model_dump(mode="json", exclude_none=False)
    proposal.pop("target_volume_l", None)
    proposal["ingredients"].append(
        {
            "ingredient_key": ingredient_key,
            "name": "caffeine concentrate",
            "quantity": 100.0,
            "unit": "g",
            "material_type": "other",
        }
    )
    # Every typed-non-absent sensitive leaf in the dumped row gets exact
    # evidence: name, quantity, unit, material_type, plus defaulted typed
    # enum leaves (scaling/mode, must_preparation, schedule_allocation).
    # Optional defaulted fields (None/""/[]) are intentionally omitted.
    change_basis = [
        {"field_path": f"/ingredients/{ingredient_key}/name", "basis": "research", "evidence": ["exact citation"]},
        {"field_path": f"/ingredients/{ingredient_key}/quantity", "basis": "research", "evidence": ["exact citation"]},
        {"field_path": f"/ingredients/{ingredient_key}/unit", "basis": "research", "evidence": ["exact citation"]},
        {"field_path": f"/ingredients/{ingredient_key}/material_type", "basis": "research", "evidence": ["exact citation"]},
        {"field_path": f"/ingredients/{ingredient_key}/scaling/mode", "basis": "research", "evidence": ["exact citation"]},
        {"field_path": f"/ingredients/{ingredient_key}/must_preparation", "basis": "research", "evidence": ["exact citation"]},
        {"field_path": f"/ingredients/{ingredient_key}/schedule_allocation", "basis": "research", "evidence": ["exact citation"]},
    ]
    result = RecipeAutofillResultV1.model_validate({
        "proposal": proposal,
        "change_basis": change_basis,
    })
    # Must not raise: omitted optional fields do not require evidence, while
    # populated sensitive leaves are covered with exact evidence.
    validate_sensitive_change_basis(result, recipe)


def test_model_json_rejects_prose_and_duplicate_keys() -> None:
    with pytest.raises(ContractError):
        parse_model_json("Here is the JSON: {}", RecipeTargetMetricsDraft)
    with pytest.raises(ContractError):
        parse_model_json('{"target_abv": 0.1, "target_abv": 0.2}', RecipeTargetMetricsDraft)


def test_recipe_audit_rejects_the_observed_guessed_finding_shape() -> None:
    raw = json.dumps({
        "envelope_version": 1,
        "kind": "recipe_audit",
        "summary": "invalid guessed shape",
        "findings": [{
            "path": "/ingredients/0",
            "finding": "guessed prose",
            "evidence_pointer": "/draft/ingredients/0",
        }],
    })
    with pytest.raises(ContractError) as exc_info:
        parse_model_json(raw, RecipeAuditResultV1, context={"draft": _recipe().model_dump(mode="json")})
    assert any(error.endswith("extra_forbidden") for error in exc_info.value.errors)


def _model_finding_payload(*, domain: str) -> dict[str, object]:
    return {
        "envelope_version": 1,
        "kind": "recipe_audit",
        "summary": "bounded",
        "findings": [
            {
                "finding_id": "model-domain-1",
                "origin": "model",
                "rule_id": None,
                "field_path": "/draft/notes",
                "severity": "advisory",
                "category": "process",
                "domain": domain,
                "current_value": None,
                "suggested_value": None,
                "rationale": "A concise synthetic finding.",
                "evidence": [],
                "uncertainty": "",
            }
        ],
    }


def test_recipe_audit_normalizes_category_value_used_as_model_domain() -> None:
    parsed = parse_model_json(
        json.dumps(_model_finding_payload(domain="process")),
        RecipeAuditResultV1,
    )
    assert parsed.findings[0].domain == "general"


def test_recipe_audit_cannot_claim_deterministic_provenance() -> None:
    payload = _model_finding_payload(domain="process")
    findings = payload["findings"]
    assert isinstance(findings, list)
    finding = findings[0]
    assert isinstance(finding, dict)
    finding["origin"] = "deterministic_rule"
    finding["rule_id"] = "MODEL-INVENTED-RULE"
    parsed = parse_model_json(json.dumps(payload), RecipeAuditResultV1)
    assert parsed.findings[0].origin == "model"
    assert parsed.findings[0].rule_id is None
    assert parsed.findings[0].domain == "general"


def test_recipe_audit_normalizes_only_resolvable_model_metadata() -> None:
    payload = _model_finding_payload(domain="process")
    findings = payload["findings"]
    assert isinstance(findings, list)
    finding = findings[0]
    assert isinstance(finding, dict)
    finding["field_path"] = "notes"
    finding["uncertainty"] = None
    finding["evidence"] = [
        {"pointer": "notes", "source_url": "", "title": "", "excerpt": ""}
    ]
    parsed = parse_model_json(
        json.dumps(payload),
        RecipeAuditResultV1,
        context={"draft": {"notes": "synthetic note"}},
    )
    assert parsed.findings[0].field_path == "/notes"
    assert parsed.findings[0].domain == "general"
    assert parsed.findings[0].uncertainty == ""
    evidence = parsed.findings[0].evidence[0]
    assert not isinstance(evidence, str)
    assert evidence.pointer == "/draft/notes"


def test_recipe_audit_unwraps_one_json_string_object_layer() -> None:
    payload = _model_finding_payload(domain="general")
    parsed = parse_model_json(
        json.dumps(json.dumps(payload)),
        RecipeAuditResultV1,
        context={"draft": {"notes": "synthetic note"}},
    )
    assert parsed.findings[0].field_path == "/draft/notes"


def test_recipe_audit_unwraps_exact_schema_echo_wrapper() -> None:
    payload = _model_finding_payload(domain="general")
    payload["envelope_version"] = {"const": 1, "type": "integer"}
    payload["evidence"] = [{"pointer": "draft.notes"}]
    wrapped = {
        "$defs": {},
        "additionalProperties": False,
        "properties": payload,
    }
    parsed = parse_model_json(
        json.dumps(wrapped),
        RecipeAuditResultV1,
        context={"draft": {"notes": "synthetic note"}},
    )
    assert parsed.kind == "recipe_audit"
    assert parsed.findings[0].finding_id == "model-domain-1"
    evidence = parsed.evidence[0]
    assert not isinstance(evidence, str)
    assert evidence.pointer == "/draft/notes"


def test_recipe_audit_normalizes_resolvable_dot_bracket_field_path() -> None:
    payload = _model_finding_payload(domain="culture")
    findings = payload["findings"]
    assert isinstance(findings, list)
    finding = findings[0]
    assert isinstance(finding, dict)
    finding["field_path"] = "culture_profiles[0].notes"
    parsed = parse_model_json(
        json.dumps(payload),
        RecipeAuditResultV1,
        context={"draft": {"culture_profiles": [{"notes": "operator starter"}]}},
    )
    assert parsed.findings[0].field_path == "/culture_profiles/0/notes"


def test_recipe_audit_normalizes_research_source_url_to_context_pointer() -> None:
    payload = _model_finding_payload(domain="general")
    research_url = "https://example.invalid/research/honey"
    payload["evidence"] = [
        {
            "pointer": research_url,
            "source_url": research_url,
            "title": "Honey reference",
            "excerpt": "A bounded research excerpt.",
        }
    ]
    parsed = parse_model_json(
        json.dumps(payload),
        RecipeAuditResultV1,
        context={
            "draft": {"notes": "synthetic note"},
            "indexed_reference_matches": [{"source_url": research_url}],
        },
    )
    evidence = parsed.evidence[0]
    assert not isinstance(evidence, str)
    assert evidence.pointer == "/indexed_reference_matches/0/source_url"
    assert evidence.source_url == research_url


def test_recipe_audit_normalizes_resolvable_draft_prefixed_field_path() -> None:
    payload = _model_finding_payload(domain="culture")
    findings = payload["findings"]
    assert isinstance(findings, list)
    finding = findings[0]
    assert isinstance(finding, dict)
    finding["field_path"] = "draft.culture_profiles[0]"
    parsed = parse_model_json(
        json.dumps(payload),
        RecipeAuditResultV1,
        context={"draft": {"culture_profiles": [{"notes": "operator starter"}]}},
    )
    assert parsed.findings[0].field_path == "/culture_profiles/0"


def test_model_field_path_normalization_remains_fail_closed() -> None:
    payload = _model_finding_payload(domain="general")
    findings = payload["findings"]
    assert isinstance(findings, list)
    finding = findings[0]
    assert isinstance(finding, dict)
    finding["field_path"] = "materials[0].name"
    with pytest.raises(ContractError) as exc_info:
        parse_model_json(
            json.dumps(payload),
            RecipeAuditResultV1,
            context={"draft": {"ingredients": [{"name": "honey"}]}},
        )
    assert "/evidence:context_pointer_invalid" in exc_info.value.errors


def test_recipe_autofill_drops_only_null_draft_target_volume() -> None:
    proposal = _recipe().model_dump(mode="json", exclude_none=False)
    assert proposal["target_volume_l"] is None
    payload = {
        "envelope_version": 1,
        "kind": "recipe_autofill",
        "proposal": proposal,
    }
    parsed = parse_model_json(
        json.dumps(payload),
        RecipeAutofillResultV1,
        context={"draft": proposal},
    )
    assert "target_volume_l" not in parsed.proposal.model_dump(mode="json")

    proposal["target_volume_l"] = 12.0
    with pytest.raises(ContractError) as exc_info:
        parse_model_json(json.dumps(payload), RecipeAutofillResultV1)
    assert "/proposal/target_volume_l:extra_forbidden" in exc_info.value.errors


@pytest.mark.parametrize("caffeine_status", ["decaf", "none"])
def test_recipe_autofill_normalizes_observed_legacy_tannin_detail(caffeine_status: str) -> None:
    proposal = _recipe().model_dump(mode="json", exclude_none=False)
    proposal.pop("target_volume_l")
    proposal["scheduled_additions"] = []
    ingredient = proposal["ingredients"][0]
    ingredient["material_type"] = "tannin"
    ingredient["tannin_detail"] = {
        "source": "oak",
        "form": "powder",
        "preparation": "none",
        "caffeine_status": caffeine_status,
    }
    payload = {
        "envelope_version": 1,
        "kind": "recipe_autofill",
        "proposal": proposal,
    }
    parsed = parse_model_json(json.dumps(payload), RecipeAutofillResultV1)
    tannin = parsed.proposal.ingredients[0].tannin_detail
    assert tannin is not None
    assert tannin.source_kind == "oak"
    assert tannin.caffeine_status == "unknown"


def test_recipe_rewrite_normalizes_proposal_prefixed_change_basis_path() -> None:
    recipe = _recipe()
    draft = recipe.model_dump(mode="json", exclude_none=False)
    proposal = recipe.model_copy(deep=True)
    proposal.ingredients[0].material_type = "tannin"
    proposal.ingredients[0].tannin_detail = TanninDetailDraft(
        source_kind="oak",
        caffeine_status="unknown",
    )
    payload = {
        "envelope_version": 1,
        "kind": "recipe_rewrite",
        "parent_job_id": _client_id("99"),
        "approved_finding_ids": ["finding-1"],
        "proposal": proposal.model_dump(mode="json", exclude_none=False),
        "change_basis": [{
            "field_path": "/proposal/ingredients/0/tannin_detail/source_kind",
            "basis": "inference",
            "evidence": ["The proposed source is inferred from the ingredient name."],
            "uncertainty": "Moderate.",
        }],
    }
    parsed = parse_model_json(
        json.dumps(payload),
        RecipeRewriteResultV1,
        context={"draft": draft},
    )
    assert parsed.change_basis[0].field_path == (
        f"/ingredients/{recipe.ingredients[0].ingredient_key}/tannin_detail/source_kind"
    )


def test_recipe_rewrite_normalizes_numeric_existing_draft_basis_path() -> None:
    recipe = _recipe()
    proposal = recipe.model_copy(deep=True)
    proposal.ingredients[0].material_type = "tannin"
    proposal.ingredients[0].tannin_detail = TanninDetailDraft(
        source_kind="oak",
        caffeine_status="unknown",
    )
    payload = {
        "envelope_version": 1,
        "kind": "recipe_rewrite",
        "parent_job_id": _client_id("95"),
        "approved_finding_ids": ["finding-1"],
        "proposal": proposal.model_dump(mode="json", exclude_none=False),
        "change_basis": [{
            "field_path": "/ingredients/0/tannin_detail/source_kind",
            "basis": "inference",
            "evidence": ["The proposed source is inferred from the ingredient name."],
            "uncertainty": "Moderate.",
        }],
    }
    parsed = parse_model_json(
        json.dumps(payload),
        RecipeRewriteResultV1,
        context={"draft": recipe.model_dump(mode="json", exclude_none=False)},
    )
    assert parsed.change_basis[0].field_path == (
        f"/ingredients/{recipe.ingredients[0].ingredient_key}/tannin_detail/source_kind"
    )

    parent_payload = dict(payload)
    parent_payload["change_basis"] = [dict(payload["change_basis"][0])]
    parent_payload["change_basis"][0]["field_path"] = "/ingredients/0"
    parent_parsed = parse_model_json(
        json.dumps(parent_payload),
        RecipeRewriteResultV1,
        context={"draft": recipe.model_dump(mode="json", exclude_none=False)},
    )
    assert parent_parsed.change_basis[0].field_path == "/ingredients/0"
    with pytest.raises(ContractError):
        validate_sensitive_change_basis(parent_parsed, recipe)


def test_recipe_rewrite_accepts_proposal_only_leaf_basis_without_relaxing_evidence() -> None:
    recipe = _recipe()
    recipe.ingredients[0].material_type = "tannin"
    draft = recipe.model_dump(mode="json", exclude_none=False)
    proposal = recipe.model_copy(deep=True)
    proposal.ingredients[0].material_type = "tannin"
    proposal.ingredients[0].tannin_detail = TanninDetailDraft(
        source_kind="oak",
        caffeine_status="unknown",
    )
    ingredient_path = f"/ingredients/{recipe.ingredients[0].ingredient_key}/tannin_detail"
    payload = {
        "envelope_version": 1,
        "kind": "recipe_rewrite",
        "parent_job_id": _client_id("98"),
        "approved_finding_ids": ["finding-1"],
        "proposal": proposal.model_dump(mode="json", exclude_none=False),
        "change_basis": [
            {
                "field_path": f"{ingredient_path}/caffeine_status",
                "basis": "inference",
                "evidence": ["The ingredient name does not indicate caffeine."],
                "uncertainty": "Low.",
            },
            {
                "field_path": f"{ingredient_path}/source_kind",
                "basis": "inference",
                "evidence": ["The ingredient name indicates an oak source."],
                "uncertainty": "Moderate.",
            },
        ],
    }
    parsed = parse_model_json(
        json.dumps(payload),
        RecipeRewriteResultV1,
        context={"draft": draft},
    )
    validate_sensitive_change_basis(parsed, recipe)
    assert {
        record.field_path for record in parsed.change_basis
    } == {
        f"{ingredient_path}/caffeine_status",
        f"{ingredient_path}/source_kind",
    }


def test_recipe_rewrite_rejects_proposal_only_new_row_field_path() -> None:
    recipe = _recipe()
    draft = recipe.model_dump(mode="json", exclude_none=False)
    proposal = recipe.model_copy(deep=True)
    new_key = _client_id("97")
    proposal.ingredients.append(
        RecipeIngredientDraft(
            ingredient_key=new_key,
            name="new ingredient",
            quantity=1.0,
            unit="g",
            material_type="other",
        )
    )
    payload = {
        "envelope_version": 1,
        "kind": "recipe_rewrite",
        "parent_job_id": _client_id("96"),
        "approved_finding_ids": ["finding-1"],
        "proposal": proposal.model_dump(mode="json", exclude_none=False),
        "change_basis": [{
            "field_path": f"/ingredients/{new_key}/name",
            "basis": "inference",
            "evidence": ["The new ingredient is inferred from the request."],
            "uncertainty": "High.",
        }],
    }
    with pytest.raises(ContractError) as exc_info:
        parse_model_json(
            json.dumps(payload),
            RecipeRewriteResultV1,
            context={"draft": draft},
        )
    assert "/evidence:context_pointer_invalid" in exc_info.value.errors


def test_recipe_rewrite_proposal_prefixed_unknown_field_path_remains_fail_closed() -> None:
    payload = _model_finding_payload(domain="general")
    findings = payload["findings"]
    assert isinstance(findings, list)
    finding = findings[0]
    assert isinstance(finding, dict)
    finding["field_path"] = "/proposal/materials/0/name"
    with pytest.raises(ContractError) as exc_info:
        parse_model_json(
            json.dumps(payload),
            RecipeAuditResultV1,
            context={"draft": {"ingredients": [{"name": "honey"}]}},
        )
    assert "/evidence:context_pointer_invalid" in exc_info.value.errors


def test_recipe_audit_still_rejects_arbitrary_invalid_model_domain() -> None:
    with pytest.raises(ContractError) as exc_info:
        parse_model_json(
            json.dumps(_model_finding_payload(domain="fermentation")),
            RecipeAuditResultV1,
        )
    assert "/findings/0/domain:literal_error" in exc_info.value.errors


def test_model_pointers_resolve_stable_list_keys() -> None:
    recipe = _recipe()
    ingredient_key = recipe.ingredients[0].ingredient_key
    raw = json.dumps({
        "envelope_version": 1,
        "kind": "recipe_audit",
        "findings": [{
            "finding_id": "model:quantity",
            "origin": "model",
            "field_path": f"/ingredients/{ingredient_key}/quantity",
            "severity": "advisory",
            "category": "measurement",
            "domain": "sugar",
            "rationale": "Check the quantity.",
            "evidence": [{"pointer": f"/draft/ingredients/{ingredient_key}/quantity"}],
        }],
    })
    parsed = parse_model_json(raw, RecipeAuditResultV1, context={"draft": recipe.model_dump(mode="json")})
    assert parsed.findings[0].field_path.endswith(f"{ingredient_key}/quantity")


def test_scheduled_draft_requires_exact_identity_and_values() -> None:
    raw = json.dumps({
        "envelope_version": 1,
        "kind": "brew_event_draft",
        "brew_run_id": 1,
        "event_type": "scheduled_addition_recorded",
    })
    with pytest.raises(ContractError):
        parse_model_json(raw, BrewEventDraftResultV1)


def test_job_store_is_idempotent(tmp_path: Path) -> None:
    store = AssistantJobStore(tmp_path / "brew.db")
    request = AssistantJobRequest(
        kind="chat",
        client_request_id=_client_id("20"),
        surface="recipe",
        message="hello",
        scope=AssistantScope(),
    )
    first, created = store.create(request, {"draft": None})
    second, duplicate = store.create(request, {"draft": {"different": True}})
    assert created is True
    assert duplicate is False
    assert second["job_id"] == first["job_id"]


def test_job_store_persists_ordered_workflow_stages(tmp_path: Path) -> None:
    store = AssistantJobStore(tmp_path / "brew.db")
    request = AssistantJobRequest(
        kind="chat",
        client_request_id=_client_id("23"),
        surface="recipe",
        message="hello",
    )
    record, created = store.create(request, {})
    assert created is True
    assert [stage["stage"] for stage in record["stages"]] == [
        "admission", "research", "model", "persist"
    ]
    assert [stage["status"] for stage in record["stages"]] == [
        "succeeded", "skipped", "pending", "pending"
    ]
    assert record["current_stage"] == "model"

    assert store.start_stage(record["job_id"], "model") is True
    assert store.finish_stage(record["job_id"], "model", "succeeded", detail={"calls": 1}) is True
    assert store.start_stage(record["job_id"], "persist") is True
    assert store.finish_stage(record["job_id"], "persist", "succeeded") is True
    finished = store.get(record["job_id"])
    assert finished is not None
    assert [stage["status"] for stage in finished["stages"]] == [
        "succeeded", "skipped", "succeeded", "succeeded"
    ]
    assert finished["current_stage"] is None


def test_pipeline_processes_one_job_and_persists_receipt(tmp_path: Path) -> None:
    store = AssistantJobStore(tmp_path / "brew.db")

    def handler(job: dict[str, object]) -> PipelineOutcome:
        return PipelineOutcome(result={"ok": True}, model="test", model_latency_ms=1)

    pipeline = AssistantPipeline(store, handler)
    request = AssistantJobRequest(
        kind="chat",
        client_request_id=_client_id("21"),
        surface="recipe",
        message="hello",
    )
    record, created = pipeline.submit(request, {"scope": "test"})
    assert created is True
    finished = pipeline.wait(record["job_id"], 5.0)
    pipeline.stop()
    assert finished is not None
    assert finished["status"] == "succeeded"
    assert finished["result"] == {"ok": True}
    assert [stage["status"] for stage in finished["stages"]] == [
        "succeeded", "skipped", "succeeded", "succeeded"
    ]
    # Behavioral regression: on the success path, the persisted 'persist'
    # stage status must match the job's terminal status, and its detail must
    # distinguish the successful-job receipt.
    persist_stage = next(stage for stage in finished["stages"] if stage["stage"] == "persist")
    assert persist_stage["status"] == "succeeded"
    assert persist_stage["detail"].get("job_status") == "succeeded"


def test_pipeline_sanitizes_handler_crash(tmp_path: Path) -> None:
    store = AssistantJobStore(tmp_path / "brew.db")

    def handler(job: dict[str, object]) -> PipelineOutcome:
        raise RuntimeError("secret should not escape")

    pipeline = AssistantPipeline(store, handler)
    request = AssistantJobRequest(
        kind="chat",
        client_request_id=_client_id("22"),
        surface="recipe",
        message="hello",
    )
    record, _ = pipeline.submit(request, {})
    finished = pipeline.wait(record["job_id"], 5.0)
    pipeline.stop()
    assert finished is not None
    assert finished["status"] == "failed"
    assert finished["error_code"] == "internal_error"
    # Stage-truthfulness regression: when the handler outcome is failed, the
    # persisted 'persist' stage must NOT report succeeded.  The failed-job
    # receipt was still written to the job row, but the persist stage label
    # must mirror the job's terminal status.
    assert [stage["status"] for stage in finished["stages"]] == [
        "succeeded", "skipped", "failed", "failed"
    ]
    persist_stage = next(stage for stage in finished["stages"] if stage["stage"] == "persist")
    assert persist_stage["status"] == "failed"
    assert persist_stage["detail"].get("job_status") == "failed"
    assert persist_stage["detail"].get("receipt_kind") == "failed_job_receipt"
    assert "secret" not in json.dumps(finished)
