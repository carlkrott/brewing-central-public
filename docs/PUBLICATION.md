# Public Publication Lane — Brewing Central

This document defines the source-only publication lane that produces the
**Brewing Central** public mirror at
[`carlkrott/brewing-central-public`](https://github.com/carlkrott/brewing-central-public)
from the private iSpindel dashboard repository. It is the only blessed path
for moving bytes into the public mirror.

> **Status (Sept 2026):** the workflow is **dormant**. `PUBLIC_REPO_TOKEN` is
> **not** configured on the private repository, so pushes and PRs do not
> occur. The workflow is wired and tested, but it will short-circuit cleanly
> with `publication=skipped reason=missing_secret` until the secret is added.

## 1. Source of truth

| Concern | Source |
| --- | --- |
| Tracked files | Private repo, at the exact commit named by the workflow |
| Untracked / ignored / `.git` contents | **Never** exported |
| Public mirror branch base | `carlkrott/brewing-central-public` `main` |
| Publication branch | `publication/run-<id>` (per-run, unique) |

The exporter reads files **only** via `git ls-tree` + `git cat-file --batch`
against the chosen source commit. The working tree, ignored files, and
untracked files are never consulted. There is no path by which a local
experiment can leak into the public mirror: the public tree is a
byte-for-byte deterministic function of the source commit and the policy
file.

## 2. Exact classification (today)

Every tracked path at the selected source commit is listed exactly once in
`config/public-export.json`, under either `public_paths` or `private_paths`.
The current private-only paths are:

| Path | Reason |
| --- | --- |
| `contracts/ispindel-predecessor-expected-missing-amendment-v2.json` | Private amendment set; not for public consumption |
| `descriptors/RELEASE.json` | Internal release descriptor; carries operator-only metadata |
| `.github/workflows/publish-public.yml` | The publication lane itself; the public mirror does not need to (and must not) push to itself |
Adding a path to `private_paths` is a **deletion** from the public mirror —
the operator is asserting that this file must never be published. Removing
an entry is a deliberate "this is now public" decision and must be reviewed
like a code change.

The exporter includes every path in `public_paths`, including the publication
policy and its focused test. The test generates token-shaped fixtures only in
temporary repositories; no credential-shaped value is stored in the test
source.

### 2.1 Exact public paths

`config/public-export.json` lists the exact `public_paths` set. A tracked path
is exported only if:

1. It is **not** in `private_paths`, **and**
2. It is a regular blob (`100644` or `100755`), **and**
3. It appears in `public_paths`.

New tracked files are **not** exported by default. Adding one requires an
explicit classification review and an exact `public_paths` or `private_paths`
entry in the same source change.

### 2.2 Failure modes for classification drift

The exporter **fails closed**: if a classified path is missing, a tracked path
is unclassified, a path is listed twice, or public/private sets overlap, the
export returns exit code 2 and writes nothing. This prevents a new private
file from being silently omitted from review.

## 3. Exporter invocation

The exporter is `scripts/export-public.py`. It is stdlib-only and works
against any working git repository. From the private repo root:

```bash
python scripts/export-public.py \
    --source-root   "$PWD" \
    --output-root   /tmp/brewing-central-public-export \
    --policy        config/public-export.json \
    --source-commit HEAD
```

Useful flags:

- `--check-only` — perform selection, extraction, and the forbidden-pattern
  scan, but write nothing to `--output-root`. The summary line includes the
  `aggregate_sha256` of the manifest, which can be diffed across runs.
- `--source-commit <ref>` — any commit-ish resolvable via `git rev-parse
  --verify <ref>^{commit}` (tag, branch, full SHA, `HEAD`).

Output structure under `--output-root`:

```
<output-root>/
  <every selected tracked file, mode 0644/0755 preserved>
  public-export.manifest.json
```

`public-export.manifest.json` lists every emitted file with `path`, `mode`,
`size`, `sha256`, plus the top-level `aggregate_sha256` digest.

### 3.1 Forbidden-pattern scan

After selection, every emitted file is scanned byte-wise against the regex
set in `config/public-export.json` → `forbidden_patterns`. The set covers
the canonical secret shapes:

- AWS access key ID and `aws_secret_access_key`.
- GitHub classic (`ghp_…`) and fine-grained (`github_pat_…`) tokens.
- Slack bot/user tokens (`xox[baprs]-…`).
- PEM private key headers.
- RFC 1918/private IPv4 ranges, CGNAT ranges, and Tailnet DNS (`*.ts.net`).
- Operator Tailnet host env vars and Bearer JWTs.

Patterns intentionally **do not** match documentation examples:

- `192.0.2.1`, `198.51.100.1`, `203.0.113.1` (RFC 5737) — not in the IP
  blocklist.
- `example.test`, `example.com` — not flagged.

The full list lives in `config/public-export.json`, which is published with
the source exporter so reviewers can inspect the exact classification.

A hit returns exit code 3 and writes nothing (because `--check-only`
produces a manifest only, and the full path also aborts before writing).

## 4. Missing-secret behavior

While `PUBLIC_REPO_TOKEN` is absent on the private repo, the workflow:

1. Detects the absence on the **first step** (`Gate on PUBLIC_REPO_TOKEN`).
2. Emits `publication=skipped reason=missing_secret` to `$GITHUB_OUTPUT`
   and `$GITHUB_STEP_SUMMARY`.
3. Records a "Publication skipped" section in the run summary explaining
   why no push, PR, or working-tree mutation occurred.
4. **Exits successfully** (exit 0) so the absence does not page on-call.

No token value is ever logged or echoed. The `Skip record` step never has
access to the secret environment; all later steps are gated on
`steps.gate.outputs.publication == 'ready'` and do not run when the gate is
`skipped`.

When the secret becomes available, the workflow runs the full lane
unchanged — no schema change is required, just the addition of the secret.

## 5. Required credential boundary

`PUBLIC_REPO_TOKEN` must:

- Be a fine-grained PAT (or GitHub App installation token) bound to
  `carlkrott/brewing-central-public`.
- Have **only** `Contents: read & write` and **only** on the publication
  repo (not the private source).
- Be scoped to the public repo (`carlkrott/brewing-central-public`) — the
  workflow explicitly checks out `carlkrott/brewing-central-public` with
  this token and the **private** repo with the runner's default
  `GITHUB_TOKEN` (which has no cross-repo power by default).
- Never be logged, echoed, or copied into artifacts.

The workflow refuses to print the secret anywhere — `set -euo pipefail`,
`printf '%s'`, and GitHub's automatic secret masking handle this; do not
disable secret masking on this job.

## 6. No-force-push PR flow

The workflow **never** force-pushes. The PR flow is:

1. `actions/checkout` private repo with `persist-credentials: false`.
2. `actions/checkout` public repo's `main` with the PAT, also
   `persist-credentials: false`.
3. Exporter writes the new public tree INTO the public worktree (replacing
   prior contents of the worktree; public `main` is **not yet touched**).
4. The workflow creates a new local branch `publication/run-<id>` from `main`.
5. Commits any diff with a generic publication message and the private workflow
   run ID; private commit identities are not placed in public Git metadata.
6. `git push -u origin HEAD:refs/heads/<branch>` (no `--force`, no
   `--force-with-lease`).
7. POST `/repos/carlkrott/brewing-central-public/pulls` with `draft: true`
   and `base: main`. Public `main` remains untouched.
8. Auto-merge is **not** implemented in this workflow. Operators merge
   after the public repo's checks pass and the manifest matches the
   expected digest.

Every step after the gate uses `timeout-minutes: 20` at the job level and
the job is in concurrency group
`publish-public-${{ github.workflow }}-${{ github.ref }}` with
`cancel-in-progress: false` — overlapping runs queue rather than race.

### 6.1 Branch protection expectations on the public repo

`carlkrott/brewing-central-public` should have `main` protected with at
minimum:

- Require PRs before merging
- Require linear history (rebase) — supports the manual/rebase merge step
- Require the public repo's `tests`, `runtime`, `gitleaks`, and
  `analyze (python)` checks to pass before merge
- Disallow force-pushes and deletions on `main`

## 7. Manual / rebase merge

After the PR is opened, an operator:

1. Reads back the PR via `gh pr view` against
   `carlkrott/brewing-central-public` and confirms:
   - `public-export.manifest.json` `aggregate_sha256` matches the digest
     produced by a local `--check-only` run against the approved source.
   - `gitleaks` and `tree` scan pass on the public worktree.
2. Confirms that the only changed paths are present in `public_paths` and that
   no `private_paths` entry was reintroduced.
3. Rebases the PR (or merges with the rebase strategy allowed by branch
   protection) — never squash, which would re-author history.
4. Confirms via the GitHub UI that the merge commit landed on `main` and
   that the manifest still hashes the same way at HEAD.

Auto-merge is intentionally **not** wired up at this stage.

## 8. Rollback

To roll back a publication that has already landed on `main`:

1. Open a revert PR against `carlkrott/brewing-central-public`:
   `gh pr create --repo carlkrott/brewing-central-public --base main --head revert/<sha> --title "Revert publication <sha>"`.
2. Wait for the public repo's checks to pass on the revert PR.
3. Merge with the same rebase strategy used for the original publication.
4. After merge, the manifest SHA at HEAD reflects the rolled-back state;
   diff it against the production mirror to confirm.

We do **not** rewrite history on `main` — once a publication has landed,
roll forward via revert, not force-push.

## 9. Future: enabling the canonical branch

The workflow currently triggers on `push: branches: [main]` so it will fire
when the private repository's default branch is the canonical `main` branch.
Before enabling standard publication:

1. Switch the private repo's default branch to `main` (canonical) via the
   GitHub UI. Verify with `gh repo view --json defaultBranchRef`.
2. Add `PUBLIC_REPO_TOKEN` to the private repo's Actions secrets with the
   scope described in §5.
3. Re-run the workflow manually via **Run workflow** to validate the full
   lane end-to-end against a draft PR; inspect the manifest.
4. Only then enable push-triggered publication by leaving the workflow
   file as-is — the file is already configured to fire on `main` pushes.

Until step 1, the workflow can only be exercised via `workflow_dispatch`,
which is the intended operator surface for the current default-branch state.

## 10. Verification commands

Local smoke checks (no network, no GitHub writes):

```bash
# 1. Dry-run the exporter at HEAD against the policy in this repo.
python scripts/export-public.py \
    --source-root "$PWD" \
    --output-root "$(mktemp -d)" \
    --policy      config/public-export.json \
    --source-commit HEAD \
    --check-only

# 2. Run the focused pytest module.
python -m pytest -q tests/test_public_export.py
```

The first command prints the `aggregate_sha256` for HEAD; the second
exercises determinism, exclusion enforcement, forbidden-pattern rejection,
and manifest/hash equality across temporary git repositories.

## 11. File map

| File | Role |
| --- | --- |
| `scripts/export-public.py` | Exporter. Stdlib-only. Deterministic. Reads only tracked blobs via git plumbing. |
| `config/public-export.json` | Policy: exact `public_paths`/`private_paths` classification and `forbidden_patterns`. |
| `tests/test_public_export.py` | Focused pytest module. Temporary git repos only. |
| `.github/workflows/publish-public.yml` | Private-only workflow. No-op when `PUBLIC_REPO_TOKEN` is absent. |
| `docs/PUBLICATION.md` | This document. |
