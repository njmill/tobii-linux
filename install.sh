#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root_dir"
source "$root_dir/scripts/app/sc-bin64.sh"

app_name="Tobii Dashboard"
desktop_id="tobii-dashboard.desktop"
launcher_dir="${HOME}/.local/bin"
launcher_path="${launcher_dir}/tobii-dashboard"
desktop_dir="${HOME}/.local/share/applications"
desktop_path="${desktop_dir}/${desktop_id}"

install_system_packages=1
install_udev=1
install_desktop=1
run_build=1
run_sc_check=1
dry_run=0

usage() {
  cat <<EOF
Usage: ./install.sh [options]

Sets up Tobii Linux runtime dependencies, MediaPipe, udev permissions,
build artifacts, and a desktop launcher named "Tobii Dashboard".

Options:
  --no-system-packages   Do not install distro packages.
  --no-udev              Do not install the Tobii udev rule.
  --no-desktop           Do not create the desktop/menu launcher.
  --no-build             Do not run MediaPipe/model/build setup.
  --no-sc-check          Do not check the Star Citizen stock Tobii DLL.
  --preflight            Dry-run only: print what would be done and exit.
  -h, --help             Show this help.

Environment:
  SUDO=sudo              Command used for privilege escalation.
  MEDIAPIPE_PYTHON=...   Use a specific Python 3.11/3.12 for MediaPipe.
  STAR_CITIZEN_PREFIX=... or SC_BIN64=...
                         Used for Star Citizen detection and stock DLL check.
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
    --no-system-packages) install_system_packages=0 ;;
    --no-udev) install_udev=0 ;;
    --no-desktop) install_desktop=0 ;;
    --no-build) run_build=0 ;;
    --no-sc-check) run_sc_check=0 ;;
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

sudo_cmd="${SUDO:-sudo}"
if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  die "do not run install.sh with sudo; it creates user-owned virtualenv and menu files. It will ask for sudo only when needed."
fi

need_cmd() {
  command -v "$1" >/dev/null 2>&1
}

need_pkg_config() {
  pkg-config --exists "$1" >/dev/null 2>&1
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

install_system_deps() {
  local pm="$1"
  local packages
  packages="$(system_packages_for_pm "$pm")"
  [[ -n "$packages" ]] || die "unsupported package manager. Install dependencies from README.md, then rerun with --no-system-packages."

  log "Installing/checking system packages with $pm"
  case "$pm" in
    apt)
      $sudo_cmd apt-get update
      $sudo_cmd apt-get install -y $packages
      ;;
    dnf)
      $sudo_cmd dnf install -y $packages
      ;;
    pacman)
      $sudo_cmd pacman -Syu --needed --noconfirm $packages
      ;;
  esac
}

package_installed() {
  local pm="$1"
  local package="$2"
  case "$pm" in
    apt) dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q "install ok installed" ;;
    dnf) rpm -q "$package" >/dev/null 2>&1 ;;
    pacman) pacman -Q "$package" >/dev/null 2>&1 ;;
    *) return 1 ;;
  esac
}

missing_system_packages() {
  local pm="$1"
  local package
  for package in $(system_packages_for_pm "$pm"); do
    if ! package_installed "$pm" "$package"; then
      printf '%s\n' "$package"
    fi
  done
}

check_build_deps() {
  local missing=0
  local commands=(make cc pkg-config x86_64-w64-mingw32-gcc wine python3 curl tar sha256sum)
  for cmd in "${commands[@]}"; do
    if ! need_cmd "$cmd"; then
      warn "missing command: $cmd"
      missing=1
    fi
  done
  if ! need_pkg_config libusb-1.0; then
    warn "missing pkg-config dependency: libusb-1.0"
    missing=1
  fi
  if ! need_pkg_config openssl; then
    warn "missing pkg-config dependency: openssl"
    missing=1
  fi
  return "$missing"
}

setup_udev_rule() {
  log "Installing Tobii udev rule"
  $sudo_cmd install -m 0644 configs/udev/99-tobii-eyetracker5.rules /etc/udev/rules.d/99-tobii-eyetracker5.rules
  if need_cmd udevadm; then
    $sudo_cmd udevadm control --reload-rules || true
    $sudo_cmd udevadm trigger || true
  else
    warn "udevadm not found; reboot or reload udev rules manually."
  fi
  if getent group plugdev >/dev/null 2>&1 && ! id -nG "$USER" | tr ' ' '\n' | grep -qx plugdev; then
    warn "user $USER is not in plugdev. Add with: sudo usermod -aG plugdev $USER"
    warn "then log out and back in before using the Tobii device without sudo."
  fi
}

setup_runtime() {
  log "Setting up MediaPipe-compatible Python"
  if [[ -n "${MEDIAPIPE_PYTHON:-}" ]]; then
    echo "using MEDIAPIPE_PYTHON=$MEDIAPIPE_PYTHON"
  elif ! need_cmd python3.12 && ! need_cmd python3.11; then
    make mediapipe-python
  else
    echo "using system Python 3.11/3.12 for MediaPipe"
  fi

  log "Creating MediaPipe virtualenv"
  make mediapipe-venv

  log "Fetching MediaPipe face model"
  make mediapipe-fetch-model

  log "Building runtime helpers"
  make all
}

write_desktop_launcher() {
  log "Creating desktop/menu launcher"
  mkdir -p "$launcher_dir" "$desktop_dir"

cat >"$launcher_path" <<EOF
#!/usr/bin/env bash
set -euo pipefail
root="$root_dir"
log_dir="\${XDG_STATE_HOME:-\$HOME/.local/state}/tobii-linux"
mkdir -p "\$log_dir"
log="\$log_dir/dashboard.log"
cd "\$root"
{
  echo
  echo "---- \$(date -Is) starting Tobii Dashboard from \$root ----"
} >>"\$log"
exec env SC_TOBII_STOCK_DLL_PREFLIGHT=0 ./scripts/app/run-sc-tobii-native-runtime.sh dashboard >>"\$log" 2>&1
EOF
  chmod 0755 "$launcher_path"

  cat >"$desktop_path" <<EOF
[Desktop Entry]
Type=Application
Name=$app_name
Comment=Start the Tobii Linux dashboard and Star Citizen compatibility runtime
Exec=$launcher_path
Path=$root_dir
Terminal=false
Categories=Utility;Game;
StartupNotify=true
Icon=input-gaming
EOF

  if need_cmd update-desktop-database; then
    update-desktop-database "$desktop_dir" >/dev/null 2>&1 || true
  fi

  echo "desktop_entry=$desktop_path"
  echo "launcher=$launcher_path"
  echo "launcher_log=\${XDG_STATE_HOME:-\$HOME/.local/state}/tobii-linux/dashboard.log"
}

find_sc_bin64() {
  sc_find_bin64
}

report_sc_detection() {
  local bin64="$1"
  if [[ -z "$bin64" || ! -d "$bin64" ]]; then
    echo "star_citizen=not-detected"
    return 1
  fi

  local dll="$bin64/tobii_gameintegration_x64.dll"
  echo "star_citizen=detected"
  echo "sc_bin64=$bin64"
  if [[ ! -f "$dll" ]]; then
    echo "stock_tobii_dll=missing"
    return 1
  fi
  local shim="$root_dir/.tmp/sc-tobii-gameintegration/tobii_gameintegration_x64.dll"
  if [[ -f "$shim" ]] && cmp -s "$dll" "$shim"; then
    echo "stock_tobii_dll=replacement-shim-detected"
    return 2
  fi
  echo "stock_tobii_dll=present"
  return 0
}

run_stock_dll_check() {
  local bin64="$1"
  if [[ -z "$bin64" || ! -d "$bin64" ]]; then
    warn "Star Citizen was not detected; skipping stock Tobii DLL safety check."
    warn "Set SC_BIN64=/path/to/StarCitizen/LIVE/Bin64 later if needed."
    return 0
  fi
  log "Checking Star Citizen stock Tobii DLL"
  SC_BIN64="$bin64" make preflight
}

print_preflight_report() {
  local pm="$1"
  local sc_bin64="$2"

  log "Installer preflight report"
  echo "project=$root_dir"
  echo "package_manager=$pm"
  echo

  echo "Planned actions:"
  [[ "$install_system_packages" -eq 1 ]] && echo "  - install missing system packages" || echo "  - skip system packages"
  [[ "$install_udev" -eq 1 ]] && echo "  - install Tobii udev rule" || echo "  - skip Tobii udev rule"
  [[ "$run_build" -eq 1 ]] && echo "  - prepare MediaPipe/model and build helpers" || echo "  - skip MediaPipe/model/build setup"
  [[ "$install_desktop" -eq 1 ]] && echo "  - create application menu entry: $app_name" || echo "  - skip desktop/menu launcher"
  [[ "$run_sc_check" -eq 1 ]] && echo "  - check Star Citizen stock Tobii DLL if detected" || echo "  - skip Star Citizen stock DLL check"
  echo

  if [[ "$install_system_packages" -eq 1 ]]; then
    if [[ "$pm" == "none" ]]; then
      echo "System packages: unsupported package manager"
    else
      mapfile -t missing_pkgs < <(missing_system_packages "$pm")
      if [[ "${#missing_pkgs[@]}" -eq 0 ]]; then
        echo "System packages: all installer-known packages are present"
      else
        echo "System packages that would be installed:"
        printf '  - %s\n' "${missing_pkgs[@]}"
      fi
    fi
    echo
  fi

  echo "Build dependency check:"
  if check_build_deps; then
    echo "  current shell has required build commands/libraries"
  else
    echo "  missing items are listed above; package install would try to fix them"
  fi
  echo

  echo "MediaPipe Python:"
  if [[ -n "${MEDIAPIPE_PYTHON:-}" ]]; then
    echo "  would use MEDIAPIPE_PYTHON=$MEDIAPIPE_PYTHON"
  elif need_cmd python3.12; then
    echo "  would use system python3.12"
  elif need_cmd python3.11; then
    echo "  would use system python3.11"
  else
    echo "  would install project-local Python via uv"
  fi
  echo

  echo "Star Citizen detection:"
  report_sc_detection "$sc_bin64" || true
  echo
  echo "No changes were made."
}

log "Tobii Linux installer"
echo "project=$root_dir"
pm="$(detect_package_manager)"
sc_bin64="$(find_sc_bin64 || true)"

if [[ "$dry_run" -eq 1 ]]; then
  print_preflight_report "$pm" "$sc_bin64"
  exit 0
fi

if [[ "$install_system_packages" -eq 1 ]]; then
  install_system_deps "$pm"
else
  log "Skipping system package install"
fi

log "Checking build dependencies"
if ! check_build_deps; then
  die "missing build dependencies remain. Install them and rerun ./install.sh."
fi

if [[ "$install_udev" -eq 1 ]]; then
  setup_udev_rule
else
  log "Skipping udev rule install"
fi

if [[ "$run_build" -eq 1 ]]; then
  setup_runtime
else
  log "Skipping MediaPipe/model/build setup"
fi

if [[ "$install_desktop" -eq 1 ]]; then
  write_desktop_launcher
else
  log "Skipping desktop/menu launcher"
fi

log "Star Citizen detection"
report_sc_detection "$sc_bin64" || true

if [[ "$run_sc_check" -eq 1 ]]; then
  run_stock_dll_check "$sc_bin64"
else
  log "Skipping Star Citizen stock DLL safety check"
fi

cat <<EOF

Install complete.

Next steps:
  1. Replug the Tobii Eye Tracker 5, or reboot if device permissions do not update.
  2. Open "$app_name" from your application menu, or run:
       make runtime
     If the menu item does not appear to open, check:
       ${XDG_STATE_HOME:-$HOME/.local/state}/tobii-linux/dashboard.log
  3. On first launch, complete screen calibration and gaze calibration.
  4. Start Star Citizen and select Tobii for head tracking.

EOF
