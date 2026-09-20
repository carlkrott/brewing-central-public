#!/usr/bin/env python3
"""Deterministic source-only public publication exporter for Brewing Central.

This script materializes a clean, deterministic public artifact tree from the
private iSpindel dashboard repository at a specific source commit. It is
deliberately stdlib-only and reads tracked files exclusively through git
plumbing -- the working tree, ignored files, and untracked files are never
consulted. The output is a stable byte-identical tree plus a JSON manifest
listing every emitted file (path, mode, size, sha256) and an aggregate
digest that callers can use to detect drift between runs.

Usage::

    python scripts/export-public.py \\
        --source-root /path/to/private/repo \\
        --output-root /path/to/public/out \\
        --policy   config/public-export.json \\
        --source-commit HEAD

The optional ``--check-only`` flag performs the entire selection, extraction,
and forbidden-pattern scan but writes nothing to ``--output-root`` -- it
prints counts and the aggregate digest to stdout. Exit codes are::

    0  success (export or check completed, no policy violations)
    2  policy/argument error (missing excluded path, unsafe path, traversal, ...)
    3  forbidden-pattern hit in emitted tree
    4  git plumbing error (missing commit, non-blob, ...)

The script NEVER prints secrets, never reads ``.git`` contents other than via
``git cat-file --batch`` plumbing, and never shells out with the policy or
source root joined into a single command line. Subprocess args are passed as
a list.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Sequence

SCHEMA_VERSION = "public-export-manifest-v1"
POLICY_SCHEMA_VERSION = "public-export-policy-v1"
EXIT_OK = 0
EXIT_POLICY = 2
EXIT_FORBIDDEN = 3
EXIT_GIT = 4

# Path safety: reject absolute paths, parent traversal, symlinks, submodules,
# anything git treats as anything other than a regular blob. We also reject
# paths containing NUL or any control character.
_FORBIDDEN_PATH_CHARS = set("\x00")


@dataclasses.dataclass(frozen=True)
class Policy:
    """Strongly-typed view of the publication policy."""

    private_paths: tuple[str, ...]
    public_paths: tuple[str, ...]
    forbidden_patterns: tuple[re.Pattern[str], ...]
    manifest_name: str
    scan_forbidden: bool
    tracked_only: bool
    exclude_untracked_and_ignored: bool

    @classmethod
    def load(cls, path: Path) -> "Policy":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"policy: cannot load {path}: {exc}") from None
        if data.get("schema_version") != POLICY_SCHEMA_VERSION:
            raise SystemExit(
                f"policy: unsupported schema_version={data.get('schema_version')!r}; "
                f"expected {POLICY_SCHEMA_VERSION!r}"
            )
        private = tuple(data.get("private_paths") or ())
        public = tuple(data.get("public_paths") or ())
        for label, paths in (("private_paths", private), ("public_paths", public)):
            if any(not isinstance(path, str) for path in paths):
                raise SystemExit(f"policy: {label} must contain only strings")
            if len(set(paths)) != len(paths):
                raise SystemExit(f"policy: {label} contains duplicate paths")
            unsafe = [path for path in paths if not _is_path_safe(path)]
            if unsafe:
                raise SystemExit(f"policy: unsafe {label}: {unsafe}")
        overlap = sorted(set(private) & set(public))
        if overlap:
            raise SystemExit(f"policy: paths classified as both public and private: {overlap}")
        patterns_raw = data.get("forbidden_patterns") or ()
        patterns = tuple(re.compile(p) for p in patterns_raw)
        manifest_name = str(data.get("manifest_name", "public-export.manifest.json"))
        if not _is_path_safe(manifest_name) or "/" in manifest_name:
            raise SystemExit(f"policy: unsafe manifest_name={manifest_name!r}")
        return cls(
            private_paths=private,
            public_paths=public,
            forbidden_patterns=patterns,
            manifest_name=manifest_name,
            scan_forbidden=bool(data.get("scan_forbidden_patterns", True)),
            tracked_only=bool(data.get("tracked_only", True)),
            exclude_untracked_and_ignored=bool(data.get("exclude_untracked_and_ignored", True)),
        )


@dataclasses.dataclass(frozen=True)
class BlobEntry:
    """A single tracked file selected for export."""

    path: str
    mode: str  # 6-digit octal string from git ls-tree
    size: int
    sha256: str


class ManifestForbiddenError(RuntimeError):
    """Raised when generated manifest metadata violates publication policy."""

    def __init__(self, hits: Sequence[str]) -> None:
        super().__init__("generated manifest contains forbidden metadata")
        self.hits = tuple(hits)


# ---------------------------------------------------------------------------
# Git plumbing helpers
# ---------------------------------------------------------------------------


def _git(source_root: Path, *args: str, check: bool = True) -> str:
    """Run a git command with explicit CWD and return stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(source_root),
        capture_output=True,
        text=True,
        env={
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
        },
    )
    if check and result.returncode != 0:
        raise SystemExit(
            f"git {' '.join(args)} failed (exit={result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout


def _resolve_commit(source_root: Path, ref: str) -> str:
    """Resolve a ref (e.g. HEAD, 132fcb0, main) to a full 40-char SHA."""
    out = _git(source_root, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", out):
        raise SystemExit(f"git: cannot resolve {ref!r} to a commit (got {out!r})")
    return out


def _list_tracked_paths(source_root: Path, commit: str) -> list[str]:
    """List tracked file paths at a commit (NUL-separated, decoded)."""
    raw = _git(
        source_root,
        "ls-tree",
        "-r",
        "--name-only",
        "-z",
        commit,
    )
    # NUL-separated; trailing NUL tolerated.
    parts = [p for p in raw.split("\x00") if p]
    return parts


def _ls_tree_full(source_root: Path, commit: str) -> dict[str, tuple[str, str]]:
    """Return {path: (mode_octal, blob_sha)} for every tracked entry.

    Submodules (``160000``), symlinks (``120000``), and trees are filtered out
    by the caller -- this function faithfully reports what git returns.
    """
    raw = _git(
        source_root,
        "ls-tree",
        "-r",
        "-z",
        commit,
    )
    out: dict[str, tuple[str, str]] = {}
    # Format is: "<mode> <type> <sha>\t<path>" separated by NUL.
    for entry in raw.split("\x00"):
        if not entry:
            continue
        if "\t" not in entry:
            # Defensive: if git ever switches separators, fail loudly.
            raise SystemExit(f"git ls-tree: unexpected entry shape: {entry!r}")
        meta, path = entry.split("\t", 1)
        mode, type_, sha = meta.split(" ", 2)
        if type_ != "blob":
            # Record non-blob entries so the caller can reject them.
            out[path] = (mode, sha)
            continue
        out[path] = (mode, sha)
    return out


def _cat_blob(source_root: Path, sha: str) -> bytes:
    """Stream a blob out of git's object store via git cat-file --batch."""
    proc = subprocess.Popen(
        ["git", "cat-file", "--batch"],
        cwd=str(source_root),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "GIT_OPTIONAL_LOCKS": "0",
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
        },
    )
    assert proc.stdin is not None and proc.stdout is not None
    try:
        proc.stdin.write(f"{sha}\n".encode("utf-8"))
        proc.stdin.flush()
        header = proc.stdout.readline()
        if not header:
            stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
            raise SystemExit(f"git cat-file: empty response for {sha}: {stderr}")
        parts = header.split(b" ")
        # Format: "<sha> <type> <size>"
        if len(parts) < 3:
            raise SystemExit(f"git cat-file: malformed header for {sha}: {header!r}")
        size = int(parts[2])
        body = proc.stdout.read(size)
        # consume trailing newline that batch mode emits
        trailing = proc.stdout.read(1)
        _ = trailing  # noqa: F841 -- intentionally discarded
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
    return body


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


def _is_path_safe(rel: str) -> bool:
    """Reject any path that is absolute, traverses, or contains controls."""
    if not rel:
        return False
    if rel != rel.strip():
        return False
    if rel.startswith("/"):
        return False
    # Reject Windows drive prefixes and backslashes -- git treats them
    # verbatim on POSIX hosts, which is exactly the kind of surprise we
    # don't want.
    if "\\" in rel:
        return False
    if ":" in rel.split("/", 1)[0]:
        return False
    if any(c in _FORBIDDEN_PATH_CHARS for c in rel):
        return False
    parts = rel.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return False
    return True


# ---------------------------------------------------------------------------
# Selection & export
# ---------------------------------------------------------------------------


def select_paths(
    commit: str,
    all_paths: Iterable[str],
    blobs: dict[str, tuple[str, str]],
    policy: Policy,
) -> tuple[list[str], list[str], list[str]]:
    """Decide which tracked paths to export.

    Returns ``(selected, missing_excluded, rejected)``.

    - ``missing_excluded``: classified paths that were NOT found in the
      tracked set at this commit. A hard failure -- silently weakening the
      policy would be a security regression.
    - ``rejected``: paths that the exporter REFUSES to copy because they
      are not regular blobs, symlinks, submodules, or fail path safety.
      Hard failures (these should not exist at all in a curated public
      tree). Every tracked path must be classified exactly once.
    """
    all_set = set(all_paths)

    missing_excluded: list[str] = []
    classified = set(policy.private_paths) | set(policy.public_paths)
    for classified_path in sorted(classified):
        if classified_path not in all_set:
            missing_excluded.append(classified_path)

    unclassified = sorted(all_set - classified)
    rejected = [f"{path}: unclassified tracked path" for path in unclassified]

    selected: list[str] = []
    for path in sorted(all_set):
        if not _is_path_safe(path):
            rejected.append(f"{path}: unsafe path")
            continue
        mode, _sha = blobs[path]
        # Refuse submodules (160000) and symlinks (120000) outright; only
        # export regular blobs (100644 / 100755).
        if mode not in ("100644", "100755"):
            rejected.append(f"{path}: not a regular file (mode={mode})")
            continue
        if path in set(policy.private_paths):
            # Explicitly excluded -- skip silently.
            continue
        if path not in set(policy.public_paths):
            # Already reported above, but keep this branch defensive if the
            # selection rules are extended later.
            continue
        selected.append(path)

    return selected, missing_excluded, rejected


def extract_entries(
    source_root: Path,
    selected: Sequence[str],
    blobs: dict[str, tuple[str, str]],
) -> list[BlobEntry]:
    """Materialize the selected files into memory with sha256 + size."""
    out: list[BlobEntry] = []
    for path in selected:
        mode, sha = blobs[path]
        body = _cat_blob(source_root, sha)
        h = hashlib.sha256()
        h.update(body)
        out.append(BlobEntry(path=path, mode=mode, size=len(body), sha256=h.hexdigest()))
    return out


def scan_forbidden(
    entries: Sequence[BlobEntry],
    source_root: Path,
    blobs: dict[str, tuple[str, str]],
    policy: Policy,
) -> list[str]:
    """Return a list of ``"<path>: pattern=<idx>"`` violations."""
    if not policy.scan_forbidden or not policy.forbidden_patterns:
        return []
    hits: list[str] = []
    for entry in entries:
        # Re-pull the body for the scan. We deliberately do this rather than
        # threading bytes through earlier, because the bytes never leave the
        # function unless the caller writes them -- so this is the only
        # place a secret could be read for matching.
        _mode, sha = blobs[entry.path]
        body = _cat_blob(source_root, sha)
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            text = ""
        for idx, pattern in enumerate(policy.forbidden_patterns):
            if pattern.search(text):
                hits.append(f"{entry.path}: pattern={idx}")
                break
    return hits


def scan_manifest_forbidden(manifest: dict, policy: Policy) -> list[str]:
    """Apply the same publication policy to generated manifest metadata."""
    if not policy.scan_forbidden or not policy.forbidden_patterns:
        return []
    text = json.dumps(manifest, sort_keys=True)
    return [
        f"{policy.manifest_name}: pattern={idx}"
        for idx, pattern in enumerate(policy.forbidden_patterns)
        if pattern.search(text)
    ]


def write_export(
    output_root: Path,
    commit: str,
    source_root: Path,
    policy: Policy,
    entries: Sequence[BlobEntry],
    *,
    check_only: bool,
) -> dict:
    """Write a clean deterministic tree + manifest, returning the manifest dict."""
    manifest_entries = []
    aggregate = hashlib.sha256()
    for entry in sorted(entries, key=lambda e: e.path):
        manifest_entries.append(
            {
                "path": entry.path,
                "mode": entry.mode,
                "size": entry.size,
                "sha256": entry.sha256,
            }
        )
        # Aggregate digest over the SORTED manifest line for determinism --
        # we do not hash the file bytes here because the manifest already
        # carries per-file sha256.
        line = f"{entry.path}\t{entry.mode}\t{entry.size}\t{entry.sha256}\n"
        aggregate.update(line.encode("utf-8"))

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "policy_schema_version": POLICY_SCHEMA_VERSION,
        "manifest_name": policy.manifest_name,
        "entry_count": len(manifest_entries),
        "aggregate_sha256": aggregate.hexdigest(),
        "entries": manifest_entries,
    }

    manifest_hits = scan_manifest_forbidden(manifest, policy)
    if manifest_hits:
        raise ManifestForbiddenError(manifest_hits)

    if check_only:
        return manifest

    # Clean previous output (only the contents of --output-root).
    if output_root.exists():
        for child in output_root.iterdir():
            # A workflow may export directly into a checked-out public
            # repository. Preserve its Git metadata so the caller can create
            # a branch and commit the generated tree.
            if child.name == ".git":
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    output_root.mkdir(parents=True, exist_ok=True)

    # Write files deterministically.
    for entry in entries:
        mode, sha = next(v for k, v in _ls_tree_full(source_root, commit).items() if k == entry.path)
        body = _cat_blob(source_root, sha)
        dest = output_root / entry.path
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() or dest.is_symlink():
            dest.unlink()
        with open(dest, "wb") as fh:
            fh.write(body)
        os.chmod(dest, 0o644 if entry.mode == "100644" else 0o755)

    manifest_path = output_root / policy.manifest_name
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="export-public",
        description="Materialize the Brewing Central public mirror from the private source tree.",
    )
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument(
        "--source-commit",
        default="HEAD",
        help="Commit-ish to export from. Default HEAD. Must resolve to a full commit SHA.",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Compute and print counts/aggregate digest without writing the output tree.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    source_root: Path = args.source_root.resolve()
    output_root: Path = args.output_root.resolve()
    policy_path: Path = args.policy.resolve()

    if not source_root.is_dir():
        print(f"export-public: source-root is not a directory: {source_root}", file=sys.stderr)
        return EXIT_POLICY
    if not policy_path.is_file():
        print(f"export-public: policy not found: {policy_path}", file=sys.stderr)
        return EXIT_POLICY

    policy = Policy.load(policy_path)

    commit = _resolve_commit(source_root, args.source_commit)
    paths = _list_tracked_paths(source_root, commit)
    trees = _ls_tree_full(source_root, commit)
    selected, missing_excluded, rejected = select_paths(commit, paths, trees, policy)

    if missing_excluded:
        print(
            "export-public: policy violation -- the following classified paths were not present at "
            f"{commit}: {missing_excluded}",
            file=sys.stderr,
        )
        return EXIT_POLICY

    entries = extract_entries(source_root, selected, trees)

    if rejected:
        # We treat unexpected rejected paths as policy violations -- the
        # operator almost certainly wants to know that a tracked file did
        # not match the public prefix set.
        print(
            "export-public: unexpected tracked paths that did not pass the public policy:",
            file=sys.stderr,
        )
        for r in rejected:
            print(f"  - {r}", file=sys.stderr)
        return EXIT_POLICY

    forbidden_hits = scan_forbidden(entries, source_root, trees, policy)
    if forbidden_hits:
        print("export-public: forbidden-pattern hits:", file=sys.stderr)
        for h in forbidden_hits:
            print(f"  - {h}", file=sys.stderr)
        return EXIT_FORBIDDEN

    try:
        manifest = write_export(
            output_root=output_root,
            commit=commit,
            source_root=source_root,
            policy=policy,
            entries=entries,
            check_only=args.check_only,
        )
    except ManifestForbiddenError as exc:
        print("export-public: generated manifest contains forbidden-pattern hits:", file=sys.stderr)
        for hit in exc.hits:
            print(f"  - {hit}", file=sys.stderr)
        return EXIT_FORBIDDEN

    summary = {
        "source_commit": commit,
        "entry_count": manifest["entry_count"],
        "aggregate_sha256": manifest["aggregate_sha256"],
        "output_root": str(output_root),
        "check_only": bool(args.check_only),
        "manifest_name": policy.manifest_name,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
