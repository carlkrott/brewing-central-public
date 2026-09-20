# Security — Production Boundary and Credentials

## Production state (deployed 2026-09-12)

The normal browser path terminates on deployment-host Caddy at
`https://<TAILNET_CADDY_HOST>/`. It uses the internal Caddy CA and
HTTP Basic Auth, then proxies to the phone over the Tailnet. The LAN listener
at `<LAN_INGEST_HOST>:8098` accepts only `POST /api/ingest`, enforces the body cap,
and proxies to the same phone application. All other LAN paths/methods fail
closed.

The phone application verifies the stable iSpindel identity separately from
the body `token`, applies the in-process rate limit/body validation, and strips
the token before persisting `raw_json`. Phone `phone.env`, the ingest verifier,
and the ZeroClaw token are Termux-private mode `0600`; their values must never
be printed, logged, committed, or copied into receipts.

Direct `<PHONE_TAILNET_HOST>:8098` access is a Tailnet-restricted operations/recovery
surface. It does not replace Caddy's Basic Auth browser boundary. Headscale ACLs
also narrowly allow the phone to reach combined-Gemma text/vision, SearXNG, and
Kiwix; the phone does not receive GPU-lifecycle authority.

ZeroClaw is loopback-only on the phone. Research is mediated by fixed broker
endpoints and treats retrieved content as untrusted data. Camera analysis is
restricted to structural safety for an intentionally empty vessel and cannot
infer liquid, fermentation, contamination, gravity, or temperature.

The deployment-host predecessor is a cold rollback target, not a live parallel writer.
Do not start it against phone-owned data or expose it concurrently without a
separately verified isolation and cutover procedure.

See [PHONE-RUNBOOK.md](PHONE-RUNBOOK.md) for the no-remote-security-disable rule,
ADB boundary, secret-safe checks, and calibration activation gate.

## Historical predecessor/candidate model

The remainder documents the earlier deployment-host-only predecessor and proposed
boundary. Its `CURRENT`/`TARGET` labels are historical; the production-state
section above is authoritative.

## TL;DR

- **CURRENT**: the application is reachable on `0.0.0.0:8098` with **no
  authentication and no TLS**. Anyone on the LAN or the tailnet can read
  data, read the device list, and POST telemetry. The application does
  not separate the device identity from the token.
- **TARGET**: a Caddy two-listener design puts LAN ingest behind a
  path/method boundary with per-device JSON `token` auth, and the tailnet
  UI behind HTTPS with HTTP Basic Auth and a Headscale ACL.

## Threat model

What we protect against:

- An unauthorized LAN attacker reading telemetry or changing configuration.
- An unauthorized tailnet attacker reaching the dashboard or its API.
- An attacker replaying, fuzzing, or flooding `/api/ingest`.
- An attacker reading the token out of the database, response bodies, or
  structured logs.

What we **do not** protect against in this release:

- An attacker with root on the production host. They can read the SQLite
  file directly.
- An attacker who can MITM the LAN at the physical layer. Without ARP
  authentication, they can replay a captured POST.
- An attacker who controls the iSpindel firmware. Stock firmware is trusted
  to set the `ID`/`id`/`device_id`/`name` field honestly.

Phase 7 C/D/E (transitive descriptor binding, Docker daemon attestation,
cross-host reviewer independence) are explicitly deferred. See
[docs/HARDENING-BACKLOG.md](HARDENING-BACKLOG.md).

## Network boundary

### CURRENT

- Container publishes `0.0.0.0:8098` and `[::]:8098` directly to FastAPI.
- Any host on the LAN or tailnet can reach every route.
- The only application-level cap is the 64 KiB body size middleware in
  `app/main.py`.
- No TLS, no auth, no rate limit, no per-device quota.

Graphically:

```
LAN/tailnet -> 0.0.0.0:8098 -> FastAPI (any route)
```

### TARGET

```
LAN               -> <LAN_INGEST_HOST>:8098 -> Caddy LAN listener
                                           allow: POST /api/ingest
                                           deny:  every other method/path -> 404
                                           body max 64 KiB
                                           -> 127.0.0.1:18098 (Docker loopback)
                                              FastAPI: token validation
                                                       + token-bucket limit
                                                       + 64 KiB body cap
                                              -> SQLite

Tailnet (<TAILNET_CADDY_HOST>:443) -> Caddy tailnet listener
                              TLS internal (Caddy CA)
                              Basic Auth
                              All application routes
                              -> 127.0.0.1:18098
```

Three properties follow:

1. **The container is loopback-only.** Its only host publication is
   `127.0.0.1:18098`. There is no host route that reaches FastAPI without
   going through Caddy.
2. **LAN can only ingest.** The Caddy LAN listener denies every other path
   or method. The decision is made before the request reaches FastAPI.
3. **Tailnet can only reach the dashboard after auth.** Headscale ACL
   permits only authorized user/group nodes to reach the tailnet port.

## Authentication

### CURRENT

- There is no authentication.
- The `token` field is **also accepted as a device-ID alias** in the
  predecessor. This is the bug T4 fixes.

### TARGET

Two distinct authentication paths:

| Surface | Auth method | Credential location |
|---|---|---|
| LAN `/api/ingest` | Per-device JSON `token` (salted verifier or constant-time comparable) | Body field `token` (stock firmware constraint). |
| Tailnet UI | HTTP Basic Auth (scrypt/bcrypt hash) | `Authorization` header. |

Identity priority for ingest is `ID` → `id` → `device_id` → `name`. The
**token is never a device-ID alias**. Token validation must separate the
token from the device identity.

Production startup is fail-closed: if `/run/secrets/ingest-tokens.json` or
the admin configuration is missing/invalid, the application refuses to
start. Development/test mode is explicit and must be enabled deliberately.

## Token file and rotation

The token file is `/run/secrets/ingest-tokens.json` on the production host,
mounted read-only into the container. It maps each device ID to one or two
PBKDF2-HMAC-SHA256 verifiers (100,000 iterations), each with a hexadecimal salt,
verifier digest, and nullable UTC `not_after`. See the fake-only
`config/ingest-tokens.example.json`. **The raw token is never stored in
SQLite, response bodies, structured logs, or evidence files.**

Rotation works as follows:

1. **Add the new verifier** alongside the existing verifier in
   `/run/secrets/ingest-tokens.json`. At most two verifier slots are allowed.
2. **Reconfigure the iSpindel** with the new token. Stock firmware has no
   dual-credential mode; the new token is uploaded in place.
3. Set the old verifier's `not_after` to an explicit UTC timestamp ending in
   `Z`. The normal operational window is at most 24 hours.
4. **Restart the container** so the validated token file is re-read. There is
   no signal-driven or mid-flight secret reload in this release.
5. Remove the expired verifier and restart again after the overlap ends.

The rotation window exists because the iSpindel firmware uploads to the
service on its own schedule and cannot be instructed to re-upload on
demand. The bounded window is the proven mitigation.

During the rotation window, both old and new tokens are accepted. Unknown
tokens return `401` with a generic body (no enumeration). The response
timing for unknown tokens is constant-time with the response timing for
valid-but-wrong-device tokens.

## Rate limiting and body cap

The target `/api/ingest` enforces:

- **Per-device in-memory token bucket.** Default permits normal sensor
  cadence plus retries. Returns `429` with `Retry-After` on overflow.
- **64 KiB body cap before JSON parsing.** Body content type must be
  `application/json`. Other content types are rejected.
- **Constant-time error shape.** Unknown device/token pairs and
  token-for-other-device pairs execute the same two-verifier timing shape and
  return the same generic `401` body. A missing stable device identity is a
  malformed payload and returns `422`.

Using an in-process token bucket avoids the operational cost of a non-core
Caddy rate-limit plugin and keeps the boundary logic in the application
where it can be tested.

## TrustedHost, CSP, headers

- `TrustedHostMiddleware` accepts: the stable tailnet FQDN, `<LAN_INGEST_HOST>`,
  `127.0.0.1`, and test hosts. Forwarded headers are trusted only because
  the app is loopback-published and Caddy overwrites them.
- **CSP** is preserved.
- **HSTS** is added **only** on HTTPS responses, never on the LAN listener.
- `X-Content-Type-Options: nosniff`, `Referrer-Policy`, and frame denial
  are set on every response.

## Headscale / Tailscale ACL

The publisher host is `<TAILNET_CADDY_HOST>` on the Headscale control plane
`https://<HEADSCALE_HOST>/`. The control plane lives on a
separate operator host. The active policy is `/path/to/headscale/config/acl.yaml`
and is default-deny.

The target ACL changes:

- Defines a named group (or enumerated user identities) for authorized
  operator devices.
- Permits that group to `tag:publisher:443` only.
- Preserves the existing admin rule (`tag:admin` → `tag:publisher`).
- Does **not** grant ordinary users access to port 8098 or other publisher
  ports.

The ACL is captured and verified before any candidate is applied. The
predecessor ACL bytes, SHA-256, owner, group, and mode are recorded under
`$RUN_ROOT/acl-predecessor/` so rollback is exact.

## Logging redaction

The log contract is: **no token, no Authorization header, no request body,
no query string, no secret**. Caddy access logs redact query strings and
Authorization. Application logs never include the request body on the
ingest path. journald events emitted by the target schemas
([docs/OPERATIONS.md](OPERATIONS.md)) are structured and explicitly
secret-free.

## What an operator does NOT do

- **Do not** put the token in a URL, query string, or `Authorization`
  header. The target protocol is JSON body only.
- **Do not** commit `/run/secrets/ingest-tokens.json`, Basic Auth hashes,
  or any production secret to Git. The repository `.gitignore` excludes
  `.env*` and `secrets/`.
- **Do not** edit the Headscale ACL without capturing the predecessor
  bytes. The plan's T5 step enforces this.
- **Do not** expose port 8098 on the tailnet. The Caddy listener is bound
  to `<LAN_INGEST_HOST>` only.
- **Do not** assume the predecessor `run/secrets` is mounted. It is not
  yet configured; the container currently publishes `0.0.0.0:8098` with
  no auth.

## Read next

- [docs/ARCHITECTURE.md](ARCHITECTURE.md) for the data flow.
- [docs/OPERATIONS.md](OPERATIONS.md) for service ownership and alerts.
- [docs/RECOVERY.md](RECOVERY.md) for the recovery path.
- [docs/HARDENING-BACKLOG.md](HARDENING-BACKLOG.md) for deferred hardening.
