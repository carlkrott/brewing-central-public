"""Strict, queued assistant contracts for Brewing Central.

This module owns validation and durable job mechanics.  It deliberately does
not know how to mutate a recipe or a brew: model results are proposals until
an operator applies them through the existing application controls.
"""
from __future__ import annotations

import hashlib
import json
import math
import queue
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, TypeVar, Union, get_args

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

MAX_STAGE_BYTES = 64 * 1024
MAX_CONTEXT_BYTES = 64 * 1024
MAX_PERSISTED_JSON_BYTES = 1024 * 1024
MAX_RAW_RESPONSE_BYTES = 64 * 1024
MAX_RAW_RESPONSE_TOTAL_BYTES = 384 * 1024
QUEUE_CAPACITY = 8

JobKind = Literal[
    "chat",
    "recipe_autofill",
    "recipe_audit",
    "recipe_rewrite",
    "brew_analyze",
    "brew_event_draft",
    "archive_compare",
]
JobStatus = Literal["queued", "running", "succeeded", "failed"]
Surface = Literal["recipe", "brew"]

WORKFLOW_STAGE_NAMES = ("admission", "research", "model", "persist")
WORKFLOW_STAGE_STATUSES = ("pending", "running", "succeeded", "failed", "skipped")

MATERIAL_TYPES = (
    "water",
    "fermentable",
    "culture",
    "nutrient",
    "tannin",
    "acid",
    "enzyme",
    "preservative",
    "fining",
    "flavour",
    "mineral",
    "packaging",
    "other",
)
UNIT_CODES = ("g", "kg", "ml", "l", "each", "pack", "tsp", "tbsp", "custom")
ALLERGENS = (
    "wheat",
    "milk",
    "egg",
    "soy",
    "sesame",
    "peanut",
    "tree_nut",
    "fish",
    "shellfish",
    "other",
)
SENSITIVITIES = ("gluten", "sulphites", "caffeine", "alcohol", "other")
ADDITION_STAGES = ("mash", "boil", "fermenter", "secondary", "keg", "bottle", "other")
SERIES_KINDS = (
    "culture_feed",
    "inoculation",
    "sugar_step",
    "nutrient_feed",
    "tannin_addition",
    "acid_adjustment",
    "enzyme_addition",
    "preservative_addition",
    "fining_addition",
    "flavour_addition",
    "water_adjustment",
    "other",
    # Accepted aliases from early plan drafts; they canonicalize nowhere and
    # remain distinct values so no schedule is silently merged.
    "nutrient_step",
    "tannin_step",
    "acid_step",
    "aeration",
    "manual",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def canonical_json(value: Any) -> str:
    """Return the byte-stable JSON representation used for hashes."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=False)
    return _json_dump(value)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _bounded_text(value: str, limit: int, *, blank: bool = True) -> str:
    value = value.strip()
    if not blank and not value:
        raise ValueError("value must not be blank")
    if len(value) > limit:
        raise ValueError(f"value exceeds {limit} characters")
    return value


def _uuid4(value: str) -> str:
    value = value.strip()
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("value must be a UUID v4") from exc
    if parsed.version != 4:
        raise ValueError("value must be a UUID v4")
    return str(parsed)


def _finite(value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError("boolean is not a number")
    if isinstance(value, (int, float)) and not math.isfinite(float(value)):
        raise ValueError("number must be finite")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ScalePointDraft(StrictModel):
    volume_l: float = Field(ge=1.0, le=200.0)
    quantity: float = Field(ge=0.0, le=1_000_000_000.0)

    @field_validator("volume_l", "quantity", mode="before")
    @classmethod
    def finite_numbers(cls, value: Any) -> Any:
        return _finite(value)


class ScaleRuleDraft(StrictModel):
    mode: Literal["linear", "fixed", "power", "piecewise"] = "linear"
    exponent: float | None = Field(default=None, ge=0.0, le=2.0)
    points: list[ScalePointDraft] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_rule(self) -> "ScaleRuleDraft":
        if self.mode == "power" and self.exponent is None:
            raise ValueError("power scaling requires exponent")
        if self.mode != "power" and self.exponent is not None:
            raise ValueError("exponent is only valid for power scaling")
        if self.mode == "piecewise":
            if len(self.points) < 2:
                raise ValueError("piecewise scaling requires at least two points")
            volumes = [point.volume_l for point in self.points]
            if len(set(volumes)) != len(volumes):
                raise ValueError("piecewise scaling volumes must be distinct")
            if any(right <= left for left, right in zip(volumes, volumes[1:])):
                raise ValueError("piecewise scaling volumes must be increasing")
        elif self.points:
            raise ValueError("points are only valid for piecewise scaling")
        return self


class RecipeTargetMetricsDraft(StrictModel):
    initial_gravity: float | None = None
    effective_original_gravity_after_all_feeds: float | None = None
    target_final_gravity: float | None = None
    gravity_basis: Literal["initial_charge", "effective_all_fermentables", "unknown"] = "unknown"
    abv_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    ph_min: float | None = Field(default=None, ge=0.0, le=14.0)
    ph_max: float | None = Field(default=None, ge=0.0, le=14.0)
    sweetness: Literal["dry", "off_dry", "medium", "sweet", "very_sweet", "unknown"] = "unknown"
    carbonation: Literal["still", "petillant", "sparkling", "other", "unknown"] = "unknown"
    balance_notes: str = Field(default="", max_length=4_000)

    @field_validator("initial_gravity", "effective_original_gravity_after_all_feeds", "target_final_gravity", "abv_percent", "ph_min", "ph_max", mode="before")
    @classmethod
    def finite_optional_numbers(cls, value: Any) -> Any:
        return None if value is None else _finite(value)

    @model_validator(mode="after")
    def validate_range(self) -> "RecipeTargetMetricsDraft":
        for name in ("initial_gravity", "effective_original_gravity_after_all_feeds", "target_final_gravity"):
            value = getattr(self, name)
            if value is not None and not 0.8 <= value <= 2.0:
                raise ValueError(f"{name} must be between 0.8 and 2.0")
        if self.ph_min is not None and self.ph_max is not None and self.ph_min > self.ph_max:
            raise ValueError("ph_min must not exceed ph_max")
        return self


class ProductDetailDraft(StrictModel):
    manufacturer: str = Field(default="", max_length=160)
    product_name: str = Field(default="", max_length=200)
    formulation_or_grade: str = Field(default="", max_length=200)
    label_directions: str = Field(default="", max_length=4_000)
    source_url: str = Field(default="", max_length=2_000)

    @field_validator("manufacturer", "product_name", "formulation_or_grade", "label_directions", "source_url")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return _bounded_text(value, 4_000)


class TanninDetailDraft(StrictModel):
    source_kind: Literal[
        "black_tea",
        "green_tea",
        "oolong_tea",
        "white_tea",
        "herbal_infusion",
        "fruit_skin_or_seed",
        "oak",
        "enological_tannin",
        "other",
    ]
    form: str = Field(default="", max_length=120)
    botanical_or_style: str = Field(default="", max_length=160)
    caffeine_status: Literal["yes", "no", "unknown"] = "unknown"
    steep_temp_c: float | None = Field(default=None, ge=0.0, le=130.0)
    steep_minutes: float | None = Field(default=None, ge=0.0, le=10_000.0)
    contact_duration_minutes: float | None = Field(default=None, ge=0.0, le=100_000.0)
    balance_notes: str = Field(default="", max_length=2_000)

    @field_validator("steep_temp_c", "steep_minutes", "contact_duration_minutes", mode="before")
    @classmethod
    def finite_optional_numbers(cls, value: Any) -> Any:
        return None if value is None else _finite(value)


class NutrientDetailDraft(StrictModel):
    nutrient_class: Literal["organic", "inorganic", "mixed", "rehydration_protectant", "unknown"] = "unknown"
    composition_notes: str = Field(default="", max_length=2_000)
    yan_contribution: str = Field(default="", max_length=200)


class CultureSubstrateDraft(StrictModel):
    name: str = Field(default="", max_length=160)
    material_type: Literal["flour", "grain", "other"] = "other"
    allergen_tags: list[Literal["wheat", "gluten", "other"]] = Field(default_factory=list, max_length=8)
    provenance: str = Field(default="", max_length=1_000)


class MaintenanceRatioDraft(StrictModel):
    # These remain opaque strings on purpose.  The assistant does not perform
    # numeric starter-feed math from a maintenance notation.
    starter_parts: str = Field(default="", max_length=80)
    flour_parts: str = Field(default="", max_length=80)
    water_parts: str = Field(default="", max_length=80)
    mass_basis: str = Field(default="", max_length=160)


class CultureProfileDraft(StrictModel):
    culture_key: str = Field(default_factory=lambda: str(uuid.uuid4()), max_length=36)
    client_key: str | None = Field(default=None, max_length=36)
    display_name: str = Field(default="", max_length=160)
    ingredient_key: str | None = Field(default=None, max_length=36)
    culture_kind: Literal[
        "commercial_pure_culture",
        "yeast_strain",
        "mixed_yeast_lab",
        "sourdough_starter",
        "mixed_culture",
        "wild_culture",
        "wild_capture",
        "kombucha_scoby",
        "kombucha_scooby",
        "brett_blend",
        "other",
    ] = "other"
    organism_status: Literal["catalogued_single", "mixed_known", "mixed_unknown", "uncharacterized"] = "uncharacterized"
    identity_assertion: Literal["single_strain", "mixed_consortium", "unknown"] = "unknown"
    source: str = Field(default="", max_length=1_000)
    provenance: str = Field(default="", max_length=1_000)
    catalog_reference: str = Field(default="", max_length=300)
    substrates: list[CultureSubstrateDraft] = Field(default_factory=list, max_length=8)
    hydration_percent: float | None = Field(default=None, ge=0.0, le=1_000.0)
    maintenance_ratio: MaintenanceRatioDraft | None = None
    backslop_ratio: str = Field(default="", max_length=160)
    typical_feed_interval: str = Field(default="", max_length=160)
    refresh_interval_hours: str | None = Field(default=None, max_length=160)
    storage_temperature_c: float | None = Field(default=None, ge=-50.0, le=100.0)
    pH_observed: float | None = Field(default=None, ge=0.0, le=14.0)
    established_at: str | None = Field(default=None, max_length=80)
    readiness_criteria: str = Field(default="", max_length=2_000)
    notes: str = Field(default="", max_length=4_000)
    allergen_tags: list[str] = Field(default_factory=list, max_length=16)
    sensitivity_tags: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("culture_key", "ingredient_key", "client_key", mode="before")
    @classmethod
    def validate_optional_keys(cls, value: Any) -> Any:
        if value is None or value == "":
            return None if value is None or value == "" else value
        return _uuid4(value)

    @field_validator("hydration_percent", "storage_temperature_c", "pH_observed", mode="before")
    @classmethod
    def finite_optional_numbers(cls, value: Any) -> Any:
        return None if value is None else _finite(value)

    @model_validator(mode="after")
    def validate_identity(self) -> "CultureProfileDraft":
        if self.organism_status == "catalogued_single" and not self.catalog_reference.strip():
            raise ValueError("catalogued_single requires an operator-supplied catalog_reference")
        if self.identity_assertion == "single_strain" and not self.catalog_reference.strip():
            raise ValueError("single_strain requires an operator-supplied catalog_reference")
        return self


class RecipeIngredientDraft(StrictModel):
    ingredient_key: str = Field(default_factory=lambda: str(uuid.uuid4()), max_length=36)
    client_key: str | None = Field(default=None, max_length=36)
    name: str = Field(default="", max_length=160)
    quantity: float | None = Field(default=None, ge=0.0, le=1_000_000_000.0)
    unit: Literal["g", "kg", "ml", "l", "each", "pack", "tsp", "tbsp", "custom"] | None = None
    custom_unit: str = Field(default="", max_length=80)
    category: str = Field(default="", max_length=80)
    material_type: Literal["water", "fermentable", "culture", "nutrient", "tannin", "acid", "enzyme", "preservative", "fining", "flavour", "mineral", "packaging", "other"] = "other"
    purpose: str = Field(default="", max_length=200)
    addition_stage: Literal["mash", "boil", "fermenter", "secondary", "keg", "bottle", "other"] | None = None
    addition_timing: str = Field(default="", max_length=200)
    scaling: ScaleRuleDraft = Field(default_factory=ScaleRuleDraft)
    product: ProductDetailDraft | None = None
    provenance: str = Field(default="", max_length=1_000)
    allergen_tags: list[str] = Field(default_factory=list, max_length=16)
    other_allergen: str = Field(default="", max_length=200)
    sensitivity_tags: list[str] = Field(default_factory=list, max_length=16)
    other_sensitivity: str = Field(default="", max_length=200)
    tannin_detail: TanninDetailDraft | None = None
    nutrient_detail: NutrientDetailDraft | None = None
    must_preparation: Literal["none", "rehydrate", "dissolve", "crush", "slurry", "custom"] = "none"
    preparation_other: str = Field(default="", max_length=500)
    schedule_allocation: Literal["none", "partial", "complete"] = "none"

    @field_validator("ingredient_key", "client_key", mode="before")
    @classmethod
    def validate_keys(cls, value: Any) -> Any:
        if value is None or value == "":
            return None if value is None or value == "" else value
        return _uuid4(value)

    @field_validator("quantity", mode="before")
    @classmethod
    def finite_quantity(cls, value: Any) -> Any:
        return None if value is None else _finite(value)

    @field_validator("name", "custom_unit", "category", "purpose", "addition_timing", "provenance", "other_allergen", "other_sensitivity", "preparation_other")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("allergen_tags")
    @classmethod
    def validate_allergens(cls, values: list[str]) -> list[str]:
        invalid = set(values) - set(ALLERGENS)
        if invalid:
            raise ValueError(f"unsupported allergen tag: {sorted(invalid)[0]}")
        return list(dict.fromkeys(values))

    @field_validator("sensitivity_tags")
    @classmethod
    def validate_sensitivities(cls, values: list[str]) -> list[str]:
        invalid = set(values) - set(SENSITIVITIES)
        if invalid:
            raise ValueError(f"unsupported sensitivity tag: {sorted(invalid)[0]}")
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def validate_details(self) -> "RecipeIngredientDraft":
        if self.unit == "custom" and not self.custom_unit:
            raise ValueError("custom unit requires custom_unit")
        if self.unit != "custom" and self.custom_unit:
            raise ValueError("custom_unit is only valid with unit=custom")
        if self.tannin_detail is not None and self.material_type != "tannin":
            raise ValueError("tannin_detail requires material_type=tannin")
        if self.nutrient_detail is not None and self.material_type != "nutrient":
            raise ValueError("nutrient_detail requires material_type=nutrient")
        return self


class TriggerModel(StrictModel):
    @model_validator(mode="before")
    @classmethod
    def accept_type_alias(cls, value: Any) -> Any:
        if isinstance(value, dict) and "kind" not in value and "type" in value:
            value = dict(value)
            value["kind"] = value.pop("type")
        return value


class ManualTrigger(TriggerModel):
    kind: Literal["manual"] = "manual"


class ElapsedSinceBrewStartTrigger(TriggerModel):
    kind: Literal["elapsed_since_brew_start"] = "elapsed_since_brew_start"
    hours: float = Field(ge=0.0, le=100_000.0)

    @field_validator("hours", mode="before")
    @classmethod
    def finite_hours(cls, value: Any) -> Any:
        return _finite(value)


class ElapsedSinceStepTrigger(TriggerModel):
    kind: Literal["elapsed_since_step"] = "elapsed_since_step"
    step_key: str
    hours: float = Field(ge=0.0, le=100_000.0)

    @field_validator("step_key")
    @classmethod
    def valid_step_key(cls, value: str) -> str:
        return _uuid4(value)

    @field_validator("hours", mode="before")
    @classmethod
    def finite_hours(cls, value: Any) -> Any:
        return _finite(value)


class GravityAtOrBelowTrigger(TriggerModel):
    kind: Literal["gravity_at_or_below"] = "gravity_at_or_below"
    gravity: float = Field(ge=0.8, le=2.0)
    freshness_minutes: float = Field(default=30.0, ge=0.0, le=10_080.0)
    dwell_minutes: float = Field(default=0.0, ge=0.0, le=10_080.0)
    requires_active_calibration: StrictBool = True

    @field_validator("gravity", "freshness_minutes", "dwell_minutes", mode="before")
    @classmethod
    def finite_values(cls, value: Any) -> Any:
        return _finite(value)


class GravityDropAtLeastTrigger(TriggerModel):
    kind: Literal["gravity_drop_at_least"] = "gravity_drop_at_least"
    delta: float = Field(ge=0.0, le=2.0)
    freshness_minutes: float = Field(default=30.0, ge=0.0, le=10_080.0)
    dwell_minutes: float = Field(default=0.0, ge=0.0, le=10_080.0)
    requires_active_calibration: StrictBool = True
    baseline: Literal["brew_start", "last_recorded_sugar_addition", "recorded_step"] = "brew_start"
    step_key: str | None = None

    @field_validator("delta", "freshness_minutes", "dwell_minutes", mode="before")
    @classmethod
    def finite_values(cls, value: Any) -> Any:
        return _finite(value)

    @field_validator("step_key")
    @classmethod
    def valid_optional_step_key(cls, value: str | None) -> str | None:
        return None if value is None else _uuid4(value)

    @model_validator(mode="after")
    def validate_baseline(self) -> "GravityDropAtLeastTrigger":
        if self.baseline == "recorded_step" and self.step_key is None:
            raise ValueError("recorded_step baseline requires step_key")
        if self.baseline != "recorded_step" and self.step_key is not None:
            raise ValueError("step_key is only valid for recorded_step baseline")
        return self


class TemperatureInRangeTrigger(TriggerModel):
    kind: Literal["temperature_in_range"] = "temperature_in_range"
    min_c: float = Field(ge=-50.0, le=100.0)
    max_c: float = Field(ge=-50.0, le=100.0)
    freshness_minutes: float = Field(default=30.0, ge=0.0, le=10_080.0)
    dwell_minutes: float = Field(default=0.0, ge=0.0, le=10_080.0)

    @field_validator("min_c", "max_c", "freshness_minutes", "dwell_minutes", mode="before")
    @classmethod
    def finite_values(cls, value: Any) -> Any:
        return _finite(value)

    @model_validator(mode="after")
    def validate_range(self) -> "TemperatureInRangeTrigger":
        if self.min_c > self.max_c:
            raise ValueError("temperature min_c must not exceed max_c")
        return self


class PhAtOrBelowTrigger(TriggerModel):
    kind: Literal["ph_at_or_below"] = "ph_at_or_below"
    ph: float = Field(ge=0.0, le=14.0)
    measurement_source: Literal["manual"] = "manual"

    @field_validator("ph", mode="before")
    @classmethod
    def finite_ph(cls, value: Any) -> Any:
        return _finite(value)


class OperatorObservationTrigger(TriggerModel):
    kind: Literal["operator_observation"] = "operator_observation"
    condition: str = Field(min_length=1, max_length=1_000)

    @field_validator("condition")
    @classmethod
    def strip_condition(cls, value: str) -> str:
        return value.strip()


Trigger = Union[
    ManualTrigger,
    ElapsedSinceBrewStartTrigger,
    ElapsedSinceStepTrigger,
    GravityAtOrBelowTrigger,
    GravityDropAtLeastTrigger,
    TemperatureInRangeTrigger,
    PhAtOrBelowTrigger,
    OperatorObservationTrigger,
]


class ScheduledAdditionDraft(StrictModel):
    addition_key: str = Field(default_factory=lambda: str(uuid.uuid4()), max_length=36)
    client_key: str | None = Field(default=None, max_length=36)
    series_key: str = Field(default_factory=lambda: str(uuid.uuid4()), max_length=36)
    sequence: StrictInt = Field(default=1, ge=1, le=100_000)
    series_kind: Literal[
        "culture_feed",
        "inoculation",
        "sugar_step",
        "nutrient_feed",
        "tannin_addition",
        "acid_adjustment",
        "enzyme_addition",
        "preservative_addition",
        "fining_addition",
        "flavour_addition",
        "water_adjustment",
        "other",
        "nutrient_step",
        "tannin_step",
        "acid_step",
        "aeration",
        "manual",
    ] = "other"
    ingredient_key: str | None = None
    quantity: float | None = Field(default=None, ge=0.0, le=1_000_000_000.0)
    unit: Literal["g", "kg", "ml", "l", "each", "pack", "tsp", "tbsp", "custom"] | None = None
    custom_unit: str = Field(default="", max_length=80)
    scaling: ScaleRuleDraft = Field(default_factory=ScaleRuleDraft)
    addition_stage: Literal["mash", "boil", "fermenter", "secondary", "keg", "bottle", "other"] | None = None
    preparation_method: Literal["direct", "dissolve", "rehydrate", "infuse", "steep_and_remove", "blend", "other"] = "direct"
    must_preparation: Literal["none", "rehydrate", "dissolve", "crush", "slurry", "custom"] = "none"
    preparation: str = Field(default="", max_length=1_000)
    instructions: str = Field(default="", max_length=4_000)
    contact_duration_minutes: float | None = Field(default=None, ge=0.0, le=100_000.0)
    removal_required: StrictBool = False
    contributes_volume: StrictBool = False
    sanitation_notes: str = Field(default="", max_length=2_000)
    trigger: Trigger | None = None
    due_window_hours: float | None = Field(default=None, ge=0.0, le=100_000.0)
    late_window_hours: float | None = Field(default=None, ge=0.0, le=100_000.0)
    preconditions: list[str] = Field(default_factory=list, max_length=16)
    stop_conditions: list[str] = Field(default_factory=list, max_length=16)
    completion_checks: list[str] = Field(default_factory=list, max_length=16)
    suggestion_basis: dict[str, Any] | None = None

    @field_validator("addition_key", "series_key", "ingredient_key", "client_key", mode="before")
    @classmethod
    def validate_keys(cls, value: Any) -> Any:
        if value is None or value == "":
            return None if value is None or value == "" else value
        return _uuid4(value)

    @field_validator("quantity", "contact_duration_minutes", "due_window_hours", "late_window_hours", mode="before")
    @classmethod
    def finite_optional_values(cls, value: Any) -> Any:
        return None if value is None else _finite(value)

    @field_validator("custom_unit", "preparation", "instructions", "sanitation_notes")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_unit(self) -> "ScheduledAdditionDraft":
        if self.unit == "custom" and not self.custom_unit:
            raise ValueError("custom unit requires custom_unit")
        if self.unit != "custom" and self.custom_unit:
            raise ValueError("custom_unit is only valid with unit=custom")
        return self


class RecipeProcessStepDraft(StrictModel):
    step_key: str = Field(default_factory=lambda: str(uuid.uuid4()), max_length=36)
    client_key: str | None = Field(default=None, max_length=36)
    sequence: StrictInt = Field(default=1, ge=1, le=100_000)
    phase: Literal["culture_build", "base_preparation", "inoculation", "primary", "secondary", "conditioning", "packaging", "other"] = "other"
    method: Literal["prepare", "sanitize", "mix", "heat", "cool", "inoculate", "feed_sugar", "feed_nutrient", "add_tannin", "add_additive", "measure", "hold", "transfer", "condition", "package", "other"] = "other"
    title: str = Field(default="", max_length=200)
    instructions: str = Field(default="", max_length=4_000)
    trigger: Trigger | None = None
    linked_addition_key: str | None = None
    linked_culture_key: str | None = None
    preconditions: list[str] = Field(default_factory=list, max_length=16)
    stop_conditions: list[str] = Field(default_factory=list, max_length=16)
    verification: str = Field(default="", max_length=2_000)

    @field_validator("step_key", "client_key", "linked_addition_key", "linked_culture_key", mode="before")
    @classmethod
    def validate_keys(cls, value: Any) -> Any:
        if value is None or value == "":
            return None if value is None or value == "" else value
        return _uuid4(value)

    @field_validator("title", "instructions", "verification")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class RecipeFormDraft(StrictModel):
    name: str = Field(default="", max_length=160)
    style: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=4_000)
    beverage_type: Literal["beer", "wine", "mead", "cider", "kombucha", "other"] | None = None
    base_volume_l: float | None = Field(default=None, ge=1.0, le=200.0)
    initial_fermenter_volume_l: float | None = Field(default=None, ge=1.0, le=200.0)
    target_metrics: RecipeTargetMetricsDraft | None = None
    notes: str = Field(default="", max_length=20_000)
    ingredients: list[RecipeIngredientDraft] = Field(default_factory=list, max_length=128)
    culture_profiles: list[CultureProfileDraft] = Field(default_factory=list, max_length=8)
    scheduled_additions: list[ScheduledAdditionDraft] = Field(default_factory=list, max_length=128)
    process_steps: list[RecipeProcessStepDraft] = Field(default_factory=list, max_length=128)
    target_volume_l: float | None = Field(default=None, ge=1.0, le=200.0)

    @field_validator("name", "style", "description", "notes")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("base_volume_l", "initial_fermenter_volume_l", "target_volume_l", mode="before")
    @classmethod
    def finite_optional_values(cls, value: Any) -> Any:
        return None if value is None else _finite(value)

    @model_validator(mode="after")
    def unique_logical_keys(self) -> "RecipeFormDraft":
        groups = {
            "ingredient_key": [row.ingredient_key for row in self.ingredients],
            "culture_key": [row.culture_key for row in self.culture_profiles],
            "addition_key": [row.addition_key for row in self.scheduled_additions],
            "series_key": [row.series_key for row in self.scheduled_additions],
            "step_key": [row.step_key for row in self.process_steps],
        }
        for name, values in groups.items():
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {name}")
        return self


class RecipeProposal(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    style: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=4_000)
    beverage_type: Literal["beer", "wine", "mead", "cider", "kombucha", "other"] | None = None
    base_volume_l: float = Field(ge=1.0, le=200.0)
    initial_fermenter_volume_l: float | None = Field(default=None, ge=1.0, le=200.0)
    target_metrics: RecipeTargetMetricsDraft | None = None
    notes: str = Field(default="", max_length=20_000)
    ingredients: list[RecipeIngredientDraft] = Field(min_length=1, max_length=128)
    culture_profiles: list[CultureProfileDraft] = Field(default_factory=list, max_length=8)
    scheduled_additions: list[ScheduledAdditionDraft] = Field(default_factory=list, max_length=128)
    process_steps: list[RecipeProcessStepDraft] = Field(default_factory=list, max_length=128)

    @field_validator("name", "style", "description", "notes")
    @classmethod
    def strip_text(cls, value: str) -> str:
        value = value.strip()
        return value

    @model_validator(mode="after")
    def complete_graph(self) -> "RecipeProposal":
        if not self.name:
            raise ValueError("proposal name must not be blank")
        if any(not row.name or row.quantity is None or row.unit is None for row in self.ingredients):
            raise ValueError("proposal ingredients must have name, quantity, and unit")
        draft = RecipeFormDraft.model_validate(self.model_dump(mode="python"))
        errors = recipe_contract_errors(draft, require_complete=True)
        if errors:
            raise ValueError(errors[0])
        return self


class EvidenceRef(StrictModel):
    pointer: str = Field(min_length=1, max_length=512)
    source_url: str = Field(default="", max_length=2_000)
    title: str = Field(default="", max_length=500)
    excerpt: str = Field(default="", max_length=2_000)


class Finding(StrictModel):
    finding_id: str = Field(min_length=1, max_length=128)
    origin: Literal["model", "deterministic_rule"]
    rule_id: str | None = Field(default=None, max_length=128)
    field_path: str = Field(min_length=1, max_length=512)
    severity: Literal["info", "advisory", "warning", "blocker"]
    category: Literal["completeness", "consistency", "scaling", "process", "style", "measurement", "safety", "grounding"]
    domain: Literal["general", "culture", "sugar", "nutrient", "tannin", "additive", "schedule", "allergen"]
    current_value: Any = None
    suggested_value: Any = None
    rationale: str = Field(min_length=1, max_length=4_000)
    evidence: list[Union[EvidenceRef, str]] = Field(default_factory=list, max_length=16)
    uncertainty: str = Field(default="", max_length=2_000)

    @field_validator("finding_id", "field_path", "rationale", "uncertainty")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class SuggestionBasisRecord(StrictModel):
    field_path: str = Field(min_length=1, max_length=512)
    basis: Literal["user_value", "product_label", "research", "inference"]
    evidence: list[Union[EvidenceRef, str]] = Field(default_factory=list, max_length=16)
    uncertainty: str = Field(default="", max_length=2_000)

    @field_validator("field_path", "uncertainty")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class ResultBase(StrictModel):
    envelope_version: Literal[1] = 1
    summary: str = Field(default="", max_length=8_000)
    evidence: list[Union[EvidenceRef, str]] = Field(default_factory=list, max_length=32)
    uncertainties: list[str] = Field(default_factory=list, max_length=32)


class RecipeAutofillResultV1(ResultBase):
    kind: Literal["recipe_autofill"] = "recipe_autofill"
    proposal: RecipeProposal
    change_basis: list[SuggestionBasisRecord] = Field(default_factory=list, max_length=256)
    applicable_diff: list[dict[str, Any]] = Field(default_factory=list, max_length=512)
    excluded_change_count: StrictInt = Field(default=0, ge=0)


class RecipeAuditResultV1(ResultBase):
    kind: Literal["recipe_audit"] = "recipe_audit"
    findings: list[Finding] = Field(default_factory=list, max_length=512)

    @model_validator(mode="after")
    def unique_findings(self) -> "RecipeAuditResultV1":
        ids = [finding.finding_id for finding in self.findings]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate finding_id")
        return self


class RecipeRewriteResultV1(ResultBase):
    kind: Literal["recipe_rewrite"] = "recipe_rewrite"
    parent_job_id: str = Field(min_length=1, max_length=36)
    approved_finding_ids: list[str] = Field(min_length=1, max_length=512)
    proposal: RecipeProposal
    change_basis: list[SuggestionBasisRecord] = Field(default_factory=list, max_length=256)
    applicable_diff: list[dict[str, Any]] = Field(default_factory=list, max_length=512)
    excluded_change_count: StrictInt = Field(default=0, ge=0)


class BrewAnalyzeResultV1(ResultBase):
    kind: Literal["brew_analyze"] = "brew_analyze"
    measured_observations: list[dict[str, Any]] = Field(default_factory=list, max_length=128)
    estimates: list[dict[str, Any]] = Field(default_factory=list, max_length=128)
    findings: list[Finding] = Field(default_factory=list, max_length=256)


class BrewEventDraftResultV1(ResultBase):
    kind: Literal["brew_event_draft"] = "brew_event_draft"
    brew_run_id: StrictInt = Field(gt=0)
    addition_key: str | None = None
    event_type: Literal["scheduled_addition_recorded", "scheduled_addition_skipped", "other"] = "other"
    quantity: float | None = Field(default=None, ge=0.0, le=1_000_000_000.0)
    unit: str | None = Field(default=None, max_length=80)
    reason: str = Field(default="", max_length=2_000)
    change_basis: list[SuggestionBasisRecord] = Field(default_factory=list, max_length=64)

    @field_validator("addition_key")
    @classmethod
    def valid_addition_key(cls, value: str | None) -> str | None:
        return None if value is None else _uuid4(value)

    @model_validator(mode="after")
    def validate_scheduled_draft(self) -> "BrewEventDraftResultV1":
        if self.event_type in {"scheduled_addition_recorded", "scheduled_addition_skipped"} and self.addition_key is None:
            raise ValueError("scheduled addition drafts require addition_key")
        if self.event_type == "scheduled_addition_recorded" and (self.quantity is None or not self.unit or not self.unit.strip()):
            raise ValueError("recorded addition drafts require quantity and unit")
        if self.event_type == "scheduled_addition_skipped" and not self.reason.strip():
            raise ValueError("skipped addition drafts require reason")
        return self


class ArchiveCompareResultV1(ResultBase):
    kind: Literal["archive_compare"] = "archive_compare"
    comparison: list[dict[str, Any]] = Field(default_factory=list, max_length=256)
    findings: list[Finding] = Field(default_factory=list, max_length=256)


AssistantResult = Union[
    RecipeAutofillResultV1,
    RecipeAuditResultV1,
    RecipeRewriteResultV1,
    BrewAnalyzeResultV1,
    BrewEventDraftResultV1,
    ArchiveCompareResultV1,
]


class AssistantScope(StrictModel):
    recipe_id: StrictInt | None = Field(default=None, gt=0)
    recipe_revision: StrictInt | None = Field(default=None, ge=1)
    brew_run_id: StrictInt | None = Field(default=None, gt=0)
    device_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("device_id")
    @classmethod
    def strip_device(cls, value: str | None) -> str | None:
        return None if value is None else value.strip()


class AssistantJobRequest(StrictModel):
    kind: JobKind
    client_request_id: str = Field(min_length=36, max_length=36)
    surface: Surface
    message: str = Field(default="", max_length=8_000)
    conversation_id: str | None = Field(default=None, max_length=128)
    research: StrictBool = False
    scope: AssistantScope = Field(default_factory=AssistantScope)
    draft: RecipeFormDraft | None = None
    parent_job_id: str | None = Field(default=None, max_length=36)
    approved_finding_ids: list[str] = Field(default_factory=list, max_length=512)
    fill_strategy: Literal["empty_only", "full"] = "empty_only"
    audit_profile: Literal["ordinary", "full"] = "ordinary"

    @field_validator("client_request_id")
    @classmethod
    def client_uuid(cls, value: str) -> str:
        return _uuid4(value)

    @field_validator("message", "conversation_id")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        return None if value is None else value.strip()

    @model_validator(mode="after")
    def validate_surface_kind(self) -> "AssistantJobRequest":
        recipe_kinds = {"recipe_autofill", "recipe_audit", "recipe_rewrite"}
        brew_kinds = {"brew_analyze", "brew_event_draft", "archive_compare"}
        if self.kind in recipe_kinds and self.surface != "recipe":
            raise ValueError("recipe job kind requires recipe surface")
        if self.kind in brew_kinds and self.surface != "brew":
            raise ValueError("brew job kind requires brew surface")
        if self.kind in recipe_kinds and self.draft is None:
            raise ValueError("recipe job requires draft")
        if self.kind == "recipe_rewrite" and not self.parent_job_id:
            raise ValueError("recipe_rewrite requires parent_job_id")
        return self


class AssistantJobRecord(StrictModel):
    job_id: str
    client_request_id: str
    parent_job_id: str | None = None
    kind: JobKind
    surface: Surface
    status: JobStatus
    conversation_id: str | None = None
    scope: AssistantScope = Field(default_factory=AssistantScope)
    draft_hash: str | None = None
    context_hash: str | None = None
    created_at: str
    queued_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    queue_wait_ms: int | None = None
    model_latency_ms: int | None = None
    model: str | None = None
    result: Any = None
    raw_response: Any = None
    parse_errors: list[dict[str, str]] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    research_status: dict[str, str] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    approval_decision: Literal["approved", "partial", "rejected"] | None = None
    approved_finding_ids: list[str] = Field(default_factory=list)
    approved_at: str | None = None
    stages: list[dict[str, Any]] = Field(default_factory=list)
    current_stage: str | None = None


class ApprovalPayload(StrictModel):
    decision: Literal["approved", "partial", "rejected"]
    finding_ids: list[str] = Field(default_factory=list, max_length=512)


class QueueFullError(RuntimeError):
    pass


class ContractError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(errors[0] if errors else "contract validation failed")


def _entity_key(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _compatible_units(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    return {left, right} <= {"g", "kg"} or {left, right} <= {"ml", "l"}


def _to_base_unit(quantity: float, unit: str) -> tuple[float, str]:
    if unit == "kg":
        return quantity * 1_000.0, "g"
    if unit == "l":
        return quantity * 1_000.0, "ml"
    return quantity, unit


def recipe_contract_errors(draft: RecipeFormDraft, *, require_complete: bool = False) -> list[str]:
    """Return deterministic contract failures without calling a model."""
    errors: list[str] = []
    ingredient_keys = {row.ingredient_key for row in draft.ingredients}
    culture_keys = {row.culture_key for row in draft.culture_profiles}
    addition_keys = {row.addition_key for row in draft.scheduled_additions}
    step_keys = {row.step_key for row in draft.process_steps}
    if require_complete:
        if not draft.name:
            errors.append("recipe name must not be blank")
        if draft.base_volume_l is None:
            errors.append("base_volume_l is required")
        if not draft.ingredients:
            errors.append("at least one ingredient is required")
    for index, ingredient in enumerate(draft.ingredients):
        if require_complete and (not ingredient.name or ingredient.quantity is None or ingredient.unit is None):
            errors.append(f"/ingredients/{index} is incomplete")
        if ingredient.material_type == "culture" and ingredient.ingredient_key not in ingredient_keys:
            errors.append(f"/ingredients/{index}/ingredient_key is unresolved")
    for index, profile in enumerate(draft.culture_profiles):
        if profile.ingredient_key is not None:
            target = next((row for row in draft.ingredients if row.ingredient_key == profile.ingredient_key), None)
            if target is None:
                errors.append(f"/culture_profiles/{index}/ingredient_key is unresolved")
            elif target.material_type != "culture":
                errors.append(f"/culture_profiles/{index}/ingredient_key must reference a culture material")
    for index, addition in enumerate(draft.scheduled_additions):
        if addition.ingredient_key is not None and addition.ingredient_key not in ingredient_keys:
            errors.append(f"/scheduled_additions/{index}/ingredient_key is unresolved")
        if require_complete and (addition.ingredient_key is None or addition.quantity is None or addition.unit is None):
            errors.append(f"/scheduled_additions/{index} is incomplete")
        if addition.trigger is not None:
            if isinstance(addition.trigger, ElapsedSinceStepTrigger) and addition.trigger.step_key not in step_keys:
                errors.append(f"/scheduled_additions/{index}/trigger/step_key is unresolved")
            if isinstance(addition.trigger, GravityDropAtLeastTrigger) and addition.trigger.step_key is not None and addition.trigger.step_key not in step_keys:
                errors.append(f"/scheduled_additions/{index}/trigger/step_key is unresolved")
    for index, step in enumerate(draft.process_steps):
        if step.linked_addition_key is not None and step.linked_addition_key not in addition_keys:
            errors.append(f"/process_steps/{index}/linked_addition_key is unresolved")
        if step.linked_culture_key is not None and step.linked_culture_key not in culture_keys:
            errors.append(f"/process_steps/{index}/linked_culture_key is unresolved")
        if isinstance(step.trigger, ElapsedSinceStepTrigger) and step.trigger.step_key not in step_keys:
            errors.append(f"/process_steps/{index}/trigger/step_key is unresolved")
    linked_additions = [step.linked_addition_key for step in draft.process_steps if step.linked_addition_key is not None]
    if len(linked_additions) != len(set(linked_additions)):
        errors.append("one scheduled addition may be linked from only one process step")
    ordered_steps = sorted(draft.process_steps, key=lambda row: (row.sequence, row.step_key))
    position = {row.step_key: index for index, row in enumerate(ordered_steps)}
    for index, addition in enumerate(draft.scheduled_additions):
        trigger = addition.trigger
        if isinstance(trigger, ElapsedSinceStepTrigger) and position.get(trigger.step_key, len(ordered_steps)) >= len(ordered_steps):
            errors.append(f"/scheduled_additions/{index}/trigger/step_key must reference an earlier step")
    for index, culture in enumerate(draft.culture_profiles):
        if culture.culture_kind in {"sourdough_starter", "mixed_culture", "wild_culture", "wild_capture"} and culture.identity_assertion == "single_strain" and not culture.catalog_reference:
            errors.append(f"/culture_profiles/{index}/identity_assertion requires catalog_reference")
    if draft.base_volume_l is not None and draft.initial_fermenter_volume_l is not None and draft.initial_fermenter_volume_l > draft.base_volume_l:
        errors.append("initial_fermenter_volume_l must not exceed base_volume_l")
    return errors


def deterministic_recipe_findings(draft: RecipeFormDraft) -> list[Finding]:
    findings: list[Finding] = []
    seen_ids: set[str] = set()
    def add(rule_id: str, path: str, severity: Literal["info", "advisory", "warning", "blocker"], domain: Literal["general", "culture", "sugar", "nutrient", "tannin", "additive", "schedule", "allergen"], rationale: str, current: Any = None, suggested: Any = None) -> None:
        finding_id = f"rule:{rule_id}:{path}"
        if finding_id in seen_ids:
            return
        seen_ids.add(finding_id)
        findings.append(Finding(finding_id=finding_id, origin="deterministic_rule", rule_id=rule_id, field_path=path, severity=severity, category="completeness", domain=domain, current_value=current, suggested_value=suggested, rationale=rationale))

    for index, ingredient in enumerate(draft.ingredients):
        path = f"/ingredients/{index}"
        if ingredient.material_type == "nutrient" and not ingredient.product:
            add("nutrient_product_unknown", path, "advisory", "nutrient", "The product name alone does not establish formulation, YAN contribution, or label dosage.")
        if ingredient.material_type == "tannin" and ingredient.tannin_detail is None:
            add("tannin_detail_missing", path, "warning", "tannin", "Tannin source, form, caffeine status, and preparation are needed before treating tannins as interchangeable.")
        if ingredient.material_type == "tannin" and ingredient.tannin_detail and ingredient.tannin_detail.caffeine_status == "yes":
            add("caffeine_disclosure", path, "advisory", "tannin", "Caffeine status is retained as an operational sensitivity disclosure.")
        if "wheat" in ingredient.allergen_tags or "gluten" in ingredient.sensitivity_tags:
            add("allergen_disclosure", path, "warning", "allergen", "Fermentation does not remove the supplied wheat/gluten disclosure.")
    for index, culture in enumerate(draft.culture_profiles):
        path = f"/culture_profiles/{index}"
        if culture.culture_kind == "sourdough_starter":
            add("sourdough_mixed_ecosystem", path, "advisory", "culture", "Treat this starter as a variable yeast/LAB ecosystem; do not infer a single strain, attenuation, YAN demand, or alcohol tolerance.")
        if culture.organism_status == "catalogued_single" and culture.catalog_reference:
            add("operator_catalogued_identity", path, "info", "culture", "Single-culture identity is operator-supplied and catalog-bound, not inferred by the model.")
        inherited = set(culture.allergen_tags) | {tag for substrate in culture.substrates for tag in substrate.allergen_tags}
        if inherited:
            add("culture_allergen_propagation", path, "warning", "allergen", "Culture substrate disclosures propagate to the recipe and brew snapshot.", sorted(inherited))
    sugar_series = {row.series_key for row in draft.scheduled_additions if row.series_kind in {"sugar_step"}}
    nutrient_series = {row.series_key for row in draft.scheduled_additions if row.series_kind in {"nutrient_feed", "nutrient_step"}}
    if sugar_series and nutrient_series and sugar_series & nutrient_series:
        add("separate_sugar_nutrient_series", "/scheduled_additions", "blocker", "schedule", "Sugar stepping and nutrient feeding require distinct series keys even when timed together.")
    return findings


def _list_identity_key(path: str, item: dict[str, Any]) -> str | None:
    if path.endswith("/ingredients"):
        return item.get("ingredient_key")
    if path.endswith("/culture_profiles"):
        return item.get("culture_key")
    if path.endswith("/scheduled_additions"):
        return item.get("addition_key")
    if path.endswith("/process_steps"):
        return item.get("step_key")
    return None


def compute_recipe_diff(before: Any, after: Any, *, fill_empty_only: bool = False) -> list[dict[str, Any]]:
    """Compute a DOM-apply diff using logical UUID keys, never row IDs."""
    if isinstance(before, BaseModel):
        before = before.model_dump(mode="json", exclude_none=False)
    if isinstance(after, BaseModel):
        after = after.model_dump(mode="json", exclude_none=False)
    changes: list[dict[str, Any]] = []

    def empty(value: Any) -> bool:
        return value is None or value == ""

    def walk(left: Any, right: Any, path: str) -> None:
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right)):
                child = f"{path}/{key.replace('~', '~0').replace('/', '~1')}"
                if key not in left:
                    changes.append({"path": child, "before": None, "after": right[key]})
                elif key not in right:
                    continue
                else:
                    walk(left[key], right[key], child)
            return
        if isinstance(left, list) and isinstance(right, list):
            indexed_left = {_list_identity_key(path, item): item for item in left if isinstance(item, dict) and _list_identity_key(path, item)}
            indexed_right = {_list_identity_key(path, item): item for item in right if isinstance(item, dict) and _list_identity_key(path, item)}
            if indexed_left or indexed_right:
                keys = {key for key in set(indexed_left) | set(indexed_right) if key is not None}
                for key in sorted(keys):
                    child = f"{path}/{key}"
                    if key not in indexed_left:
                        changes.append({"path": child, "before": None, "after": indexed_right[key]})
                    elif key in indexed_right:
                        walk(indexed_left[key], indexed_right[key], child)
                return
        if left != right and (not fill_empty_only or empty(left)):
            changes.append({"path": path or "/", "before": left, "after": right})

    walk(before, after, "")
    return changes


def _pointer_tokens(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError("JSON pointer must be empty or start with '/'")
    tokens: list[str] = []
    for raw in pointer[1:].split("/"):
        if re.search(r"~(?![01])", raw):
            raise ValueError("invalid JSON pointer escape")
        tokens.append(raw.replace("~1", "/").replace("~0", "~"))
    return tokens


def resolve_json_pointer(document: Any, pointer: str) -> Any:
    current = document
    for token in _pointer_tokens(pointer):
        if isinstance(current, dict):
            if token not in current:
                raise KeyError(pointer)
            current = current[token]
        elif isinstance(current, list):
            if token == "-":
                raise KeyError(pointer)
            if token.isdigit():
                if int(token) >= len(current):
                    raise KeyError(pointer)
                current = current[int(token)]
                continue
            matches = [
                item
                for item in current
                if isinstance(item, dict)
                and token in {
                    item.get("ingredient_key"), item.get("culture_key"),
                    item.get("addition_key"), item.get("step_key"),
                }
            ]
            if len(matches) != 1:
                raise KeyError(pointer)
            current = matches[0]
        else:
            raise KeyError(pointer)
    return current


def validate_json_pointer(pointer: str, document: Any) -> None:
    resolve_json_pointer(document, pointer)


def _duplicate_key_guard(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


T = TypeVar("T", bound=BaseModel)


def sanitized_validation_errors(exc: Exception) -> list[dict[str, str]]:
    if hasattr(exc, "errors"):
        result: list[dict[str, str]] = []
        for item in exc.errors():  # type: ignore[attr-defined]
            location = "/" + "/".join(str(part) for part in item.get("loc", ()))
            result.append({"path": location or "/", "type": str(item.get("type", "validation_error"))})
        return result
    return [{"path": "/", "type": "invalid_model_output"}]


_FINDING_CATEGORY_VALUES = frozenset({
    "completeness",
    "consistency",
    "scaling",
    "process",
    "style",
    "measurement",
    "safety",
    "grounding",
})
_FINDING_DOMAIN_VALUES = frozenset({
    "general",
    "culture",
    "sugar",
    "nutrient",
    "tannin",
    "additive",
    "schedule",
    "allergen",
})
_TANNIN_SOURCE_KIND_VALUES = frozenset({
    "black_tea",
    "green_tea",
    "oolong_tea",
    "white_tea",
    "herbal_infusion",
    "fruit_skin_or_seed",
    "oak",
    "enological_tannin",
    "other",
})


def _normalize_known_model_enum_confusions(
    value: dict[str, Any],
    context: Any = None,
) -> None:
    """Normalize only observed, lossless model metadata formatting mistakes."""
    findings = value.get("findings")
    finding_rows = findings if isinstance(findings, list) else []
    basis = value.get("change_basis")
    basis_rows = basis if isinstance(basis, list) else []
    proposal = value.get("proposal")
    if (
        isinstance(proposal, dict)
        and "target_volume_l" in proposal
        and proposal["target_volume_l"] is None
    ):
        proposal.pop("target_volume_l")
    ingredients = proposal.get("ingredients") if isinstance(proposal, dict) else None
    if isinstance(ingredients, list):
        for ingredient in ingredients:
            if not isinstance(ingredient, dict):
                continue
            tannin = ingredient.get("tannin_detail")
            if not isinstance(tannin, dict):
                continue
            source = tannin.get("source")
            if "source_kind" not in tannin and source in _TANNIN_SOURCE_KIND_VALUES:
                tannin["source_kind"] = tannin.pop("source")
            if tannin.get("preparation") == "none":
                tannin.pop("preparation")
            if tannin.get("caffeine_status") in {"decaf", "none"}:
                tannin["caffeine_status"] = "unknown"
    for row in [*finding_rows, *basis_rows]:
        if not isinstance(row, dict):
            continue
        if row.get("uncertainty") is None:
            row["uncertainty"] = ""
    for finding in finding_rows:
        if not isinstance(finding, dict):
            continue
        finding["origin"] = "model"
        finding["rule_id"] = None
        domain = finding.get("domain")
        if domain in _FINDING_CATEGORY_VALUES and domain not in _FINDING_DOMAIN_VALUES:
            finding["domain"] = "general"

    if not isinstance(context, dict):
        return
    draft = context.get("draft")

    def normalized_field_pointer(field_path: str) -> str | None:
        if field_path.startswith("/proposal/"):
            if not isinstance(proposal, dict):
                return None
            try:
                tokens = _pointer_tokens(field_path.removeprefix("/proposal"))
            except Exception:
                return None
            current: Any = proposal
            canonical_tokens: list[str] = []
            for token in tokens:
                if isinstance(current, dict):
                    if token not in current:
                        return None
                    canonical_tokens.append(token)
                    current = current[token]
                    continue
                if not isinstance(current, list):
                    return None
                if token.isdigit():
                    index = int(token)
                    if index >= len(current) or not isinstance(current[index], dict):
                        return None
                    identity = _list_identity_key(
                        "/" + "/".join(canonical_tokens), current[index]
                    )
                    if not isinstance(identity, str) or not identity:
                        return None
                    canonical_tokens.append(identity)
                    current = current[index]
                    continue
                matches = [
                    item for item in current
                    if isinstance(item, dict)
                    and token in {
                        item.get("ingredient_key"), item.get("culture_key"),
                        item.get("addition_key"), item.get("step_key"),
                    }
                ]
                if len(matches) != 1:
                    return None
                canonical_tokens.append(token)
                current = matches[0]
            candidate = "/" + "/".join(
                token.replace("~", "~0").replace("/", "~1")
                for token in canonical_tokens
            )
            try:
                validate_json_pointer(candidate, proposal)
            except Exception:
                return None
            return candidate
        if field_path.startswith("/"):
            if not isinstance(draft, dict):
                return None
            try:
                tokens = _pointer_tokens(field_path)
            except Exception:
                return None
            collections = {
                "ingredients", "culture_profiles", "scheduled_additions", "process_steps",
            }
            if len(tokens) < 3 or tokens[0] not in collections or not tokens[1].isdigit():
                return None
            rows = draft.get(tokens[0])
            index = int(tokens[1])
            if not isinstance(rows, list) or index >= len(rows) or not isinstance(rows[index], dict):
                return None
            identity = _list_identity_key("/" + tokens[0], rows[index])
            if not isinstance(identity, str) or not identity:
                return None
            canonical_tokens = [tokens[0], identity, *tokens[2:]]
            candidate = "/" + "/".join(
                token.replace("~", "~0").replace("/", "~1")
                for token in canonical_tokens
            )
            try:
                validate_json_pointer(candidate, draft)
            except Exception:
                if not isinstance(proposal, dict):
                    return None
                try:
                    validate_json_pointer(candidate, proposal)
                except Exception:
                    return None
            return candidate
        if field_path.startswith("draft."):
            field_path = field_path.removeprefix("draft.")
        if re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*(?:(?:\.[A-Za-z_][A-Za-z0-9_]*)|(?:\[\d+\]))*",
            field_path,
        ) is None:
            return None
        tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?=\])", field_path)
        candidate = "/" + "/".join(tokens)
        if not isinstance(draft, dict):
            return None
        try:
            validate_json_pointer(candidate, draft)
        except Exception:
            return None
        return candidate

    def normalize_field_paths(node: Any) -> None:
        if isinstance(node, dict):
            field_path = node.get("field_path")
            if isinstance(field_path, str):
                candidate = normalized_field_pointer(field_path)
                if candidate is not None:
                    node["field_path"] = candidate
            for child in node.values():
                normalize_field_paths(child)
        elif isinstance(node, list):
            for child in node:
                normalize_field_paths(child)

    normalize_field_paths(value)

    research_pointers: dict[str, str] = {}
    if isinstance(context, dict):
        for collection in ("indexed_reference_matches", "fresh_research"):
            entries = context.get(collection)
            if not isinstance(entries, list):
                continue
            for index, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    continue
                source_url = entry.get("source_url")
                if isinstance(source_url, str) and source_url:
                    research_pointers.setdefault(
                        source_url,
                        f"/{collection}/{index}/source_url",
                    )

    def normalize_evidence(node: Any) -> None:
        if isinstance(node, dict):
            evidence = node.get("evidence")
            if isinstance(evidence, list):
                for item in evidence:
                    if not isinstance(item, dict):
                        continue
                    pointer = item.get("pointer")
                    if not isinstance(pointer, str) or pointer.startswith("/"):
                        continue
                    if pointer in research_pointers:
                        item["pointer"] = research_pointers[pointer]
                        continue
                    candidates = ["/" + pointer]
                    field_pointer = normalized_field_pointer(pointer)
                    if field_pointer is not None:
                        candidates.insert(0, "/draft" + field_pointer)
                    if isinstance(draft, dict):
                        candidates.append("/draft/" + pointer)
                    for candidate in candidates:
                        try:
                            validate_json_pointer(candidate, context)
                        except Exception:
                            continue
                        item["pointer"] = candidate
                        break
            for child in node.values():
                normalize_evidence(child)
        elif isinstance(node, list):
            for child in node:
                normalize_evidence(child)

    normalize_evidence(value)


def parse_model_json(raw: str, model_type: type[T], *, context: Any = None) -> T:
    """Parse exactly one raw/fenced JSON object and return a validated model."""
    if not isinstance(raw, str) or not raw.strip():
        raise ContractError(["/:invalid_model_output"])
    if len(raw.encode("utf-8")) > MAX_RAW_RESPONSE_BYTES:
        raise ContractError(["/:response_too_large"])
    text = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", text, flags=re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text, object_pairs_hook=_duplicate_key_guard, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON number")))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError(["/:invalid_model_output"]) from exc
    if isinstance(value, str):
        try:
            value = json.loads(
                value.strip(),
                object_pairs_hook=_duplicate_key_guard,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON number")),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError(["/:expected_json_object"]) from exc
    if not isinstance(value, dict):
        raise ContractError(["/:expected_json_object"])
    if set(value) == {"$defs", "additionalProperties", "properties"}:
        properties = value.get("properties")
        if (
            isinstance(properties, dict)
            and "kind" in properties
            and "envelope_version" in properties
        ):
            value = properties
            if value.get("envelope_version") == {"const": 1, "type": "integer"}:
                value["envelope_version"] = 1
    _normalize_known_model_enum_confusions(value, context)
    try:
        result = model_type.model_validate(value)
    except Exception as exc:
        raise ContractError([f"{item['path']}:{item['type']}" for item in sanitized_validation_errors(exc)]) from exc
    if context is not None:
        try:
            validate_result_pointers(result, context)
        except Exception as exc:
            raise ContractError(["/evidence:context_pointer_invalid"]) from exc
    return result


def validate_result_pointers(result: BaseModel, context: Any) -> None:
    document = context.model_dump(mode="json", exclude_none=False) if isinstance(context, BaseModel) else context
    proposal = getattr(result, "proposal", None)
    proposal_document = (
        proposal.model_dump(mode="json", exclude_none=False)
        if isinstance(proposal, BaseModel)
        else None
    )

    def validate_context_pointer(
        pointer: str,
        *,
        draft_pointer: bool = False,
        proposal_pointer: bool = False,
    ) -> None:
        if not isinstance(pointer, str) or (pointer != "" and not pointer.startswith("/")):
            raise ValueError("invalid RFC 6901 pointer")
        candidates = [document]
        if draft_pointer and isinstance(document, dict) and "draft" in document:
            candidates.append(document["draft"])
        proposal_row_collections = {
            "ingredients", "culture_profiles", "scheduled_additions", "process_steps",
        }
        proposal_row_is_existing = True
        if proposal_pointer and isinstance(document, dict) and isinstance(document.get("draft"), dict):
            try:
                tokens = _pointer_tokens(pointer)
            except Exception:
                tokens = []
            if len(tokens) >= 2 and tokens[0] in proposal_row_collections:
                row_pointer = "/" + "/".join(
                    token.replace("~", "~0").replace("/", "~1")
                    for token in tokens[:2]
                )
                try:
                    validate_json_pointer(row_pointer, document["draft"])
                except Exception:
                    proposal_row_is_existing = False
        if proposal_pointer and proposal_document is not None and proposal_row_is_existing:
            candidates.append(proposal_document)
        last_error: Exception | None = None
        for candidate in candidates:
            try:
                validate_json_pointer(pointer, candidate)
                return
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise ValueError("missing pointer document")

    def inspect(value: Any, *, evidence_value: bool = False) -> None:
        if isinstance(value, EvidenceRef):
            validate_context_pointer(value.pointer)
            return
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "pointer" and isinstance(child, str):
                    validate_context_pointer(child)
                elif key == "field_path" and isinstance(child, str):
                    validate_context_pointer(child, draft_pointer=True, proposal_pointer=True)
                elif key == "evidence":
                    inspect(child, evidence_value=True)
                else:
                    inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child, evidence_value=evidence_value)
        elif evidence_value and isinstance(value, str) and value.startswith("/"):
            validate_context_pointer(value)
        elif isinstance(value, BaseModel):
            inspect(value.model_dump(mode="python", exclude_none=False))
    inspect(result)


def validate_sensitive_change_basis(result: RecipeAutofillResultV1 | RecipeRewriteResultV1, context: RecipeFormDraft) -> None:
    allowed = {record.field_path: record for record in result.change_basis}
    proposal = result.proposal.model_dump(mode="json", exclude_none=False)
    original = context.model_dump(mode="json", exclude_none=False)

    def is_sensitive(path: str) -> bool:
        parts = [part for part in path.split("/") if part]
        if not parts:
            return False
        collection = parts[0]
        if collection in {"ingredients", "scheduled_additions"}:
            return True
        if len(parts) == 2 and collection in {"culture_profiles", "process_steps"}:
            return True
        return any(
            token in path
            for token in (
                "quantity",
                "gravity",
                "ph",
                "abv",
                "temperature",
                "minutes",
                "hydration",
                "unit",
            )
        )

    def is_absent_leaf(value: Any) -> bool:
        # Treat None, blank strings, and empty collections as absent.  Preserve
        # valid zero/False and other typed primitive values as populated so the
        # inserted-row validator still demands exact evidence for them.
        if value is None:
            return True
        if isinstance(value, bool):
            return False
        if isinstance(value, str):
            return value.strip() == ""
        if isinstance(value, (list, tuple, set, dict)):
            return len(value) == 0
        return False

    def leaf_paths(path: str, value: Any) -> list[str]:
        # Enumerate typed leaves that carry an actual proposed value.  Skip
        # optional absent values (None, blank strings, empty collections) in
        # newly inserted rows so callers do not need exact evidence for fields
        # that were deliberately omitted.  Skip entity-key fields that identify
        # the row but do not propose a sensitive value.
        skip_keys = {
            "ingredient_key",
            "culture_key",
            "addition_key",
            "step_key",
            "client_key",
            "series_key",
        }
        if isinstance(value, dict):
            paths: list[str] = []
            for key in sorted(value):
                if key in skip_keys:
                    continue
                child_path = f"{path}/{key.replace('~', '~0').replace('/', '~1')}"
                child = value[key]
                if isinstance(child, dict):
                    paths.extend(leaf_paths(child_path, child))
                elif isinstance(child, list):
                    # Recurse into list-of-dict rows with indexed paths so
                    # evidence can be supplied per item.  Empty lists are
                    # absent and contribute no leaves.
                    for index, item in enumerate(child):
                        if isinstance(item, dict):
                            paths.extend(leaf_paths(f"{child_path}/{index}", item))
                        elif is_absent_leaf(item):
                            continue
                        else:
                            paths.append(f"{child_path}/{index}")
                elif is_absent_leaf(child):
                    continue
                else:
                    paths.append(child_path)
            return paths
        return [path]

    def required_paths(change: dict[str, Any]) -> list[str]:
        path = change["path"]
        parts = [part for part in path.split("/") if part]
        if (
            change.get("before") is None
            and isinstance(change.get("after"), dict)
            and len(parts) >= 2
            and parts[0] in {"ingredients", "culture_profiles", "scheduled_additions", "process_steps"}
        ):
            return leaf_paths(path, change["after"])
        return [path] if is_sensitive(path) else []

    for change in compute_recipe_diff(original, proposal):
        for path in required_paths(change):
            record = allowed.get(path)
            if record is None:
                raise ContractError([f"{path}:missing_suggestion_basis"])
            if record.basis == "user_value":
                raise ContractError([f"{path}:user_value_provenance_unavailable"])
            if not record.evidence:
                raise ContractError([f"{path}:missing_evidence"])
            if record.basis == "inference" and not record.uncertainty.strip():
                raise ContractError([f"{path}:missing_uncertainty"])


ASSISTANT_JOBS_DDL = """
CREATE TABLE IF NOT EXISTS assistant_jobs (
    id TEXT PRIMARY KEY,
    client_request_id TEXT NOT NULL UNIQUE,
    parent_job_id TEXT,
    kind TEXT NOT NULL CHECK(kind IN ('chat','recipe_autofill','recipe_audit','recipe_rewrite','brew_analyze','brew_event_draft','archive_compare')),
    surface TEXT NOT NULL CHECK(surface IN ('recipe','brew')),
    status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed')),
    conversation_id TEXT,
    recipe_id INTEGER,
    recipe_revision INTEGER,
    brew_run_id INTEGER,
    device_id TEXT,
    draft_hash TEXT,
    request_json TEXT NOT NULL,
    context_json TEXT NOT NULL,
    context_hash TEXT NOT NULL,
    raw_response TEXT,
    result_json TEXT,
    parse_errors_json TEXT NOT NULL DEFAULT '[]',
    tool_calls_json TEXT NOT NULL DEFAULT '[]',
    research_status_json TEXT NOT NULL DEFAULT '{}',
    model_calls_json TEXT NOT NULL DEFAULT '[]',
    approval_decision TEXT CHECK(approval_decision IN ('approved','partial','rejected')),
    approved_finding_ids_json TEXT NOT NULL DEFAULT '[]',
    approved_at TEXT,
    created_at TEXT NOT NULL,
    queued_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    queue_wait_ms INTEGER,
    model_latency_ms INTEGER,
    model TEXT,
    error_code TEXT,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_assistant_jobs_status_created ON assistant_jobs(status, created_at, id);
CREATE INDEX IF NOT EXISTS idx_assistant_jobs_parent ON assistant_jobs(parent_job_id, created_at, id);
"""

ASSISTANT_WORKFLOW_DDL = """
CREATE TABLE IF NOT EXISTS assistant_job_stages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    stage TEXT NOT NULL CHECK(stage IN ('admission','research','model','persist')),
    status TEXT NOT NULL CHECK(status IN ('pending','running','succeeded','failed','skipped')),
    attempt INTEGER NOT NULL DEFAULT 1 CHECK(attempt > 0),
    started_at TEXT,
    finished_at TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(job_id, stage, attempt)
);
CREATE INDEX IF NOT EXISTS idx_assistant_job_stages_job ON assistant_job_stages(job_id, id);
"""


class AssistantJobStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(ASSISTANT_JOBS_DDL)
            conn.executescript(ASSISTANT_WORKFLOW_DDL)

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        def decode(name: str, default: Any) -> Any:
            value = row[name]
            if value is None:
                return default
            try:
                return json.loads(value)
            except (TypeError, ValueError, json.JSONDecodeError):
                return default
        return {
            "job_id": row["id"],
            "client_request_id": row["client_request_id"],
            "parent_job_id": row["parent_job_id"],
            "kind": row["kind"],
            "surface": row["surface"],
            "status": row["status"],
            "conversation_id": row["conversation_id"],
            "scope": {"recipe_id": row["recipe_id"], "recipe_revision": row["recipe_revision"], "brew_run_id": row["brew_run_id"], "device_id": row["device_id"]},
            "request": decode("request_json", {}),
            "context": decode("context_json", {}),
            "draft_hash": row["draft_hash"],
            "context_hash": row["context_hash"],
            "created_at": row["created_at"],
            "queued_at": row["queued_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "queue_wait_ms": row["queue_wait_ms"],
            "model_latency_ms": row["model_latency_ms"],
            "model": row["model"],
            "result": decode("result_json", None),
            "raw_response": decode("raw_response", None),
            "parse_errors": decode("parse_errors_json", []),
            "tool_calls": decode("tool_calls_json", []),
            "research_status": decode("research_status_json", {}),
            "model_calls": decode("model_calls_json", []),
            "approval_decision": row["approval_decision"],
            "approved_finding_ids": decode("approved_finding_ids_json", []),
            "approved_at": row["approved_at"],
            "error_code": row["error_code"],
            "error_message": row["error_message"],
        }

    def _with_stages(self, record: dict[str, Any]) -> dict[str, Any]:
        stages = self.list_stages(record["job_id"])
        record["stages"] = stages
        record["current_stage"] = next(
            (stage["stage"] for stage in stages if stage["status"] == "running"),
            next((stage["stage"] for stage in stages if stage["status"] == "pending"), None),
        )
        return record

    def list_stages(self, job_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT stage,status,attempt,started_at,finished_at,detail_json
                   FROM assistant_job_stages WHERE job_id=? ORDER BY id""",
                (job_id,),
            ).fetchall()
        stages: list[dict[str, Any]] = []
        for row in rows:
            try:
                detail = json.loads(row["detail_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                detail = {}
            stages.append(
                {
                    "stage": row["stage"],
                    "status": row["status"],
                    "attempt": row["attempt"],
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "detail": detail if isinstance(detail, dict) else {},
                }
            )
        return stages

    @staticmethod
    def _stage_detail(detail: dict[str, Any] | None) -> str:
        encoded = canonical_json(detail or {})
        if len(encoded.encode("utf-8")) > 16 * 1024:
            raise ContractError(["workflow_stage_detail_too_large"])
        return encoded

    def start_stage(self, job_id: str, stage: str) -> bool:
        if stage not in WORKFLOW_STAGE_NAMES:
            raise ContractError(["unknown_workflow_stage"])
        now = _utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE assistant_job_stages SET status='running',started_at=?
                   WHERE job_id=? AND stage=? AND status='pending'""",
                (now, job_id, stage),
            )
        return cursor.rowcount == 1

    def finish_stage(
        self,
        job_id: str,
        stage: str,
        status: Literal["succeeded", "failed", "skipped"],
        *,
        detail: dict[str, Any] | None = None,
    ) -> bool:
        if stage not in WORKFLOW_STAGE_NAMES:
            raise ContractError(["unknown_workflow_stage"])
        detail_json = self._stage_detail(detail)
        now = _utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE assistant_job_stages
                   SET status=?,finished_at=?,detail_json=?
                   WHERE job_id=? AND stage=? AND status IN ('pending','running')""",
                (status, now, detail_json, job_id, stage),
            )
        return cursor.rowcount == 1

    def fail_unfinished_stages(self, job_id: str, *, reason: str) -> None:
        detail_json = self._stage_detail({"reason": reason})
        with self._connect() as conn:
            conn.execute(
                """UPDATE assistant_job_stages
                   SET status='failed',finished_at=?,detail_json=?
                   WHERE job_id=? AND stage <> 'persist' AND status IN ('pending','running')""",
                (_utc_now(), detail_json, job_id),
            )

    def update_context(
        self,
        job_id: str,
        context: dict[str, Any],
        *,
        research_status: dict[str, str] | None = None,
    ) -> bool:
        context_json = canonical_json(context)
        if len(context_json.encode("utf-8")) > MAX_PERSISTED_JSON_BYTES:
            raise ContractError(["request_or_context_too_large"])
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE assistant_jobs
                   SET context_json=?,context_hash=?,research_status_json=?
                   WHERE id=? AND status='running'""",
                (
                    context_json,
                    canonical_hash(json.loads(context_json)),
                    canonical_json(research_status or {}),
                    job_id,
                ),
            )
        return cursor.rowcount == 1

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM assistant_jobs WHERE id=?", (job_id,)).fetchone()
        return None if row is None else self._with_stages(self._decode(row))

    def get_by_client_request_id(self, client_request_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM assistant_jobs WHERE client_request_id=?", (client_request_id,)).fetchone()
        return None if row is None else self._with_stages(self._decode(row))

    def mark_orphans(self) -> int:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """UPDATE assistant_job_stages
                   SET status='failed', finished_at=?, detail_json=?
                   WHERE status IN ('pending','running')
                     AND job_id IN (SELECT id FROM assistant_jobs WHERE status IN ('queued','running'))""",
                (now, canonical_json({"reason": "orphaned_by_restart"})),
            )
            cursor = conn.execute(
                "UPDATE assistant_jobs SET status='failed', finished_at=?, error_code='orphaned_by_restart', error_message='job was interrupted by a process restart' WHERE status IN ('queued','running')",
                (now,),
            )
        return cursor.rowcount

    def create(self, request: AssistantJobRequest, context: dict[str, Any], *, draft_hash: str | None = None) -> tuple[dict[str, Any], bool]:
        existing = self.get_by_client_request_id(request.client_request_id)
        if existing is not None:
            return existing, False
        job_id = str(uuid.uuid4())
        now = _utc_now()
        request_json = canonical_json(request)
        context_json = canonical_json(context)
        if len(request_json.encode("utf-8")) > MAX_PERSISTED_JSON_BYTES or len(context_json.encode("utf-8")) > MAX_PERSISTED_JSON_BYTES:
            raise ContractError(["request_or_context_too_large"])
        scope = request.scope
        with self._connect() as conn:
            try:
                conn.execute(
                    """INSERT INTO assistant_jobs(
                        id,client_request_id,parent_job_id,kind,surface,status,conversation_id,
                        recipe_id,recipe_revision,brew_run_id,device_id,draft_hash,request_json,
                        context_json,context_hash,created_at,queued_at
                    ) VALUES (?,?,?,?,?,'queued',?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        job_id,
                        request.client_request_id,
                        request.parent_job_id,
                        request.kind,
                        request.surface,
                        request.conversation_id,
                        scope.recipe_id,
                        scope.recipe_revision,
                        scope.brew_run_id,
                        scope.device_id,
                        draft_hash,
                        request_json,
                        context_json,
                        canonical_hash(json.loads(context_json)),
                        now,
                        now,
                    ),
                )
                research_requested = bool(request.research and request.message)
                conn.executemany(
                    """INSERT INTO assistant_job_stages(
                        job_id,stage,status,started_at,finished_at,detail_json
                    ) VALUES (?,?,?, ?, ?, ?)""",
                    [
                        (
                            job_id,
                            stage,
                            "succeeded" if stage == "admission" else (
                                "pending" if stage == "research" and research_requested else (
                                    "skipped" if stage == "research" else "pending"
                                )
                            ),
                            now if stage == "admission" else None,
                            now if stage == "admission" else (now if stage == "research" and not research_requested else None),
                            canonical_json(
                                {"reason": "job_accepted"}
                                if stage == "admission"
                                else ({"reason": "not_requested"} if not research_requested and stage == "research" else {})
                            ),
                        )
                        for stage in WORKFLOW_STAGE_NAMES
                    ],
                )
            except sqlite3.IntegrityError:
                existing = conn.execute("SELECT * FROM assistant_jobs WHERE client_request_id=?", (request.client_request_id,)).fetchone()
                if existing is None:
                    raise
                existing_id = existing["id"]
                duplicate = True
            else:
                existing_id = job_id
                duplicate = False
        if duplicate:
            existing_record = self.get(existing_id)
            assert existing_record is not None
            return existing_record, False
        result = self.get(job_id)
        assert result is not None
        return result, True

    def claim(self, job_id: str) -> dict[str, Any] | None:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM assistant_jobs WHERE id=? AND status='queued'", (job_id,)).fetchone()
            if row is None:
                conn.rollback()
                return None
            queued_at = row["queued_at"] or row["created_at"]
            try:
                wait_ms = max(0, int((datetime.fromisoformat(now.replace("Z", "+00:00")) - datetime.fromisoformat(queued_at.replace("Z", "+00:00"))).total_seconds() * 1000))
            except (TypeError, ValueError):
                wait_ms = None
            conn.execute("UPDATE assistant_jobs SET status='running',started_at=?,queue_wait_ms=? WHERE id=?", (now, wait_ms, job_id))
            row = conn.execute("SELECT * FROM assistant_jobs WHERE id=?", (job_id,)).fetchone()
            conn.commit()
        return None if row is None else self._with_stages(self._decode(row))

    def finish(self, job_id: str, *, result: Any = None, raw_response: Any = None, parse_errors: list[dict[str, str]] | None = None, tool_calls: list[dict[str, Any]] | None = None, research_status: dict[str, str] | None = None, model_calls: list[dict[str, Any]] | None = None, model: str | None = None, model_latency_ms: int | None = None, error_code: str | None = None, error_message: str | None = None) -> dict[str, Any] | None:
        status = "succeeded" if error_code is None else "failed"
        now = _utc_now()
        result_json = None if result is None else canonical_json(result)
        raw_json = None if raw_response is None else canonical_json(raw_response)
        if raw_json is not None and len(raw_json.encode("utf-8")) > MAX_RAW_RESPONSE_TOTAL_BYTES:
            raw_json = canonical_json({"error": "raw_response_bounded"})
        # Atomic terminal job/persist update.  The persisted 'persist' stage must
        # mirror the job's terminal status: a failed job cannot have a
        # 'succeeded' persist stage, even though the failed-job receipt was
        # itself written to the row.
        persist_status = "succeeded" if status == "succeeded" else "failed"
        persist_detail: dict[str, Any] = {"job_status": status}
        if status == "failed":
            persist_detail["receipt_kind"] = "failed_job_receipt"
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE assistant_jobs SET status=?,finished_at=?,result_json=?,raw_response=?,parse_errors_json=?,tool_calls_json=?,research_status_json=?,model_calls_json=?,model=?,model_latency_ms=?,error_code=?,error_message=? WHERE id=? AND status='running'""",
                (status, now, result_json, raw_json, canonical_json(parse_errors or []), canonical_json(tool_calls or []), canonical_json(research_status or {}), canonical_json(model_calls or []), model, model_latency_ms, error_code, error_message, job_id),
            )
            if cursor.rowcount == 1:
                conn.execute(
                    """UPDATE assistant_job_stages
                       SET status=?,finished_at=?,detail_json=?
                       WHERE job_id=? AND stage='persist' AND status IN ('pending','running')""",
                    (persist_status, now, canonical_json(persist_detail), job_id),
                )
        return self.get(job_id)

    def approve(self, job_id: str, payload: ApprovalPayload) -> dict[str, Any] | None:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM assistant_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                conn.rollback()
                return None
            if row["approval_decision"] is not None:
                conn.rollback()
                raise ContractError(["approval_already_recorded"])
            result = self._decode(row).get("result") or {}
            findings = result.get("findings", []) if isinstance(result, dict) else []
            valid = {item.get("finding_id") for item in findings if isinstance(item, dict)}
            requested = set(payload.finding_ids)
            if payload.decision == "rejected" and requested:
                conn.rollback()
                raise ContractError(["rejected_requires_empty_finding_ids"])
            if payload.decision in {"approved", "partial"} and not requested:
                conn.rollback()
                raise ContractError(["approval_requires_finding_ids"])
            if not requested <= valid:
                conn.rollback()
                raise ContractError(["unknown_finding_id"])
            conn.execute("UPDATE assistant_jobs SET approval_decision=?,approved_finding_ids_json=?,approved_at=? WHERE id=?", (payload.decision, canonical_json(sorted(requested)), now, job_id))
            row = conn.execute("SELECT * FROM assistant_jobs WHERE id=?", (job_id,)).fetchone()
            conn.commit()
        return None if row is None else self._with_stages(self._decode(row))


@dataclass
class PipelineOutcome:
    result: Any = None
    raw_response: Any = None
    parse_errors: list[dict[str, str]] | None = None
    tool_calls: list[dict[str, Any]] | None = None
    research_status: dict[str, str] | None = None
    model_calls: list[dict[str, Any]] | None = None
    model: str | None = None
    model_latency_ms: int | None = None
    error_code: str | None = None
    error_message: str | None = None


class AssistantPipeline:
    """One bounded in-process queue and one worker for synchronous urllib."""

    def __init__(self, store: AssistantJobStore, handler: Callable[[dict[str, Any]], PipelineOutcome]):
        self.store = store
        self.handler = handler
        self.queue: queue.Queue[str] = queue.Queue(maxsize=QUEUE_CAPACITY)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self.orphaned_count = self.store.mark_orphans()

    def start(self) -> None:
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="brewing-central-assistant", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def submit(self, request: AssistantJobRequest, context: dict[str, Any], *, draft_hash: str | None = None) -> tuple[dict[str, Any], bool]:
        existing = self.store.get_by_client_request_id(request.client_request_id)
        if existing is not None:
            return existing, False
        if self.queue.full():
            raise QueueFullError("assistant queue is full")
        record, created = self.store.create(request, context, draft_hash=draft_hash)
        if created:
            try:
                self.queue.put_nowait(record["job_id"])
            except queue.Full as exc:
                self.store.finish(record["job_id"], error_code="queue_saturated", error_message="assistant queue is full")
                self.store.fail_unfinished_stages(record["job_id"], reason="queue_saturated")
                raise QueueFullError("assistant queue is full") from exc
            self.start()
        return record, created

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                job = self.store.claim(job_id)
                if job is None:
                    continue
                try:
                    outcome = self.handler(job)
                except Exception:
                    outcome = PipelineOutcome(error_code="internal_error", error_message="assistant job failed")
                self.store.finish_stage(
                    job_id,
                    "model",
                    "succeeded" if outcome.error_code is None else "failed",
                    detail={"error_code": outcome.error_code} if outcome.error_code else {},
                )
                if outcome.error_code is not None:
                    self.store.fail_unfinished_stages(job_id, reason=outcome.error_code)
                self.store.start_stage(job_id, "persist")
                research_status = outcome.research_status
                if research_status is None:
                    context = job.get("context", {})
                    research_status = context.get("research_status", {}) if isinstance(context, dict) else {}
                self.store.finish(
                    job_id,
                    result=outcome.result,
                    raw_response=outcome.raw_response,
                    parse_errors=outcome.parse_errors,
                    tool_calls=outcome.tool_calls,
                    research_status=research_status,
                    model_calls=outcome.model_calls,
                    model=outcome.model,
                    model_latency_ms=outcome.model_latency_ms,
                    error_code=outcome.error_code,
                    error_message=outcome.error_message,
                )
                # The atomic terminal job/persist update inside finish() already
                # wrote the 'persist' stage with a status that mirrors the
                # job's terminal status; no separate finish_stage call is
                # needed (and would be a no-op once persist is terminal).
            finally:
                self.queue.task_done()

    def wait(self, job_id: str, timeout: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = self.store.get(job_id)
            if record is None or record["status"] in {"succeeded", "failed"}:
                return record
            time.sleep(0.05)
        return self.store.get(job_id)


__all__ = [
    "ADDITION_STAGES",
    "AssistantJobRecord",
    "AssistantJobRequest",
    "AssistantJobStore",
    "AssistantPipeline",
    "AssistantScope",
    "ApprovalPayload",
    "ArchiveCompareResultV1",
    "BrewAnalyzeResultV1",
    "BrewEventDraftResultV1",
    "ContractError",
    "CultureProfileDraft",
    "ElapsedSinceBrewStartTrigger",
    "ElapsedSinceStepTrigger",
    "Finding",
    "GravityAtOrBelowTrigger",
    "GravityDropAtLeastTrigger",
    "ManualTrigger",
    "OperatorObservationTrigger",
    "PhAtOrBelowTrigger",
    "PipelineOutcome",
    "QueueFullError",
    "RecipeAuditResultV1",
    "RecipeAutofillResultV1",
    "RecipeFormDraft",
    "RecipeIngredientDraft",
    "RecipeProcessStepDraft",
    "RecipeProposal",
    "RecipeRewriteResultV1",
    "RecipeTargetMetricsDraft",
    "ScaleRuleDraft",
    "ScheduledAdditionDraft",
    "TemperatureInRangeTrigger",
    "canonical_hash",
    "canonical_json",
    "compute_recipe_diff",
    "deterministic_recipe_findings",
    "parse_model_json",
    "recipe_contract_errors",
    "resolve_json_pointer",
    "sanitized_validation_errors",
    "validate_json_pointer",
    "validate_result_pointers",
    "validate_sensitive_change_basis",
]
