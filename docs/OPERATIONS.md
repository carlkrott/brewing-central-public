# Operations — Service Ownership, Evidence, and Cutover

## Production state (deployed 2026-09-12)

| Process | Active owner | Endpoint / state |
|---|---|---|
| Brewing Central FastAPI | Termux on the Android phone | `<PHONE_TAILNET_HOST>:8098` |
| ZeroClaw gateway | Termux on the Android phone | loopback `127.0.0.1:3100` |
| Camera observer | Termux on the Android phone | hourly, when enabled |
| Boot recovery | Termux:Boot + `RunCommandService` | bounded retry, command-bound PIDs |
| Phone health evidence | Termux:Boot → `termux-main-start.sh` → `phone-health-loop.sh` | sole scheduler owner of `battery-state.json` + `heartbeat.json` (W6 source-defined; off-host backup/restore still a separate, unqualified gate) |
| HTTPS/LAN ingress | `ispindel-caddy.service` on the deployment host | stable HTTPS URL and LAN ingest listener |
| Predecessor app | Docker on the deployment host | stopped cold rollback material, not the active writer |

Routine checks, attended ADB recovery, process control, phone logs, and release
rollback are in [PHONE-RUNBOOK.md](PHONE-RUNBOOK.md). The production cutover
receipt is
`~/.local/state/ispindel-phone-rollout/20260912T193247Z/cutover-final-go.json`.

Operational invariants:

- Use `https://<TAILNET_CADDY_HOST>/` for browsers.
- A physical ingest gate requires new authenticated rows across multiple sleep
  intervals, not just an HTTP health response.
- Tailnet ADB does not survive reboot; recover through authorized USB ADB or an
  attended Wireless Debugging pairing.
- Caddy runs with `admin off`; configuration changes require a bounded restart,
  never a reload assumption.
- The predecessor is not a running standby. A rollback must verify its exact
  image/configuration, start it deliberately, and prove readiness before Caddy
  is considered restored.
- The phone databases require SQLite `Connection.backup()` plus integrity/hash
  verification. The legacy deployment-host timer design does not prove phone backup.
- Production writes, Caddy restarts, database restore, calibration activation,
  and secret rotation remain explicit authorization boundaries.

## Historical T0/T6/T7 plan

The remainder is retained as design history. Its `CURRENT` and `TARGET` labels
refer to the earlier deployment-host-only design and are not live-state authority.

## Service ownership

### CURRENT

| Process | Owner | Restart policy | Notes |
|---|---|---|---|
| `ispindel-dashboard` container | Docker `restart=unless-stopped` | automatic | Predecessor image `sha256:0797406…`. |
| `zeroclaw` service | systemd (manual) | none (currently disabled) | Two cron jobs blocked by shell metacharacter policy. |
| Poller `scripts/ispindel-check.py` | ZeroClaw cron | none | Default targets wrong port (8080). |
| `send-alert.sh` | ZeroClaw cron | none | Exits 1 after successful delivery; supervisors record success as failure. |
| Host heartbeat / battery evidence | ad-hoc writes by operator user | none | Files mode 0600; unreadable by container UID `10001`. |

The CURRENT ownership is fragmented: Docker restarts the container, the
ZeroClaw daemon is disabled, and the “scheduler” is a manual mix of cron
fragments. There is no verified alert path, no verified backup path, and no
verified boot recovery.

### TARGET

| Process | Owner | Restart policy | Notes |
|---|---|---|---|
| `ispindel-dashboard` container | Docker `restart=unless-stopped` | automatic | Container crash restart only. |
| `ispindel-stack.service` | systemd (oneshot reconciler) | runs after Docker/network/Tailscale | Runs permanent Compose after prerequisites are ready. Does not run a second supervisor loop. |
| `ispindel-poll.timer` | systemd | bounded randomized delay | Runs `scripts/poll-alert.py`. |
| `ispindel-heartbeat.timer` | systemd | bounded randomized delay | Runs `scripts/write-heartbeat.py`. |
| `ispindel-backup.timer` | systemd | every 6 h, `Persistent=true` | Runs `scripts/backup-production.py`. |
| `ispindel-restore-drill.timer` | systemd | monthly, `Persistent=false` | Runs `scripts/rehearse-restore.py` against a disposable volume. |
| `ispindel-caddy.service` | systemd | host-controlled | Owns the two Caddy listeners. |
| ZeroClaw | systemd `zeroclaw.service` | enabled | **Transport only.** Never the scheduler. |
| n8n | disabled | none | Not in this release. Re-enabling requires a separate decision. |

The TARGET model is: **one supervisor per concern** (Docker restarts the
container, systemd timers schedule scripts, Caddy terminates TLS, ZeroClaw
transports alerts). No two owners race for the same resource.

## Timer cadence (TARGET)

| Timer | OnCalendar | Persistent | Notes |
|---|---|---|---|
| `ispindel-poll.timer` | every 5 min | yes | Polls `/api/status`, `/api/devices`, per-device samples; emits alerts. |
| `ispindel-heartbeat.timer` | every 5 min | yes | Writes `heartbeat.json` evidence. |
| `ispindel-backup.timer` | every 6 h, randomized delay | yes | RPO target: 6 h. |
| `ispindel-restore-drill.timer` | monthly | no | Selects an exact manifest path from the index, never a glob. |

Randomized delay prevents synchronized thundering-herd; `Persistent=true`
ensures missed runs after a reboot are caught up on the next boot.

## Evidence schema

### Directory layout

- **Path**: `/var/lib/ispindel/evidence`
- **Owner**: `root:ispindel`
- **Mode**: directory `0750`; files written `0640` atomically.
- **Supplemental group**: the container is added to the `ispindel` group via
  `group_add: ["${ISPINDEL_GID:?…}"]` so it can read but not write.

The CURRENT layout uses `/path/to/.zeroclaw/workspace/memory/` with
mode `0600`, which is unreadable by the container UID `10001`. The TARGET
supplemental group is the fix.

### File kinds

| Kind | Path | Producer | Required fields |
|---|---|---|---|
| `battery` | `battery-state.json` | upstream ZeroClaw or poller | `schema_version`, `kind`, `observed_at` (UTC ISO-8601), `state`, `detail`. |
| `heartbeat` | `heartbeat.json` | `write-heartbeat.py` | `schema_version`, `kind`, `observed_at`, `state`, `poll_failed`, `alert_attempted`, `alert_delivered`. |
| `backup` | written by backup tool | `backup-production.py` | manifest plus per-file SHA-256; see [docs/RECOVERY.md](RECOVERY.md). |
| `backup_health` | `backup_health.json` | `backup-production.py` (after off-host verification + directory promotion) | `schema="ispindel-backup-health/v1"`, `run_id`, `verified_at` (UTC ISO-8601 with trailing `Z`), `mode` (`single`/`dual`). The container reads only `run_id`, `mode`, `verified_at`, and the file mtime; `offhost_path` and `remote_retention` are written by the producer but NEVER returned by the endpoint. The producer writes the receipt with mode `0o644` so the dashboard container (UID 10001) can read it across the read-only bind mount while remaining unable to mutate the producer's bytes. |

The parser rejects wrong `schema_version`, wrong `kind`, malformed time, and
**never turns stale evidence green**. A parser result of `missing`, `stale`,
or `parse_error` is surfaced as non-green health; the absence of a fresh
heartbeat does not hide stale sensor telemetry.

### Backup-health endpoint (`/api/backup-health`)

The dashboard exposes `GET /api/backup-health` so operators can confirm
that `scripts/backup-production.py` has published a fresh receipt inside
the bounded TTL (default 86400 s = 24 h, comfortably above the 6-hour
- The receipt file is mounted read-only at
  `/backup-health/backup_health.json` (single file bind, source
  `${ISPINDEL_BACKUP_HEALTH_FILE:-/var/backups/ispindel-dashboard/backup_health.json}`);
  the database generations directory is deliberately not mounted.

- `HTTP 200` + `status="ok"` ⇒ the receipt and its filesystem mtime are
  both inside the TTL.
- `HTTP 503` + `status in {missing, parse_error, freshness_violation, stale}`
  ⇒ the receipt is missing, malformed, future-dated, or its mtime is older
  than the TTL. The body is sanitized and never contains the configured
  path, `offhost_path`, or `remote_retention`.

The endpoint reports **freshness of evidence only**. It does not prove that
a backup is currently deployed, that an off-host copy is reachable, or that
a restore would succeed — those checks belong to the restore-drill gate.

### Atomic writer

Evidence files are written with: same-directory temp file → `fsync` →
`chmod 0640` → `rename`. A partial write is never observable as valid
evidence.

### journald event contract

Every terminal script path emits one structured journald event with `event=`,
`run_id=`, `duration_ms=`, `outcome=`, and (on failure) `error_class=`.
Required event names:

- `poll_completed`
- `heartbeat_written`
- `alert_suppressed`
- `alert_sent`
- `alert_failed`
- `backup_verified`
- `restore_drill_completed`

Credentials and request bodies must never appear in the event fields.

## Alert semantics

| Exit code | Meaning |
|---|---|
| 0 | Delivered, or intentionally suppressed by deduplication. |
| 1 | Delivery failed after bounded retry. |
| 2 | Invalid configuration/input. |

The CURRENT `send-alert.sh` exits 1 after a successful send, which causes
supervisors to record successful alerts as failures. The TARGET alert
implementation:

- Writes `pending` before dispatch.
- Writes `sent` only after application-level delivery acknowledgement.
- Leaves a retryable `pending` record on transport failure.
- **Never** records a failed send as delivered.
- **Never** suppresses the next retry because of a previous failure.

Stale-device alerting uses the application's effective per-device interval
(`expected_interval_sec` and per-device overrides), not a second hard-coded
threshold. It implements startup grace, warning/critical hysteresis, one
alert per device/state, and one recovery event.

### How to test the alert path

```bash
# Manually invoke the canonical alert script with a synthetic body.
ISPINDEL_DRY_RUN=1 /usr/local/lib/ispindel/send-alert.sh "synthetic test"

# Confirm journald record:
journalctl -u ispindel-stack.service -n 5 --output=json | grep alert_sent
```

A successful test produces `event=alert_sent`, `outcome=ok`, exit 0. A
synthetic stale condition produces exactly one delivered notification and one
recovery event; immediate repeats are suppressed by the dedup state machine.

## Backup locations

| Location | Path | Retention |
|---|---|---|
| Local on deployment host | `/var/backups/ispindel-dashboard/$RUN_ID/` | 28 most recent verified generations |
| Off-host on `<OFFHOST_BACKUP_HOST>` | `/path/to/backups/ispindel-dashboard/$RUN_ID/` | 56 most recent verified generations + 1 verified per ISO week for 12 weeks |

`$RUN_ID` is bound once per run as
`$(date -u +%Y%m%dT%H%M%SZ)-$(python3 -c 'import secrets; print(secrets.token_hex(4))')`
and reused end-to-end (container temp path, host path, manifest, checksum,
off-host path, restore input). Retention is applied **only after** off-host
verification. `.partial`, corrupt, or unverified generations are never
promoted or counted as recoverable.

The CURRENT state has no timer, no off-host copy, and no restore drill. The
historical `/path/to/.zeroclaw/workspace/memory/alert-heartbeat.log` is
not a backup; it is the current evidence file and is not retained off-host.

## Restore authorization boundary

Restoring the production database is a **separately authorized** action. The
rules:

- **No automated restore.** A transient health check failure never triggers
  a restore.
- **Restore requires** `--confirm-production-restore "$RUN_ID"` where
  `RUN_ID` is read from an already-verified manifest that the operator
  passes explicitly.
- **Restore requires** a fresh pre-restore backup, integrity proof, and
  human authorization.
- **Restore rehearsal** uses nonce names (`ispindel-rehearsal-volume-$NONCE`,
  etc.) and never writes to production identifiers.

The TARGET entry point is `scripts/restore-production.py` with
`--confirm-production-restore`. The CURRENT entry point is **manual
investigation of the production container**; there is no supported restore
command.

## Exact rollback entrypoint

The rollback entry point for the **application** is `Stage 05` of the
deployment harness:

```
scripts/deploy/05-rollback-application.sh
```

This restores the exact predecessor Compose/Caddy/systemd bytes and the
predecessor image. It does **not** restore the database automatically. If a
migration made the database predecessor-incompatible, the rollback stops and
requires the separately authorized production-restore procedure above.

The frozen predecessor records two Compose paths, but the temporary
`compose.override.yml` is an observed expected-missing input. Stage 01 may
accept that one absence only when both `--allow-expected-missing` and the exact
`--expected-missing-contract-version` value activate the release-inventoried
contract at
`contracts/ispindel-predecessor-expected-missing-amendment-v1.json`. The
receipt binds the complete Compose snapshot and its SHA-256, the expected-
missing list and its canonical SHA-256, the per-path presence map, volume
inspect provenance, systemd-state hash, complete source-archive hash, and
matching stable pre/post-backup container fingerprints. Any unexpected
absence, unexpected presence, contract drift, or receipt drift fails closed.
Stage 05 restores every snapshot row (including deleting paths proven absent)
but invokes Compose with only the ordered paths whose Stage 01 rows prove
`present=true`; it never fabricates the missing override. The authorized
runner requires
`ISPINDEL_EXPECTED_MISSING_CONTRACT_VERSION=ispindel-predecessor-expected-missing/2026-08-03-v1`
and passes both activation keys to Stage 01 exactly once.

The CURRENT rollback path is manual container recreation from the
predecessor image; there is no script. The T0 descriptor
`descriptors/predecessor-production.json` captures the exact predecessor
bytes needed to reconstruct it.

## What an operator does today

Until T4–T11 land, the supported operations are:

1. `docker ps` to confirm the container is running.
2. `curl -fs http://127.0.0.1:8098/health/ready` to confirm readiness.
3. Review the SQLite evidence (manually) if asked.
4. Trigger `scripts/ispindel-check.py --json` for a one-shot check; the T3
   repository default is `http://127.0.0.1:8098` and exit codes are stable.

Anything else (deploying, rotating the token, rotating the database, editing
the Headscale ACL, restarting the container under load) is **not yet a
documented operator action**. It is part of the production plan and lives
behind an explicit authorization gate.
