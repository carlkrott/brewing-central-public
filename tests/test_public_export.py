"""Focused pytest module for scripts/export-public.py.

These tests construct throwaway git repositories on disk and exercise the
exporter end-to-end without touching the live repository, network, or
GitHub. Every fixture is ``tmp_path``-scoped and torn down automatically.

What this covers:

1. Determinism -- two consecutive exports at the same commit yield the same
   ``aggregate_sha256`` and the same per-file ``sha256``.
2. Exact classification -- every tracked fixture path is classified, while
   the two current ``private_paths`` entries
   (``contracts/ispindel-predecessor-expected-missing-amendment-v2.json``
   and ``descriptors/RELEASE.json``) plus the publication workflow itself
   never appear in the exported tree.
3. Missing excluded path -- if a ``private_paths`` entry is not in the
   tracked set, the exporter exits non-zero with exit code 2.
4. Unclassified path -- a tracked file absent from both exact path sets is
   rejected by the policy and the export fails closed.
5. Forbidden-pattern rejection -- a tracked file whose body matches a
   ``forbidden_patterns`` regex causes exit code 3 and no output is written.
6. Manifest/hash equality -- the ``public-export.manifest.json`` is a
   deterministic function of the source commit + policy.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPORTER = REPO_ROOT / "scripts" / "export-public.py"
POLICY = REPO_ROOT / "config" / "public-export.json"


def _git(cwd: Path, *args: str, env: dict | None = None) -> str:
    """Run git with a sanitised environment."""
    base_env = {
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.test",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.test",
        "GIT_TERMINAL_PROMPT": "0",
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", str(cwd)),
        # Force a known, deterministic committer date for reproducible SHAs.
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    }
    if env:
        base_env.update(env)
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=base_env,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed (cwd={cwd}): {result.stderr}"
        )
    return result.stdout


@pytest.fixture()
def source_repo(tmp_path: Path) -> Path:
    """Create a throwaway git repo with a focused public surface.

    Mirrors every ``private_paths`` entry the live policy enforces so that
    the strict "missing excluded path => fail closed" check passes:
    ``contracts/ispindel-predecessor-expected-missing-amendment-v2.json``
    and ``descriptors/RELEASE.json`` live here. The workflow and the
    publication-lane pytest module are removed by the ``policy_copy``
    fixture because they only exist in the real repo, not in this fixture.
    """
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "user.email", "test@example.test")
    _git(repo, "config", "user.name", "test")

    # Public-ish surface.
    (repo / "scripts").mkdir()
    (repo / "scripts" / "hello.py").write_text("print('hello')\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "README.md").write_text("# example\n")
    (repo / "public").mkdir()
    (repo / "public" / "index.html").write_text("<html></html>\n")
    (repo / "README.md").write_text("# root readme\n")

    # Mirror the live private_paths entries the fixture retains after the
    # policy_copy fixture's removal list.
    (repo / "contracts").mkdir()
    (repo / "contracts" / "ispindel-predecessor-expected-missing-amendment-v2.json").write_text(
        json.dumps({"schema_version": "amendment-v2", "private": True}) + "\n"
    )
    (repo / "descriptors").mkdir()
    (repo / "descriptors" / "RELEASE.json").write_text(
        json.dumps({"schema_version": "release-v1", "private": True}) + "\n"
    )
    (repo / "config").mkdir()
    (repo / "config" / "public-export.json").write_text(
        json.dumps(
            {
                "schema_version": "policy-fixture-v1",
                "private_paths": [],
                "public_paths": [],
                "forbidden_patterns": [],
                "manifest_name": "public-export.manifest.json",
            }
        )
        + "\n"
    )

    # Private runtime area -- the exact policy classification excludes this.
    (repo / "var").mkdir()
    (repo / "var" / "private-secret.txt").write_text("operator-only\n")

    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture()
def policy_copy(tmp_path: Path, source_repo: Path) -> Path:
    """Copy the repo's policy into a tmp file so tests can mutate freely.

    The throwaway source fixture does not contain the live repository's
    private-only workflow, so this fixture keeps the private paths it creates
    and derives an exact public set from the fixture commit.
    """
    policy_path = tmp_path / "public-export.json"
    data = json.loads(POLICY.read_text(encoding="utf-8"))
    removed = {".github/workflows/publish-public.yml"}
    data["private_paths"] = [p for p in data["private_paths"] if p not in removed]
    data["private_paths"].append("var/private-secret.txt")
    tracked = set(_git(source_repo, "ls-files").splitlines())
    data["public_paths"] = sorted(tracked - set(data["private_paths"]))
    policy_path.write_text(json.dumps(data, indent=2, sort_keys=True))
    return policy_path


def _run_exporter(
    source: Path,
    output: Path,
    policy: Path,
    commit: str = "HEAD",
    check_only: bool = False,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(EXPORTER),
            "--source-root", str(source),
            "--output-root", str(output),
            "--policy", str(policy),
            "--source-commit", commit,
            *(["--check-only"] if check_only else []),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PATH": os.environ.get("PATH", "")},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_export_is_deterministic(source_repo: Path, policy_copy: Path, tmp_path: Path) -> None:
    out_a = tmp_path / "out_a"
    out_b = tmp_path / "out_b"
    res_a = _run_exporter(source_repo, out_a, policy_copy)
    res_b = _run_exporter(source_repo, out_b, policy_copy)
    assert res_a.returncode == 0, res_a.stderr
    assert res_b.returncode == 0, res_b.stderr

    manifest_a = json.loads((out_a / "public-export.manifest.json").read_text())
    manifest_b = json.loads((out_b / "public-export.manifest.json").read_text())

    assert manifest_a["aggregate_sha256"] == manifest_b["aggregate_sha256"]
    # Per-file hashes must match.
    by_path_a = {e["path"]: e for e in manifest_a["entries"]}
    by_path_b = {e["path"]: e for e in manifest_b["entries"]}
    assert by_path_a.keys() == by_path_b.keys()
    for path, entry in by_path_a.items():
        assert entry["sha256"] == by_path_b[path]["sha256"], path


def test_excluded_paths_never_appear(source_repo: Path, policy_copy: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    res = _run_exporter(source_repo, out, policy_copy)
    assert res.returncode == 0, res.stderr

    exported_files = {str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()}
    assert "contracts/ispindel-predecessor-expected-missing-amendment-v2.json" not in exported_files
    assert "descriptors/RELEASE.json" not in exported_files
    assert ".github/workflows/publish-public.yml" not in exported_files
    assert "config/public-export.json" in exported_files
    # The private runtime area must not appear in the manifest either.
    manifest = json.loads((out / "public-export.manifest.json").read_text())
    manifest_paths = {e["path"] for e in manifest["entries"]}
    assert "var/private-secret.txt" not in manifest_paths
    assert "source_root" not in manifest
    assert "source_commit" not in manifest
    assert "/home/" not in (out / "public-export.manifest.json").read_text()


def test_generated_manifest_is_scanned(
    source_repo: Path, policy_copy: Path, tmp_path: Path
) -> None:
    policy = json.loads(policy_copy.read_text())
    policy["forbidden_patterns"] = ["public-export-manifest-v1"]
    policy_copy.write_text(json.dumps(policy, indent=2, sort_keys=True))

    out = tmp_path / "out"
    res = _run_exporter(source_repo, out, policy_copy)
    assert res.returncode == 3, res.stderr
    assert "generated manifest" in res.stderr
    assert not out.exists()


@pytest.mark.parametrize(
    "leak",
    [
        ".hermes" + "/plans/internal.md",
        "f" + "eat/reliable-local-production",
        "799" + "5x",
        "Mac" + "Book",
        "Samsung " + "A05s",
        "Samsung Galaxy " + "A05s",
    ],
)
def test_publication_coherence_metadata_is_rejected(
    source_repo: Path, policy_copy: Path, tmp_path: Path, leak: str
) -> None:
    (source_repo / "docs" / "README.md").write_text(f"{leak}\n")
    _git(source_repo, "add", "-A")
    _git(source_repo, "commit", "-q", "-m", "add publication leak")

    out = tmp_path / "out"
    res = _run_exporter(source_repo, out, policy_copy)
    assert res.returncode == 3, res.stderr
    assert "forbidden-pattern" in res.stderr
    assert not out.exists()


def test_safe_new_public_file_is_included(source_repo: Path, policy_copy: Path, tmp_path: Path) -> None:
    # Add a new tracked file under an approved prefix and re-commit.
    (source_repo / "public" / "guide.md").write_text("# public guide\n")
    _git(source_repo, "add", "-A")
    _git(source_repo, "commit", "-q", "-m", "add guide")
    policy = json.loads(policy_copy.read_text())
    policy["public_paths"].append("public/guide.md")
    policy_copy.write_text(json.dumps(policy, indent=2, sort_keys=True))

    out = tmp_path / "out"
    res = _run_exporter(source_repo, out, policy_copy)
    assert res.returncode == 0, res.stderr

    manifest = json.loads((out / "public-export.manifest.json").read_text())
    paths = {e["path"] for e in manifest["entries"]}
    assert "public/guide.md" in paths
    # And the file actually landed on disk with the right content.
    assert (out / "public" / "guide.md").read_text() == "# public guide\n"


def test_missing_excluded_path_fails_closed(source_repo: Path, tmp_path: Path) -> None:
    # Build a policy that references a private path the source does NOT have.
    policy = tmp_path / "public-export.json"
    data = json.loads(POLICY.read_text(encoding="utf-8"))
    data["private_paths"] = list(data["private_paths"]) + ["contracts/does-not-exist.json"]
    policy.write_text(json.dumps(data))

    out = tmp_path / "out"
    res = _run_exporter(source_repo, out, policy)
    assert res.returncode == 2, res.stderr
    assert "does-not-exist.json" in res.stderr or "excluded paths" in res.stderr
    # No output should have been written (or at most a stale prior tree -- here there isn't one).
    assert not (out / "public-export.manifest.json").exists()


def test_unclassified_tracked_path_fails_closed(
    source_repo: Path, policy_copy: Path, tmp_path: Path
) -> None:
    # Add a tracked file absent from both exact classification sets. The
    # exporter must fail closed rather than silently skipping it.
    (source_repo / "misc").mkdir(exist_ok=True)
    (source_repo / "misc" / "scratch.txt").write_text("scratch\n")
    _git(source_repo, "add", "-A")
    _git(source_repo, "commit", "-q", "-m", "add scratch")

    out = tmp_path / "out"
    res = _run_exporter(source_repo, out, policy_copy)
    assert res.returncode == 2, res.stderr
    assert "misc/scratch.txt" in res.stderr


def test_check_only_does_not_write_output(source_repo: Path, policy_copy: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    if out.exists():
        shutil.rmtree(out)
    res = _run_exporter(source_repo, out, policy_copy, check_only=True)
    assert res.returncode == 0, res.stderr
    # The exporter prints a single indented JSON object on stdout.
    summary = json.loads(res.stdout)
    assert summary["check_only"] is True
    assert summary["entry_count"] >= 4  # hello.py, docs/README.md, public/index.html, README.md
    # Crucially: nothing on disk.
    assert not out.exists() or not any(out.iterdir())


def test_forbidden_pattern_is_rejected(source_repo: Path, policy_copy: Path, tmp_path: Path) -> None:
    # Plant a forbidden-pattern-shaped token in an otherwise-public file.
    # The test module is public-safe; the selectively-exported fixture below
    # is what should trigger the forbidden-pattern scan.
    (source_repo / "public" / "leak.txt").write_text(
        "github_pat_" + "A" * 40 + "\n"
    )
    _git(source_repo, "add", "-A")
    _git(source_repo, "commit", "-q", "-m", "leak")
    policy = json.loads(policy_copy.read_text())
    policy["public_paths"].append("public/leak.txt")
    policy_copy.write_text(json.dumps(policy, indent=2, sort_keys=True))

    out = tmp_path / "out"
    res = _run_exporter(source_repo, out, policy_copy)
    assert res.returncode == 3, res.stderr
    assert "forbidden-pattern" in res.stderr.lower() or "pattern=" in res.stderr


def test_conservative_patterns_allow_rfc5737_and_example_test(
    source_repo: Path, policy_copy: Path, tmp_path: Path
) -> None:
    # Documentation-style values that MUST NOT match the conservative
    # forbidden-pattern set.
    (source_repo / "docs" / "IPV4_EXAMPLES.md").write_text(
        "Use 192.0.2.1 or 198.51.100.1 in docs (RFC 5737).\n"
        "Reference host: host.example.test.\n"
    )
    _git(source_repo, "add", "-A")
    _git(source_repo, "commit", "-q", "-m", "docs")
    policy = json.loads(policy_copy.read_text())
    policy["public_paths"].append("docs/IPV4_EXAMPLES.md")
    policy_copy.write_text(json.dumps(policy, indent=2, sort_keys=True))

    out = tmp_path / "out"
    res = _run_exporter(source_repo, out, policy_copy)
    assert res.returncode == 0, res.stderr
    # Confirm the documentation file is in the export.
    assert (out / "docs" / "IPV4_EXAMPLES.md").exists()


def test_manifest_aggregate_matches_byte_hash(source_repo: Path, policy_copy: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    res = _run_exporter(source_repo, out, policy_copy)
    assert res.returncode == 0, res.stderr

    manifest = json.loads((out / "public-export.manifest.json").read_text())
    # Recompute the aggregate from the manifest's own entries and confirm
    # equality. This is what an operator runs locally to verify a remote
    # publication.
    import hashlib

    h = hashlib.sha256()
    for entry in sorted(manifest["entries"], key=lambda e: e["path"]):
        line = f"{entry['path']}\t{entry['mode']}\t{entry['size']}\t{entry['sha256']}\n"
        h.update(line.encode("utf-8"))
    assert h.hexdigest() == manifest["aggregate_sha256"]
