#!/usr/bin/env bash
# Small, pipe-safe entry point; the downloaded Python installer uses only stdlib.
set -euo pipefail

main() {
  local option preflight=0 pm='' privilege='' temporary repository ref
  for option in "$@"; do
    case "$option" in
      --preflight) preflight=1 ;;
      -h|--help)
        cat <<'EOF'
Tobii Linux one-line installer (Linux x86-64, glibc).
Run as your normal user. Installs and updates the same managed installation.

  --preflight            Show the plan without persistent changes.
  --version TAG          Install a release (default: latest, or main if none).
  --ref REF              Install a specific Git reference instead of a release.
  --install-dir PATH     Default: $XDG_DATA_HOME/tobii-linux (~/.local/share).
  --migrate-from PATH    Copy settings from an existing source installation.
  --rollback             Reactivate the previous managed version.
  --force                Reinstall even if already current.
  --no-start             Register installation without starting service or UI.
  --no-system-packages   Use already installed system dependencies.
  --no-udev              Do not change the USB permission rule.

Advanced/offline: --archive PATH [--sha256 HEX], --repo OWNER/NAME.
EOF
        return ;;
    esac
  done
  [[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || {
    echo 'This runtime currently requires x86-64 Linux.' >&2; return 1;
  }
  [[ "$EUID" -ne 0 ]] || { echo 'Run without sudo; only system dependencies request elevation.' >&2; return 1; }
  if ! command -v python3 >/dev/null 2>&1; then
    if (( preflight )); then
      echo 'Would install Python 3 and CA certificates, then download the managed installer.'
      echo 'No persistent changes made.'; return
    fi
    if command -v sudo >/dev/null 2>&1; then privilege=sudo
    elif command -v doas >/dev/null 2>&1; then privilege=doas
    else echo 'Install Python 3 and CA certificates first, or provide sudo/doas.' >&2; return 1; fi
    if command -v apt-get >/dev/null 2>&1; then pm=apt
    elif command -v dnf >/dev/null 2>&1; then pm=dnf
    elif command -v pacman >/dev/null 2>&1; then pm=pacman
    else echo 'Install Python 3 using your system package manager, then rerun.' >&2; return 1; fi
    case "$pm" in
      apt) "$privilege" apt-get update; "$privilege" apt-get install -y python3 ca-certificates ;;
      dnf) "$privilege" dnf install -y python3 ca-certificates ;;
      pacman) "$privilege" pacman -S --needed --noconfirm python ca-certificates ;;
    esac
  fi
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || {
    echo 'The bootstrap needs Python 3.8 or newer.' >&2; return 1;
  }
  repository="${TOBII_INSTALLER_REPOSITORY:-njmill/tobii-linux-pub}"
  ref="${TOBII_INSTALLER_REF:-main}"
  [[ "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ && "$ref" =~ ^[A-Za-z0-9_./-]+$ ]] || return 2
  temporary="$(mktemp -d)"
  trap "rm -rf -- $(printf '%q' "$temporary")" EXIT
  local url="https://raw.githubusercontent.com/$repository/$ref/scripts/app/install-managed.py"
  if command -v curl >/dev/null 2>&1; then
    curl --proto '=https' --tlsv1.2 -fsSL --retry 3 --connect-timeout 15 "$url" -o "$temporary/installer.py"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$temporary/installer.py" "$url"
  else echo 'Install curl or wget, then rerun.' >&2; return 1; fi
  python3 "$temporary/installer.py" --repo "$repository" "$@"
}

# Define everything before starting a program that may read the terminal.
main "$@"
