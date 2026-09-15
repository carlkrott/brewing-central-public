#!/usr/bin/env bash
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)"
VERSION="v2.11.4"
ARCHIVE_NAME="caddy_2.11.4_linux_amd64.tar.gz"
EXPECTED_SHA512="8220d1f013b6f27510247b2360c9e0ca9f018feebd82515f07635318b34ff9777ccc8fd0b6e6f2486ce3a33fe389fbb7db12d05baa474f4587509fb4f5ebf1c9"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/ispindel-dashboard/caddy-v2.11.4"
CADDY_BIN="${CADDY_BIN:-$CACHE/caddy}"
CADDY_TARBALL="${CADDY_TARBALL:-$CACHE/$ARCHIVE_NAME}"

fail() { printf 'CADDY_CONFIG_INVALID reason=%s\n' "$1" >&2; exit 1; }
[[ -x "$CADDY_BIN" ]] || fail "missing-validator"
[[ -f "$CADDY_TARBALL" ]] || fail "missing-pinned-archive"
actual_version="$($CADDY_BIN version | awk '{print $1}')"
[[ "$actual_version" == "$VERSION" ]] || fail "version-mismatch"
printf '%s  %s\n' "$EXPECTED_SHA512" "$CADDY_TARBALL" | sha512sum --check --status || fail "archive-hash-mismatch"

export ISPINDEL_LAN_IP="${ISPINDEL_LAN_IP:-192.0.2.25}"
export ISPINDEL_TAILNET_IP="${ISPINDEL_TAILNET_IP:-198.51.100.3}"
export TAILNET_FQDN="${TAILNET_FQDN:-ispindel.test.invalid}"
export ISPINDEL_ADMIN_USER="${ISPINDEL_ADMIN_USER:-fixture-admin}"
export ISPINDEL_ADMIN_PASSWORD_HASH="${ISPINDEL_ADMIN_PASSWORD_HASH:-\$2a\$14\$lx8bISeh63WW2Y1ED8cORet5IcAUZ/oeoDV5gt.0xFtbgRfThpFt6}"

"$CADDY_BIN" validate --config "$ROOT/ops/caddy/Caddyfile" --adapter caddyfile >/dev/null
"$CADDY_BIN" adapt --config "$ROOT/ops/caddy/Caddyfile" --adapter caddyfile --pretty >/dev/null
printf 'CADDY_CONFIG_OK version=%s archive_sha512=%s\n' "$VERSION" "$EXPECTED_SHA512"
