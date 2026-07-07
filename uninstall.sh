#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root_dir"

app_name="Tobii Dashboard"
desktop_id="tobii-dashboard.desktop"
launcher_path="${HOME}/.local/bin/tobii-dashboard"
desktop_path="${HOME}/.local/share/applications/${desktop_id}"
udev_rule="/etc/udev/rules.d/99-tobii-eyetracker5.rules"

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

detect_package_manager() {
  if need_cmd apt-get; then
    echo apt
  elif need_cmd dnf; then
    echo dnf
  elif need_cmd pacman; then
    echo pacman
  else
    echo none
  fi
}

system_packages_for_pm() {
  case "$1" in
    apt)
      echo build-essential make pkg-config libusb-1.0-0-dev libssl-dev mingw-w64 wine python3-tk python3-venv curl tar ca-certificates
      ;;
    dnf)
      echo gcc gcc-c++ make pkgconf-pkg-config libusb1-devel openssl-devel mingw64-gcc wine python3-tkinter python3-virtualenv curl tar ca-certificates systemd-udev
      ;;
    pacman)
      echo base-devel pkgconf libusb openssl mingw-w64-gcc wine python tk curl tar ca-certificates
      ;;
    *)
      echo
      ;;
  esac
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
      $sudo_cmd rm -f "$path"
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
      $sudo_cmd apt-get remove -y $packages
      ;;
    dnf)
      $sudo_cmd dnf remove -y $packages
      ;;
    pacman)
      $sudo_cmd pacman -Rs --noconfirm $packages
      ;;
  esac
}

reload_udev() {
  if [[ "$dry_run" -eq 1 ]]; then
    return 0
  fi
  if need_cmd udevadm; then
    $sudo_cmd udevadm control --reload-rules || true
    $sudo_cmd udevadm trigger || true
  else
    warn "udevadm not found; reboot or reload udev manually if the rule was installed."
  fi
}

print_plan() {
  local pm="$1"
  log "Uninstall preflight report"
  echo "project=$root_dir"
  echo "package_manager=$pm"
  echo
  echo "Planned removals:"
  echo "  - desktop entry: $desktop_path"
  echo "  - launcher: $launcher_path"
  echo "  - build outputs: build/, .tmp/sc-tobii-stock-recon/"
  echo "  - MediaPipe venv: .venv/mediapipe/"
  [[ "$keep_mediapipe_model" -eq 1 ]] && echo "  - keep MediaPipe model" || echo "  - MediaPipe model: assets/mediapipe/face_landmarker.task"
  [[ "$keep_user_data" -eq 1 ]] && echo "  - keep runtime user data/logs" || echo "  - runtime user data/logs: .tmp/sc-tobii-native-runtime/"
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

log "Stopping local Tobii runtime processes"
pkill -f "$root_dir/scripts/recon/eye-pose-dashboard.py" 2>/dev/null || true
pkill -f "$root_dir/build/tobii-ttp-mux" 2>/dev/null || true
pkill -f "$root_dir/scripts/app/tobii-middleware-spy.py" 2>/dev/null || true
pkill -f "tobii-middleware-pipe-spy.exe" 2>/dev/null || true

log "Removing application launcher"
rm_path "$desktop_path"
rm_path "$launcher_path"
if need_cmd update-desktop-database; then
  update-desktop-database "${HOME}/.local/share/applications" >/dev/null 2>&1 || true
fi

log "Removing generated project artifacts"
rm_path "$root_dir/build"
rm_path "$root_dir/.tmp/sc-tobii-stock-recon"
rm_path "$root_dir/.venv/mediapipe"
rm_path "$root_dir/.tmp/uv"
rm_path "$root_dir/.tmp/uv-python"
if [[ "$keep_user_data" -eq 0 ]]; then
  rm_path "$root_dir/.tmp/sc-tobii-native-runtime"
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
