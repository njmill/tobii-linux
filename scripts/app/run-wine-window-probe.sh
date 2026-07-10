#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$root_dir/scripts/app/sc-bin64.sh"
sc_load_config
work_dir="${SC_TOBII_NATIVE_RUNTIME_DIR:-$root_dir/.tmp/sc-tobii-native-runtime}"
stock_dir="$root_dir/.tmp/sc-tobii-stock-recon"
probe="$stock_dir/wine-window-probe.exe"
if [[ -z "${STAR_CITIZEN_PREFIX:-}" && -n "${SC_BIN64:-}" ]]; then
  STAR_CITIZEN_PREFIX="$(sc_prefix_from_bin64 "$SC_BIN64" || true)"
fi
prefix="${STAR_CITIZEN_PREFIX:-$HOME/Games/star-citizen}"
wine_bin="${STAR_CITIZEN_WINE:-}"
log="${SC_TOBII_WINDOW_PROBE_LOG:-$work_dir/wine-window-probe.log}"

if [[ -z "$wine_bin" && -f "$prefix/sc-launch.sh" ]]; then
  wine_path="$(
    awk -F= '/^[[:space:]]*export[[:space:]]+wine_path=/{print $2}' "$prefix/sc-launch.sh" \
      | tail -1 \
      | sed -e 's/^"//' -e 's/"$//'
  )"
  if [[ -n "$wine_path" && -x "$wine_path/wine" ]]; then
    wine_bin="$wine_path/wine"
  fi
fi
wine_bin="${wine_bin:-wine}"

if [[ ! -f "$probe" ]]; then
  echo "error: missing $probe" >&2
  echo "run: make wine-window-probe-build" >&2
  exit 1
fi

mkdir -p "$work_dir"
(
  cd "$stock_dir"
  env WINEPREFIX="$prefix" WINEDEBUG="${SC_TOBII_WINDOW_PROBE_WINEDEBUG:--all}" \
    "$wine_bin" wine-window-probe.exe
) >"$log" 2>&1

echo "log=$log"
python3 - "$log" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
lines = path.read_text(errors="replace").splitlines()
window_lines = [line for line in lines if line.startswith("wine_window ")]
interesting = []
for line in window_lines:
    lower = line.lower()
    if any(token in lower for token in ("star", "citizen", "rsi", "launcher", "wine", "explorer")):
        interesting.append(line)
print(f"windows={len(window_lines)} interesting={len(interesting)}")
for line in interesting[:40]:
    print(line)
if len(interesting) > 40:
    print(f"... {len(interesting) - 40} more interesting windows omitted")
PY
