# Security Policy

This is the source repository for a self-hosted home brewing telemetry
stack. The repository is published publicly; the deployment is not.

The full threat model, auth boundary, and credential handling live in
[docs/SECURITY.md](docs/SECURITY.md); this file states only the
repository-facing rules.

## Supported versions

The only supported line is the current `main` branch and its short-lived
feature branches. No backports are issued. Older snapshots under `dist/`
are historical release artifacts and are not patched.

## Reporting a vulnerability

If this repository is hosted on a forge that exposes GitHub's private
**Security Advisory** flow (GitHub.com, GitHub Enterprise Server, and
compatible mirrors), use that flow for a private report. Do not file a
public issue, and do not open a public pull request that reproduces the
vulnerability.

If no private advisory channel is available on the host forge, follow
the contact instructions published on the maintainer's profile or the
project homepage. There is no project-specific disclosure email, no
security.txt, and no response SLA defined here — do not invent one.

## What must never be committed

The following are production state and must never appear in this
repository, in any branch, tag, release, fork, paste, or screenshot:

- `phone.env` and any other real `.env` files (the tracked
  `phone.env.example` / `compose.env.example` are templates only).
- Real iSpindel ingest tokens, Basic Auth hashes, or the contents of
  `/run/secrets/ingest-tokens.json` (the tracked
  `config/ingest-tokens.example.json` is a synthetic example).
- Gateway tokens, Headscale ACL private material, internal CA private
  keys (`*.key`) or any other private key / certificate.
- Live SQLite databases (`*.db`, `*.db-wal`, `*.db-shm`) and the local
  `data/` directory.
- Backups, restore drills, and activation receipts (`backups/`,
  `**/receipts/`, `**/activation*.json`).
- Operator dumps, journal excerpts, and screenshots that contain
  secrets, internal hostnames, MagicDNS names, real port numbers on
  production hosts, or token-shaped strings.

The repository `.gitignore` excludes `.env*`, `secrets/`, `*.db`,
`phone.env`, `backups/`, `**/receipts/`, `**/activation*.json`, `*.pem`,
`*.key`, and `*.p12`; `.dockerignore` excludes `data/`, `dist/`,
`release-staging/`, and `tests/` from the build context as its
default-deny four-path closure. `.gitleaks.toml` extends the default
ruleset to suppress synthetic-test UUIDs only.

## Where credentials and logs must live

- Real credentials live in `phone.env` (Termux, mode `0600`) and the
  `/run/secrets/` bind mounts on the deployment host. They are mounted
  read-only into the container and are not part of the image.
- Issue trackers, commit messages, pull request descriptions, CI logs,
  chat screenshots, and audit documents must not contain credentials,
  token-shaped strings, real hostnames, or internal URLs.

## Operational dependencies (not a vulnerability surface)

The following are operational dependencies of the deployed system and
are out of scope for this repository's vulnerability reporting:

- **Reverse proxy** and its internal CA on the deployment host.
- **Headscale / Tailscale** control plane and ACLs.
- **Termux** on the deployment Android phone and its `phone.env`.
- **Model service / GPU host** for combined-Gemma and other model
  endpoints reached over the Tailnet.

A failure or misconfiguration in any of these is an operational
incident, not a repository vulnerability. Handle them through the
existing operational runbooks and on-call paths, not via Security
Advisory.

## Operational rule

Production secrets, phone.env, tokens, private keys, databases,
backups, receipts, and dumps are produced and consumed outside this
repository. If something sensitive accidentally lands here, rotate the
credential and remove the content via the normal history-rewrite
process — do not just delete the file in a new commit.