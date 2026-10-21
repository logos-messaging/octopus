#!/bin/sh
# Install or upgrade Octopus on macOS and Linux. Re-run it to upgrade.
#
#   curl -fsSL https://raw.githubusercontent.com/logos-messaging/octopus/main/scripts/install.sh | sh
#
# OCTOPUS_REF picks the branch, tag or commit to install (default: main).
set -eu

REPO="https://github.com/logos-messaging/octopus"
REF="${OCTOPUS_REF:-main}"
SPEC="octopus[logos] @ git+${REPO}@${REF}"

die() { echo "error: $*" >&2; exit 1; }

PYTHON=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
    "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    PYTHON="$candidate"
    break
  fi
done
[ -n "$PYTHON" ] || die "could not find Python 3.10 or newer: install it, then re-run"

if command -v pipx >/dev/null 2>&1; then
  echo "installing with pipx"
  pipx install --force --python "$PYTHON" "$SPEC"
elif command -v uv >/dev/null 2>&1; then
  echo "installing with uv"
  uv tool install --force --python "$PYTHON" "$SPEC"
else
  echo "installing into a private virtualenv"
  VENV="${OCTOPUS_HOME:-$HOME/.local/share/octopus}/venv"
  BIN="$HOME/.local/bin"
  "$PYTHON" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet --upgrade "$SPEC"
  mkdir -p "$BIN"
  ln -sf "$VENV/bin/octopus" "$BIN/octopus"
  case ":$PATH:" in
    *":$BIN:"*) ;;
    *) echo "note: add $BIN to your PATH" ;;
  esac
fi

OCTOPUS_BIN="$(command -v octopus || true)"
[ -n "$OCTOPUS_BIN" ] || OCTOPUS_BIN="${BIN:-$HOME/.local/bin}/octopus"

echo "fetching the native library (liblogosdelivery)"
if ! "$OCTOPUS_BIN" lib install; then
  echo "note: could not fetch it; build it as in the README, then run: octopus lib install --file <path>"
fi

cat <<'MSG'

Octopus is installed. Next:
  octopus runner setup --coordinator <key>     # who may send you jobs
  octopus runner allow <org/repo>               # which repos you serve
  octopus runner install-service                # start at login
  octopus lib status                            # check the native library
MSG
