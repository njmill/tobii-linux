#!/usr/bin/env bash
set -u

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$root_dir/scripts/app/sc-bin64.sh"
sc_load_config

udev_rule="/etc/udev/rules.d/99-tobii-eyetracker5.rules"
model_file="$root_dir/assets/mediapipe/face_landmarker.task"
venv_ready="$root_dir/.venv/mediapipe/.ready"
dashboard_log="${XDG_STATE_HOME:-$HOME/.local/state}/tobii-linux/dashboard.log"
diag_tmp="${TMPDIR:-/tmp}/tobii-linux-diag"

ok() {
  printf '[ok] %s: %s\n' "$1" "$2"
}

warn() {
  printf '[!!] %s: %s\n' "$1" "$2"
}

info() {
  printf '[..] %s: %s\n' "$1" "$2"
}

resolve_wine_bin() {
  local prefix="$1"
  local wine_bin="${STAR_CITIZEN_WINE:-}"
  if [[ -z "$wine_bin" && -f "$prefix/sc-launch.sh" ]]; then
    local wine_path
    wine_path="$(
      awk -F= '/^[[:space:]]*export[[:space:]]+wine_path=/{print $2}' "$prefix/sc-launch.sh" \
        | tail -1 \
        | sed -e 's/^"//' -e 's/"$//'
    )"
    if [[ -n "$wine_path" && -x "$wine_path/wine" ]]; then
      wine_bin="$wine_path/wine"
    fi
  fi
  printf '%s\n' "${wine_bin:-wine}"
}

hook_state() {
  local launch_script="$1"
  if [[ ! -f "$launch_script" ]]; then
    printf 'missing-launch-script'
  elif grep -q '# tobii-linux-native-tobii-hook begin' "$launch_script"; then
    printf 'installed'
  elif grep -q 'wineserver[[:space:]]+-k' "$launch_script"; then
    printf 'missing-wineserver-k'
  else
    printf 'missing-no-wineserver-k'
  fi
}

echo "Tobii Linux setup diagnostics"
echo "project=$root_dir"
echo

if command -v lsusb >/dev/null 2>&1; then
  if lsusb -d 2104:0313 >/dev/null 2>&1; then
    ok "tobii usb" "$(lsusb -d 2104:0313 | head -1)"
  else
    warn "tobii usb" "not visible in lsusb; check cable/port and replug the ET5"
  fi
else
  warn "tobii usb" "lsusb not installed; cannot check USB visibility"
fi

if [[ -f "$udev_rule" ]]; then
  if grep -q 'plugdev' "$udev_rule"; then
    warn "udev rule" "$udev_rule still references plugdev; reinstall with ./install.sh or make install-udev-rule"
  elif grep -q '2104' "$udev_rule" && grep -q '0313' "$udev_rule"; then
    ok "udev rule" "$udev_rule installed without plugdev dependency"
  else
    warn "udev rule" "$udev_rule exists but does not look like the Tobii ET5 rule"
  fi
else
  warn "udev rule" "not installed; run make install-udev-rule or ./install.sh"
fi

if [[ -x "$root_dir/build/tobii-ttp-mux" ]]; then
  mkdir -p "$diag_tmp"
  mux_log="$diag_tmp/mux-open.log"
  if timeout 4 "$root_dir/build/tobii-ttp-mux" \
      --label diag \
      --seconds 0.5 \
      --samples 1 \
      --csv "$diag_tmp/gaze.csv" \
      --events-csv "$diag_tmp/events.csv" \
      --out "$diag_tmp/frames" \
      --image-write-hz 99 \
      --image-ring-size 1 \
      >"$mux_log" 2>&1; then
    ok "libusb open" "Tobii opened through native mux"
  else
    if grep -q 'device_not_found' "$mux_log"; then
      warn "libusb open" "device_not_found; udev permissions may not be active yet, or another service owns the device"
    elif grep -qi 'access' "$mux_log" || grep -qi 'permission' "$mux_log"; then
      warn "libusb open" "permission denied; replug the Tobii or reboot after installing the udev rule"
    else
      warn "libusb open" "native mux failed; see $mux_log"
    fi
  fi
else
  warn "libusb open" "build/tobii-ttp-mux is missing; run make all"
fi

if [[ -f "$venv_ready" ]]; then
  ok "mediapipe venv" "$venv_ready"
else
  warn "mediapipe venv" "missing; run make mediapipe-venv"
fi

if [[ -f "$model_file" ]]; then
  ok "mediapipe model" "$model_file"
else
  warn "mediapipe model" "missing; run make mediapipe-fetch-model"
fi

sc_bin64="$(sc_find_bin64 2>/dev/null || true)"
if [[ -n "$sc_bin64" && -d "$sc_bin64" ]]; then
  ok "star citizen" "Bin64 detected at $sc_bin64"
  dll="$sc_bin64/tobii_gameintegration_x64.dll"
  if [[ -f "$dll" ]]; then
    ok "stock tobii dll" "$dll"
  else
    warn "stock tobii dll" "missing at $dll"
  fi

  prefix="${STAR_CITIZEN_PREFIX:-}"
  if [[ -z "$prefix" ]]; then
    prefix="$(sc_prefix_from_bin64 "$sc_bin64" || true)"
  fi
  if [[ -n "$prefix" ]]; then
    ok "wine prefix" "$prefix"
  else
    warn "wine prefix" "could not derive from SC_BIN64; set STAR_CITIZEN_PREFIX"
  fi

  wine_bin="$(resolve_wine_bin "$prefix")"
  if command -v "$wine_bin" >/dev/null 2>&1 || [[ -x "$wine_bin" ]]; then
    ok "wine runner" "$wine_bin"
  else
    warn "wine runner" "$wine_bin not found/executable; set STAR_CITIZEN_WINE to the runner used by SC"
  fi

  launch_script="${STAR_CITIZEN_LAUNCH_SCRIPT:-$prefix/sc-launch.sh}"
  state="$(hook_state "$launch_script")"
  case "$state" in
    installed)
      ok "launch hook" "installed in $launch_script"
      ;;
    missing-wineserver-k)
      warn "launch hook" "missing; run make install-launch-hook so services survive SC's wineserver -k"
      ;;
    missing-launch-script)
      warn "launch hook" "launch script not found at $launch_script"
      ;;
    *)
      warn "launch hook" "missing and no wineserver -k insertion point found in $launch_script"
      ;;
  esac
else
  warn "star citizen" "Bin64 not detected; set SC_BIN64=\"/path/to/StarCitizen/LIVE/Bin64\""
  info "path quoting" "quote paths with spaces; do not backslash spaces inside quotes"
fi

if [[ -f "$dashboard_log" ]]; then
  ok "dashboard log" "$dashboard_log"
else
  info "dashboard log" "$dashboard_log will be created when launching from the menu"
fi

echo
echo "Next commands:"
echo "  Open dashboard: make runtime"
echo "  Runtime status after SC starts: make status"
echo "  Reinstall launch hook if needed: make install-launch-hook"

exit 0
