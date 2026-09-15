from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError

BREW_SCHEMA_VERSION = 6
SQLITE_BUSY_TIMEOUT_MS = 5000

ANNOTATION_CLASSIFICATIONS = frozenset(
    {"operator_post_brew", "tasting_outcome", "hypothesis", "annotation_clarification"}
)
ANNOTATION_ORIGINS = frozenset({"operator", "model", "deterministic_rule", "retrieval"})

OPERATING_MODES = frozenset({"stored", "preparing", "brewing"})
CAMERA_POLICIES = frozenset({"off", "active_brew_structural", "area_structural"})
DEFAULT_OPERATING_MODE = "brewing"
DEFAULT_CAMERA_POLICY = "active_brew_structural"

_NORMALIZED_UNITS = {"g", "kg", "ml", "l", "each", "pack", "tsp", "tbsp", "custom"}
_UNIT_ALIASES = {
    "g": "g",
    "gram": "g",
    "grams": "g",
    "kg": "kg",
    "kilogram": "kg",
    "kilograms": "kg",
    "ml": "ml",
    "millilitre": "ml",
    "millilitres": "ml",
    "milliliter": "ml",
    "milliliters": "ml",
    "l": "l",
    "litre": "l",
    "litres": "l",
    "liter": "l",
    "liters": "l",
    "ea": "each",
    "each": "each",
    "piece": "each",
    "pieces": "each",
    "pkg": "pack",
    "pack": "pack",
    "packs": "pack",
    "tsp": "tsp",
    "teaspoon": "tsp",
    "teaspoons": "tsp",
    "tbsp": "tbsp",
    "tablespoon": "tbsp",
    "tablespoons": "tbsp",
}


def _new_uuid4() -> str:
    return str(uuid.uuid4())


def _validate_uuid4(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("value must be a UUID v4") from exc
    if parsed.version != 4:
        raise ValueError("value must be a UUID v4")
    return str(parsed)


def _normalize_unit(value: str, unit_other: str | None = None) -> tuple[str, str | None]:
    original = value.strip()
    normalized = _UNIT_ALIASES.get(original.lower())
    if normalized is not None:
        return normalized, unit_other
    if original.lower() == "custom":
        return "custom", unit_other
    return "custom", unit_other or original


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class ScalePoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    volume_l: float = Field(ge=1.0, le=200.0)
    quantity: float = Field(ge=0.0, le=1_000_000_000.0)


class ScaleRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["linear", "fixed", "power", "piecewise"] = "linear"
    exponent: float | None = Field(default=None, ge=0.0, le=2.0)
    points: list[ScalePoint] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_rule(self) -> "ScaleRule":
        if self.mode == "power" and self.exponent is None:
            raise ValueError("power scaling requires exponent")
        if self.mode != "power" and self.exponent is not None:
            raise ValueError("exponent is only valid for power scaling")
        if self.mode == "piecewise":
            if len(self.points) < 2:
                raise ValueError("piecewise scaling requires at least two points")
            volumes = [point.volume_l for point in self.points]
            if len(volumes) != len(set(volumes)):
                raise ValueError("piecewise scaling volumes must be distinct")
        elif self.points:
            raise ValueError("points are only valid for piecewise scaling")
        return self


class RecipeIngredientPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ingredient_key: str = Field(default_factory=_new_uuid4, max_length=36)
    client_key: str | None = Field(default=None, max_length=36)
    name: str = Field(min_length=1, max_length=120)
    quantity: float = Field(ge=0.0, le=1_000_000_000.0)
    unit: str = Field(min_length=1, max_length=24)
    unit_other: str | None = Field(default=None, max_length=80)
    category: str = Field(default="other", min_length=1, max_length=64)
    material_type: Literal[
        "water", "fermentable", "culture", "nutrient", "tannin", "acid",
        "enzyme", "preservative", "fining", "flavour", "mineral",
        "packaging", "other",
    ] = "other"
    purpose: str = Field(default="", max_length=200)
    addition_stage: Literal[
        "mash", "boil", "fermenter", "secondary", "keg", "bottle", "other"
    ] | None = None
    addition_timing: str = Field(default="", max_length=200)
    scaling: ScaleRule = Field(default_factory=ScaleRule)
    product: dict[str, Any] | None = None
    provenance: str = Field(default="", max_length=1_000)
    allergen_tags: list[str] = Field(default_factory=list, max_length=16)
    other_allergen: str = Field(default="", max_length=200)
    sensitivity_tags: list[str] = Field(default_factory=list, max_length=16)
    other_sensitivity: str = Field(default="", max_length=200)
    tannin_detail: dict[str, Any] | None = None
    nutrient_detail: dict[str, Any] | None = None
    must_preparation: Literal[
        "none", "rehydrate", "dissolve", "crush", "slurry", "custom"
    ] = "none"
    preparation_other: str = Field(default="", max_length=500)
    schedule_allocation: Literal["none", "partial", "complete"] = "none"

    @field_validator("ingredient_key")
    @classmethod
    def validate_ingredient_key(cls, value: str) -> str:
        return _validate_uuid4(value)

    @field_validator("client_key")
    @classmethod
    def validate_client_key(cls, value: str | None) -> str | None:
        return None if value is None else _validate_uuid4(value)

    @field_validator("name", "unit", "category")
    @classmethod
    def strip_nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value

    @field_validator("purpose", "addition_timing", "provenance", "other_allergen", "other_sensitivity", "preparation_other")
    @classmethod
    def strip_extended_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("unit_other")
    @classmethod
    def strip_unit_other(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("allergen_tags", "sensitivity_tags")
    @classmethod
    def unique_tags(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(tag.strip() for tag in value if tag.strip()))

    @model_validator(mode="after")
    def normalize_extended_fields(self) -> "RecipeIngredientPayload":
        self.unit, self.unit_other = _normalize_unit(self.unit, self.unit_other)
        if self.tannin_detail is not None and self.material_type != "tannin":
            raise ValueError("tannin_detail requires material_type=tannin")
        if self.nutrient_detail is not None and self.material_type != "nutrient":
            raise ValueError("nutrient_detail requires material_type=nutrient")
        if self.unit == "custom" and not self.unit_other:
            raise ValueError("custom unit requires unit_other")
        return self


class RecipePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=160)
    style: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=4000)
    base_volume_l: float = Field(ge=1.0, le=200.0)
    beverage_type: Literal["beer", "wine", "mead", "cider", "kombucha", "other"] | None = None
    initial_fermenter_volume_l: float | None = Field(default=None, ge=1.0, le=200.0)
    target_metrics: dict[str, Any] | None = None
    notes: str = Field(default="", max_length=20_000)
    ingredients: list[RecipeIngredientPayload] = Field(min_length=1, max_length=128)
    culture_profiles: list[dict[str, Any]] = Field(default_factory=list, max_length=8)
    scheduled_additions: list[dict[str, Any]] = Field(default_factory=list, max_length=128)
    process_steps: list[dict[str, Any]] = Field(default_factory=list, max_length=128)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("recipe name must not be blank")
        return value

    @field_validator("style", "description", "notes")
    @classmethod
    def strip_optional(cls, value: str) -> str:
        return value.strip()

    @field_validator("target_metrics", "culture_profiles", "scheduled_additions", "process_steps")
    @classmethod
    def bounded_json_fields(cls, value: Any) -> Any:
        encoded = _json_dump(value)
        if len(encoded.encode("utf-8")) > 256_000:
            raise ValueError("recipe structured fields are too large")
        return value

    @model_validator(mode="after")
    def validate_recipe_graph(self) -> "RecipePayload":
        if self.initial_fermenter_volume_l is not None and self.initial_fermenter_volume_l > self.base_volume_l:
            raise ValueError("initial_fermenter_volume_l must not exceed base_volume_l")
        ingredient_keys = [ingredient.ingredient_key for ingredient in self.ingredients]
        if len(ingredient_keys) != len(set(ingredient_keys)):
            raise ValueError("ingredient_key values must be unique")
        addition_keys = [item.get("addition_key") for item in self.scheduled_additions if item.get("addition_key")]
        if len(addition_keys) != len(set(addition_keys)):
            raise ValueError("addition_key values must be unique")
        culture_keys = [item.get("culture_key") for item in self.culture_profiles if item.get("culture_key")]
        if len(culture_keys) != len(set(culture_keys)):
            raise ValueError("culture_key values must be unique")
        step_keys = [item.get("step_key") for item in self.process_steps if item.get("step_key")]
        if len(step_keys) != len(set(step_keys)):
            raise ValueError("step_key values must be unique")
        return self


class RecipeUpdatePayload(RecipePayload):
    expected_revision: StrictInt = Field(ge=1)


class OperatingIntentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["stored", "preparing", "brewing"]
    camera_policy: Literal["off", "active_brew_structural", "area_structural"]


class BeginBrewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(min_length=1, max_length=128)
    recipe_id: StrictInt = Field(gt=0)
    target_volume_l: float = Field(ge=1.0, le=200.0)
    notes: str = Field(default="", max_length=20_000)
    culture_lot_observations: list[dict[str, Any]] = Field(default_factory=list, max_length=8)

    @field_validator("device_id")
    @classmethod
    def strip_device_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("device id must not be blank")
        return value

    @field_validator("notes")
    @classmethod
    def strip_notes(cls, value: str) -> str:
        return value.strip()

    @field_validator("culture_lot_observations")
    @classmethod
    def bound_culture_lot_observations(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(_json_dump(value).encode("utf-8")) > 64_000:
            raise ValueError("culture lot observations are too large")
        return value


class BrewEventPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: str = Field(min_length=1, max_length=64)
    source: Literal["manual", "automated", "agent", "camera"] = "manual"
    notes: str = Field(default="", max_length=20_000)
    data: dict[str, Any] = Field(default_factory=dict)
    client_request_id: str | None = Field(default=None, max_length=36)
    occurred_at: str | None = Field(default=None, max_length=80)

    @field_validator("event_type")
    @classmethod
    def normalize_event_type(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(ch) < 32 for ch in value):
            raise ValueError("event type must be non-empty and contain no control characters")
        return value

    @field_validator("notes")
    @classmethod
    def normalize_event_notes(cls, value: str) -> str:
        return value.strip()

    @field_validator("data")
    @classmethod
    def validate_event_data(cls, value: dict[str, Any]) -> dict[str, Any]:
        encoded = _json_dump(value)
        if len(encoded.encode("utf-8")) > 32_768:
            raise ValueError("event data is too large")
        return value

    @field_validator("client_request_id")
    @classmethod
    def validate_client_request_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_uuid4(value)

    @field_validator("occurred_at")
    @classmethod
    def normalize_occurred_at(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")
        parsed = parsed.astimezone(timezone.utc)
        if (parsed - datetime.now(timezone.utc)).total_seconds() > 300:
            raise ValueError("occurred_at is too far in the future")
        return parsed.isoformat().replace("+00:00", "Z")


class StopBrewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["completed", "aborted"] = "completed"
    notes: str = Field(default="", max_length=20_000)

    @field_validator("notes")
    @classmethod
    def normalize_stop_notes(cls, value: str) -> str:
        return value.strip()


class WaterReferencePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(default="Water reference", min_length=1, max_length=120)

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("water reference label must not be blank")
        return value


class ArchiveAnnotationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    classification: Literal[
        "operator_post_brew", "tasting_outcome", "hypothesis", "annotation_clarification"
    ]
    origin: Literal["operator", "model", "deterministic_rule", "retrieval"] | None = None
    content: dict[str, Any]
    parent_annotation_id: StrictInt | None = Field(default=None, ge=1)

    @field_validator("content")
    @classmethod
    def bound_content(cls, value: dict[str, Any]) -> dict[str, Any]:
        encoded = _json_dump(value)
        if len(encoded.encode("utf-8")) > 16_384:
            raise ValueError("annotation content is too large")
        return value


class ArchiveForkPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_recipe_id: StrictInt = Field(gt=0)
    source_recipe_revision: StrictInt = Field(ge=1)
    source_brew_run_id: StrictInt = Field(gt=0)
    new_name: str = Field(min_length=1, max_length=160)
    parent_job_id: str | None = Field(default=None, max_length=64)

    @field_validator("new_name")
    @classmethod
    def strip_new_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("new_name must not be blank")
        return value

    @field_validator("parent_job_id")
    @classmethod
    def validate_parent_job_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip()
        if not candidate:
            return None
        return candidate


def _scaled_quantity(
    base_quantity: float,
    base_volume_l: float,
    target_volume_l: float,
    rule: dict[str, Any],
) -> float:
    mode = rule["mode"]
    if mode == "fixed":
        result = base_quantity
    elif mode == "linear":
        result = base_quantity * target_volume_l / base_volume_l
    elif mode == "power":
        result = base_quantity * (target_volume_l / base_volume_l) ** float(rule["exponent"])
    elif mode == "piecewise":
        points = sorted(rule["points"], key=lambda point: point["volume_l"])
        if target_volume_l <= points[0]["volume_l"]:
            result = float(points[0]["quantity"])
        elif target_volume_l >= points[-1]["volume_l"]:
            result = float(points[-1]["quantity"])
        else:
            result = 0.0
            for left, right in zip(points, points[1:]):
                if left["volume_l"] <= target_volume_l <= right["volume_l"]:
                    width = right["volume_l"] - left["volume_l"]
                    fraction = (target_volume_l - left["volume_l"]) / width
                    result = left["quantity"] + fraction * (right["quantity"] - left["quantity"])
                    break
    else:  # Defensive against corrupted stored JSON.
        raise RuntimeError(f"unsupported scaling mode in brewing database: {mode}")
    if not math.isfinite(result) or result < 0:
        raise RuntimeError("stored scaling rule produced an invalid quantity")
    return float(result)


def _parse_aware(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def derive_scheduled_addition_states(
    recipe_snapshot: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    started_at: str | None = None,
    now: datetime | None = None,
    telemetry: dict[str, Any] | None = None,
    active_calibration_id: int | str | None = None,
) -> list[dict[str, Any]]:
    """Derive advisory addition state without writing or executing anything."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    terminal: dict[str, str] = {}
    for event in events:
        data = event.get("data") if isinstance(event, dict) else None
        if not isinstance(data, dict):
            continue
        addition_key = data.get("addition_key")
        if isinstance(addition_key, str) and event.get("event_type") in {
            "scheduled_addition_recorded", "scheduled_addition_skipped"
        }:
            terminal.setdefault(
                addition_key,
                "recorded" if event["event_type"] == "scheduled_addition_recorded" else "skipped",
            )
    start = _parse_aware(started_at)
    telemetry = telemetry or {}
    results: list[dict[str, Any]] = []
    for raw in recipe_snapshot.get("scheduled_additions", []):
        if not isinstance(raw, dict):
            continue
        addition = dict(raw)
        key = addition.get("addition_key")
        state = terminal.get(key, "pending")
        reason = ""
        trigger = addition.get("trigger") or {}
        trigger_kind = trigger.get("kind", trigger.get("type")) if isinstance(trigger, dict) else None
        if state == "pending":
            if trigger_kind == "elapsed_since_brew_start" and start is not None:
                hours = float(trigger.get("hours", 0.0))
                due_at = start.timestamp() + hours * 3600.0
                due = datetime.fromtimestamp(due_at, timezone.utc)
                if current < due:
                    state = "upcoming"
                else:
                    late_hours = float(addition.get("late_window_hours") or 0.0)
                    state = "overdue" if late_hours and current > due + timedelta(hours=late_hours) else "due"
                addition["scheduled_for"] = due.isoformat().replace("+00:00", "Z")
            elif trigger_kind in {"gravity_at_or_below", "gravity_drop_at_least", "temperature_in_range"}:
                latest = telemetry.get("latest") if isinstance(telemetry.get("latest"), dict) else telemetry
                if active_calibration_id is None and trigger_kind.startswith("gravity"):
                    state, reason = "blocked", "active calibration is required"
                elif not isinstance(latest, dict):
                    state, reason = "blocked", "fresh telemetry is unavailable"
                elif trigger_kind == "gravity_at_or_below":
                    measured = latest.get("calibrated_gravity")
                    state = "condition_met" if measured is not None and float(measured) <= float(trigger.get("gravity")) else "pending"
                    if state == "pending":
                        reason = "gravity condition is not met"
                elif trigger_kind == "gravity_drop_at_least":
                    drop = latest.get("gravity_drop")
                    state = "condition_met" if drop is not None and float(drop) >= float(trigger.get("delta", 0.0)) else "pending"
                    if state == "pending":
                        reason = "gravity-drop condition is not met"
                else:
                    temperature = latest.get("temperature_c")
                    state = "condition_met" if temperature is not None and float(trigger.get("min_c", -999)) <= float(temperature) <= float(trigger.get("max_c", 999)) else "pending"
                    if state == "pending":
                        reason = "temperature condition is not met"
            elif trigger_kind == "ph_at_or_below":
                state, reason = "pending", "pH requires a validated manual measurement"
            elif trigger_kind in {"manual", "operator_observation", None}:
                state = "due"
        addition["state"] = state
        if reason:
            addition["state_reason"] = reason
        results.append(addition)
    return results


class BrewingStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

    @classmethod
    def _add_column(
        cls,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        if column not in cls._columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _migrate_v2_to_v3(self, conn: sqlite3.Connection) -> None:
        """Upgrade the already-shipped brew schema without losing rows."""
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._add_column(conn, "brew_schema", "applied_at", "TEXT")
            self._add_column(conn, "recipes", "beverage_type", "TEXT")
            self._add_column(conn, "recipes", "initial_fermenter_volume_l", "REAL")
            self._add_column(conn, "recipes", "target_metrics_json", "TEXT NOT NULL DEFAULT '{}'")

            # ingredient_key is intentionally added nullable first so legacy
            # rows can be assigned stable UUIDs before the unique index lands.
            self._add_column(conn, "recipe_ingredients", "ingredient_key", "TEXT")
            self._add_column(conn, "recipe_ingredients", "client_key", "TEXT")
            self._add_column(conn, "recipe_ingredients", "unit_other", "TEXT")
            self._add_column(conn, "recipe_ingredients", "material_type", "TEXT NOT NULL DEFAULT 'other'")
            self._add_column(conn, "recipe_ingredients", "purpose", "TEXT NOT NULL DEFAULT ''")
            self._add_column(conn, "recipe_ingredients", "addition_stage", "TEXT")
            self._add_column(conn, "recipe_ingredients", "addition_timing", "TEXT NOT NULL DEFAULT ''")
            self._add_column(conn, "recipe_ingredients", "product_json", "TEXT NOT NULL DEFAULT '{}'")
            self._add_column(conn, "recipe_ingredients", "provenance", "TEXT NOT NULL DEFAULT ''")
            self._add_column(conn, "recipe_ingredients", "allergen_tags_json", "TEXT NOT NULL DEFAULT '[]'")
            self._add_column(conn, "recipe_ingredients", "other_allergen", "TEXT NOT NULL DEFAULT ''")
            self._add_column(conn, "recipe_ingredients", "sensitivity_tags_json", "TEXT NOT NULL DEFAULT '[]'")
            self._add_column(conn, "recipe_ingredients", "other_sensitivity", "TEXT NOT NULL DEFAULT ''")
            self._add_column(conn, "recipe_ingredients", "tannin_detail_json", "TEXT")
            self._add_column(conn, "recipe_ingredients", "nutrient_detail_json", "TEXT")
            self._add_column(conn, "recipe_ingredients", "must_preparation", "TEXT NOT NULL DEFAULT 'none'")
            self._add_column(conn, "recipe_ingredients", "preparation_other", "TEXT NOT NULL DEFAULT ''")
            self._add_column(conn, "recipe_ingredients", "schedule_allocation", "TEXT NOT NULL DEFAULT 'none'")

            rows = conn.execute(
                "SELECT id,unit,unit_other,ingredient_key FROM recipe_ingredients ORDER BY id"
            ).fetchall()
            used: set[str] = set()
            for row in rows:
                key = row["ingredient_key"]
                try:
                    key = _validate_uuid4(key) if key else None
                except ValueError:
                    key = None
                if key is None or key in used:
                    key = _new_uuid4()
                    while key in used:
                        key = _new_uuid4()
                used.add(key)
                unit, unit_other = _normalize_unit(row["unit"], row["unit_other"])
                conn.execute(
                    "UPDATE recipe_ingredients SET ingredient_key=?,unit=?,unit_other=? WHERE id=?",
                    (key, unit, unit_other, row["id"]),
                )

            self._add_column(conn, "brew_runs", "brew_inputs_json", "TEXT NOT NULL DEFAULT '{}'")
            self._add_column(conn, "brew_events", "client_request_id", "TEXT")
            conn.execute(
                "UPDATE recipes SET target_metrics_json='{}' WHERE target_metrics_json IS NULL"
            )
            conn.execute(
                "UPDATE brew_runs SET brew_inputs_json='{}' WHERE brew_inputs_json IS NULL"
            )
            conn.execute(
                "UPDATE brew_schema SET version=?,applied_at=? WHERE version IN (1,2)",
                (3, _now_iso()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @staticmethod
    def _ensure_v4_tables(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS device_operating_intent (
                device_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL CHECK(mode IN ('stored','preparing','brewing')),
                camera_policy TEXT NOT NULL CHECK(camera_policy IN ('off','active_brew_structural','area_structural')),
                updated_at TEXT NOT NULL,
                updated_by TEXT NOT NULL DEFAULT 'operator'
            );
            CREATE INDEX IF NOT EXISTS idx_device_operating_intent_updated
                ON device_operating_intent(updated_at DESC, device_id ASC);
            """
        )

    @staticmethod
    def _ensure_v5_tables(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS research_document_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                version_no INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                captured_at_utc TEXT NOT NULL,
                freshness_at_utc TEXT NOT NULL,
                completeness_status TEXT NOT NULL CHECK(completeness_status IN ('complete','partial','empty')),
                quality_status TEXT NOT NULL CHECK(quality_status IN ('unreviewed','usable','rejected')),
                discard_reason TEXT CHECK(discard_reason IN ('off_topic','unverified','stale','duplicate','low_quality')),
                FOREIGN KEY(document_id) REFERENCES research_documents(id) ON DELETE CASCADE,
                UNIQUE(document_id, version_no),
                UNIQUE(document_id, content_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_research_document_versions_hash
                ON research_document_versions(content_hash, document_id);
            CREATE INDEX IF NOT EXISTS idx_research_document_versions_freshness
                ON research_document_versions(freshness_at_utc DESC, document_id DESC);
            CREATE TABLE IF NOT EXISTS research_evidence_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                document_id INTEGER NOT NULL,
                version_id INTEGER NOT NULL,
                field_path TEXT NOT NULL,
                excerpt TEXT NOT NULL,
                support_status TEXT NOT NULL CHECK(support_status IN ('unreviewed','supports','contradicts','not_supporting')),
                origin TEXT NOT NULL CHECK(origin IN ('retrieval','operator','model')),
                created_at TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES assistant_jobs(id) ON DELETE CASCADE,
                FOREIGN KEY(document_id) REFERENCES research_documents(id) ON DELETE CASCADE,
                FOREIGN KEY(version_id) REFERENCES research_document_versions(id) ON DELETE CASCADE,
                UNIQUE(job_id, version_id, field_path, excerpt)
            );
            CREATE INDEX IF NOT EXISTS idx_research_evidence_links_job
                ON research_evidence_links(job_id, field_path, id);
            """
        )

    @staticmethod
    def _rebuild_research_documents_fts(conn: sqlite3.Connection) -> None:
        """Repopulate the external-content FTS5 index from the content table.

        The ``research_documents_fts`` virtual table was added lazily with
        ``CREATE VIRTUAL TABLE IF NOT EXISTS``, so rows committed before the
        FTS table/triggers existed never flowed through the AFTER INSERT
        trigger and were invisible to ``search_research``. FTS5's built-in
        ``'rebuild'`` command deterministically walks the external content
        table and re-indexes every row; running it on an already-built index
        yields the same state, so this is safe to call on every initialize().
        The brewing DB only holds research content (telemetry lives in a
        separate database), so rebuilding here cannot affect telemetry.
        """
        conn.execute(
            "INSERT INTO research_documents_fts(research_documents_fts) VALUES('rebuild')"
        )

    def _migrate_v4_to_v5(self, conn: sqlite3.Connection) -> None:
        """Add versioned research evidence metadata without rewriting source rows."""
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._ensure_v5_tables(conn)
            for row in conn.execute(
                "SELECT id,content,created_at FROM research_documents ORDER BY id"
            ).fetchall():
                content = str(row["content"] or "")
                content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                completeness = "complete" if content.strip() else "empty"
                conn.execute(
                    """INSERT OR IGNORE INTO research_document_versions(
                        document_id,version_no,content_hash,captured_at_utc,
                        freshness_at_utc,completeness_status,quality_status,discard_reason
                    ) VALUES (?,?,?,?,?,?,?,NULL)""",
                    (
                        row["id"],
                        1,
                        content_hash,
                        row["created_at"],
                        row["created_at"],
                        completeness,
                        "unreviewed",
                    ),
                )
            conn.execute(
                "UPDATE brew_schema SET version=?,applied_at=? WHERE version=4",
                (5, _now_iso()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @staticmethod
    def _ensure_v6_tables(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS archive_evidence_bundles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brew_run_id INTEGER NOT NULL UNIQUE,
                source_snapshot_json TEXT NOT NULL,
                source_events_json TEXT NOT NULL,
                evidence_hash TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                FOREIGN KEY(brew_run_id) REFERENCES brew_runs(id) ON DELETE RESTRICT
            );
            CREATE TABLE IF NOT EXISTS archive_annotations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brew_run_id INTEGER NOT NULL,
                evidence_bundle_id INTEGER NOT NULL,
                parent_annotation_id INTEGER,
                revision_no INTEGER NOT NULL,
                classification TEXT NOT NULL CHECK(classification IN
                    ('operator_post_brew','tasting_outcome','hypothesis','annotation_clarification')),
                origin TEXT NOT NULL CHECK(origin IN
                    ('operator','model','deterministic_rule','retrieval')),
                content_json TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(brew_run_id) REFERENCES brew_runs(id) ON DELETE RESTRICT,
                FOREIGN KEY(evidence_bundle_id) REFERENCES archive_evidence_bundles(id) ON DELETE RESTRICT,
                FOREIGN KEY(parent_annotation_id) REFERENCES archive_annotations(id) ON DELETE RESTRICT,
                UNIQUE(brew_run_id, revision_no)
            );
            CREATE INDEX IF NOT EXISTS idx_archive_annotations_brew_revision
                ON archive_annotations(brew_run_id, revision_no ASC);
            CREATE TABLE IF NOT EXISTS recipe_lineage (
                child_recipe_id INTEGER PRIMARY KEY,
                source_recipe_id INTEGER NOT NULL,
                source_recipe_revision INTEGER NOT NULL,
                source_brew_run_id INTEGER NOT NULL,
                parent_job_id TEXT,
                fork_kind TEXT NOT NULL CHECK(fork_kind='archive_improved_draft'),
                created_at TEXT NOT NULL,
                FOREIGN KEY(child_recipe_id) REFERENCES recipes(id) ON DELETE RESTRICT,
                FOREIGN KEY(source_recipe_id) REFERENCES recipes(id) ON DELETE RESTRICT,
                FOREIGN KEY(source_brew_run_id) REFERENCES brew_runs(id) ON DELETE RESTRICT
            );
            CREATE INDEX IF NOT EXISTS idx_recipe_lineage_source
                ON recipe_lineage(source_recipe_id, source_brew_run_id);
            """
        )

    def _migrate_v5_to_v6(self, conn: sqlite3.Connection) -> None:
        """Add immutable archive evidence, append-only annotations, and recipe lineage."""
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._ensure_v6_tables(conn)
            conn.execute(
                "UPDATE brew_schema SET version=?,applied_at=? WHERE version=5",
                (6, _now_iso()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _migrate_v3_to_v4(self, conn: sqlite3.Connection) -> None:
        """Add operator-owned operating intent without changing telemetry."""
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._ensure_v4_tables(conn)
            conn.execute(
                "UPDATE brew_schema SET version=?,applied_at=? WHERE version=3",
                (4, _now_iso()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @staticmethod
    def _ensure_v3_tables(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS recipe_culture_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipe_id INTEGER NOT NULL,
                culture_key TEXT NOT NULL,
                ingredient_key TEXT NOT NULL,
                sort_order INTEGER NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                culture_kind TEXT NOT NULL DEFAULT 'other',
                organism_status TEXT NOT NULL DEFAULT 'uncharacterized',
                payload_json TEXT NOT NULL,
                FOREIGN KEY(recipe_id) REFERENCES recipes(id) ON DELETE CASCADE,
                FOREIGN KEY(recipe_id, ingredient_key) REFERENCES recipe_ingredients(recipe_id, ingredient_key) ON DELETE RESTRICT,
                UNIQUE(recipe_id, culture_key),
                UNIQUE(recipe_id, sort_order)
            );
            CREATE TABLE IF NOT EXISTS recipe_scheduled_additions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipe_id INTEGER NOT NULL,
                addition_key TEXT NOT NULL,
                series_key TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                series_kind TEXT NOT NULL,
                ingredient_key TEXT,
                quantity REAL,
                unit TEXT,
                scaling_json TEXT NOT NULL DEFAULT '{}',
                payload_json TEXT NOT NULL,
                FOREIGN KEY(recipe_id) REFERENCES recipes(id) ON DELETE CASCADE,
                FOREIGN KEY(recipe_id, ingredient_key) REFERENCES recipe_ingredients(recipe_id, ingredient_key) ON DELETE RESTRICT,
                UNIQUE(recipe_id, addition_key),
                UNIQUE(recipe_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS recipe_process_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipe_id INTEGER NOT NULL,
                step_key TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                phase TEXT NOT NULL DEFAULT 'other',
                method TEXT NOT NULL DEFAULT 'other',
                linked_addition_key TEXT,
                linked_culture_key TEXT,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(recipe_id) REFERENCES recipes(id) ON DELETE CASCADE,
                FOREIGN KEY(recipe_id, linked_addition_key) REFERENCES recipe_scheduled_additions(recipe_id, addition_key) ON DELETE RESTRICT,
                FOREIGN KEY(recipe_id, linked_culture_key) REFERENCES recipe_culture_profiles(recipe_id, culture_key) ON DELETE RESTRICT,
                UNIQUE(recipe_id, step_key),
                UNIQUE(recipe_id, sequence)
            );
            """
        )

    @staticmethod
    def _ensure_v3_indexes(conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_recipe_ingredients_logical_key "
            "ON recipe_ingredients(recipe_id, ingredient_key)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_brew_events_client_request "
            "ON brew_events(brew_run_id, client_request_id) "
            "WHERE client_request_id IS NOT NULL"
        )

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS brew_schema (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recipes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    style TEXT NOT NULL,
                    description TEXT NOT NULL,
                    base_volume_l REAL NOT NULL CHECK(base_volume_l BETWEEN 1.0 AND 200.0),
                    beverage_type TEXT,
                    initial_fermenter_volume_l REAL,
                    target_metrics_json TEXT NOT NULL DEFAULT '{}',
                    notes TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    archived_at TEXT
                );
                CREATE TABLE IF NOT EXISTS recipe_ingredients (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recipe_id INTEGER NOT NULL,
                    sort_order INTEGER NOT NULL,
                    ingredient_key TEXT NOT NULL,
                    client_key TEXT,
                    name TEXT NOT NULL,
                    quantity REAL NOT NULL CHECK(quantity >= 0),
                    unit TEXT NOT NULL,
                    unit_other TEXT,
                    category TEXT NOT NULL,
                    material_type TEXT NOT NULL DEFAULT 'other',
                    purpose TEXT NOT NULL DEFAULT '',
                    addition_stage TEXT,
                    addition_timing TEXT NOT NULL DEFAULT '',
                    scaling_json TEXT NOT NULL,
                    product_json TEXT NOT NULL DEFAULT '{}',
                    provenance TEXT NOT NULL DEFAULT '',
                    allergen_tags_json TEXT NOT NULL DEFAULT '[]',
                    other_allergen TEXT NOT NULL DEFAULT '',
                    sensitivity_tags_json TEXT NOT NULL DEFAULT '[]',
                    other_sensitivity TEXT NOT NULL DEFAULT '',
                    tannin_detail_json TEXT,
                    nutrient_detail_json TEXT,
                    must_preparation TEXT NOT NULL DEFAULT 'none',
                    preparation_other TEXT NOT NULL DEFAULT '',
                    schedule_allocation TEXT NOT NULL DEFAULT 'none',
                    FOREIGN KEY(recipe_id) REFERENCES recipes(id) ON DELETE CASCADE,
                    UNIQUE(recipe_id, sort_order),
                    UNIQUE(recipe_id, ingredient_key)
                );
                CREATE TABLE IF NOT EXISTS brew_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL,
                    recipe_id INTEGER NOT NULL,
                    recipe_snapshot_json TEXT NOT NULL,
                    brew_inputs_json TEXT NOT NULL DEFAULT '{}',
                    target_volume_l REAL NOT NULL CHECK(target_volume_l BETWEEN 1.0 AND 200.0),
                    status TEXT NOT NULL CHECK(status IN ('active','completed','aborted')),
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    notes TEXT NOT NULL,
                    FOREIGN KEY(recipe_id) REFERENCES recipes(id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS brew_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    brew_run_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    event_at TEXT NOT NULL,
                    source TEXT NOT NULL CHECK(source IN ('manual','automated','agent','camera')),
                    notes TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    client_request_id TEXT,
                    FOREIGN KEY(brew_run_id) REFERENCES brew_runs(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS water_references (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    angle REAL NOT NULL,
                    temperature_c REAL,
                    gravity_reference REAL NOT NULL CHECK(gravity_reference = 1.0),
                    source_sample_id INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assistant_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                    surface TEXT NOT NULL CHECK(surface IN ('recipe','brew')),
                    message TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    model TEXT,
                    tool_calls_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
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
                    error_message TEXT,
                    FOREIGN KEY(recipe_id) REFERENCES recipes(id) ON DELETE SET NULL,
                    FOREIGN KEY(brew_run_id) REFERENCES brew_runs(id) ON DELETE SET NULL
                );
                CREATE TABLE IF NOT EXISTS research_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_kind TEXT NOT NULL CHECK(source_kind IN ('searxng','kiwix','agent_tool')),
                    source_url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS research_documents_fts USING fts5(
                    title,
                    content,
                    source_url UNINDEXED,
                    content='research_documents',
                    content_rowid='id'
                );
                CREATE TRIGGER IF NOT EXISTS research_documents_ai AFTER INSERT ON research_documents BEGIN
                    INSERT INTO research_documents_fts(rowid,title,content,source_url)
                    VALUES (new.id,new.title,new.content,new.source_url);
                END;
                CREATE TRIGGER IF NOT EXISTS research_documents_ad AFTER DELETE ON research_documents BEGIN
                    INSERT INTO research_documents_fts(research_documents_fts,rowid,title,content,source_url)
                    VALUES ('delete',old.id,old.title,old.content,old.source_url);
                END;
                CREATE TRIGGER IF NOT EXISTS research_documents_au AFTER UPDATE ON research_documents BEGIN
                    INSERT INTO research_documents_fts(research_documents_fts,rowid,title,content,source_url)
                    VALUES ('delete',old.id,old.title,old.content,old.source_url);
                    INSERT INTO research_documents_fts(rowid,title,content,source_url)
                    VALUES (new.id,new.title,new.content,new.source_url);
                END;
                CREATE INDEX IF NOT EXISTS idx_recipes_updated
                    ON recipes(archived_at, updated_at DESC, id DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_brew_runs_one_active_device
                    ON brew_runs(device_id) WHERE status='active';
                CREATE INDEX IF NOT EXISTS idx_brew_runs_archive
                    ON brew_runs(status, started_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_brew_events_run_time
                    ON brew_events(brew_run_id, event_at ASC, id ASC);
                CREATE INDEX IF NOT EXISTS idx_water_references_device_time
                    ON water_references(device_id, observed_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_assistant_messages_conversation_time
                    ON assistant_messages(conversation_id, created_at ASC, id ASC);
                CREATE INDEX IF NOT EXISTS idx_assistant_jobs_status_created
                    ON assistant_jobs(status, created_at, id);
                CREATE INDEX IF NOT EXISTS idx_assistant_jobs_parent
                    ON assistant_jobs(parent_job_id, created_at, id);
                CREATE INDEX IF NOT EXISTS idx_research_documents_time
                    ON research_documents(created_at DESC, id DESC);
                """
            )
            versions = [row[0] for row in conn.execute("SELECT version FROM brew_schema")]
            if not versions:
                conn.execute(
                    "INSERT INTO brew_schema(version, applied_at) VALUES (?, ?)",
                    (BREW_SCHEMA_VERSION, _now_iso()),
                )
            elif versions in ([1], [2]):
                self._migrate_v2_to_v3(conn)
                self._migrate_v3_to_v4(conn)
                self._migrate_v4_to_v5(conn)
                self._migrate_v5_to_v6(conn)
            elif versions == [3]:
                self._migrate_v3_to_v4(conn)
                self._migrate_v4_to_v5(conn)
                self._migrate_v5_to_v6(conn)
            elif versions == [4]:
                self._migrate_v4_to_v5(conn)
                self._migrate_v5_to_v6(conn)
            elif versions == [5]:
                self._migrate_v5_to_v6(conn)
            elif versions != [BREW_SCHEMA_VERSION]:
                raise RuntimeError(f"unsupported brewing schema versions: {versions}")
            self._ensure_v3_tables(conn)
            self._ensure_v3_indexes(conn)
            self._ensure_v4_tables(conn)
            self._ensure_v5_tables(conn)
            self._ensure_v6_tables(conn)
            # External-content FTS5 is created lazily by `CREATE VIRTUAL TABLE
            # IF NOT EXISTS` above, so legacy rows committed before the FTS
            # table/triggers existed never flowed through the AFTER INSERT
            # trigger. Rebuild deterministically from the content table; this
            # is idempotent and avoids touching telemetry (separate DB).
            self._rebuild_research_documents_fts(conn)
            conn.execute(f"PRAGMA user_version={BREW_SCHEMA_VERSION}")
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("brewing database foreign key check failed")

    @staticmethod
    def _recipe_from_conn(
        conn: sqlite3.Connection,
        recipe_id: int,
        volume_l: float | None = None,
    ) -> dict[str, Any] | None:
        def load(value: str | None, default: Any) -> Any:
            if value is None:
                return default
            try:
                return json.loads(value)
            except (TypeError, ValueError, json.JSONDecodeError):
                return default

        row = conn.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
        if row is None:
            return None
        ingredients = []
        target = float(volume_l if volume_l is not None else row["base_volume_l"])
        additions_by_ingredient: dict[str, list[tuple[float, str]]] = {}
        for ingredient in conn.execute(
            "SELECT * FROM recipe_ingredients WHERE recipe_id=? ORDER BY sort_order",
            (recipe_id,),
        ):
            scaling = load(ingredient["scaling_json"], {"mode": "linear"})
            ingredient_key = ingredient["ingredient_key"]
            scheduled = conn.execute(
                "SELECT quantity,unit FROM recipe_scheduled_additions "
                "WHERE recipe_id=? AND ingredient_key=? AND quantity IS NOT NULL",
                (recipe_id, ingredient_key),
            ).fetchall()
            additions_by_ingredient[ingredient_key] = [
                (float(item["quantity"]), str(item["unit"]))
                for item in scheduled
                if item["unit"]
            ]
            allocation_warning = None
            allocation_status = ingredient["schedule_allocation"] or "none"
            if allocation_status in {"complete", "partial"} and scheduled:
                compatible = all(
                    _normalize_unit(str(item["unit"]), None)[0]
                    == _normalize_unit(str(ingredient["unit"]), None)[0]
                    or _normalize_unit(str(item["unit"]), None)[0] in {"g", "kg"}
                    and _normalize_unit(str(ingredient["unit"]), None)[0] in {"g", "kg"}
                    or _normalize_unit(str(item["unit"]), None)[0] in {"ml", "l"}
                    and _normalize_unit(str(ingredient["unit"]), None)[0] in {"ml", "l"}
                    for item in scheduled
                )
                if not compatible:
                    allocation_status = "invalid"
                    allocation_warning = "scheduled additions use incompatible units"
            ingredients.append(
                {
                    "id": ingredient["id"],
                    "ingredient_key": ingredient_key,
                    "client_key": ingredient["client_key"],
                    "name": ingredient["name"],
                    "quantity": ingredient["quantity"],
                    "unit": ingredient["unit"],
                    "unit_other": ingredient["unit_other"],
                    "category": ingredient["category"],
                    "material_type": ingredient["material_type"],
                    "purpose": ingredient["purpose"],
                    "addition_stage": ingredient["addition_stage"],
                    "addition_timing": ingredient["addition_timing"],
                    "scaling": scaling,
                    "product": load(ingredient["product_json"], {}),
                    "provenance": ingredient["provenance"],
                    "allergen_tags": load(ingredient["allergen_tags_json"], []),
                    "other_allergen": ingredient["other_allergen"],
                    "sensitivity_tags": load(ingredient["sensitivity_tags_json"], []),
                    "other_sensitivity": ingredient["other_sensitivity"],
                    "tannin_detail": load(ingredient["tannin_detail_json"], None),
                    "nutrient_detail": load(ingredient["nutrient_detail_json"], None),
                    "must_preparation": ingredient["must_preparation"],
                    "preparation_other": ingredient["preparation_other"],
                    "schedule_allocation": allocation_status,
                    "allocation_warning": allocation_warning,
                    "scaled_quantity": _scaled_quantity(
                        float(ingredient["quantity"]),
                        float(row["base_volume_l"]),
                        target,
                        scaling,
                    ),
                }
            )
        cultures = []
        for culture in conn.execute(
            "SELECT * FROM recipe_culture_profiles WHERE recipe_id=? ORDER BY sort_order",
            (recipe_id,),
        ):
            item = load(culture["payload_json"], {})
            item.update(
                {
                    "culture_key": culture["culture_key"],
                    "ingredient_key": culture["ingredient_key"],
                    "display_name": culture["display_name"],
                    "culture_kind": culture["culture_kind"],
                    "organism_status": culture["organism_status"],
                }
            )
            cultures.append(item)
        scheduled_additions = []
        series_totals: dict[str, dict[str, float | str]] = {}
        for addition in conn.execute(
            "SELECT * FROM recipe_scheduled_additions WHERE recipe_id=? ORDER BY sequence,id",
            (recipe_id,),
        ):
            item = load(addition["payload_json"], {})
            scaling = load(addition["scaling_json"], {"mode": "linear"})
            item.update(
                {
                    "addition_key": addition["addition_key"],
                    "series_key": addition["series_key"],
                    "sequence": addition["sequence"],
                    "series_kind": addition["series_kind"],
                    "ingredient_key": addition["ingredient_key"],
                    "quantity": addition["quantity"],
                    "unit": addition["unit"],
                    "scaling": scaling,
                    "scaled_quantity": (
                        _scaled_quantity(
                            float(addition["quantity"]),
                            float(row["base_volume_l"]),
                            target,
                            scaling,
                        )
                        if addition["quantity"] is not None
                        else None
                    ),
                }
            )
            scheduled_additions.append(item)
            if addition["quantity"] is not None and addition["unit"]:
                key = f"{addition['series_key']}:{addition['unit']}"
                series_totals[key] = {
                    "series_key": addition["series_key"],
                    "unit": addition["unit"],
                    "quantity": float(series_totals.get(key, {}).get("quantity", 0.0)) + float(item["scaled_quantity"] or 0.0),
                }
        process_steps = []
        for step in conn.execute(
            "SELECT * FROM recipe_process_steps WHERE recipe_id=? ORDER BY sequence,id",
            (recipe_id,),
        ):
            item = load(step["payload_json"], {})
            item.update(
                {
                    "step_key": step["step_key"],
                    "sequence": step["sequence"],
                    "phase": step["phase"],
                    "method": step["method"],
                    "linked_addition_key": step["linked_addition_key"],
                    "linked_culture_key": step["linked_culture_key"],
                }
            )
            process_steps.append(item)
        return {
            "id": row["id"],
            "name": row["name"],
            "style": row["style"],
            "description": row["description"],
            "base_volume_l": row["base_volume_l"],
            "beverage_type": row["beverage_type"],
            "initial_fermenter_volume_l": row["initial_fermenter_volume_l"],
            "target_metrics": load(row["target_metrics_json"], {}),
            "target_volume_l": target,
            "notes": row["notes"],
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "archived_at": row["archived_at"],
            "ingredients": ingredients,
            "culture_profiles": cultures,
            "scheduled_additions": scheduled_additions,
            "process_steps": process_steps,
            "series_totals": list(series_totals.values()),
        }

    @staticmethod
    def _replace_ingredients(
        conn: sqlite3.Connection,
        recipe_id: int,
        ingredients: list[RecipeIngredientPayload],
    ) -> None:
        # Child rows are removed first because their composite foreign keys
        # deliberately protect stable material identity while they exist.
        conn.execute("DELETE FROM recipe_process_steps WHERE recipe_id=?", (recipe_id,))
        conn.execute("DELETE FROM recipe_scheduled_additions WHERE recipe_id=?", (recipe_id,))
        conn.execute("DELETE FROM recipe_culture_profiles WHERE recipe_id=?", (recipe_id,))
        conn.execute("DELETE FROM recipe_ingredients WHERE recipe_id=?", (recipe_id,))
        conn.executemany(
            """
            INSERT INTO recipe_ingredients(
                recipe_id,sort_order,ingredient_key,client_key,name,quantity,unit,unit_other,
                category,material_type,purpose,addition_stage,addition_timing,scaling_json,
                product_json,provenance,allergen_tags_json,other_allergen,sensitivity_tags_json,
                other_sensitivity,tannin_detail_json,nutrient_detail_json,must_preparation,
                preparation_other,schedule_allocation
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    recipe_id,
                    index,
                    ingredient.ingredient_key,
                    ingredient.client_key,
                    ingredient.name,
                    ingredient.quantity,
                    ingredient.unit,
                    ingredient.unit_other,
                    ingredient.category,
                    ingredient.material_type,
                    ingredient.purpose,
                    ingredient.addition_stage,
                    ingredient.addition_timing,
                    _json_dump(ingredient.scaling.model_dump()),
                    _json_dump(ingredient.product or {}),
                    ingredient.provenance,
                    _json_dump(ingredient.allergen_tags),
                    ingredient.other_allergen,
                    _json_dump(ingredient.sensitivity_tags),
                    ingredient.other_sensitivity,
                    _json_dump(ingredient.tannin_detail) if ingredient.tannin_detail is not None else None,
                    _json_dump(ingredient.nutrient_detail) if ingredient.nutrient_detail is not None else None,
                    ingredient.must_preparation,
                    ingredient.preparation_other,
                    ingredient.schedule_allocation,
                )
                for index, ingredient in enumerate(ingredients)
            ],
        )

    @staticmethod
    def _child_key(value: Any, *, label: str) -> str:
        if value in (None, ""):
            return _new_uuid4()
        try:
            return _validate_uuid4(str(value))
        except ValueError as exc:
            raise ValueError(f"{label} must be a UUID v4") from exc

    @classmethod
    def _replace_recipe_children(
        cls,
        conn: sqlite3.Connection,
        recipe_id: int,
        payload: RecipePayload,
    ) -> None:
        # _replace_ingredients performs the child-to-parent deletion and
        # parent material insertion. The remaining inserts are ordered so all
        # composite foreign-key references resolve before commit.
        cls._replace_ingredients(conn, recipe_id, payload.ingredients)
        ingredient_keys = {ingredient.ingredient_key for ingredient in payload.ingredients}
        culture_keys: set[str] = set()
        for index, raw in enumerate(payload.culture_profiles):
            item = dict(raw)
            culture_key = cls._child_key(item.get("culture_key"), label="culture_key")
            ingredient_key = item.get("ingredient_key")
            if ingredient_key not in ingredient_keys:
                raise ValueError(f"culture_profiles[{index}].ingredient_key is unresolved")
            culture_keys.add(culture_key)
            payload_json = dict(item)
            payload_json.pop("client_key", None)
            conn.execute(
                """INSERT INTO recipe_culture_profiles(
                    recipe_id,culture_key,ingredient_key,sort_order,display_name,
                    culture_kind,organism_status,payload_json
                ) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    recipe_id,
                    culture_key,
                    ingredient_key,
                    index,
                    str(item.get("display_name") or item.get("name") or "")[:160],
                    str(item.get("culture_kind") or "other"),
                    str(item.get("organism_status") or "uncharacterized"),
                    _json_dump(payload_json),
                ),
            )
        addition_keys: set[str] = set()
        for index, raw in enumerate(payload.scheduled_additions):
            item = dict(raw)
            addition_key = cls._child_key(item.get("addition_key"), label="addition_key")
            series_key = cls._child_key(item.get("series_key"), label="series_key")
            ingredient_key = item.get("ingredient_key")
            if ingredient_key is not None and ingredient_key not in ingredient_keys:
                raise ValueError(f"scheduled_additions[{index}].ingredient_key is unresolved")
            sequence = int(item.get("sequence") or index + 1)
            scaling = item.get("scaling") or {"mode": "linear"}
            payload_json = dict(item)
            payload_json.pop("client_key", None)
            conn.execute(
                """INSERT INTO recipe_scheduled_additions(
                    recipe_id,addition_key,series_key,sequence,series_kind,
                    ingredient_key,quantity,unit,scaling_json,payload_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    recipe_id,
                    addition_key,
                    series_key,
                    sequence,
                    str(item.get("series_kind") or "other"),
                    ingredient_key,
                    item.get("quantity"),
                    item.get("unit"),
                    _json_dump(scaling),
                    _json_dump(payload_json),
                ),
            )
            addition_keys.add(addition_key)
        for index, raw in enumerate(payload.process_steps):
            item = dict(raw)
            step_key = cls._child_key(item.get("step_key"), label="step_key")
            linked_addition = item.get("linked_addition_key")
            linked_culture = item.get("linked_culture_key")
            if linked_addition is not None and linked_addition not in addition_keys:
                raise ValueError(f"process_steps[{index}].linked_addition_key is unresolved")
            if linked_culture is not None and linked_culture not in culture_keys:
                raise ValueError(f"process_steps[{index}].linked_culture_key is unresolved")
            sequence = int(item.get("sequence") or index + 1)
            payload_json = dict(item)
            payload_json.pop("client_key", None)
            conn.execute(
                """INSERT INTO recipe_process_steps(
                    recipe_id,step_key,sequence,phase,method,linked_addition_key,
                    linked_culture_key,payload_json
                ) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    recipe_id,
                    step_key,
                    sequence,
                    str(item.get("phase") or "other"),
                    str(item.get("method") or "other"),
                    linked_addition,
                    linked_culture,
                    _json_dump(payload_json),
                ),
            )

    def create_recipe(self, payload: RecipePayload) -> dict[str, Any]:
        now = _now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                INSERT INTO recipes(
                    name,style,description,base_volume_l,beverage_type,
                    initial_fermenter_volume_l,target_metrics_json,notes,revision,
                    created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?, ?,1,?,?)
                """,
                (
                    payload.name,
                    payload.style,
                    payload.description,
                    payload.base_volume_l,
                    payload.beverage_type,
                    payload.initial_fermenter_volume_l,
                    _json_dump(payload.target_metrics or {}),
                    payload.notes,
                    now,
                    now,
                ),
            )
            if cursor.lastrowid is None:
                conn.rollback()
                raise RuntimeError("SQLite did not return a recipe id")
            recipe_id = int(cursor.lastrowid)
            self._replace_recipe_children(conn, recipe_id, payload)
            recipe = self._recipe_from_conn(conn, recipe_id)
            conn.commit()
        assert recipe is not None
        return recipe

    def list_recipes(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            ids = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM recipes WHERE archived_at IS NULL ORDER BY updated_at DESC,id DESC"
                )
            ]
            return [self._recipe_from_conn(conn, recipe_id) for recipe_id in ids]  # type: ignore[misc]

    def get_recipe(self, recipe_id: int, volume_l: float | None = None) -> dict[str, Any] | None:
        with self._connect() as conn:
            return self._recipe_from_conn(conn, recipe_id, volume_l)

    def update_recipe(self, recipe_id: int, payload: RecipeUpdatePayload) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT revision FROM recipes WHERE id=? AND archived_at IS NULL", (recipe_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            if row["revision"] != payload.expected_revision:
                conn.rollback()
                raise ValueError("stale_revision")
            revision = int(row["revision"]) + 1
            conn.execute(
                """
                UPDATE recipes
                SET name=?,style=?,description=?,base_volume_l=?,beverage_type=?,
                    initial_fermenter_volume_l=?,target_metrics_json=?,notes=?,revision=?,updated_at=?
                WHERE id=?
                """,
                (
                    payload.name,
                    payload.style,
                    payload.description,
                    payload.base_volume_l,
                    payload.beverage_type,
                    payload.initial_fermenter_volume_l,
                    _json_dump(payload.target_metrics or {}),
                    payload.notes,
                    revision,
                    _now_iso(),
                    recipe_id,
                ),
            )
            self._replace_recipe_children(conn, recipe_id, payload)
            recipe = self._recipe_from_conn(conn, recipe_id)
            conn.commit()
        return recipe

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "brew_run_id": row["brew_run_id"],
            "event_type": row["event_type"],
            "event_at": row["event_at"],
            "source": row["source"],
            "notes": row["notes"],
            "data": json.loads(row["data_json"]),
            "client_request_id": row["client_request_id"],
        }

    @classmethod
    def _brew_from_conn(cls, conn: sqlite3.Connection, brew_id: int) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM brew_runs WHERE id=?", (brew_id,)).fetchone()
        if row is None:
            return None
        recipe_snapshot = json.loads(row["recipe_snapshot_json"])
        events = [
            cls._event_dict(event)
            for event in conn.execute(
                "SELECT * FROM brew_events WHERE brew_run_id=? ORDER BY event_at,id", (brew_id,)
            )
        ]
        scheduled_additions = derive_scheduled_addition_states(
            recipe_snapshot,
            events,
            started_at=row["started_at"],
        )
        return {
            "id": row["id"],
            "device_id": row["device_id"],
            "recipe_id": row["recipe_id"],
            "recipe_snapshot": recipe_snapshot,
            "brew_inputs": json.loads(row["brew_inputs_json"] or "{}"),
            "target_volume_l": row["target_volume_l"],
            "status": row["status"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "notes": row["notes"],
            "events": events,
            "scheduled_additions": scheduled_additions,
        }

    def begin_brew(self, payload: BeginBrewPayload) -> dict[str, Any]:
        now = _now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            recipe = self._recipe_from_conn(conn, payload.recipe_id, payload.target_volume_l)
            if recipe is None or recipe["archived_at"] is not None:
                conn.rollback()
                raise KeyError("recipe")
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO brew_runs(
                        device_id,recipe_id,recipe_snapshot_json,brew_inputs_json,
                        target_volume_l,status,started_at,ended_at,notes
                    ) VALUES (?,?,?,?,?,'active',?,NULL,?)
                    """,
                    (
                        payload.device_id,
                        payload.recipe_id,
                        _json_dump(recipe),
                        _json_dump({"culture_lot_observations": payload.culture_lot_observations}),
                        payload.target_volume_l,
                        now,
                        payload.notes,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                if "idx_brew_runs_one_active_device" in str(exc) or "brew_runs.device_id" in str(exc):
                    raise ValueError("active_conflict") from exc
                raise
            if cursor.lastrowid is None:
                conn.rollback()
                raise RuntimeError("SQLite did not return a brew id")
            brew_id = int(cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO brew_events(brew_run_id,event_type,event_at,source,notes,data_json)
                VALUES (?, 'brew_started', ?, 'manual', ?, '{}')
                """,
                (brew_id, now, payload.notes),
            )
            brew = self._brew_from_conn(conn, brew_id)
            conn.commit()
        assert brew is not None
        return brew

    def get_brew(self, brew_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            return self._brew_from_conn(conn, brew_id)

    def list_brews(self, status: str | None = None, device_id: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if device_id is not None:
            clauses.append("device_id=?")
            params.append(device_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as conn:
            ids = [
                row[0]
                for row in conn.execute(
                    f"SELECT id FROM brew_runs{where} ORDER BY started_at DESC,id DESC LIMIT 500",
                    params,
                )
            ]
            return [self._brew_from_conn(conn, brew_id) for brew_id in ids]  # type: ignore[misc]

    def count_active_brews(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM brew_runs WHERE status='active'").fetchone()
        return int(row["count"] if row else 0)

    @staticmethod
    def _default_operating_intent(device_id: str) -> dict[str, Any]:
        return {
            "device_id": device_id,
            "mode": DEFAULT_OPERATING_MODE,
            "camera_policy": DEFAULT_CAMERA_POLICY,
            "configured": False,
            "updated_at": None,
            "updated_by": None,
        }

    @staticmethod
    def _operating_intent_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "device_id": row["device_id"],
            "mode": row["mode"],
            "camera_policy": row["camera_policy"],
            "configured": True,
            "updated_at": row["updated_at"],
            "updated_by": row["updated_by"],
        }

    def get_operating_intent(self, device_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT device_id,mode,camera_policy,updated_at,updated_by "
                "FROM device_operating_intent WHERE device_id=?",
                (device_id,),
            ).fetchone()
        return self._default_operating_intent(device_id) if row is None else self._operating_intent_dict(row)

    def get_operating_intents(self, device_ids: list[str]) -> dict[str, dict[str, Any]]:
        unique_ids = list(dict.fromkeys(str(device_id) for device_id in device_ids))
        values = {device_id: self._default_operating_intent(device_id) for device_id in unique_ids}
        if not unique_ids:
            return values
        placeholders = ",".join("?" for _ in unique_ids)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT device_id,mode,camera_policy,updated_at,updated_by "
                f"FROM device_operating_intent WHERE device_id IN ({placeholders})",
                unique_ids,
            ).fetchall()
        values.update({row["device_id"]: self._operating_intent_dict(row) for row in rows})
        return values

    def set_operating_intent(
        self,
        device_id: str,
        mode: str,
        camera_policy: str,
        *,
        updated_by: str = "operator",
    ) -> dict[str, Any]:
        if mode not in OPERATING_MODES:
            raise ValueError("invalid_operating_mode")
        if camera_policy not in CAMERA_POLICIES:
            raise ValueError("invalid_camera_policy")
        if not updated_by or len(updated_by) > 80:
            raise ValueError("invalid_updated_by")
        now = _now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO device_operating_intent(device_id,mode,camera_policy,updated_at,updated_by) "
                "VALUES (?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET "
                "mode=excluded.mode,camera_policy=excluded.camera_policy,"
                "updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                (device_id, mode, camera_policy, now, updated_by),
            )
            row = conn.execute(
                "SELECT device_id,mode,camera_policy,updated_at,updated_by "
                "FROM device_operating_intent WHERE device_id=?",
                (device_id,),
            ).fetchone()
            conn.commit()
        assert row is not None
        return self._operating_intent_dict(row)

    def add_event(self, brew_id: int, payload: BrewEventPayload) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            brew = conn.execute(
                "SELECT status,recipe_snapshot_json FROM brew_runs WHERE id=?", (brew_id,)
            ).fetchone()
            if brew is None:
                conn.rollback()
                return None
            if payload.client_request_id is not None:
                prior = conn.execute(
                    "SELECT * FROM brew_events WHERE brew_run_id=? AND client_request_id=?",
                    (brew_id, payload.client_request_id),
                ).fetchone()
                if prior is not None:
                    conn.rollback()
                    return self._event_dict(prior)
            if brew["status"] != "active":
                conn.rollback()
                raise ValueError("not_active")
            event_data = dict(payload.data)
            if payload.event_type in {
                "scheduled_addition_recorded", "scheduled_addition_skipped"
            }:
                if payload.client_request_id is None:
                    conn.rollback()
                    raise ValueError("scheduled_event_requires_client_request_id")
                addition_key = event_data.get("addition_key")
                if not isinstance(addition_key, str):
                    conn.rollback()
                    raise ValueError("scheduled_event_requires_addition_key")
                try:
                    addition_key = _validate_uuid4(addition_key)
                except ValueError as exc:
                    conn.rollback()
                    raise ValueError("addition_key_invalid") from exc
                snapshot = json.loads(brew["recipe_snapshot_json"])
                addition = next(
                    (
                        item for item in snapshot.get("scheduled_additions", [])
                        if isinstance(item, dict) and item.get("addition_key") == addition_key
                    ),
                    None,
                )
                if addition is None:
                    conn.rollback()
                    raise ValueError("addition_not_found")
                existing_terminal = conn.execute(
                    "SELECT 1 FROM brew_events WHERE brew_run_id=? "
                    "AND event_type IN ('scheduled_addition_recorded','scheduled_addition_skipped') "
                    "AND json_extract(data_json,'$.addition_key')=? LIMIT 1",
                    (brew_id, addition_key),
                ).fetchone()
                if existing_terminal is not None:
                    conn.rollback()
                    raise ValueError("addition_already_terminal")
                if payload.event_type == "scheduled_addition_recorded":
                    actual_quantity = event_data.get("actual_quantity", event_data.get("quantity"))
                    actual_unit = event_data.get("actual_unit", event_data.get("unit"))
                    if isinstance(actual_quantity, bool) or not isinstance(actual_quantity, (int, float)) or not math.isfinite(float(actual_quantity)) or float(actual_quantity) < 0:
                        conn.rollback()
                        raise ValueError("recorded_event_requires_actual_quantity")
                    if not isinstance(actual_unit, str) or not actual_unit.strip():
                        conn.rollback()
                        raise ValueError("recorded_event_requires_actual_unit")
                    event_data["actual_quantity"] = float(actual_quantity)
                    event_data["actual_unit"] = actual_unit.strip()
                else:
                    reason = event_data.get("reason") or payload.notes
                    if not isinstance(reason, str) or not reason.strip():
                        conn.rollback()
                        raise ValueError("skipped_event_requires_reason")
                    event_data["reason"] = reason.strip()
                # Planned values are server-owned and come from the immutable
                # snapshot; browser/model prose cannot replace them.
                event_data.update(
                    {
                        "addition_key": addition_key,
                        "planned_quantity": addition.get("quantity"),
                        "planned_unit": addition.get("unit"),
                        "planned_scaled_quantity": addition.get("scaled_quantity"),
                    }
                )
            event_at = payload.occurred_at or _now_iso()
            cursor = conn.execute(
                """
                INSERT INTO brew_events(
                    brew_run_id,event_type,event_at,source,notes,data_json,client_request_id
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    brew_id,
                    payload.event_type,
                    event_at,
                    payload.source,
                    payload.notes,
                    _json_dump(event_data),
                    payload.client_request_id,
                ),
            )
            row = conn.execute("SELECT * FROM brew_events WHERE id=?", (cursor.lastrowid,)).fetchone()
            conn.commit()
        assert row is not None
        return self._event_dict(row)

    def stop_brew(self, brew_id: int, payload: StopBrewPayload) -> dict[str, Any] | None:
        now = _now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            brew = conn.execute("SELECT status FROM brew_runs WHERE id=?", (brew_id,)).fetchone()
            if brew is None:
                conn.rollback()
                return None
            if brew["status"] != "active":
                conn.rollback()
                raise ValueError("not_active")
            conn.execute(
                "UPDATE brew_runs SET status=?,ended_at=? WHERE id=?",
                (payload.outcome, now, brew_id),
            )
            conn.execute(
                """
                INSERT INTO brew_events(brew_run_id,event_type,event_at,source,notes,data_json)
                VALUES (?,?,?,?,?,'{}')
                """,
                (brew_id, f"brew_{payload.outcome}", now, "manual", payload.notes),
            )
            result = self._brew_from_conn(conn, brew_id)
            conn.commit()
        return result

    def record_water_reference(
        self,
        device_id: str,
        sample: dict[str, Any],
        label: str,
    ) -> dict[str, Any]:
        if sample.get("angle") is None:
            raise ValueError("sample_missing_angle")
        now = _now_iso()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO water_references(
                    device_id,observed_at,angle,temperature_c,gravity_reference,
                    source_sample_id,label,created_at
                ) VALUES (?,?,?,?,1.0,?,?,?)
                """,
                (
                    device_id,
                    sample["observed_at"],
                    sample["angle"],
                    sample.get("temperature_c"),
                    sample["sample_id"],
                    label,
                    now,
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return a water reference id")
            row = conn.execute(
                "SELECT * FROM water_references WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return dict(row)

    def list_water_references(self, device_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT * FROM water_references
                    WHERE device_id=? ORDER BY observed_at DESC,id DESC LIMIT 100
                    """,
                    (device_id,),
                )
            ]

    @staticmethod
    def _archive_event_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "brew_run_id": row["brew_run_id"],
            "event_type": row["event_type"],
            "event_at": row["event_at"],
            "source": row["source"],
            "notes": row["notes"],
            "data": json.loads(row["data_json"]),
            "client_request_id": row["client_request_id"],
        }

    @staticmethod
    def _compute_evidence_hash(
        source_snapshot_json: str, source_events_json: str
    ) -> str:
        return hashlib.sha256(
            (source_snapshot_json + "|" + source_events_json).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _archive_bundle_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "brew_run_id": row["brew_run_id"],
            "source_snapshot_json": row["source_snapshot_json"],
            "source_events_json": row["source_events_json"],
            "evidence_hash": row["evidence_hash"],
            "captured_at": row["captured_at"],
        }

    @staticmethod
    def _archive_annotation_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "brew_run_id": row["brew_run_id"],
            "evidence_bundle_id": row["evidence_bundle_id"],
            "parent_annotation_id": row["parent_annotation_id"],
            "revision_no": row["revision_no"],
            "classification": row["classification"],
            "origin": row["origin"],
            "content": json.loads(row["content_json"]),
            "content_hash": row["content_hash"],
            "created_at": row["created_at"],
        }

    def freeze_archive_evidence(self, brew_id: int) -> dict[str, Any] | None:
        """Return the deterministic evidence bundle for a completed/aborted brew.

        Repeated calls return the same row and hash. Returns None for active or
        missing brews; raises ``ValueError("brew_not_terminal")`` for active brews
        so the router can map to a stable 409.

        Atomic/idempotent under concurrent callers: the existing-row check and
        INSERT run inside a ``BEGIN IMMEDIATE`` transaction so two concurrent
        freezes serialize. The loser of a UNIQUE race re-fetches the existing
        row and returns the same bundle rather than leaking
        ``sqlite3.IntegrityError``.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM brew_runs WHERE id=?", (brew_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            if row["status"] != "completed" and row["status"] != "aborted":
                conn.rollback()
                raise ValueError("brew_not_terminal")
            existing = conn.execute(
                "SELECT * FROM archive_evidence_bundles WHERE brew_run_id=?",
                (brew_id,),
            ).fetchone()
            if existing is not None:
                conn.rollback()
                return self._archive_bundle_dict(existing)
            snapshot_row = conn.execute(
                "SELECT recipe_snapshot_json FROM brew_runs WHERE id=?",
                (brew_id,),
            ).fetchone()
            assert snapshot_row is not None
            source_snapshot_json = snapshot_row["recipe_snapshot_json"]
            events = [
                self._archive_event_dict(event)
                for event in conn.execute(
                    "SELECT * FROM brew_events WHERE brew_run_id=? ORDER BY event_at,id",
                    (brew_id,),
                )
            ]
            source_events_json = _json_dump(events)
            evidence_hash = self._compute_evidence_hash(
                source_snapshot_json, source_events_json
            )
            captured_at = _now_iso()
            try:
                cursor = conn.execute(
                    """INSERT INTO archive_evidence_bundles(
                        brew_run_id,source_snapshot_json,source_events_json,
                        evidence_hash,captured_at
                    ) VALUES (?,?,?,?,?)""",
                    (
                        brew_id,
                        source_snapshot_json,
                        source_events_json,
                        evidence_hash,
                        captured_at,
                    ),
                )
            except sqlite3.IntegrityError:
                # A concurrent caller won the race; re-fetch their row and
                # return it. The payload bytes are deterministic, so any
                # caller that observed the same brew snapshot+events will
                # compute the same hash and any caller that lost the race
                # will get the winner's identical row.
                conn.rollback()
                with self._connect() as conn2:
                    existing = conn2.execute(
                        "SELECT * FROM archive_evidence_bundles WHERE brew_run_id=?",
                        (brew_id,),
                    ).fetchone()
                if existing is None:
                    raise
                return self._archive_bundle_dict(existing)
            bundle_id = int(cursor.lastrowid)
            conn.commit()
        return {
            "id": bundle_id,
            "brew_run_id": brew_id,
            "source_snapshot_json": source_snapshot_json,
            "source_events_json": source_events_json,
            "evidence_hash": evidence_hash,
            "captured_at": captured_at,
        }

    def get_archive_evidence(self, brew_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM archive_evidence_bundles WHERE brew_run_id=?",
                (brew_id,),
            ).fetchone()
        if row is None:
            return None
        return self._archive_bundle_dict(row)

    def list_archive_annotations(self, brew_id: int) -> list[dict[str, Any]] | None:
        with self._connect() as conn:
            brew = conn.execute(
                "SELECT 1 FROM brew_runs WHERE id=?", (brew_id,)
            ).fetchone()
            if brew is None:
                return None
            rows = conn.execute(
                "SELECT * FROM archive_annotations WHERE brew_run_id=? "
                "ORDER BY revision_no ASC, id ASC",
                (brew_id,),
            ).fetchall()
        return [self._archive_annotation_dict(row) for row in rows]

    def create_archive_annotation(
        self, brew_id: int, payload: ArchiveAnnotationPayload
    ) -> dict[str, Any] | None:
        """Append one operator-origin annotation to the brew's archive.

        Raises ``ValueError`` with stable codes the router maps to 409/422.
        Returns None if the brew does not exist.
        """
        origin = payload.origin or "operator"
        if origin != "operator":
            raise ValueError("annotation_origin_must_be_operator")
        if payload.classification not in ANNOTATION_CLASSIFICATIONS:
            raise ValueError("annotation_classification_invalid")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            brew = conn.execute(
                "SELECT id FROM brew_runs WHERE id=?", (brew_id,)
            ).fetchone()
            if brew is None:
                conn.rollback()
                return None
            bundle = conn.execute(
                "SELECT id FROM archive_evidence_bundles WHERE brew_run_id=?",
                (brew_id,),
            ).fetchone()
            if bundle is None:
                conn.rollback()
                raise ValueError("evidence_not_frozen")
            latest = conn.execute(
                "SELECT id, revision_no FROM archive_annotations "
                "WHERE brew_run_id=? ORDER BY revision_no DESC LIMIT 1",
                (brew_id,),
            ).fetchone()
            next_revision = (int(latest["revision_no"]) + 1) if latest else 1
            parent_id = payload.parent_annotation_id
            if latest is None:
                if parent_id is not None:
                    conn.rollback()
                    raise ValueError("annotation_root_must_have_no_parent")
                parent_for_insert: int | None = None
            else:
                if parent_id is None:
                    conn.rollback()
                    raise ValueError("annotation_parent_required")
                parent_row = conn.execute(
                    "SELECT id, brew_run_id FROM archive_annotations WHERE id=?",
                    (parent_id,),
                ).fetchone()
                if parent_row is None:
                    conn.rollback()
                    raise ValueError("annotation_parent_not_found")
                if parent_row["brew_run_id"] != brew_id:
                    conn.rollback()
                    raise ValueError("annotation_parent_foreign_brew")
                if int(parent_row["id"]) != int(latest["id"]):
                    conn.rollback()
                    raise ValueError("annotation_parent_not_immediately_previous")
                parent_for_insert = int(parent_row["id"])
            content_json = _json_dump(payload.content)
            content_hash = hashlib.sha256(content_json.encode("utf-8")).hexdigest()
            created_at = _now_iso()
            cursor = conn.execute(
                """INSERT INTO archive_annotations(
                    brew_run_id,evidence_bundle_id,parent_annotation_id,revision_no,
                    classification,origin,content_json,content_hash,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    brew_id,
                    int(bundle["id"]),
                    parent_for_insert,
                    next_revision,
                    payload.classification,
                    origin,
                    content_json,
                    content_hash,
                    created_at,
                ),
            )
            annotation_id = int(cursor.lastrowid)
            conn.commit()
        return {
            "id": annotation_id,
            "brew_run_id": brew_id,
            "evidence_bundle_id": int(bundle["id"]),
            "parent_annotation_id": parent_for_insert,
            "revision_no": next_revision,
            "classification": payload.classification,
            "origin": origin,
            "content": payload.content,
            "content_hash": content_hash,
            "created_at": created_at,
        }

    def get_recipe_lineage(self, recipe_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM recipe_lineage WHERE child_recipe_id=?",
                (recipe_id,),
            ).fetchone()
            if row is None:
                return None
            child = conn.execute(
                "SELECT id FROM recipes WHERE id=?", (recipe_id,)
            ).fetchone()
            if child is None:
                return None
            return {
                "child_recipe_id": row["child_recipe_id"],
                "source_recipe_id": row["source_recipe_id"],
                "source_recipe_revision": row["source_recipe_revision"],
                "source_brew_run_id": row["source_brew_run_id"],
                "parent_job_id": row["parent_job_id"],
                "fork_kind": row["fork_kind"],
                "created_at": row["created_at"],
            }

    def fork_recipe_from_archive(
        self, payload: ArchiveForkPayload
    ) -> dict[str, Any] | None:
        """Create a disconnected child recipe from a frozen brew snapshot.

        The child gets fresh UUID keys via the existing RecipePayload validation.
        The source recipe, brew and events are never mutated. Raises
        ``ValueError`` with stable codes the router maps to 409/422/404.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            brew_row = conn.execute(
                "SELECT recipe_id, recipe_snapshot_json, status FROM brew_runs WHERE id=?",
                (payload.source_brew_run_id,),
            ).fetchone()
            if brew_row is None:
                conn.rollback()
                raise ValueError("source_brew_not_found")
            if brew_row["status"] not in {"completed", "aborted"}:
                conn.rollback()
                raise ValueError("brew_not_terminal")
            snapshot = json.loads(brew_row["recipe_snapshot_json"])
            if int(brew_row["recipe_id"]) != int(payload.source_recipe_id):  # type: ignore[arg-type]
                conn.rollback()
                raise ValueError("source_recipe_id_mismatch")
            if int(snapshot.get("revision", -1)) != int(payload.source_recipe_revision):  # type: ignore[arg-type]
                conn.rollback()
                raise ValueError("source_recipe_revision_mismatch")
            child = conn.execute(
                "SELECT child_recipe_id FROM recipe_lineage WHERE source_recipe_id=? "
                "AND source_brew_run_id=?",
                (int(payload.source_recipe_id), int(payload.source_brew_run_id)),  # type: ignore[arg-type]
            ).fetchone()
            if child is not None:
                conn.rollback()
                raise ValueError("child_lineage_exists")
            fork_payload = self._snapshot_to_fork_payload(snapshot, payload.new_name)
            now = _now_iso()
            cursor = conn.execute(
                """
                INSERT INTO recipes(
                    name,style,description,base_volume_l,beverage_type,
                    initial_fermenter_volume_l,target_metrics_json,notes,revision,
                    created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?, ?,1,?,?)
                """,
                (
                    fork_payload.name,
                    fork_payload.style,
                    fork_payload.description,
                    fork_payload.base_volume_l,
                    fork_payload.beverage_type,
                    fork_payload.initial_fermenter_volume_l,
                    _json_dump(fork_payload.target_metrics or {}),
                    fork_payload.notes,
                    now,
                    now,
                ),
            )
            child_recipe_id = int(cursor.lastrowid)
            self._replace_recipe_children(conn, child_recipe_id, fork_payload)
            conn.execute(
                """INSERT INTO recipe_lineage(
                    child_recipe_id,source_recipe_id,source_recipe_revision,
                    source_brew_run_id,parent_job_id,fork_kind,created_at
                ) VALUES (?,?,?,?,?,?,?)""",
                (
                    child_recipe_id,
                    payload.source_recipe_id,
                    payload.source_recipe_revision,
                    payload.source_brew_run_id,
                    payload.parent_job_id,
                    "archive_improved_draft",
                    now,
                ),
            )
            new_recipe = self._recipe_from_conn(conn, child_recipe_id)
            conn.commit()
        assert new_recipe is not None
        return new_recipe

    @staticmethod
    def _snapshot_to_fork_payload(
        snapshot: dict[str, Any], new_name: str
    ) -> RecipePayload:
        """Translate a historical brew snapshot into a fresh RecipePayload.

        Generates new UUIDs for all stable child keys so the child recipe has
        no shared identity with the source.
        """
        ingredient_fields = set(RecipeIngredientPayload.model_fields.keys())
        ingredient_payloads: list[RecipeIngredientPayload] = []
        for raw in snapshot.get("ingredients", []):
            if not isinstance(raw, dict):
                continue
            filtered = {key: raw[key] for key in ingredient_fields if key in raw}
            filtered["ingredient_key"] = _new_uuid4()
            filtered.pop("client_key", None)
            ingredient_payloads.append(RecipeIngredientPayload.model_validate(filtered))
        fork = RecipePayload.model_validate(
            {
                "name": new_name,
                "style": snapshot.get("style", ""),
                "description": snapshot.get("description", ""),
                "base_volume_l": snapshot.get("base_volume_l", 10.0),
                "beverage_type": snapshot.get("beverage_type"),
                "initial_fermenter_volume_l": snapshot.get("initial_fermenter_volume_l"),
                "target_metrics": snapshot.get("target_metrics", {}),
                "notes": snapshot.get("notes", ""),
                "ingredients": [ing.model_dump() for ing in ingredient_payloads],
                "culture_profiles": snapshot.get("culture_profiles", []),
                "scheduled_additions": snapshot.get("scheduled_additions", []),
                "process_steps": snapshot.get("process_steps", []),
            }
        )
        return fork


def create_brewing_router(
    store: BrewingStore,
    device_exists: Callable[[str], bool],
    latest_device_sample: Callable[[str], dict[str, Any] | None],
) -> APIRouter:
    router = APIRouter()

    @router.post("/api/recipes", status_code=201)
    def create_recipe(payload: RecipePayload) -> dict[str, Any]:
        return store.create_recipe(payload)

    @router.get("/api/recipes")
    def list_recipes() -> dict[str, Any]:
        return {"recipes": store.list_recipes()}

    @router.get("/api/recipes/{recipe_id}")
    def get_recipe(
        recipe_id: int,
        volume_l: float | None = Query(default=None, ge=1.0, le=200.0),
    ) -> dict[str, Any]:
        recipe = store.get_recipe(recipe_id, volume_l)
        if recipe is None:
            raise HTTPException(status_code=404, detail="recipe not found")
        return recipe

    @router.put("/api/recipes/{recipe_id}")
    def update_recipe(recipe_id: int, payload: RecipeUpdatePayload) -> dict[str, Any]:
        try:
            recipe = store.update_recipe(recipe_id, payload)
        except ValueError as exc:
            if str(exc) == "stale_revision":
                raise HTTPException(
                    status_code=409, detail="recipe was changed by another request"
                ) from exc
            raise
        if recipe is None:
            raise HTTPException(status_code=404, detail="recipe not found")
        return recipe

    @router.post("/api/brews", status_code=201)
    def begin_brew(payload: BeginBrewPayload) -> dict[str, Any]:
        if not device_exists(payload.device_id):
            raise HTTPException(status_code=404, detail="device not found")
        try:
            return store.begin_brew(payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="recipe not found") from exc
        except ValueError as exc:
            if str(exc) == "active_conflict":
                raise HTTPException(
                    status_code=409, detail="device already has an active brew"
                ) from exc
            raise

    @router.get("/api/brews")
    def list_brews(
        status: Literal["active", "completed", "aborted"] | None = None,
        device_id: str | None = Query(default=None, min_length=1, max_length=128),
    ) -> dict[str, Any]:
        return {"brews": store.list_brews(status, device_id)}

    @router.get("/api/device/{device_id}/operating-intent")
    def get_operating_intent(device_id: str) -> dict[str, Any]:
        if not device_exists(device_id):
            raise HTTPException(status_code=404, detail="device not found")
        return store.get_operating_intent(device_id)

    @router.put("/api/device/{device_id}/operating-intent")
    def set_operating_intent(
        device_id: str, payload: OperatingIntentPayload
    ) -> dict[str, Any]:
        if not device_exists(device_id):
            raise HTTPException(status_code=404, detail="device not found")
        try:
            return store.set_operating_intent(
                device_id, payload.mode, payload.camera_policy
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/api/brews/{brew_id}")
    def get_brew(brew_id: int) -> dict[str, Any]:
        brew = store.get_brew(brew_id)
        if brew is None:
            raise HTTPException(status_code=404, detail="brew not found")
        return brew

    @router.post("/api/brews/{brew_id}/events", status_code=201)
    def add_brew_event(brew_id: int, payload: BrewEventPayload) -> dict[str, Any]:
        try:
            event = store.add_event(brew_id, payload)
        except ValueError as exc:
            if str(exc) == "not_active":
                raise HTTPException(status_code=409, detail="brew is not active") from exc
            if str(exc) in {
                "scheduled_event_requires_client_request_id",
                "scheduled_event_requires_addition_key",
                "addition_key_invalid",
                "addition_not_found",
                "addition_already_terminal",
                "recorded_event_requires_actual_quantity",
                "recorded_event_requires_actual_unit",
                "skipped_event_requires_reason",
            }:
                raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc
            raise
        if event is None:
            raise HTTPException(status_code=404, detail="brew not found")
        return event

    @router.post("/api/brews/{brew_id}/stop")
    def stop_brew(brew_id: int, payload: StopBrewPayload) -> dict[str, Any]:
        try:
            brew = store.stop_brew(brew_id, payload)
        except ValueError as exc:
            if str(exc) == "not_active":
                raise HTTPException(status_code=409, detail="brew is not active") from exc
            raise
        if brew is None:
            raise HTTPException(status_code=404, detail="brew not found")
        return brew

    @router.post("/api/device/{device_id}/water-reference", status_code=201)
    def record_water_reference(
        device_id: str, payload: WaterReferencePayload
    ) -> dict[str, Any]:
        if not device_exists(device_id):
            raise HTTPException(status_code=404, detail="device not found")
        sample = latest_device_sample(device_id)
        if sample is None:
            raise HTTPException(status_code=409, detail="device has no telemetry sample")
        try:
            return store.record_water_reference(device_id, sample, payload.label)
        except ValueError as exc:
            if str(exc) == "sample_missing_angle":
                raise HTTPException(
                    status_code=409, detail="latest sample has no tilt angle"
                ) from exc
            raise

    @router.get("/api/device/{device_id}/water-references")
    def list_water_references(device_id: str) -> dict[str, Any]:
        if not device_exists(device_id):
            raise HTTPException(status_code=404, detail="device not found")
        return {"water_references": store.list_water_references(device_id)}

    @router.post("/api/brews/{brew_id}/archive-evidence")
    def freeze_archive_evidence(brew_id: int) -> dict[str, Any]:
        try:
            bundle = store.freeze_archive_evidence(brew_id)
        except ValueError as exc:
            if str(exc) == "brew_not_terminal":
                raise HTTPException(
                    status_code=409, detail={"code": "brew_not_terminal"}
                ) from exc
            raise
        if bundle is None:
            raise HTTPException(status_code=404, detail="brew not found")
        return bundle

    @router.get("/api/brews/{brew_id}/archive-annotations")
    def list_archive_annotations(brew_id: int) -> dict[str, Any]:
        annotations = store.list_archive_annotations(brew_id)
        if annotations is None:
            raise HTTPException(status_code=404, detail="brew not found")
        return {"annotations": annotations}

    @router.post("/api/brews/{brew_id}/archive-annotations", status_code=201)
    def create_archive_annotation(
        brew_id: int, payload: ArchiveAnnotationPayload
    ) -> dict[str, Any]:
        try:
            annotation = store.create_archive_annotation(brew_id, payload)
        except ValueError as exc:
            code = str(exc)
            if code in {
                "evidence_not_frozen",
                "annotation_parent_required",
                "annotation_parent_not_found",
                "annotation_parent_foreign_brew",
                "annotation_parent_not_immediately_previous",
                "annotation_root_must_have_no_parent",
            }:
                raise HTTPException(status_code=409, detail={"code": code}) from exc
            if code in {
                "annotation_origin_must_be_operator",
                "annotation_classification_invalid",
            }:
                raise HTTPException(status_code=422, detail={"code": code}) from exc
            raise
        if annotation is None:
            raise HTTPException(status_code=404, detail="brew not found")
        return annotation

    @router.post("/api/archive/fork-recipe", status_code=201)
    def fork_recipe_from_archive(payload: ArchiveForkPayload) -> dict[str, Any]:
        try:
            new_recipe = store.fork_recipe_from_archive(payload)
        except PydanticValidationError:
            # Persisted terminal brew snapshot is malformed for the current
            # RecipePayload contract. Map to a stable 422 so callers can
            # distinguish historical corruption from generic validation
            # failures; do not catch unrelated errors broadly.
            # NOTE: pydantic.ValidationError subclasses ValueError, so this
            # handler must run BEFORE the bare ValueError handler below.
            raise HTTPException(
                status_code=422,
                detail={"code": "fork_source_snapshot_invalid"},
            )
        except ValueError as exc:
            code = str(exc)
            if code in {"brew_not_terminal", "child_lineage_exists"}:
                raise HTTPException(status_code=409, detail={"code": code}) from exc
            if code in {
                "source_recipe_id_mismatch",
                "source_recipe_revision_mismatch",
            }:
                raise HTTPException(status_code=409, detail={"code": code}) from exc
            if code == "source_brew_not_found":
                raise HTTPException(status_code=404, detail="brew not found") from exc
            raise
        if new_recipe is None:
            raise HTTPException(status_code=404, detail="brew not found")
        return new_recipe

    @router.get("/api/recipes/{recipe_id}/lineage")
    def get_recipe_lineage(recipe_id: int) -> dict[str, Any]:
        lineage = store.get_recipe_lineage(recipe_id)
        if lineage is None:
            raise HTTPException(status_code=404, detail="lineage not found")
        return lineage

    return router
