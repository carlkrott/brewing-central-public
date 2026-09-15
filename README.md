# iSpindel Local Dashboard

A small FastAPI/SQLite dashboard that ingests telemetry from stock iSpindel
devices on the local network and serves a stable operator page to authorized
Headscale/Tailscale clients. The deployment runs on a single Linux host
with an Android phone as the secondary application host.

This README is the **operator entry point**. For deeper material read:

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — current vs. target architecture
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — services, timers, evidence, alerts
- [docs/SECURITY.md](docs/SECURITY.md) — network boundary, auth model, tokens
- [docs/RECOVERY.md](docs/RECOVERY.md) — backup, restore rehearsal, rollback
- [docs/HARDENING-BACKLOG.md](docs/HARDENING-BACKLOG.md) — deferred hardening
- [docs/CURRENT-STATE.md](docs/CURRENT-STATE.md) — verified pre-remediation baseline

## Current vs. target status

The shipping state on the production host is a **predecessor** container
publishing FastAPI directly on port `8098` with no TLS, no auth, and
host-pinned evidence paths. The target architecture in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) splits LAN ingest from
authenticated tailnet UI behind a reverse proxy, but the security and
operations work to reach that target is **not yet deployed**. Do not
interpret this README as proof that the target is reached.

> **Note:** release-payload descriptors and other generated artifacts
> are produced by the internal release pipeline and are intentionally
> omitted from source exports. They are not part of this repository.

## Stock iSpindel firmware configuration

Until the target deploys, the predecessor accepts any JSON payload that
includes a device identity field. **That is the bug the LAN/tailnet
split fixes.** The configuration stock firmware should use on the target
is:

| Field | Value |
|---|---|
| Server URL | `http://<LAN_INGEST_HOST>:8098/api/ingest` |
| Method | `POST` |
| Content-Type | `application/json` |
| Body field `ID` (or `id` / `device_id` / `name`) | stable per-device string |
| Body field `token` | per-device shared secret (set after LAN/tailnet split) |
| Body: telemetry | `temperature`, `gravity`, `battery`, `RSSI`, `angle` — stock schema |

Stock firmware has no header/query auth capability. The credential lives in the
JSON body as `token`. Until the target deploy, only the LAN path is reachable;
the tailnet HTTPS page is **not** yet provisioned.

## Stable tailnet URL (target state)

The target page is reached at `https://$TAILNET_FQDN/` where `TAILNET_FQDN`
is captured live from `tailscale status --json` on the deployment host
during the tailnet/HTTPS hardening work. It is bound into the deployed
Caddy config and into `/etc/ispindel/production.env`. Do **not** hardcode
magical DNS names in clients; always read the value from the deployment
environment.

Before the target TLS listener exists, this URL is not resolvable to the
dashboard. The current page is reachable only at
`http://<TAILNET_CADDY_HOST>:8098/` with no auth.

## Internal CA installation (target state)

The target HTTPS listener uses `tls internal` and serves a
reverse-proxy-issued certificate. Authorized phones and laptops must
trust the reverse-proxy root CA.

- **Locate the CA**: `~/.local/share/caddy/pki/authorities/local/root.crt`
  on the deployment host (read-only for operators).
- **Install on iOS**: AirDrop or email the cert to the device, open the
  profile, then go to *Settings → General → About → Certificate Trust
  Settings* and enable full trust for the Caddy root.
- **Install on macOS**: double-click the cert, open *Keychain Access → System*,
  add the cert, and set *When using this certificate* to *Always Trust*.
- **Install on Linux/Windows**: copy the cert into the system trust store or
  import it into the browser's certificate manager.
- **Remove**: revoke on the device's certificate trust list and delete the
  profile. The Caddy root remains valid for other authorized clients.

The internal CA is **not** trusted by stock browsers until installed. There is
no Let's Encrypt / public CA in this deployment.

## What this repository is not

- **Not** a public-internet service. There is no Funnel, no cloud, no public
  DNS.
- **Not** an OAuth / SSO integration. Auth is HTTP Basic on the tailnet
  surface only.
- **Not** a clustered service. One FastAPI process owns the SQLite database.
- **Not** a Tailscale-Serve-only deploy. The ESP8266 firmware requires plain
  LAN HTTP, so a LAN listener is mandatory.

## Project layout

```
app/        FastAPI application (single owner of SQLite)
docs/       Operator documentation (you are here)
descriptors/  Read-only production/candidate metadata
docker-compose.yml  Portable candidate container definition (not production yet)
scripts/    Operator scripts (poller, backup, etc.)
tests/      Test suite
Dockerfile  Container image recipe
```

## Platform boundary (qualification scope)

The deployment model has **two** independent runtime targets and they do
not overlap:

- The Docker image defined by `Dockerfile` and `docker-compose.yml` is
  qualified for **`linux/amd64` only**. It builds and runs on the
  deployment host and in CI (`ubuntu-latest`). No ARM64 / Android /
  phone-class Docker image is qualified, built, or published by this
  repository.
- The Android phone remains a **native Termux** runtime. Phone
  dependencies live in `requirements-android.txt` (Python 3.14) and are
  installed via Termux `pip`, not inside any container. The phone is
  never a Docker target.

The container CI in `.github/workflows/container.yml` is **disposable
source/runtime proof**: it builds the image, emits an SBOM, runs the
Docker runtime contract, and tears the project down. It does not push
the image to a registry and it does not deploy. Any registry push would
have to come from a separate, explicitly authorised release workflow.

For repository security rules (private advisory reporting, what must
never be committed, where credentials live) see
[SECURITY.md](SECURITY.md).

## Required reading before any production action

Read [docs/RECOVERY.md](docs/RECOVERY.md) and
[docs/HARDENING-BACKLOG.md](docs/HARDENING-BACKLOG.md) before changing
anything in production. Several hardening slices (Phase 7 C/D/E) are
explicitly deferred and have re-open triggers.