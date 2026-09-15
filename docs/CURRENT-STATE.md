# Current State — Pre-Reliable-Production Baseline

Observed at: 2026-08-02T22:16:30Z

## Local source

- Root: `/path/to/ispindel-dashboard`
- The project was previously an untracked subtree of the operator home (`/path/to/...`); the baseline phase creates a dedicated repository here.
- Local `app/main.py` SHA-256: `d4e38fa2cfb8c0c9806c43d9ebc1fb7574773680ded52f7331ee96fa5d6ad105`.
- The baseline suite produced 196 passes and two known harness failures: runtime image variables were not self-resolved and the Compose command assertion did not match the Dockerfile-owned command.

## Live predecessor

- Host: `<PRODUCTION_HOSTNAME>` on `<TAILNET_CADDY_HOST>` (private production host).
- Container: `ispindel-dashboard`, healthy.
- Image ID: `sha256:0797406bfac38a2cffc13d562ed908dc0c656b84e716bc14dd945b865604237f`.
- Data volume: `ispindel-dashboard_ispindel-data`.
- Database: SQLite WAL, `integrity_check=ok`, 2 devices, 18 samples, 2 migration rows.
- Host exposure: `0.0.0.0:8098` and `[::]:8098` directly to FastAPI.
- Container `app/main.py` SHA-256 matches local source exactly.
- Compose metadata references a temporary override under `/tmp/ispindel-phase06a1-exact-build-20260730T023218Z-114bd2aa2a0dfe0943295766b177da0f/compose.override.yml`; that file is missing. The running container therefore remains functional but its recorded Compose invocation is not reproducible.

## Exact descriptors

- `descriptors/predecessor-production.json` records redacted container, image, volume, mount, Compose-label, source-hash, and read-only database metadata.
- `contracts/ispindel-predecessor-expected-missing-amendment-v3.json` binds the one frozen null-hash Compose input to the old release and predecessor descriptor. It is candidate deployment authority only when the exact two-key Stage 01 activation is present; it does not reconstruct the missing file.
- `descriptors/local-r4c-candidate.json` records the exact local source inventory before remediation.
- No database bytes or secrets were copied into the repository.

## Safety boundary

The baseline phase performed read-only production discovery only. It did not restart a service, alter Headscale, copy the production database, or change live files.
