#!/usr/bin/env bash
set -euo pipefail

tobii_begin="# tobii-linux-native-tobii-hook begin"
tobii_end="# tobii-linux-native-tobii-hook end"
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$root_dir/scripts/app/sc-bin64.sh"
sc_load_config
if [[ -z "${STAR_CITIZEN_PREFIX:-}" && -n "${SC_BIN64:-}" ]]; then
  STAR_CITIZEN_PREFIX="$(sc_prefix_from_bin64 "$SC_BIN64" || true)"
fi
prefix="${STAR_CITIZEN_PREFIX:-$HOME/Games/star-citizen}"
launch_script="${STAR_CITIZEN_LAUNCH_SCRIPT:-$prefix/sc-launch.sh}"

if [[ ! -f "$launch_script" ]]; then
  echo "error: launch script not found: $launch_script" >&2
  exit 1
fi

backup="$launch_script.bak-disable-tobii-native-$(date +%Y%m%d-%H%M%S)"
cp -f "$launch_script" "$backup"

tmp="$(mktemp)"
awk -v begin="$tobii_begin" -v end="$tobii_end" '
  BEGIN { skip = 0 }
  $0 == begin { skip = 1; changed = 1; next }
  $0 == end { skip = 0; next }
  !skip { print }
  END {
    if (!changed) {
      print "note: native Tobii launch hook was not present" > "/dev/stderr"
    }
  }
' "$launch_script" >"$tmp"

mv "$tmp" "$launch_script"
chmod +x "$launch_script"

pid_dir="$root_dir/.tmp/sc-tobii-native-runtime/pids"
if [[ -d "$pid_dir" ]]; then
  for name in dashboard mux middleware pipe etdefaultpipe tobii-prefixed-pipe tobiiprp-prefixed-pipe runtime-services; do
    pid_file="$pid_dir/$name.pid"
    [[ -f "$pid_file" ]] || continue
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    rm -f "$pid_file"
    [[ -n "$pid" ]] || continue
    kill "$pid" 2>/dev/null || true
  done
fi

echo "disabled native Tobii launch hook"
echo "launch_script=$launch_script"
echo "backup=$backup"
