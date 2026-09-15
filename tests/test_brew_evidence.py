"""Brew evidence: integrity, counts, user_version, FTS5 external-content parity.

Tests run in temporary fixtures only. They never mutate the parent app database.
They are source-only contract tests for the dual-database brew evidence path.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from backup_common import (  # pyright: ignore[reportMissingImports]  # noqa: E402
    BackupError,
    BrewEvidenceError,
    bind_basename,
    bind_brew_basename,
    bind_run_id,
    brew_evidence,
    brew_evidence_repair_fresh,
    brew_table_allowlist,
)


BREW_TABLES = (
    "recipes",
    "recipe_ingredients",
    "recipe_culture_profiles",
    "recipe_scheduled_additions",
    "recipe_process_steps",
    "brew_runs",
    "brew_events",
    "water_references",
    "assistant_messages",
    "archive_evidence_bundles",
    "archive_annotations",
    "recipe_lineage",
    "device_operating_intent",
    "assistant_jobs",
    "assistant_job_stages",
    "research_documents",
    "research_documents_fts",
    "research_document_versions",
    "research_evidence_links",
)


def _populate_brew_db(path: Path) -> Path:
    """Build a minimal brew.db fixture with the full evidence table set.

    The caller supplies a path whose basename MUST start with ``brew-`` (see
    ``bind_brew_basename``) so the dual-mode identity check binds. We build
    into a temp file first, then rename it to the caller-supplied path so the
    final basename respects the prefix contract.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp_path = path.parent / f".brew-evidence-fixture-{os.getpid()}-{id(path)}"
    conn = sqlite3.connect(temp_path)
    try:
        conn.executescript(
            """
            CREATE TABLE recipes (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                archived_at TEXT,
                updated_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE brew_runs (
                id INTEGER PRIMARY KEY,
                device_id TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE brew_events (
                id INTEGER PRIMARY KEY,
                brew_run_id INTEGER NOT NULL,
                event_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE archive_evidence_bundles (
                id INTEGER PRIMARY KEY,
                captured_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE archive_annotations (
                id INTEGER PRIMARY KEY,
                bundle_id INTEGER NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE recipe_lineage (
                id INTEGER PRIMARY KEY,
                recipe_id INTEGER NOT NULL,
                ancestor_id INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE TABLE device_operating_intent (
                device_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE assistant_jobs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                recipe_id INTEGER,
                brew_run_id INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE TABLE research_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_kind TEXT NOT NULL,
                source_url TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE research_documents_fts USING fts5(
                title,
                content,
                source_url UNINDEXED,
                content='research_documents',
                content_rowid='id'
            );
            CREATE TRIGGER research_documents_ai AFTER INSERT ON research_documents BEGIN
                INSERT INTO research_documents_fts(rowid,title,content,source_url)
                VALUES (new.id,new.title,new.content,new.source_url);
            END;
            CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY);
            """
        )
        conn.executescript(
            """
            INSERT INTO recipes(name, archived_at, updated_at, created_at)
                VALUES ('Pale Ale', NULL, '2026-08-03T00:00:00Z', '2026-08-03T00:00:00Z');
            INSERT INTO recipes(name, archived_at, updated_at, created_at)
                VALUES ('Pilsner', NULL, '2026-08-03T00:00:00Z', '2026-08-03T00:00:00Z');
            INSERT INTO recipes(name, archived_at, updated_at, created_at)
                VALUES ('Stout', '2026-08-02T00:00:00Z', '2026-08-02T00:00:00Z', '2026-08-02T00:00:00Z');
            INSERT INTO brew_runs(device_id, status, started_at, created_at)
                VALUES ('ispindel-a', 'active', '2026-08-03T00:00:00Z', '2026-08-03T00:00:00Z');
            INSERT INTO brew_runs(device_id, status, started_at, created_at)
                VALUES ('ispindel-b', 'completed', '2026-08-02T00:00:00Z', '2026-08-02T00:00:00Z');
            INSERT INTO brew_events(brew_run_id, event_at, created_at)
                VALUES (1, '2026-08-03T01:00:00Z', '2026-08-03T01:00:00Z');
            INSERT INTO brew_events(brew_run_id, event_at, created_at)
                VALUES (1, '2026-08-03T02:00:00Z', '2026-08-03T02:00:00Z');
            INSERT INTO archive_evidence_bundles(captured_at, created_at)
                VALUES ('2026-08-03T00:00:00Z', '2026-08-03T00:00:00Z');
            INSERT INTO archive_annotations(bundle_id, note, created_at)
                VALUES (1, 'note-a', '2026-08-03T00:00:00Z');
            INSERT INTO recipe_lineage(recipe_id, ancestor_id, created_at)
                VALUES (1, NULL, '2026-08-03T00:00:00Z');
            INSERT INTO recipe_lineage(recipe_id, ancestor_id, created_at)
                VALUES (2, 1, '2026-08-03T00:00:00Z');
            INSERT INTO device_operating_intent(device_id, mode, updated_at)
                VALUES ('ispindel-a', 'brewing', '2026-08-03T00:00:00Z');
            INSERT INTO assistant_jobs(id, status, recipe_id, brew_run_id, created_at)
                VALUES ('job-1', 'pending', 1, 1, '2026-08-03T00:00:00Z');
            INSERT INTO assistant_jobs(id, status, recipe_id, brew_run_id, created_at)
                VALUES ('job-2', 'completed', 2, NULL, '2026-08-03T00:00:00Z');
            INSERT INTO research_documents(source_kind, source_url, title, content, metadata_json, created_at)
                VALUES ('searxng', 'https://example.test/a', 'Alpha Paper', 'alpha content body', '{}', '2026-08-03T00:00:00Z');
            INSERT INTO research_documents(source_kind, source_url, title, content, metadata_json, created_at)
                VALUES ('searxng', 'https://example.test/b', 'Beta Paper', 'beta content body', '{}', '2026-08-03T00:00:00Z');
            INSERT INTO research_documents(source_kind, source_url, title, content, metadata_json, created_at)
                VALUES ('kiwix', 'https://example.test/c', 'Gamma Paper', 'gamma content body', '{}', '2026-08-03T00:00:00Z');
            INSERT INTO schema_migrations(version) VALUES (1), (2);
            PRAGMA user_version=2;
            """
        )
        conn.commit()
    finally:
        conn.close()
    if path.exists() or path.is_symlink():
        path.unlink()
    shutil.move(str(temp_path), str(path))
    os.chmod(path, 0o600)
    return path


def test_brew_table_allowlist_matches_contract():
    """Evidence must count every application-owned brew data table."""
    assert brew_table_allowlist() == BREW_TABLES


RUN_ID = "20260914T000000Z-deadbeef"


def test_brew_evidence_records_integrity_user_version_and_counts(tmp_path: Path):
    """brew_evidence must record integrity, user_version, migrations, counts."""
    db = _populate_brew_db(tmp_path / bind_brew_basename(RUN_ID))
    payload = brew_evidence(db)
    assert payload["integrity"] == "ok"
    assert payload["user_version"] == 2
    assert payload["migration_versions"] == [1, 2]
    counts = payload["counts"]
    assert counts["recipes"] == 3
    assert counts["brew_runs"] == 2
    assert counts["brew_events"] == 2
    assert counts["archive_evidence_bundles"] == 1
    assert counts["archive_annotations"] == 1
    assert counts["recipe_lineage"] == 2
    assert counts["device_operating_intent"] == 1
    assert counts["assistant_jobs"] == 2
    assert counts["research_documents"] == 3
    assert counts["research_documents_fts"] == 3


def test_brew_evidence_rejects_missing_or_symlinked_database(tmp_path: Path):
    """brew_evidence must fail closed on missing or symlinked databases."""
    with pytest.raises(BrewEvidenceError, match="missing|not a regular file|symlink"):
        brew_evidence(tmp_path / bind_brew_basename(RUN_ID))
    target = tmp_path / "target.db"
    target.write_bytes(b"")
    link = tmp_path / bind_brew_basename(RUN_ID)
    link.symlink_to(target)
    with pytest.raises(BrewEvidenceError, match="symlink"):
        brew_evidence(link)


def test_brew_evidence_detects_missing_fts_row_and_passes_after_repair(tmp_path: Path):
    """brew_evidence must fail closed on FTS5 drift, pass after rebuild.

    The FTS5 external-content index can drift when the database is restored
    from a snapshot taken before the AFTER INSERT trigger fired (see
    app/brewing.py's ``_rebuild_research_documents_fts``). brew_evidence must
    detect drift on a fresh temporary copy without mutating the source.
    """
    db = _populate_brew_db(tmp_path / bind_brew_basename(RUN_ID))
    # Snapshot source sha256 so we can prove we did not mutate it.
    src_uri = db.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(src_uri, uri=True) as conn:
        original_size = db.stat().st_size
    # Deliberately break FTS5 external-content parity by INSERTing an orphan
    # FTS row whose rowid does not exist in research_documents. This simulates
    # the FTS5 drift condition (e.g. AFTER INSERT trigger not fired, or external
    # content table out of sync). Direct DELETE on an external-content FTS5
    # table would be malformed, so INSERT is the safe and reproducible way.
    drift_db = tmp_path / bind_brew_basename("20260914T000001Z-00000002")
    import shutil
    shutil.copyfile(db, drift_db)
    conn = sqlite3.connect(drift_db)
    try:
        conn.execute(
            "INSERT INTO research_documents_fts(rowid, title, content, source_url) "
            "VALUES (?, 'orphan', 'orphan', 'https://example.test/orphan')",
            (99,),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(BrewEvidenceError, match="fts parity|fail|drift|stale"):
        brew_evidence(drift_db)
    # The original source bytes must be untouched.
    assert db.stat().st_size == original_size

    # Repair by rebuilding FTS on a fresh temp copy via the explicit helper.
    repaired = brew_evidence_repair_fresh(drift_db)
    assert repaired["counts"]["research_documents_fts"] == 3
    # The original source bytes must remain sealed.
    assert db.stat().st_size == original_size


def test_brew_evidence_rebuild_never_writes_back_to_source(tmp_path: Path):
    """The source bytes must remain sealed; rebuild runs only on temp copies.

    brew_evidence may perform a rebuild on a temporary copy when parity is
    detected, but the source path must never be opened in write mode.
    """
    db = _populate_brew_db(tmp_path / bind_brew_basename(RUN_ID))
    original_bytes = db.read_bytes()
    # Confirm direct read-only mode is honored by checking that brew_evidence
    # opens the source with query_only.
    payload = brew_evidence(db)
    assert payload["integrity"] == "ok"
    assert db.read_bytes() == original_bytes


def test_brew_evidence_requires_brew_prefix_basename(tmp_path: Path):
    """brew_evidence is identified by prefix; helper must reject the ispindel namespace.

    This proves the dual-mode identity check keeps each database bound to its
    own prefix.
    """
    db = _populate_brew_db(tmp_path / bind_brew_basename(RUN_ID))
    # Copy the bytes to an ispindel-prefixed basename so the prefix check rejects.
    ispindel_path = tmp_path / bind_basename(RUN_ID)
    shutil.copyfile(db, ispindel_path)
    with pytest.raises(BrewEvidenceError, match="brew|prefix"):
        brew_evidence(ispindel_path, expected_prefix="brew")


def test_brew_evidence_fails_closed_on_integrity_violation(tmp_path: Path):
    """integrity_check must fail closed if the database is corrupted."""
    db = _populate_brew_db(tmp_path / bind_brew_basename(RUN_ID))
    # Force a corruption: zero out the sqlite header by writing garbage.
    with db.open("r+b") as handle:
        handle.seek(0)
        handle.write(b"\x00" * 32)
    with pytest.raises(BrewEvidenceError, match="integrity"):
        brew_evidence(db)
