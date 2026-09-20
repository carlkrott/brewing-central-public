"""Container-context contract tests (gitignore / dockerignore / compose env / Dockerfile / Compose).

These tests are intentionally read-only. They walk the repository and the
in-repo ``ops/container/compose.env.example`` to prove the hygiene slice:
  * sensitive files are protected by .gitignore
  * sensitive files are excluded from the Docker build context
  * the compose env example contains the required variables and never
    leaks obvious secrets, raw IP literals, or production hostnames
  * Dockerfile / Compose COPY closure is preserved (whitelist path)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
GITIGNORE = ROOT / ".gitignore"
DOCKERIGNORE = ROOT / ".dockerignore"
COMPOSE_EXAMPLE = ROOT / "ops" / "container" / "compose.env.example"
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE_YML = ROOT / "docker-compose.yml"


# ---------------------------------------------------------------------------
# gitignore / dockerignore matcher
# ---------------------------------------------------------------------------


def _read_lines(path: Path) -> list[str]:
    return [line.rstrip("\n") for line in path.read_text().splitlines()]


def _compile_rule(rule: str) -> re.Pattern[str] | None:
    """Compile one ignore rule into an anchored regex matching relative paths.

    Implements the subset of gitignore semantics we care about:
      * ``/``-suffixed rules match the directory and everything under it.
      * ``**`` matches zero or more path components (including ``/``).
        Leading ``**/`` also matches zero components (the root).
      * ``*`` matches any run of non-``/`` characters.
      * ``?`` matches a single non-``/`` character.
      * Plain rules with no ``/`` and no leading ``/`` match a basename at
        any depth; a directory match also excludes everything beneath it.
      * Rules containing ``/`` match at any prefix depth (unanchored form).
      * Leading ``/`` anchors to the repository root.
    Returns ``None`` for empty/whitespace rules.
    """
    if not rule.strip():
        return None
    anchored = rule.startswith("/")
    body = rule[1:] if anchored else rule
    is_dir_rule = body.endswith("/")
    if is_dir_rule:
        body = body[:-1]

    leading_doublestar = body.startswith("**/")
    body_no_leading = body[3:] if leading_doublestar else body

    parts: list[str] = []
    for segment in body_no_leading.split("/"):
        if segment == "**":
            parts.append(".*")
        else:
            esc = re.escape(segment)
            esc = esc.replace(r"\*", "[^/]*")
            esc = esc.replace(r"\?", "[^/]")
            parts.append(esc)
    body_re = "/".join(parts)

    has_slash = "/" in body_no_leading

    if is_dir_rule:
        # ``dir/`` matches ``dir``, ``dir/x``, ``dir/x/y``...
        prefix = "(?:.*/)?" if not leading_doublestar else "(?:.*/)?"
        pattern = f"^{prefix}{body_re}(?:/.*)?$"
    elif anchored:
        pattern = f"^{body_re}$"
    elif not has_slash:
        # Basename rule. Matches a file/directory of that name at any depth;
        # a directory match also excludes everything beneath.
        # ``[^/]*`` doesn't cross slashes, so prefix with an optional path.
        prefix = "(?:.*/)?" if not leading_doublestar else "(?:.*/)?"
        pattern = f"^{prefix}{body_re}(?:/.*)?$"
    else:
        # Multi-segment unanchored rule: match at any prefix depth.
        pattern = f"^(?:.*/)?{body_re}$"

    return re.compile(pattern)


def _compiled_rules(path: Path) -> list[re.Pattern[str]]:
    """Return compiled positive (non-comment, non-negation) rule patterns."""
    patterns: list[re.Pattern[str]] = []
    for raw in _read_lines(path):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        compiled = _compile_rule(line)
        if compiled is not None:
            patterns.append(compiled)
    return patterns


def _is_ignored(rel_path: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(p.fullmatch(rel_path) is not None for p in patterns)


# ---------------------------------------------------------------------------
# Ordered .dockerignore matcher (last-match wins, negation honored)
# ---------------------------------------------------------------------------
#
# Real .dockerignore semantics: lines are processed top-to-bottom; the LAST
# pattern that matches a given relative path decides ignore/include. A leading
# ``!`` negates the decision. We use the same _compile_rule() helper so glob
# shape is identical to the positive matcher above; we just iterate all
# non-comment rules in source order and toggle the verdict at each match.


def _compiled_ordered_rules(path: Path) -> list[tuple[bool, re.Pattern[str]]]:
    """Return compiled (is_negation, pattern) pairs in source order.

    Comments and blank lines are skipped. A rule whose first non-whitespace
    character is ``!`` is treated as a negation entry.
    """
    rules: list[tuple[bool, re.Pattern[str]]] = []
    for raw in _read_lines(path):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negation = line.startswith("!")
        body = line[1:] if negation else line
        compiled = _compile_rule(body)
        if compiled is None:
            continue
        rules.append((negation, compiled))
    return rules


def _is_ignored_ordered(
    rel_path: str, rules: list[tuple[bool, re.Pattern[str]]]
) -> bool:
    """Return whether ``rel_path`` is ignored under ordered last-match rules.

    Walks the rule list in source order. Each rule that matches rel_path
    toggles the current verdict (positive -> ignored, negation -> included).
    A rel_path with no matches defaults to ``included`` (not ignored).
    """
    ignored = False
    for negation, pattern in rules:
        if pattern.fullmatch(rel_path) is not None:
            ignored = not negation
    return ignored


def _dockerignore_rules() -> list[tuple[bool, re.Pattern[str]]]:
    return _compiled_ordered_rules(DOCKERIGNORE)


def _is_dockerignored(rel_path: str) -> bool:
    return _is_ignored_ordered(rel_path, _dockerignore_rules())


# ---------------------------------------------------------------------------
# .gitignore assertions
# ---------------------------------------------------------------------------


# Files that the parent plan calls out as sensitive. Each must be matched by
# some positive .gitignore rule.
SENSITIVE_GITIGNORE_TARGETS = (
    "secrets/ingest-tokens.json",
    "config/secrets/phone.env",
    "config/phone.env",
    "config/runtime.env",
    "phone.env",
    "tls/server.pem",
    "tls/server.key",
    "tls/server.p12",
    "logs/termux-boot.log",
    "logs/dashboard.log",
    "local-state/poll-state.json",
    "scripts/deploy/activation-receipt.json",
    "descriptors/activation.json",
    "dist/releases/20260101T000000Z-deadbeef/ispindel.tar.zst",
    "release-staging/ispindel-pending.tar.gz",
    "backups/backup.tar.zst",
)


def test_gitignore_exists() -> None:
    assert GITIGNORE.is_file(), "repository .gitignore is missing"


def test_gitignore_protects_examples() -> None:
    """The tracked example files must NOT be ignored by .gitignore."""
    lines = _read_lines(GITIGNORE)
    negations = [ln.strip().lstrip("!") for ln in lines if ln.strip().startswith("!")]
    for example in (".env.example", "phone.env.example", "compose.env.example"):
        assert example in negations, (
            f"{example} must be explicitly whitelisted via a negation rule"
        )


@pytest.mark.parametrize("rel_path", SENSITIVE_GITIGNORE_TARGETS)
def test_gitignore_protects_sensitive_paths(rel_path: str) -> None:
    rules = _compiled_rules(GITIGNORE)
    assert _is_ignored(rel_path, rules), (
        f".gitignore must match {rel_path!r} (sensitive file unprotected)"
    )


# Paths that must be caught by the explicitly required .gitignore additions.
# Each one is matched by exactly one of the new dedicated patterns.
EXPLICIT_GITIGNORE_TARGETS = (
    # *.local.env (vendor-style scoped env overrides)
    ("config/dev.local.env", "*.local.env"),
    ("secrets/api.local.env", "*.local.env"),
    # *.pfx (PKCS bundles beyond the existing *.p12)
    ("tls/server.pfx", "*.pfx"),
    # *.sqlite / *.sqlite3 (sqlite variants beyond the broad *.db)
    ("data/brews.sqlite", "*.sqlite"),
    ("data/brews.sqlite3", "*.sqlite3"),
    # *.bak (backup artifacts)
    ("config/ispindel.yaml.bak", "*.bak"),
    # *.partial (in-flight download / upload artifacts)
    ("downloads/release.partial", "*.partial"),
    # activation.json (root-level exact match, defensive alongside **/activation*.json)
    ("activation.json", "activation.json"),
    # backup-command-output.json (backup script stdout capture)
    ("backup-command-output.json", "backup-command-output.json"),
    # .local/state/ (per-repo Hermes scratch directory)
    (".local/state/plans/local.md", ".local/state/"),
    (".local/state/notes.md", ".local/state/"),
)


@pytest.mark.parametrize("rel_path,expected_rule", EXPLICIT_GITIGNORE_TARGETS)
def test_gitignore_has_explicit_required_patterns(
    rel_path: str, expected_rule: str
) -> None:
    """Each explicitly required .gitignore pattern must exist as a standalone rule.

    Verifies the rule is present (not just functionally matched by some broader
    rule) so future refactors can't silently rely on side-effects.
    """
    rules = _compiled_rules(GITIGNORE)
    assert _is_ignored(rel_path, rules), (
        f".gitignore must match {rel_path!r} "
        f"(explicit pattern {expected_rule!r} missing)"
    )
    # The expected dedicated pattern must be present as a standalone rule line.
    text = GITIGNORE.read_text()
    assert expected_rule in text.splitlines(), (
        f".gitignore must contain explicit rule {expected_rule!r} "
        f"(missing required pattern)"
    )


# Tracked wheels must still be trackable: the new *.bak / *.partial / *.sqlite
# patterns must not accidentally swallow the in-repo wheels/ tree.
TRACKED_WHEEL_TARGETS = (
    "wheels/SHA256SUMS",
    "wheels/annotated_types-0.8.0-py3-none-any.whl",
    "wheels/httptools-0.8.0-cp312-cp312-manylinux1_x86_64.manylinux_2_28_x86_64.manylinux_2_5_x86_64.whl",
)


@pytest.mark.parametrize("rel_path", TRACKED_WHEEL_TARGETS)
def test_gitignore_preserves_tracked_wheels(rel_path: str) -> None:
    rules = _compiled_rules(GITIGNORE)
    assert not _is_ignored(rel_path, rules), (
        f".gitignore accidentally ignores tracked wheel {rel_path!r}"
    )


# Tracked .env.example files must remain un-ignored after the new *.local.env rule.
TRACKED_ENV_EXAMPLE_TARGETS = (
    ".env.example",
    "ops/android/phone.env.example",
    "ops/container/compose.env.example",
)


@pytest.mark.parametrize("rel_path", TRACKED_ENV_EXAMPLE_TARGETS)
def test_gitignore_preserves_tracked_env_examples(rel_path: str) -> None:
    # Use ordered last-match semantics so the explicit ``!.env.example`` /
    # ``!phone.env.example`` / ``!compose.env.example`` negations override
    # the broader ``.env`` / ``.env.*`` / ``*.env`` positive rules. The
    # positive-only matcher would falsely flag these as ignored.
    rules = _compiled_ordered_rules(GITIGNORE)
    assert not _is_ignored_ordered(rel_path, rules), (
        f".gitignore accidentally ignores tracked env example {rel_path!r}"
    )


# ---------------------------------------------------------------------------
# .dockerignore assertions
# ---------------------------------------------------------------------------


# Representative unrelated root paths the Dockerfile does NOT need.
# They must remain excluded by the closure even though they have no dedicated
# deny rule — the default-deny tail block must catch them.
UNRELATED_ROOT_TARGETS = (
    "README.md",
    "Makefile",
    "config",
    "config/ispindel.yaml",
    "contracts",
    "contracts/schema.json",
    "requirements-test.lock",
    "requirements-test.in",
    "requirements-android.txt",
    "release",
    "release/notes.md",
    "schemas",
    "schemas/manifest.json",
    ".gitleaks.toml",
    ".gitignore",
    "docker-compose.yml",
    "phase3_research.md",
    "report.md",
    ".pytest_cache",
)


# Files inside the project root that the build context must NOT see.
SENSITIVE_DOCKERIGNORE_TARGETS = (
    "data/brew.db",
    "data/ispindel.db",
    "logs/dashboard.log",
    ".private/plans/local.md",
    "descriptors/activation.json",
    "dist/releases/20260101T000000Z-deadbeef/ispindel.tar.zst",
    "release-staging/pending.tar.gz",
    "tests/test_smoke.py",
    "ops/sudoers/ispindel",
    "scripts/backup-production.py",
    "docs/README.md",
    "backups/backup.tar.zst",
    "secrets/ingest-tokens.json",
    "local-state/poll-state.json",
)


# Paths the Dockerfile COPY closure explicitly requires to remain in context.
DOCKERFILE_COPY_REQUIRED = (
    "requirements.txt",
    "requirements.lock",
    "wheels",
    "wheels/somepkg-1.0-py3-none-any.whl",
    "app",
    "app/main.py",
)


def test_dockerignore_exists() -> None:
    assert DOCKERIGNORE.is_file(), "repository .dockerignore is missing"


@pytest.mark.parametrize("rel_path", SENSITIVE_DOCKERIGNORE_TARGETS)
def test_dockerignore_protects_sensitive_paths(rel_path: str) -> None:
    rules = _dockerignore_rules()
    assert _is_ignored_ordered(rel_path, rules), (
        f".dockerignore must match {rel_path!r} (build context would leak it)"
    )


@pytest.mark.parametrize("rel_path", DOCKERFILE_COPY_REQUIRED)
def test_dockerignore_preserves_dockerfile_copy_closure(rel_path: str) -> None:
    """The whitelist paths the Dockerfile COPYs MUST stay in the context."""
    rules = _dockerignore_rules()
    assert not _is_ignored_ordered(rel_path, rules), (
        f".dockerignore accidentally excludes Dockerfile COPY source {rel_path!r}"
    )


@pytest.mark.parametrize("rel_path", UNRELATED_ROOT_TARGETS)
def test_dockerignore_ignores_unrelated_root_paths(rel_path: str) -> None:
    """Representative unrelated root paths must fall under the default-deny tail.

    The Dockerfile COPY closure only needs requirements.txt, requirements.lock,
    wheels/, and app/. Any other tracked root path is unrelated and must be
    excluded by .dockerignore.
    """
    rules = _dockerignore_rules()
    assert _is_ignored_ordered(rel_path, rules), (
        f".dockerignore must ignore unrelated root path {rel_path!r} "
        f"(default-deny closure is incomplete)"
    )


def test_dockerignore_four_path_closure_is_tightly_scoped() -> None:
    """The closure re-includes exactly the four required source roots and
    nothing else. Any extra re-inclusion would defeat the default-deny tail.
    """
    text = DOCKERIGNORE.read_text()
    negations = [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip().startswith("!")
    ]
    # The four-path closure must add exactly these six negations. The
    # earlier ``!.env.example`` whitelist is preserved as-is.
    closure_negations = {
        "!requirements.txt",
        "!requirements.lock",
        "!wheels/",
        "!wheels/**",
        "!app/",
        "!app/**",
    }
    assert closure_negations.issubset(set(negations)), (
        f".dockerignore must include all four-path closure negations "
        f"{sorted(closure_negations)} (found {sorted(set(negations))})"
    )
    # The full negation set must equal the closure negations plus the
    # pre-existing ``!.env.example`` whitelist. Anything else is an
    # accidental re-inclusion that defeats the default-deny tail.
    expected = closure_negations | {"!.env.example"}
    assert set(negations) == expected, (
        f".dockerignore negations must be exactly {sorted(expected)} "
        f"(found {sorted(set(negations))})"
    )


def test_dockerignore_has_default_deny_tail() -> None:
    """The .dockerignore must end with a ``*`` rule (default-deny) so any
    unmentioned path is excluded from the Docker build context.
    """
    text = DOCKERIGNORE.read_text()
    positive_lines = [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    # Locate the bare default-deny ``*`` rule (exact match, not ``*.ext``).
    star_idx = next(
        (i for i, ln in enumerate(positive_lines) if ln == "*"),
        None,
    )
    assert star_idx is not None, (
        ".dockerignore must contain a default-deny '*' rule"
    )
    # The default-deny ``*`` must precede the four-path closure negations so
    # that ordered last-match semantics let those negations override it. We
    # locate the FIRST closure negation (``!requirements.txt``); earlier
    # negations like ``!.env.example`` are unaffected because their re-included
    # paths are not matched by ``*`` in the first place.
    closure_negations = {
        "!requirements.txt",
        "!requirements.lock",
        "!wheels/",
        "!wheels/**",
        "!app/",
        "!app/**",
    }
    closure_indices = [
        i for i, ln in enumerate(positive_lines) if ln in closure_negations
    ]
    assert closure_indices, (
        ".dockerignore must contain at least one four-path closure negation"
    )
    first_closure_idx = min(closure_indices)
    assert star_idx < first_closure_idx, (
        ".dockerignore default-deny '*' must precede the four-path closure "
        "negations so the re-includes are evaluated after it (ordered "
        "last-match semantics)"
    )


# Paths that must be caught by the explicitly required .dockerignore additions.
# Each one is matched by exactly one of the new dedicated patterns.
EXPLICIT_DOCKERIGNORE_TARGETS = (
    # .local/ (per-repo Hermes scratch / plan state)
    (".local/state/plans/local.md", ".local/"),
    (".local/notes.md", ".local/"),
    # *.tar (raw tar archives beyond the existing .tar.zst / .tar.gz / .tgz)
    ("downloads/release.tar", "*.tar"),
    ("scratch/build.tar", "*.tar"),
    # *.partial (in-flight download / upload artifacts)
    ("downloads/release.partial", "*.partial"),
)


@pytest.mark.parametrize("rel_path,expected_rule", EXPLICIT_DOCKERIGNORE_TARGETS)
def test_dockerignore_has_explicit_required_patterns(
    rel_path: str, expected_rule: str
) -> None:
    """Each explicitly required .dockerignore pattern must exist as a standalone rule.

    Verifies the rule is present (not just functionally matched by some broader
    rule) so future refactors can't silently rely on side-effects.
    """
    rules = _dockerignore_rules()
    assert _is_ignored_ordered(rel_path, rules), (
        f".dockerignore must match {rel_path!r} "
        f"(explicit pattern {expected_rule!r} missing)"
    )
    # The expected dedicated pattern must be present as a standalone rule line.
    text = DOCKERIGNORE.read_text()
    assert expected_rule in text.splitlines(), (
        f".dockerignore must contain explicit rule {expected_rule!r} "
        f"(missing required pattern)"
    )


# ---------------------------------------------------------------------------
# ops/container/compose.env.example assertions
# ---------------------------------------------------------------------------


# The vars docker-compose.yml marks with :? and which compose refuses to
# substitute when unset.
REQUIRED_EXAMPLE_VARS = ("ISPINDEL_GID", "ISPINDEL_EVIDENCE_DIR", "ISPINDEL_SECRETS_DIR")

# Vars the compose file exposes with safe local defaults; the example may
# override them with commented hints.
SAFE_OVERRIDE_VARS = (
    "ISPINDEL_IMAGE_REF",
    "ISPINDEL_CONTAINER_NAME",
    "ISPINDEL_DATA_VOLUME",
    "ISPINDEL_BACKEND_PUBLISH",
    "ISPINDEL_BACKUP_HEALTH_FILE",
)


def test_compose_env_example_exists() -> None:
    assert COMPOSE_EXAMPLE.is_file(), (
        "ops/container/compose.env.example is required by the plan"
    )


@pytest.mark.parametrize("name", REQUIRED_EXAMPLE_VARS)
def test_compose_env_example_declares_required_vars(name: str) -> None:
    body = COMPOSE_EXAMPLE.read_text()
    # Look for an uncommented, assigned ``NAME=`` line.
    pattern = re.compile(rf"^(?!#)\s*{re.escape(name)}\s*=", re.MULTILINE)
    assert pattern.search(body), (
        f"compose.env.example must declare {name}= (required by docker-compose.yml)"
    )


@pytest.mark.parametrize("name", SAFE_OVERRIDE_VARS)
def test_compose_env_example_documents_safe_overrides(name: str) -> None:
    body = COMPOSE_EXAMPLE.read_text()
    assert name in body, (
        f"compose.env.example should at least mention override {name}"
    )


def test_compose_env_example_has_no_inline_secrets() -> None:
    """The example must not carry tokens, PEM blocks, or JWT-style secrets."""
    body = COMPOSE_EXAMPLE.read_text()
    forbidden_substrings = (
        "BEGIN PRIVATE KEY",
        "BEGIN RSA PRIVATE KEY",
        "BEGIN CERTIFICATE",
        "Bearer ",
        "ghp_",
        "xoxb-",
        "password=",
        "secret_token=",
    )
    for needle in forbidden_substrings:
        assert needle not in body, (
            f"compose.env.example must not contain {needle!r}"
        )


def test_compose_env_example_has_no_real_tailnet_or_hostnames() -> None:
    """No live Tailnet FQDNs, IPv4 literals, or production hostnames."""
    body = COMPOSE_EXAMPLE.read_text()
    ipv4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
    assert not ipv4.search(body), "compose.env.example must not embed raw IPv4 addresses"
    tailnet = re.compile(
        r"\b100\.(?:6[4-9]|[7-9]\d|1[0-1]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"
    )
    assert not tailnet.search(body), "compose.env.example must not embed overlay-network IPs"
    for needle in (".corp", ".internal", ".lan", ".local", "tailnet", "ts.net"):
        assert needle not in body.lower(), (
            f"compose.env.example must not mention {needle!r}"
        )


def test_compose_env_example_has_no_production_paths() -> None:
    """No absolute /var/lib, /var/backups, /etc paths; only local-state hints."""
    body = COMPOSE_EXAMPLE.read_text()
    forbidden_paths = (
        "/var/lib/ispindel",
        "/var/backups/ispindel-dashboard",
        "/etc/ispindel",
    )
    for needle in forbidden_paths:
        assert needle not in body, (
            f"compose.env.example must not reference production path {needle!r}"
        )


# ---------------------------------------------------------------------------
# Dockerfile / docker-compose.yml cross-checks
# ---------------------------------------------------------------------------


def test_dockerfile_copy_paths_stay_in_context() -> None:
    """Cross-check: every path the Dockerfile COPYs must survive .dockerignore."""
    text = DOCKERFILE.read_text()
    rules = _dockerignore_rules()
    for match in re.finditer(r"^COPY\s+(\S+)\s+(\S+)\s*$", text, re.MULTILINE):
        src = match.group(1)
        # For directory copies (``wheels/``) the prefix is the directory name.
        prefix = src.rstrip("/") if src.endswith("/") else src
        assert not _is_ignored_ordered(prefix, rules), (
            f"Dockerfile COPY source {src!r} is excluded by .dockerignore"
        )


def test_compose_yaml_references_example_vars() -> None:
    """Compose must reference the vars the example declares, end-to-end."""
    text = COMPOSE_YML.read_text()
    for name in REQUIRED_EXAMPLE_VARS:
        assert name in text, (
            f"docker-compose.yml must reference required variable {name}"
        )
    for name in SAFE_OVERRIDE_VARS:
        assert name in text, (
            f"docker-compose.yml must reference override variable {name}"
        )