#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root_dir"
source "$root_dir/scripts/app/sc-bin64.sh"
source "$root_dir/scripts/app/install-platform.sh"

app_name="Tobii Dashboard"
desktop_id="tobii-dashboard.desktop"
launcher_path="${TOBII_BIN_DIR:-${HOME}/.local/bin}/tobii-dashboard"
desktop_dir="${XDG_DATA_HOME:-${HOME}/.local/share}/applications"
desktop_path="${desktop_dir}/${desktop_id}"
udev_rule="/etc/udev/rules.d/99-tobii-eyetracker5.rules"
runtime_config="$(sc_config_file)"

remove_packages=0
dry_run=0
keep_user_data=0
keep_mediapipe_model=0

usage() {
  cat <<EOF
Usage: ./uninstall.sh [options]

Removes Tobii Linux generated artifacts and the "Tobii Dashboard" menu entry.
Distro packages are not removed unless --remove-packages is passed.

Options:
  --remove-packages      Also remove installer-known distro packages.
  --keep-user-data       Keep .tmp runtime settings/calibrations/logs.
  --keep-model           Keep assets/mediapipe/face_landmarker.task.
  --preflight            Dry-run only: print what would be removed and exit.
  -h, --help             Show this help.

Environment:
  SUDO=sudo              Command used for privilege escalation.
EOF
}

log() {
  printf '\n\033[1;34m==>\033[0m %s\n' "$*"
}

warn() {
  printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2
}

die() {
  printf '\033[1;31merror:\033[0m %s\n' "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --remove-packages) remove_packages=1 ;;
    --keep-user-data) keep_user_data=1 ;;
    --keep-model) keep_mediapipe_model=1 ;;
    --preflight) dry_run=1 ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
  shift
done

if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  die "do not run uninstall.sh with sudo; it removes user-owned files. It will ask for sudo only when needed."
fi

sudo_cmd="${SUDO:-sudo}"

need_cmd() {
  command -v "$1" >/dev/null 2>&1
}

rm_path() {
  local path="$1"
  if [[ -e "$path" || -L "$path" ]]; then
    echo "remove $path"
    if [[ "$dry_run" -eq 0 ]]; then
      rm -rf "$path"
    fi
  fi
}

sudo_rm_path() {
  local path="$1"
  if [[ -e "$path" || -L "$path" ]]; then
    echo "remove $path"
    if [[ "$dry_run" -eq 0 ]]; then
      run_privileged rm -f "$path"
    fi
  fi
}

remove_packages_for_pm() {
  local pm="$1"
  local packages
  packages="$(system_packages_for_pm "$pm")"
  [[ -n "$packages" ]] || die "unsupported package manager; cannot remove packages automatically."

  echo "remove packages via $pm:"
  printf '  %s\n' $packages
  if [[ "$dry_run" -eq 1 ]]; then
    return 0
  fi

  case "$pm" in
    apt)
      run_privileged apt-get remove -y $packages
      ;;
    dnf)
      run_privileged dnf remove -y $packages
      ;;
    pacman)
      run_privileged pacman -Rs --noconfirm $packages
      ;;
  esac
}

reload_udev() {
  if [[ "$dry_run" -eq 1 ]]; then
    return 0
  fi
  if need_cmd udevadm; then
    run_privileged udevadm control --reload-rules || true
    run_privileged udevadm trigger || true
  else
    warn "udevadm not found; reboot or reload udev manually if the rule was installed."
  fi
}

kill_pidfile() {
  local file="$1"
  local pid=""
  [[ -f "$file" ]] || return 0
  pid="$(cat "$file" 2>/dev/null || true)"
  rm -f "$file"
  [[ -n "$pid" ]] || return 0
  [[ "$pid" == "$$" || "$pid" == "$BASHPID" || "$pid" == "$PPID" ]] && return 0
  kill "$pid" 2>/dev/null || true
}

stop_runtime_helpers() {
  local pid_dir="$root_dir/.tmp/sc-tobii-native-runtime/pids"
  local name
  [[ -d "$pid_dir" ]] || return 0
  for name in dashboard engine mux middleware wine-bridges pipe etdefaultpipe tobii-prefixed-pipe tobiiprp-prefixed-pipe runtime-services; do
    kill_pidfile "$pid_dir/$name.pid"
  done
}

print_plan() {
  local pm="$1"
  log "Uninstall preflight report"
  echo "project=$root_dir"
  echo "package_manager=$pm"
  echo
  echo "Planned removals:"
  echo "  - user tracking service/autostart entry"
  echo "  - desktop entries: $desktop_path and ${desktop_dir}/tobii-tray.desktop"
  echo "  - launcher: $launcher_path"
  echo "  - build outputs: build/, .tmp/sc-tobii-stock-recon/"
  echo "  - MediaPipe venv: .venv/mediapipe/"
  [[ "$keep_mediapipe_model" -eq 1 ]] && echo "  - keep MediaPipe model" || echo "  - MediaPipe model: assets/mediapipe/face_landmarker.task"
  [[ "$keep_user_data" -eq 1 ]] && echo "  - keep runtime user data/logs/config" || echo "  - runtime user data/logs/config: .tmp/sc-tobii-native-runtime/, $runtime_config"
  echo "  - udev rule: $udev_rule"
  if [[ "$remove_packages" -eq 1 ]]; then
    if [[ "$pm" == "none" ]]; then
      echo "  - package removal requested, but no supported package manager was detected"
    else
      echo "  - distro packages:"
      printf '    %s\n' $(system_packages_for_pm "$pm")
    fi
  else
    echo "  - keep distro packages"
  fi
}

log "Tobii Linux uninstaller"
echo "project=$root_dir"
pm="$(detect_package_manager)"

if [[ "$dry_run" -eq 1 ]]; then
  print_plan "$pm"
  echo
  echo "No changes were made."
  exit 0
fi

log "Removing legacy managed launch hook if present"
"$root_dir/scripts/app/disable-sc-tobii-native-launch-hook.sh"

log "Stopping and unregistering tracking service"
"$root_dir/scripts/app/manage-tracking-service.py" uninstall

log "Stopping local Tobii runtime processes"
stop_runtime_helpers

log "Removing application launcher"
rm_path "$desktop_path"
rm_path "${desktop_dir}/tobii-tray.desktop"
rm_path "$launcher_path"
if need_cmd update-desktop-database; then
  update-desktop-database "$desktop_dir" >/dev/null 2>&1 || true
fi

log "Removing generated project artifacts"
rm_path "$root_dir/build"
rm_path "$root_dir/.tmp/sc-tobii-stock-recon"
rm_path "$root_dir/.venv/mediapipe"
rm_path "$root_dir/.venv/ui"
rm_path "$root_dir/.tmp/uv"
rm_path "$root_dir/.tmp/uv-python"
if [[ "$keep_user_data" -eq 0 ]]; then
  rm_path "$root_dir/.tmp/sc-tobii-native-runtime"
  rm_path "$runtime_config"
fi
if [[ "$keep_mediapipe_model" -eq 0 ]]; then
  rm_path "$root_dir/assets/mediapipe/face_landmarker.task"
  rm_path "$root_dir/assets/mediapipe/face_landmarker.task.sha256"
fi

log "Removing udev rule"
sudo_rm_path "$udev_rule"
reload_udev

if [[ "$remove_packages" -eq 1 ]]; then
  log "Removing distro packages"
  remove_packages_for_pm "$pm"
else
  log "Leaving distro packages installed"
fi

cat <<EOF

Uninstall complete.

The source tree itself was not removed:
  $root_dir

EOF

# Retain cached source versions, but clear managed registration so reinstall works.
if [[ -f "$root_dir/.managed-install.json" ]]; then
  python3 - "$root_dir" "$keep_user_data" <<'PYMANAGED'
import json
from pathlib import Path
import shutil
import sys
root=Path(sys.argv[1]).resolve()
marker=json.loads((root/'.managed-install.json').read_text())
base=Path(marker['base']).resolve()
if root.parent != base/'versions':
    raise SystemExit('Managed installation marker does not match its version directory')
if sys.argv[2]=='0':
    shutil.rmtree(base/'state/runtime',ignore_errors=True)
receipt=base/'install.json'
if receipt.exists():
    command=Path(json.loads(receipt.read_text())['bin_dir'])/'tobii'
    command.unlink(missing_ok=True)
receipt.unlink(missing_ok=True)
for name in ('current','previous'):
    path=base/name
    if path.is_symlink():path.unlink()
print('Managed integration removed. Cached versions remain in',base/'versions')
PYMANAGED
fi
