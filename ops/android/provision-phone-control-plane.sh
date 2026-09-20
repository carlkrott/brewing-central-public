#!/data/data/com.termux/files/usr/bin/bash
# provision-phone-control-plane.sh — render and install the phone control-plane
# template onto the on-device Termux tree.
#
# Contract (Wave 1 phone control plane):
#   * DRY RUN by default. Running with no flags renders the template into a
#     tmpdir and prints what WOULD happen; it does not touch the device.
#   * --apply plus an exact --confirm-target <TERMUX_ROOT> sentinel switches
#     the script into install mode. The sentinel must EQUAL the canonical
#     Termux root for this deployment, otherwise the script exits non-zero.
#   * --apply also requires an explicit --automation-pubkey-file pointing at
#     an ssh-ed25519 public key on the operator workstation. Without it,
#     --apply fails closed: the control plane must NEVER ship with no
#     authorized_keys, otherwise sshd would deny every connection (and any
#     fallback that re-enables PasswordAuthentication would be unsafe).
#   * Installs openssh and python via `pkg install openssh python` ONLY when
#     --apply is set AND the platform is Android (TERMUX_VERSION is set).
#   * Generates Ed25519 host keys ON-DEVICE only. Never copies pre-existing
#     private keys into place; never logs or prints private key material.
#   * Renders a single automation authorized_keys line with the bounded
#     command=<dispatcher> plus restrict / no-agent / no-port / no-X11 /
#     no-pty / no-user-rc options. The public key bytes are never echoed;
#     only the fingerprint summary is printed.
#   * Writes 0700 on ROOT/control/sshd (and ancestors) and 0600 on
#     sshd_config / sshd_config.tmp / authorized_keys.
#   * Never prints private key bytes, public key bytes, or authorized_keys
#     contents.
#
# This script is safe to run from a developer workstation for inspection
# (dry-run); the real device-side provisioning is gated by --apply plus
# the confirm-target sentinel.

set -eu

PROG_NAME=provision-phone-control-plane
TEMPLATE_DIR="${TEMPLATE_DIR:-$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)}"
TEMPLATE_CONFIG="$TEMPLATE_DIR/sshd_config.example"
DISPATCHER="$TEMPLATE_DIR/phone-management-dispatch.py"
SSHD_LOOP="$TEMPLATE_DIR/phone-sshd-loop.sh"

DRY_RUN=1
APPLY=0
CONFIRM_TARGET=""
AUTOMATION_PUBKEY_FILE=""

usage() {
  cat <<USAGE
Usage: $PROG_NAME [--apply] [--confirm-target <TERMUX_ROOT>] [--automation-pubkey-file <path>]
  --apply                       enable install mode (otherwise dry run)
  --confirm-target              exact Termux app root the install targets;
                                REQUIRED with --apply. Must equal the canonical
                                /data/data/com.termux/files/home path.
  --automation-pubkey-file      path to an ssh-ed25519 PUBLIC key file
                                rendered from the operator workstation. Required
                                with --apply. The file must contain exactly one
                                ssh-ed25519 line; private keys are not handled.
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; DRY_RUN=0; shift ;;
    --confirm-target) CONFIRM_TARGET="${2:-}"; shift 2 ;;
    --automation-pubkey-file) AUTOMATION_PUBKEY_FILE="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown-arg: $1" >&2; exit 64 ;;
  esac
done

# Canonical Termux root. We refuse --apply unless the operator explicitly
# types this exact value (no env-var shortcuts, no path rewrites).
CANONICAL_TERMUX_ROOT="/data/data/com.termux/files/home"

emit() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

if [ "$APPLY" -eq 1 ]; then
  if [ "$CONFIRM_TARGET" != "$CANONICAL_TERMUX_ROOT" ]; then
    emit "reject confirm-target-required canonical=$CANONICAL_TERMUX_ROOT"
    exit 65
  fi
  if [ -z "$AUTOMATION_PUBKEY_FILE" ]; then
    emit "reject automation-pubkey-required"
    exit 67
  fi
  if [ ! -r "$AUTOMATION_PUBKEY_FILE" ]; then
    emit "reject automation-pubkey-unreadable path=$AUTOMATION_PUBKEY_FILE"
    exit 68
  fi
  ROOT="$CONFIRM_TARGET/brewing-central"
else
  ROOT="${PROVISION_DRY_ROOT:-/tmp/brewing-central-dryrun}"
  mkdir -p "$ROOT"
fi

CONTROL_DIR="$ROOT/control"
SSHD_DIR="$CONTROL_DIR/sshd"
BIN_DIR="$CONTROL_DIR/bin"
SSH_USER_DIR="$CONTROL_DIR/ssh"
AUTOMATION_DIR="$SSHD_DIR/automation"

emit "mode=$( [ "$APPLY" -eq 1 ] && echo apply || echo dry-run ) root=$ROOT"

if [ "$APPLY" -eq 1 ] && [ -z "${TERMUX_VERSION:-}" ]; then
  emit "reject apply-only-on-termux TERMUX_VERSION=unset"
  exit 66
fi

# ---- step 1: install openssh (apply only) -------------------------------
if [ "$APPLY" -eq 1 ]; then
  if ! command -v sshd >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
    emit "pkg-install:openssh-python"
    pkg install -y openssh python >/dev/null 2>&1 || {
      emit "pkg-install-failed"; exit 1;
    }
  else
    emit "pkg-install:openssh-python-already-present"
  fi
fi

# ---- step 2: prepare the control tree ----------------------------------
if [ "$APPLY" -eq 1 ]; then
  mkdir -p "$SSHD_DIR" "$BIN_DIR" "$SSH_USER_DIR"
  chmod 0700 "$SSH_USER_DIR"

  # Enforce 0700 on every control-plane ancestor so sshd's StrictModes check
  # does not refuse a too-loose directory. ROOT, control, sshd.
  for ancestor in "$ROOT" "$CONTROL_DIR" "$SSHD_DIR"; do
    chmod 0700 "$ancestor" 2>/dev/null || true
  done
else
  DRY_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/brewing-central-control.XXXXXX")" || {
    emit "dry-run-temp-failed"; exit 1;
  }
  trap 'rm -rf "$DRY_ROOT"' EXIT
fi

# ---- step 3: Ed25519 host keys (on-device only, apply only) ------------
# sshd -t validates the HostKey paths in the rendered configuration, so the
# on-device host key must exist before the configuration test runs.
if [ "$APPLY" -eq 1 ]; then
  if [ ! -f "$SSHD_DIR/host_ed25519_key" ]; then
    emit "host-key-gen algo=ed25519 dst=$SSHD_DIR"
    ssh-keygen -q -N "" -t ed25519 -f "$SSHD_DIR/host_ed25519_key" || {
      emit "host-key-gen-failed"; exit 1;
    }
    chmod 0600 "$SSHD_DIR/host_ed25519_key"
    chmod 0644 "$SSHD_DIR/host_ed25519_key.pub"
    # Never print private key bytes; only the public fingerprint summary.
    ssh-keygen -lf "$SSHD_DIR/host_ed25519_key.pub" | emit "host-key-fp-summary"
  else
    emit "host-key-present path=$SSHD_DIR/host_ed25519_key"
  fi
else
  emit "dry-skip:host-key-gen"
fi

# ---- step 4: render the template into the control tree ----------------
RENDERED_CONFIG="$SSHD_DIR/sshd_config"
emit "render-template src=$TEMPLATE_CONFIG dst=$RENDERED_CONFIG"

if [ "$APPLY" -eq 1 ]; then
  # Apply: write a tmp file with 0600, validate, then move into place.
  TMP_CONFIG="$SSHD_DIR/sshd_config.tmp"
  sed \
    -e "s|<TERMUX_APP_ROOT>|$CANONICAL_TERMUX_ROOT|g" \
    -e "s|<TERMUX_PREFIX>|$CANONICAL_TERMUX_ROOT/../usr|g" \
    "$TEMPLATE_CONFIG" >"$TMP_CONFIG"
  chmod 0600 "$TMP_CONFIG"
  if command -v sshd >/dev/null 2>&1; then
    sshd -t -f "$TMP_CONFIG" || {
      emit "config-test-failed"; rm -f "$TMP_CONFIG"; exit 1;
    }
  fi
  mv "$TMP_CONFIG" "$RENDERED_CONFIG"
  chmod 0600 "$RENDERED_CONFIG"
  emit "config-installed mode=0600 path=$RENDERED_CONFIG"
else
  # Dry run: render to a tmp path with the same modes; never touch ROOT.
  DRY_TMP="$DRY_ROOT/sshd_config"
  sed \
    -e "s|<TERMUX_APP_ROOT>|/tmp/termux-dryrun|g" \
    -e "s|<TERMUX_PREFIX>|/tmp/termux-dryrun/../usr|g" \
    "$TEMPLATE_CONFIG" >"$DRY_TMP"
  chmod 0600 "$DRY_TMP" 2>/dev/null || true
  emit "dry-render tmp=$DRY_TMP"
fi

# ---- step 5: dispatcher + supervisor (apply or dry) --------------------
for src in "$DISPATCHER" "$SSHD_LOOP"; do
  if [ ! -f "$src" ]; then
    emit "missing-source path=$src"; exit 1
  fi
done

if [ "$APPLY" -eq 1 ]; then
  install -m 0755 "$DISPATCHER" "$BIN_DIR/phone-management-dispatch.py"
  install -m 0755 "$SSHD_LOOP"   "$CONTROL_DIR/phone-sshd-loop.sh"
  emit "bin-installed dispatch=$BIN_DIR/phone-management-dispatch.py"
  emit "bin-installed loop=$CONTROL_DIR/phone-sshd-loop.sh"
else
  emit "dry-skip:bin-install src=$DISPATCHER"
  emit "dry-skip:bin-install src=$SSHD_LOOP"
fi

# ---- step 6: render the automation authorized_keys ---------------------
# Render one line per automation principal. The line starts with explicit
# options: command= forces every connection through the dispatcher, and the
# restrict/no-* set disables every form of forwarding, PTY allocation, and
# user RC processing. The PUBLIC key is never echoed; only the fingerprint
# is logged for the operator record.
RENDERED_AUTHORIZED_KEYS="$AUTOMATION_DIR/authorized_keys"
DISPATCHER_REL_PATH="$CANONICAL_TERMUX_ROOT/brewing-central/control/bin/phone-management-dispatch.py"
KEY_OPTIONS="command=\"$DISPATCHER_REL_PATH\",restrict,no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-pty,no-user-rc"

if [ "$APPLY" -eq 1 ]; then
  PUBKEY_SRC="$AUTOMATION_PUBKEY_FILE"
else
  # Dry-run synthetic key used to verify the rendering path on a workstation
  # where no real automation key is available. We never write this anywhere.
  PUBKEY_SRC="$(mktemp)"
  printf 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAQ== dryrun@example.invalid\n' \
    >"$PUBKEY_SRC"
fi

# Validate the public key. ssh-keygen -l requires a key file and prints a
# "comment is not supported" warning for keys with stray fields; we trim to
# three whitespace-separated fields to keep the line canonical.
PUBKEY_LINE=""
IFS= read -r PUBKEY_LINE <"$PUBKEY_SRC" || true
if [[ ! "$PUBKEY_LINE" =~ ^ssh-ed25519[[:space:]]+[A-Za-z0-9+/=]+[[:space:]]+[^[:space:]]+$ ]]; then
  emit "reject automation-pubkey-invalid"
  if [ "$APPLY" -ne 1 ]; then rm -f "$PUBKEY_SRC"; fi
  exit 69
fi
PUBKEY_BODY="$(printf '%s\n' "$PUBKEY_LINE" | awk '{print $1, $2}')"
PUBKEY_COMMENT="$(printf '%s\n' "$PUBKEY_LINE" | awk '{print $3}')"

# Hash for the operator record (no key bytes printed).
PUBKEY_FP_TMP="$(mktemp 2>/dev/null || echo)"
if [ -n "$PUBKEY_FP_TMP" ]; then
  printf '%s\n' "$PUBKEY_LINE" >"$PUBKEY_FP_TMP"
  if [ "$APPLY" -eq 1 ]; then
    if ! FP_SUMMARY="$(ssh-keygen -lf "$PUBKEY_FP_TMP" 2>/dev/null)"; then
      rm -f "$PUBKEY_FP_TMP"
      emit "reject automation-pubkey-invalid"
      exit 69
    fi
  else
    FP_SUMMARY="dry-run-public-key"
  fi
  rm -f "$PUBKEY_FP_TMP"
fi

if [ "$APPLY" -eq 1 ]; then
  mkdir -p "$AUTOMATION_DIR"
  chmod 0700 "$AUTOMATION_DIR"
  TMP_AK="$AUTOMATION_DIR/authorized_keys.tmp"
  printf '%s %s %s\n' "$KEY_OPTIONS" "$PUBKEY_BODY" "$PUBKEY_COMMENT" >"$TMP_AK"
  chmod 0600 "$TMP_AK"
  mv "$TMP_AK" "$RENDERED_AUTHORIZED_KEYS"
  chmod 0600 "$RENDERED_AUTHORIZED_KEYS"
  emit "authorized-keys-installed mode=0600 path=$RENDERED_AUTHORIZED_KEYS options-prefix=restrict,no-agent,no-port,no-X11,no-pty,no-user-rc"
  [ -n "${FP_SUMMARY:-}" ] && emit "automation-key-fp-summary" "$FP_SUMMARY"
else
  DRY_AK="$DRY_ROOT/authorized_keys"
  printf '%s %s %s\n' "$KEY_OPTIONS" "$PUBKEY_BODY" "$PUBKEY_COMMENT" >"$DRY_AK"
  chmod 0600 "$DRY_AK" 2>/dev/null || true
  rm -f "$PUBKEY_SRC"
  emit "dry-render tmp=$DRY_AK"
fi

emit "done mode=$( [ "$APPLY" -eq 1 ] && echo apply || echo dry-run )"
