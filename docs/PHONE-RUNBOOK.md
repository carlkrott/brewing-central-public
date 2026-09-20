# Brewing Central Android Phone Runbook

## Scope and authority

The Android phone is the active Brewing Central application host. The deployment host remains the Caddy/TLS ingress host and retains predecessor configuration/image material for cold rollback. The phone does not own GPU lifecycle; ZeroClaw and the camera observer consume the canonical combined-Gemma endpoints on `<MODEL_SERVICE_HOST>`.

Production identifiers:

- phone: `<PHONE_TAILNET_HOST>` (<TAILNET_MAGICDNS_NAME>)
- USB ADB serial: `<ADB_SERIAL>`
- dashboard: `http://<PHONE_TAILNET_HOST>:8098/`
- supported browser URL: `https://<TAILNET_CADDY_HOST>/`
- telemetry device: `<EXAMPLE_DEVICE_ID>`
- application root: `<TERMUX_APP_ROOT>` (the Termux private home directory, conventionally `<TERMUX_APP_ROOT>/brewing-central` for the application checkout)

Do not expose the phone listener outside the Tailnet or bypass Caddy's TLS warning. Do not print `phone.env`, ingest-token data, ZeroClaw tokens, Basic Auth values, or request bodies.

## Runtime ownership

Termux:Boot launches:

1. `~/.termux/boot/00-brewing-central`
2. `ops/android/termux-boot-start.sh`
3. Termux `RunCommandService`
4. `ops/android/termux-main-start.sh`
5. `ops/android/start-phone-stack.sh`
6. `ops/android/phone-health-loop.sh` (started exactly once, after stack readiness)

The stack contains exactly one of each:

- Android-native `~/bin/zeroclaw gateway`, loopback `127.0.0.1:3100`
- `uvicorn app.main:app`, Tailnet `<PHONE_TAILNET_HOST>:8098`
- `python -m app.camera_observer`, when enabled

Startup uses a 90-second `flock` and command-bound PID checks. Boot recovery removes reboot-stale PID files (including `phone-health-loop.pid`) before retrying startup. `phone.env` and phone secrets are mode `0600`.

## Phone health evidence (W6)

The phone-health loop is the **sole scheduler owner** of the phone battery and
heartbeat evidence chain. There is no Termux systemd unit, no cron daemon,
and no `termux-job-scheduler` job for this evidence. Termux:Boot
→ `termux-main-start.sh` → `phone-health-loop.sh` is the only path that
writes `battery-state.json` and `heartbeat.json` on the phone.

- `phone-health-loop.sh` is single-instance (`flock`), command-bound
  (`$ROOT/run/phone-health-loop.pid`), and exits cleanly on `TERM`/`INT`.
- The interval is `PHONE_EVIDENCE_INTERVAL_SECONDS` (default `300`, bounded
  to `(1, 3600]` seconds).
- `ops/android/write-phone-evidence.py` is stdlib-only and Termux-compatible.
  Battery percent is read from `termux-battery-status` when present, otherwise
  from `/sys/class/power_supply/BAT*/capacity` + `status`. When neither source
  is available, the producer writes an explicit `source=unavailable` marker
  with `percent=-1` so the existing parser returns `parse_error`; it never
  fabricates a usable percent. Heartbeat always gets written.
- Heartbeat is an honest phone-stack probe (loopback ZeroClaw health +
  dashboard health with the configured `Host` header). `state=critical`
  whenever the stack probe fails; a failed probe does not prevent the
  evidence files from being written.
- `stop-phone-stack.sh` command-matches `phone-health-loop.sh` exactly and
  removes its PID file alongside the existing three services.

**W6 is now source-defined.** The phone-side writer exists and is wired to
the single Termux:Boot scheduler owner. Off-host backup and restore of the
phone databases remains a **separate, unqualified gate**: nothing in this
slice claims off-host copy, restore, installation, scheduling qualification,
or production readiness. The cutover SQLite snapshot and explicit phone
backups described below remain the available recovery evidence until a
phone-aware scheduled/off-host backup is separately installed and qualified.

## Routine health check

From the off-host operations host (`<OFFHOST_BACKUP_HOST>`):

```bash
curl --fail --silent --show-error \
  -H 'Host: <PHONE_TAILNET_HOST>' \
  http://<PHONE_TAILNET_HOST>:8098/health

curl --fail --silent --show-error \
  -H 'Host: <PHONE_TAILNET_HOST>' \
  http://<PHONE_TAILNET_HOST>:8098/api/status

curl --fail --silent --show-error \
  -H 'Host: <PHONE_TAILNET_HOST>' \
  http://<PHONE_TAILNET_HOST>:8098/api/assistant/status
```

Use the original HTTPS URL for browser checks. An unauthenticated probe must return `401`; authenticate only through the trusted browser/password workflow.

A physical-ingest check is complete only when all of these are fresh:

- Caddy records `POST` status `200` from the iSpindel LAN address.
- `samples` increases for device `<EXAMPLE_DEVICE_ID>`.
- New rows continue over more than one configured sleep interval.
- persisted `raw_json` contains no `token` key.
- `PRAGMA integrity_check` returns `ok`.

## ADB and attended recovery

Tailnet ADB at `<PHONE_TAILNET_HOST>:5555` does not survive a phone reboot. After reboot, use either the already-authorized USB data connection or Android Wireless Debugging pairing. Do not bypass the lock screen, ADB RSA prompt, Play Protect, Auto Blocker, or package verification.

Confirm the exact target before any command:

```bash
adb devices -l
adb -s <ADB_SERIAL> get-state
```

Run the stack manually in the Termux app context:

```bash
adb -s <ADB_SERIAL> shell \
  "run-as com.termux <TERMUX_APP_ROOT>/current/ops/android/start-phone-stack.sh"
```

Stop the command-matched Termux services and the phone-health loop:

```bash
adb -s <ADB_SERIAL> shell \
  "run-as com.termux <TERMUX_APP_ROOT>/current/ops/android/stop-phone-stack.sh"
```

`stop-phone-stack.sh` command-matches the four process trees (the three
original Termux services plus `phone-health-loop.sh`) and removes each
of their PID files. The phone-health evidence chain therefore stops
together with the rest of the stack; no separate kill is required.

## Logs

Phone logs are under `~/brewing-central/logs/`:

- `termux-boot-dispatch.log`
- `termux-boot.log`
- `phone-health-loop-launcher.log` (one-shot nohup launch record from `termux-main-start.sh`)
- `phone-health-loop.log` (interval cadence + producer results)
- `zeroclaw.log`
- `dashboard.log`
- `camera-observer.log`

Read them through `run-as com.termux`. Redact credentials, headers, query strings, and request bodies before sharing excerpts.

## Release layout and rollback

`current` is an absolute symlink into `~/brewing-central/releases/<release-id>`. Releases are hash-addressed and retained. There is currently no authoritative `previous` symlink; never invent one or select a rollback by mtime.

Before switching releases:

1. Name the exact retained release ID.
2. Verify its expected archive/source hash from its rollout receipt.
3. Create a consistent backup of both SQLite databases with Python `sqlite3.Connection.backup()`; do not copy a live SQLite file.
4. Stop the phone stack.
5. Create `current.next` as a symlink to the exact release directory and atomically rename it to `current`.
6. Start the stack and verify health, assistant status, DB integrity, exact process count, and a physical sample.
7. On any gate failure, restore the pre-recorded `current` target and repeat the same checks.

Never modify telemetry rows or activate a calibration as part of application rollback.

## Database backup

The active databases are:

- `~/brewing-central/data/ispindel.db`
- `~/brewing-central/data/brew.db`

Backups must be made by a Python process in the Termux context using `Connection.backup()`, followed by `PRAGMA integrity_check`, file SHA-256, and recorded table counts. Store the receipt outside the live database directory. A byte copy of an open WAL database is not a backup.

The legacy deployment-host backup timers do not automatically prove that phone-owned databases are protected. Until a phone-aware scheduled/off-host backup is separately installed and qualified, treat the cutover SQLite snapshot and explicit phone backups as the available recovery evidence.

## Camera observer

IP Webcam serves the rear-camera snapshot on phone loopback. The observer sends bounded images to the canonical vision service at `<MODEL_SERVICE_HOST>:8090`.

The current vessel is intentionally empty. Vision scope is structural safety only:

- vessel present and upright
- not fallen, displaced, visibly damaged, or overflowing
- view unobstructed enough to assess structure

The observer must not infer liquid, bubbles, fermentation, contamination, gravity, or temperature from images. Ambiguous contents are `uncertain`; telemetry remains separate evidence.

## ZeroClaw and research

ZeroClaw is Android/Bionic-native and runs loopback-only. It uses the configured combined-Gemma text endpoint at `<MODEL_SERVICE_HOST>:8095`; it must not manage GPU services.

Research uses fixed broker endpoints only:

1. SearXNG at `<SEARCH_SERVICE_HOST>:8080`
2. Kiwix at `<KIWIX_HOST>:9090`
3. Wikipedia fallback

A source is `ok` only when it returns at least one document. Zero results are `empty` or `unavailable`, never successful evidence.

## Calibration gate

Cubic calibration is supported, but every fitted curve must be monotonic non-decreasing over its measured angle domain. New browser-created fits are saved inactive.

Do not activate a production calibration until the water-reference gate passes:

1. Place the iSpindel freely floating in plain water at a recorded temperature; do not use the current upright/dry readings.
2. Collect at least 10 authenticated samples from device `<EXAMPLE_DEVICE_ID>` spanning at least five minutes without touching the vessel.
3. Require angle range no greater than `0.5°` and sample standard deviation no greater than `0.2°`.
4. Record the median angle as the water reference at SG `1.000` and retain the sample IDs/time window in a receipt.
5. Add the remaining independently measured sugar-solution reference points in increasing angle/SG order.
6. Save the linear, quadratic, or cubic candidate inactive; reject decreasing points or a fitted curve whose derivative becomes negative anywhere in the measured range.
7. Review coefficients, R², and calibrated outputs against every reference point.
8. Activate the exact calibration ID only after explicit operator approval.

The former provisional `24.1158°` value is not an activation authority. Current readings near `88.5°` are also not a water reference.

## Deployment-host Caddy cutover and rollback

The deployment-host Caddy service owns the supported HTTPS URL and the LAN ingest listener. It forwards both surfaces to the phone while preserving Basic Auth, body limits, LAN path/method restrictions, and access-log redaction. The predecessor is a stopped cold rollback target, not a running standby.

The cutover receipt is:

`~/.local/state/ispindel-phone-rollout/20260912T193247Z/cutover-final-go.json`

The frozen predecessor Caddyfile is:

`/var/backups/ispindel/cutover-20260912T200251Z/Caddyfile.predecessor`

Restoring that file and restarting `ispindel-caddy.service` is a production mutation with a short listener interruption. Require explicit impact-aware authorization. First verify and start the exact isolated predecessor backend; restoring Caddy alone would produce an unavailable service. Then verify both HTTPS and physical ingest after restart. Caddy has `admin off`; use a bounded restart, not reload.
