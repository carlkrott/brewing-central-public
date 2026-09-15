# Phase 3 Research: Telemetry Battery and Timestamp Semantics

## 1. Observed Facts vs. Recommendations

### Timestamps
**Observed Facts:**
- The current `app/main.py` ignores any client-provided timestamp. It generates the timestamp at ingest time: `ts = _now_iso()`.
- Recent payloads do not contain `timestamp`, `event_id`, or `sample_id` keys. They do have `_injected_at` in `raw_json`, which looks like a proxy or bridge timestamp.
- Legacy samples have `ts` populated with a server-ish timestamp.
- The V2 plan mentions `measured_at` (from device) and `received_at` (server time).
- Current database: `samples` table has `ts TEXT NOT NULL`.

**Recommendations:**
- **Schema Addition:** Add nullable `measured_at` (TEXT) and explicit `received_at` (TEXT) replacing or aliasing the legacy `ts` column.
- **Parser Policy:** Parse `_injected_at`, `timestamp`, `time`, or `date` from incoming JSON as `measured_at`. Fallback to `received_at` (`now(UTC)`).
- **Timezone/UTC Policy:** Reject or convert any non-UTC timezone. Enforce ISO 8601 UTC strings.
- **Skew/Range Validation:** If a parsed `measured_at` is more than 24 hours in the future or 10 years in the past, discard it or clamp it, falling back to `received_at`.
- **Effective Timestamp:** Define an effective timestamp column or query: `COALESCE(measured_at, received_at)` or `ts` logic to ensure API compatibility for existing consumers.
- **Exact Matching Index Strategy:** Create an index on `(device_id, COALESCE(measured_at, received_at))` to optimize the API window queries.

### Battery
**Observed Facts:**
- Live evidence shows recent payloads use bare battery values `3.91-3.984` (Volts) under the `battery` key.
- Legacy samples store battery as `REAL` in the database, presumably Volts.
- `app/main.py` has a Pydantic validation rule `ge=0, le=100`, which correctly bounds Voltage (though 100% works too).
- The V2 plan requires `battery_unit TEXT`. Legacy defaults to `unknown`.

**Recommendations:**
- **Schema Addition:** Add `battery_unit TEXT` (e.g., `V`, `%`, `unknown`).
- **Parser Policy & Safe Defaults:**
  - If `battery` <= 5.0, assume Volts (`V`).
  - If `battery` > 5.0 and <= 100, assume Percentage (`%`).
  - If units are explicitly sent (e.g. `battery_unit`), trust them.
- **Provenance:** Add `battery_provenance TEXT` (e.g. `firmware`, `injected`) to record where the battery reading came from.
- **Backfill:** Set legacy samples where `battery` is not null to `battery_unit='unknown'` (or `V` based on a heuristic if `battery` <= 5.0).

---

## 2. Exact Columns, Backfills, and Parser Policy

### Schema Alterations
1. **`samples` table:**
   - Add `measured_at TEXT NULL`
   - Add `received_at TEXT NOT NULL DEFAULT (datetime('now'))`
   - Add `battery_unit TEXT NOT NULL DEFAULT 'unknown'`
   - Add `battery_provenance TEXT NULL`

### Backfill Strategy
- `received_at`: `UPDATE samples SET received_at = ts;`
- `measured_at`: Leave `NULL` for legacy samples, or backfill from `_injected_at` in `raw_json` using JSON extraction if possible: `UPDATE samples SET measured_at = json_extract(raw_json, '$._injected_at') WHERE json_extract(raw_json, '$._injected_at') IS NOT NULL;`
- `battery_unit`: `UPDATE samples SET battery_unit = CASE WHEN battery <= 5.0 THEN 'V' WHEN battery > 5.0 THEN '%' ELSE 'unknown' END;`

### Compatibility Response Shape
The API response in `/api/device/{id}/samples` must remain backward compatible:
- It currently returns `ts`. It should return `ts` mapped to `COALESCE(measured_at, received_at)`.
- It should add `measured_at` and `received_at` directly in the payload.
- It should add `battery_unit` alongside `battery`.

---

## 3. Named Tests

### `tests/test_migrations.py`
- `test_migration_adds_timestamp_and_battery_columns()`
- `test_migration_backfills_legacy_battery_units_from_heuristic()`
- `test_migration_extracts_measured_at_from_raw_json()`

### `tests/test_ingest.py`
- `test_ingest_accepts_measured_at_aliases_and_formats()`
- `test_ingest_rejects_future_skew_for_measured_at()`
- `test_ingest_assigns_battery_volts_heuristic()`
- `test_ingest_assigns_battery_percent_heuristic()`
- `test_ingest_preserves_raw_injected_at()`

### `tests/test_samples.py`
- `test_samples_api_compatibility_returns_legacy_ts()`
- `test_samples_effective_timestamp_ordering()`
- `test_samples_returns_battery_unit()`

---

## 4. Edge Cases & Constraints

1. **Timestamp Collisions & Deduplication:** If `measured_at` is identical to an existing row for the same device, is it a retry? Since `event_id` is missing, only dedup if `measured_at`, `gravity`, and `battery` exactly match. Avoid blind `UNIQUE` constraints that drop legitimate rapid samples.
2. **Timezone Skew:** Firmware might send localized timestamps without offsets. Treat un-offsset timestamps as UTC, or ignore if they drift > 24 hours.
3. **Battery Float Extremes:** A battery reading of `0.0` or `< 0` should be nullified or marked as error.
4. **`_injected_at` Format:** The `_injected_at` key might be epoch seconds, ms, or ISO. The parser must gracefully handle numeric epochs and string ISO formats.

## 5. Rollback Concerns

- **SQLite Schema Alterations:** SQLite does not support dropping columns easily. A rollback requires `docker cp` of the pre-migration database backup (`ispindel.db.bak_TIMESTAMP`) into the named volume, overriding the migrated DB. The application code must be downgraded simultaneously.
- **Index Rebuilding:** Adding `measured_at` means the index on `ts` might need to be dropped and rebuilt on `COALESCE(measured_at, received_at)`. Rollback must include dropping the new index and restoring the older index if the schema dictates it.
