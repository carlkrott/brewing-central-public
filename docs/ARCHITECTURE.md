# Architecture — Deployed Brewing Central

## Production state

The Android phone is the active application and database host. The
deployment host remains the stable ingress/TLS host and retains
predecessor configuration/image material as a cold rollback target.

```text
iSpindel <EXAMPLE_DEVICE_ID> on LAN
  -> POST http://<LAN_INGEST_HOST>:8098/api/ingest
  -> deployment host reverse proxy: ingest-only path/method boundary, 64 KiB cap
  -> http://<PHONE_TAILNET_HOST>:8098/api/ingest
  -> phone FastAPI: per-device verifier + rate limit
  -> phone SQLite ispindel.db

Tailnet browser
  -> https://<TAILNET_CADDY_HOST>/
  -> deployment host reverse proxy: internal TLS + Basic Auth
  -> http://<PHONE_TAILNET_HOST>:8098
  -> Brewing Central UI/API + phone SQLite brew.db

phone FastAPI -> loopback gateway :3100
               -> combined-Gemma text :8095 (<MODEL_SERVICE_HOST>)
               -> SearXNG :8080 (<SEARCH_SERVICE_HOST>) / Kiwix :9090 (<KIWIX_HOST>) / Wikipedia fallback

phone IP Webcam loopback :8080 -> camera observer -> combined-Gemma vision :8090 (<MODEL_SERVICE_HOST>)
```

Runtime ownership:

- Termux:Boot and Termux `RunCommandService` own phone boot recovery.
- `ops/android/start-phone-stack.sh` serializes startup and command-binds PID
  files for the gateway, Uvicorn, and the camera observer.
- The phone owns both live SQLite files; telemetry and brewing schemas remain
  separate so the exact telemetry schema guard is preserved.
- The deployment-host reverse proxy owns both supported external surfaces.
  Direct phone `:8098` is a Tailnet-restricted operations path, not the
  normal browser URL.
- Vision is structural-safety-only while the vessel is intentionally empty;
  image evidence never substitutes for telemetry.
- The deployment-host predecessor is not a second database writer or hot
  standby. Its named container is stopped cleanly; rollback must restore
  the reverse-proxy config and start the independently verified predecessor
  rather than assuming it is already live.
- The dashboard runs as one Uvicorn worker. The assistant queue is an
  in-process bounded queue with one worker thread, started and stopped by
  FastAPI lifespan; queued/running jobs are failed closed on restart.
- Brewing schema v3 is an additive v2-to-v3 migration. Recipe snapshots
  retain stable UUID keys for ingredients, cultures, additions, and
  process steps.

See [PHONE-RUNBOOK.md](PHONE-RUNBOOK.md) for lifecycle, recovery,
calibration, and exact operator checks.

## Platform boundary (qualification scope)

The deployment has **two** independent runtime targets and they do not
overlap:

- **Docker image — `linux/amd64` only.** The `Dockerfile` and
  `docker-compose.yml` are qualified for `linux/amd64` (deployment host
  and CI `ubuntu-latest`). No ARM64 / Android / phone-class Docker image
  is qualified, built, or published by this repository.
- **Phone — native Termux.** The Android phone remains a native Termux
  Python 3.14 runtime using `requirements-android.txt`. The phone is
  never a Docker target and never an ARM64 image target.

The container CI in `.github/workflows/container.yml` is disposable
source/runtime proof: it builds the image, emits an SBOM, runs the
Docker runtime contract, and tears the project down. It does not push
images to a registry and it does not deploy.

This section is additive. It does not change the production-state
section above, the historical CURRENT/TARGET comparison below, the data
flows, or the decision record.

## Historical predecessor/target comparison

The remainder records the earlier baseline-to-target design. Its
`CURRENT` and `TARGET` labels are historical and must not override the
deployed-state section above.

## Components

| Component | CURRENT (baseline) | TARGET (post hardening) |
|---|---|---|
| FastAPI application | `app/main.py`, single owner of SQLite | Same. One process, one DB. |
| Reverse proxy | None. Container publishes `0.0.0.0:8098` directly. | Reverse proxy v2.11.4, two listeners. |
| LAN listener | None (the app *is* the LAN listener) | Reverse proxy `<LAN_INGEST_HOST>:8098`, ingest-only. |
| Tailnet HTTPS listener | None | Reverse proxy `<TAILNET_CADDY_HOST>:443`, `tls internal`, Basic Auth. |
| TLS | None | Reverse proxy internal CA only. |
| Authentication | None (any client on `0.0.0.0:8098` reaches any route) | Per-device JSON `token` on LAN ingest; Basic Auth on tailnet UI. |
| Evidence directory | Host-pinned bind mounts, mode 0600, unreadable by container UID 10001 | `/var/lib/ispindel/evidence`, root-owned, mode 0750, container joins supplemental group for read-only access. |
| Scheduler | Manual daemon running | systemd timers for poll, heartbeat, backup, restore drill. |
| Notifications | Telegram via gateway (two cron jobs blocked by shell metacharacter policy) | Canonical `send-alert.sh` invoked by systemd timers; gateway is transport only. |
| Backups | None scheduled | `ispindel-backup.timer` every 6 h, off-host rsync to `<OFFHOST_BACKUP_HOST>`, retention 28 local / 56 remote / 12 weekly. |
| Database | SQLite WAL, `ispindel-dashboard_ispindel-data` volume, 2 devices, 18 samples, integrity `ok` | Same volume, schema V2, integrity verified before each release. |

## CURRENT data flow (predecessor)

```
Stock iSpindel on LAN
       │ POST http://0.0.0.0:8098/api/ingest
       ▼
Docker container publishes 0.0.0.0:8098 directly
       │ (no auth, no rate limit, no body cap above the reverse proxy;
       │  app middleware caps at 65 536 bytes)
       ▼
FastAPI app/main.py -> SQLite /data/ispindel.db
       │ (named volume: ispindel-dashboard_ispindel-data)
       ▼
Any host on the LAN can reach /, /api/devices, /api/ingest, etc.
       │ (no path/method boundary)
       ▼
Tailnet clients reach the same port on `<TAILNET_CADDY_HOST>:8098`
       │ (no TLS, no auth)
```

The predecessor's only "boundary" is the FastAPI middleware body cap and
whatever the application routes happen to do. There is no LAN/tailnet
split and there is no authentication.

## TARGET data flow

```
Stock iSpindel on LAN
       │ POST /api/ingest
       │ Body: {ID, token, telemetry…}
       ▼
`<LAN_INGEST_HOST>:8098` (reverse proxy LAN listener, bound to `<LAN_INGEST_HOST>`)
       │ allow: POST /api/ingest only
       │ deny: every other method/path -> 404
       │ body max 64 KiB
       ▼
127.0.0.1:18098 (Docker-published loopback only)
       │ FastAPI: token validation + token-bucket limit + 64 KiB body cap
       ▼
Docker named volume -> SQLite WAL database

`<TAILNET_CADDY_HOST>:443` (reverse proxy tailnet listener)
       │ HTTPS using reverse proxy internal CA
       │ Basic Auth (scrypt/bcrypt hash)
       │ Headscale ACL permits only authorized user/group nodes
       ▼
Authorized phone/laptop -> stable Headscale MagicDNS name (e.g. `<TAILNET_CADDY_HOST>`)

systemd timers -> poll/heartbeat/backup scripts
       │             │               │
       ▼             ▼               ▼
JSON evidence   gateway alert   local verified backup
                          (atomic 0640)             │
                                                   ▼
                                rsync/SSH to `<OFFHOST_BACKUP_HOST>` off-host store
```

Key design properties of the target:

- **One FastAPI process owns the SQLite lifecycle.** Migrations and the
  database file belong to exactly one container. No second writer.
- **The reverse proxy is a host service.** It is not in the container.
  It owns both listeners and the TLS/auth policy.
- **The FastAPI container is loopback-only.** Its only host publication
  is `127.0.0.1:18098`. No host route reaches FastAPI without going
  through the reverse proxy.
- **LAN only ever reaches `/api/ingest`.** Every other path returns 404
  at the reverse proxy before the request reaches FastAPI.
- **Tailnet reaches every application route** but only after Basic Auth
  succeeds and the Headscale ACL permits the node.
- **Stock firmware compatibility is preserved.** The credential is the
  JSON field `token`. No header/query-token capability is assumed.

## Decision record

Two designs were rejected:

- **Raw Uvicorn on LAN.** Without a proxy in front, every application
  route is reachable from LAN unless the application implements a
  second interface policy. That couples boundary policy to application
  code and prevents independent testing.
- **Tailscale-Serve-only.** Tailnet Serve does not solve the physical
  ESP8266's LAN ingress. Stock firmware must reach the service on the
  LAN.

The reverse proxy two-listener design gives an independently testable
path/method boundary, one SQLite owner, local TLS/auth for the UI, and
no dependence on Funnel or Headscale certificate issuance.

## Deferred hardening slices

Deferred hardening slices were explicitly deferred for the baseline
release: **C** (transitive descriptor binding), **D** (Docker daemon
attestation), **E** (cross-host reviewer independence). Each has a
re-open trigger and a current compensating control. See
[HARDENING-BACKLOG.md](HARDENING-BACKLOG.md).

## Reading order

Continue with [SECURITY.md](SECURITY.md) for the auth model,
[OPERATIONS.md](OPERATIONS.md) for service ownership and timers,
and [RECOVERY.md](RECOVERY.md) for the recovery path. For the
repository entry point, see [../README.md](../README.md).