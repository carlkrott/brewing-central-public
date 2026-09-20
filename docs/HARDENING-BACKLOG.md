# Hardening Backlog — Phase 7 Deferred Slices

This document records the Phase 7 hardening slices that were **deliberately
deferred** for the reliable-local baseline release. Each slice has:

- A **what** it accomplishes.
- A **threat** it protects against.
- The **current compensating control** that holds for the baseline.
- The **re-open trigger** that brings it back into the critical path.

The slices are **not** release blockers. They are explicitly deferred. Read
this file before any change in scope, host, or distribution path; any of
those may convert a deferred slice into a blocker.

## Summary

| Slice | Protects against | Current compensating control | Status |
|---|---|---|---|
| **C** — Transitive descriptor binding | A transitive module being substituted outside the wheel lock | Closed Phase 7B wheelhouse, `--require-hashes`, `build.network: none` | **DEFERRED** |
| **D** — Docker daemon attestation | A compromised daemon lying about image/build state | Single-owner local host, image ID inspection, offline build, exported predecessor image, served-byte validation | **DEFERRED** |
| **E** — Cross-host reviewer independence | One host compromising both artifact and review evidence | Dual reviews plus exact hashes; production is a single private local service | **DEFERRED** |

The current compensating controls are real and sufficient **for the current
deployment shape**: a single private local service whose build is reproducible
from the local source tree under a closed wheelhouse. They become
insufficient when the distribution shape changes.

## Slice C — Transitive descriptor binding

### What it would do

Bind every transitively required artifact (Python wheel, system library,
network-fetched font, etc.) to a canonical descriptor that is independently
re-hashable from the wheel lock. Substituting any transitive module outside
the lock would be detectable by hash comparison rather than only by image
diff.

### Threat

An attacker (or a quiet pipeline break) substitutes a transitive module
that satisfies the lock's top-level constraint but is not the exact pinned
file. Under the current model, the only way to detect this is to rebuild
and compare image IDs; in distributed builds, the first image ID is the
"good one" and the substituted one is the "good one" for everyone.

### Current compensating control

The build uses a closed Phase 7B wheelhouse with `--require-hashes` and
`build.network: none`. The wheelhouse itself is hashed and recorded in the
release manifest. Substitution at the wheel layer requires breaking the
wheelhouse itself, which is the same control plane that holds the freeze.

### Re-open trigger

Re-open Slice C if any of the following becomes true:

- **Artifacts are distributed to another organization** outside the current
  host.
- **Artifacts are pushed to an external registry** that the build host
  cannot verify with the current lock/freeze.
- **A build system** that is not under the same review control plane is
  used to rebuild or repackage the dashboard.
- **The wheelhouse is rebuilt from a non-immutable source** (e.g., a live
  PyPI mirror or a re-pinned `requirements.txt`).

Until then, the closed wheelhouse plus exact-hash release binding is
sufficient.

## Slice D — Docker daemon attestation

### What it would do

Attest that the Docker daemon's image inventory, build cache, and runtime
state match what the build host believes. Detect a daemon that lies about
the image ID of an already-built image, or that serves a different image
under the same tag.

### Threat

A compromised Docker daemon returns `sha256:bf433…` to `docker image
inspect` but actually serves a different layer set on `docker run`. The
application-side checks would all pass because the runtime image matches
the build image — but neither matches the audited bytes.

### Current compensating control

- **Single-owner local host.** The build host is the same as the production
  host.
- **Image ID inspection** at every verification step (`docker image
  inspect` against the recorded hash).
- **Offline build** under `build.network: none`.
- **Exported predecessor image** (`docker save`) captured pre-promotion.
- **Served-byte validation** in Stage 04: the served HTML/JS/CSS hashes
  match the release manifest.

The build chain is short and the host is trusted. The principal of least
trust is observed at the host boundary.

### Re-open trigger

Re-open Slice D if any of the following becomes true:

- **Builders become multi-tenant** (e.g., shared CI runners, a build farm,
  a remote builder).
- **Builds happen on a remote host** that the production host does not
  control.
- **The image is signed or published** externally (e.g., to a public
  registry, a Sigstore cosign signing flow, or a notary).
- **The Docker daemon is upgraded to a feature set** that introduces a
  new build-time trust boundary (e.g., BuildKit with remote snapshotter).

Until then, the offline build plus served-byte validation is sufficient.

## Slice E — Cross-host reviewer independence

### What it would do

Require that artifact review and artifact production happen on **different
hosts**, so that one host compromising both the artifact and the review
evidence is detectable.

### Threat

A compromised host produces both the release artifact and the bytes that
the “reviewer” affirms. The release looks reviewed to anyone who only
checks the review file.

### Current compensating control

- **Two reviewers** sign the Phase 7B freeze and the release manifest.
- **Exact hashes** bind the review to the artifact.
- **Production is a single private local service.** The only host that
  matters is the deployment host, and the operator personally knows what builds
  are on it.

The dual-review guarantee is real for the freeze evidence; the
cross-host guarantee is not, because all reviews happen on the same
operator-owned filesystem.

### Re-open trigger

Re-open Slice E if any of the following becomes true:

- **External distribution** of the dashboard or its image beyond the local
  deployment host.
- **Regulated use** (e.g., medical, industrial, or compliance-mandated
  deployment) where a single-host review is not acceptable.
- **Multi-host promotion** (e.g., promoting the same release to a second
  production host with different operational owners).
- **Any third party** is expected to consume the release artifact and
  require independent attestation.

Until then, the dual-review plus exact-hash release is sufficient.

## Slices that are **not** deferred

For the avoidance of doubt, the following concerns are **not** deferred.
They are part of the baseline release:

- **Auth and TLS on the tailnet UI.** T4 and T5 deliver HTTP Basic Auth
  and Caddy `tls internal` for the tailnet listener.
- **Path/method boundary on the LAN.** T5 delivers the Caddy LAN listener
  that allows only `POST /api/ingest`.
- **Verified backup and isolated restore rehearsal.** T7 delivers the
  backup algorithm, off-host copy, and monthly restore drill.
- **Deterministic health evidence and alerts.** T6 delivers the canonical
  `send-alert.sh`, the atomic writer, and the journald event contract.
- **Exact predecessor application rollback.** T9 + T11 deliver the
  five-stage deployment with `Stage 05` as the rollback entry point.
- **Phase 7B freeze repair.** T2 reconstructs the reviewed freeze
  mechanically and binds it to a new authority JSON.

The deferred slices are **above** that baseline.

## How to reopen a deferred slice

Each re-open trigger is a **trigger condition**, not a deadline. The
production plan does not schedule these slices. They are re-opened when
the trigger fires, by writing a new bounded plan that re-runs the
relevant phases of the production plan with the additional Slice C/D/E
controls in place.

A typical re-open looks like:

1. Document the trigger that fired and the new deployment shape that
   requires the slice.
2. Write a new bounded plan describing the new control plane (e.g., a new
   registry, a new build host, a new review host).
3. Add the required evidence to the release process (hash binding, daemon
   attestation, cross-host review).
4. Update the release GO/NO-GO checklist to require the new evidence.
5. Only then proceed with the new distribution.

The production plan is intentionally explicit about which controls are
deferred and which are not. Misreading this document as “we just haven't
gotten to it yet” is the failure mode this backlog is designed to
prevent.

## Read next

- [docs/ARCHITECTURE.md](ARCHITECTURE.md) for the deployment shape.
- [docs/SECURITY.md](SECURITY.md) for the auth boundary.
- [docs/RECOVERY.md](RECOVERY.md) for the recovery path.
- [docs/OPERATIONS.md](OPERATIONS.md) for service ownership.
