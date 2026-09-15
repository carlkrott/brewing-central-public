# Recovery — Phone Production, Reverse-Proxy Rollback, and Data Safety

## Production state

Recovery is now split across two hosts:

1. The Android phone owns `ispindel.db`, `brew.db`, the gateway, and
   the camera observer under `~/brewing-central`.
2. The deployment host owns the reverse proxy / TLS / LAN ingress and
   retains predecessor configuration/image material as a cold rollback
   target.

The exact phone lifecycle and rollback procedure is in
[PHONE-RUNBOOK.md](PHONE-RUNBOOK.md). Important constraints:

- There is no authoritative phone `previous` symlink. Choose a
  retained hash-addressed release only from a verified rollout
  receipt; never by mtime.
- Back up each phone database with `sqlite3.Connection.backup()`, then
  record integrity, SHA-256, and table counts. Never copy an open WAL
  database.
- Application rollback never rewrites telemetry, brewing records, or
  active calibration state.
- The legacy deployment-host backup timers do not automatically
  protect phone-owned data. A phone-aware scheduled/off-host backup
  remains a separate promotion.
- Tailnet ADB may disappear after reboot. Use authorized USB ADB or
  attended Wireless Debugging; never bypass Android security controls.

Deployment-host ingress rollback uses the exact predecessor Caddyfile
at
`/var/backups/ispindel/cutover-20260912T200251Z/Caddyfile.predecessor`
and a bounded restart of `ispindel-caddy.service`. Caddy has
`admin off`, so reload is not a valid recovery mechanism. This is a
production mutation and requires explicit impact-aware authorization.
After rollback, verify the HTTPS UI and a new physical iSpindel
sample; service-active alone is insufficient.

Do not assume a hot standby exists. The current named predecessor
container is stopped cleanly, and the earlier release-specific
container ID is absent. A rollback transaction must verify the exact
predecessor image and database isolation, start it deliberately, prove
readiness, then switch the reverse proxy. Restoring the Caddyfile alone
would point users at an unavailable backend.

The final cutover receipt is
`~/.local/state/ispindel-phone-rollout/20260912T193247Z/cutover-final-go.json`.
It binds the production reverse-proxy hash, predecessor hash, database
snapshot, physical-ingest evidence, and rollback path.

## Historical deployment-host-only recovery design

The remainder is retained as recovery-design history. Its `CURRENT` and
`CANDIDATE` labels predate the phone cutover and are not live-state
authority.

Recovery always means the same three things:

1. **Backup** — capture the production database to a verified
   artifact.
2. **Restore rehearsal** — prove that artifact can be replayed into a
   disposable environment.
3. **Rollback** — return the application to a known predecessor.

Keep these separate. They are not interchangeable.

## Restore authorization boundary

Production restore is the **highest-trust action** in this repository.
The rules:

- **No automated restore.** A transient health check failure never
  triggers a restore. The system must never delete the running database
  on its own.
- **No rollback DB.** Application rollback (`Stage 05`) restores the
  exact predecessor Compose/reverse-proxy/systemd bytes and image. It
  does **not** restore the database. If a migration made the database
  predecessor-incompatible, the rollback stops and requires a
  separately authorized production restore.
- **Restore requires explicit human authorization**. The command is
  `scripts/restore-production.py --confirm-production-restore "$RUN_ID"`
  where `RUN_ID` is read from an already-verified manifest that the
  operator passes explicitly.
- **Restore requires a fresh pre-restore backup and integrity proof.**
- **Restore rehearsal only** is `scripts/rehearse-restore.py
  --manifest <path>`. It never writes to production identifiers.

The boundary is enforced in code: the restore command requires a
literal confirmation token that matches a verified manifest. There is no
`--latest` flag, no glob, no mtime selection.

## CURRENT state (baseline)

| Component | State |
|---|---|
| Scheduled backup | None. |
| Off-host copy | None. |
| On-host backup of the production DB | None (historical predeploy copies exist but are not durable). |
| Restore drill | None. |
| Documented restore command | None. |
| Predecessor image export | Pending preflight. |
| Database integrity | Currently `ok` per the baseline descriptor. |

If the production volume is lost today, recovery is "rebuild from
historical copies" plus last-known-good configuration. There is no SLA.
The CANDIDATE exists to remove that gap after authorized promotion.

## CANDIDATE: backup algorithm

The backup tool is `scripts/backup-production.py`. It runs once per
scheduled timer tick and binds one filename end-to-end.

1. **Bind one RUN_ID** in UTC:
   `RUN_ID=$(date -u +%Y%m%dT%H%MSZ)-$(python3 -c 'import secrets; print(secrets.token_hex(4))')`.
2. **Bind one basename** `ispindel-${RUN_ID}.db`. Reuse that exact
   string in the container temp path, the host path, the manifest, the
   checksum, the off-host path, and the restore input.
3. **Discover the production volume** from `docker inspect`. STOP if
   there is zero or more than one match. Never guess the volume name.
4. **Run** Python `sqlite3.Connection.backup()` *inside* the running
   app to `/tmp/$BASENAME` inside the container. Never copy the live
   file with `cp` or `rsync` while it is open.
5. **`docker cp`** the verified temp file to
   `/var/backups/ispindel-dashboard/$RUN_ID/$BASENAME` on the host.
6. **Verify** host-side: `PRAGMA integrity_check`, schema version,
   selected table counts, file size, SHA-256. Write `manifest.json`
   atomically.
7. **Off-host copy** the exact run directory to
   `<OFFHOST_BACKUP_HOST>:<OFFHOST_BACKUP_DIR>/ispindel-dashboard/$RUN_ID.partial`,
   verify the manifest and hashes there, then `rename` to `$RUN_ID`.
8. **Apply retention** only after off-host verification. Never delete
   the newest verified daily/weekly copy.

### Retention policy

| Scope | Policy |
|---|---|
| Local on deployment host | 28 most recent **verified** generations. |
| Off-host on `<OFFHOST_BACKUP_HOST>` | 56 most recent **verified** generations, plus 1 **verified** generation per ISO week for 12 weeks. |

Retention considers only directories whose `manifest.json` validates.
`.partial`, corrupt, and unverified generations are never promoted or
counted as recoverable. Low blocks/inodes, failed off-host copy, or a
missing image fails visibly and suppresses deletion; it never triggers
emergency cleanup.

### RPO / RTO

- **RPO target**: 6 hours. Timer runs every 6 h with bounded
  randomized delay and `Persistent=true`.
- **RTO target**: 60 minutes, **measured by the monthly restore drill**
  rather than estimated.

## CANDIDATE: restore rehearsal

The restore drill is `scripts/rehearse-restore.py`. It runs monthly
under `ispindel-restore-drill.timer` (`Persistent=false`).

1. Accept an exact `--manifest` path. Never a wildcard or "latest".
2. Verify the manifest and backup hash before creating any resource.
3. Assert the candidate image exists locally with
   `docker image inspect`; use `--pull never`. Never reach the network.
4. Create **nonce names**:
   - `ispindel-rehearsal-volume-$NONCE`
   - `ispindel-rehearsal-container-$NONCE`
   - `ispindel-rehearsal-network-$NONCE`
   Assert each is **not** an existing production identifier.
5. Restore into the disposable volume with the already-present
   candidate image. Run integrity/schema/count checks. Start the
   candidate on a loopback random port. Verify liveness/readiness/API/
   served marker.
6. Remove only the nonce resources. A trap records cleanup and fails
   if disposable resources remain.

A clean rehearsal records `REHEARSAL.json` with `result=PASS`, the
disposable names, the cleanup proof, and the
unchanged-production proof.

## CANDIDATE: production restore

Production restore is `scripts/restore-production.py`. It is a
separate command with a hard authorization gate.

- Requires `--confirm-production-restore "$RUN_ID"`.
- Requires an explicit human authorization, quiescence, a fresh
  pre-restore backup, and integrity proof.
- **Never** invoked by health checks, deployment, or automatic
  rollback.
- Does not guess "latest". The `$RUN_ID` is read from the already
  verified manifest passed to the command.

If the application rollback (`Stage 05`) cannot complete because of a
migration incompatibility, the operator pauses Stage 05 and invokes
the separate production-restore procedure above.

## Exact application rollback entrypoint

Application rollback (distinct from database restore) is the entry
point to return the application to a known predecessor after a failed
deployment.

```
scripts/deploy/05-rollback-application.sh
```

What it does:

- Restores the complete Stage 01 Compose/reverse-proxy/systemd
  snapshot, including deleting paths whose rows prove
  `present=false`.
- Starts the predecessor with only the ordered Compose inputs whose
  Stage 01 rows prove `present=true`; it does not invent or
  reconstruct a missing temporary override.
- Restores the predecessor image (the exact image referenced by
  `descriptors/predecessor-production.json`) with `--no-build --pull
  never`.
- Before Stage 03 Compose promotion, renames the stopped predecessor
  to a release-bound standby name and verifies its exact container ID.
  Promotion then uses a release-unique Compose project plus the exact
  verified `predecessor_volume_name`; this keeps Compose from
  discovering/replacing the standby while ensuring the candidate uses
  the production database volume. The predecessor's original Compose
  labels and project remain untouched.
- During Stage 05, verifies and stops the candidate, quarantines it
  under a release-bound name, restores the exact Stage 01 system/
  Compose snapshot bytes, renames the preserved standby back to
  `ispindel-dashboard`, starts that exact captured container ID, and
  verifies its final identity and readiness. It does not reconstruct a
  Compose override or invoke `docker compose` for rollback.
- Requires the release-inventoried expected-missing amendment, exact
  two-key activation, hash-bound receipt, matching stable pre/post-
  backup container fingerprint, and descriptor-matching Docker volume
  inspect evidence.
- Does **not** restore the database.
- Does not reboot.
- Does not restart Headscale or change the ACL.

What it does **not** do:

- It does not run automatically. The top-level release runner invokes
  `Stage 05` only for deterministic deployment-gate failures after
  `Stage 03`, not for a single transient probe. Operators invoke it
  manually otherwise.
- It does not assume the database is the same schema. If a migration
  made the database predecessor-incompatible, the script stops and the
  operator must use the production-restore procedure.

## What "verified" means

A backup generation is **verified** when:

- `manifest.json` exists and parses.
- Every entry in `manifest.json` has a matching file on disk.
- `sha256sum` matches the manifest for every entry.
- `PRAGMA integrity_check` returned `ok` for the database file.
- The schema version matches the recorded `user_version`.
- The selected table counts match the recorded counts.
- The off-host copy has been verified against the same manifest.

A generation without these properties is not a backup, even if its
files are on disk. It must not be used as a restore input.

## Operator runbook: backup, verify, and rehearse

The candidate commands below remain non-production until the backup
hardening promotion. The backup unit supplies roots, off-host target,
capacity floors, and image reference through the root-owned
`/etc/ispindel/backup.env` file.

```bash
# Scheduled/manual production backup after promotion:
python3 scripts/backup-production.py --production --execute

# Independent read-only verification of one exact generation:
python3 scripts/verify-backup.py \
  --manifest /var/backups/ispindel-dashboard/<RUN_ID>/manifest.json

# Disposable restore rehearsal using an exact manifest and an existing image:
python3 scripts/rehearse-restore.py \
  --manifest /var/backups/ispindel-dashboard/<RUN_ID>/manifest.json \
  --image sha256:<64-hex-image-id> \
  --evidence-dir /var/lib/ispindel/restore-drills
```

The backup command performs SQLite `Connection.backup()` inside the
running container, copies only the completed snapshot, validates it
locally, copies it to `$RUN_ID.partial` off-host, validates it again,
and promotes it atomically. Only then does verified retention run.
Remote retention keeps the newest 56 verified generations plus one
verified generation per ISO week for 12 weeks; invalid and `.partial`
directories are never deletion candidates.

Production restore is deliberately separate and is never called by a
timer:

```bash
python3 scripts/restore-production.py \
  --manifest /exact/target/<RUN_ID>/manifest.json \
  --pre-restore-manifest /exact/fresh-pre-restore/<NEW_RUN_ID>/manifest.json \
  --quiescence-proof /exact/quiescence-proof.json \
  --confirm-production-restore <RUN_ID> \
  --image sha256:<64-hex-image-id> \
  --receipt /exact/restore-receipt.json \
  --execute
```

This command still requires separate human authorization, an exact
confirmation matching the verified target manifest, a distinct fresh
pre-restore backup, a bound quiescence proof, and an already stopped
production container.

## Operator runbook: dispose of a failed rehearsal

If a rehearsal fails for any reason, **never** run a second rehearsal
on top of the leftover nonce resources. Inspect first:

```bash
docker volume ls        | grep rehearsal
docker ps -a            | grep rehearsal
docker network ls       | grep rehearsal
```

Remove any leaking nonce resources by name (not by label). The cleanup
trap in the rehearsal script normally does this; manual cleanup is only
needed when the script aborted before `trap` registered.

## Read next

- [docs/OPERATIONS.md](OPERATIONS.md) for service ownership and
  schedule.
- [docs/SECURITY.md](SECURITY.md) for the auth and trust boundary.
- [docs/HARDENING-BACKLOG.md](HARDENING-BACKLOG.md) for deferred
  recovery hardening.