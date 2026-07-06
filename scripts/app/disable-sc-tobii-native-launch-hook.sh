#!/usr/bin/env bash
set -euo pipefail

prefix="${STAR_CITIZEN_PREFIX:-$HOME/Games/star-citizen}"
launch_script="${STAR_CITIZEN_LAUNCH_SCRIPT:-$prefix/sc-launch.sh}"
tobii_begin="# tobii-linux-native-tobii-hook begin"
tobii_end="# tobii-linux-native-tobii-hook end"

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

pkill -f "run-sc-tobii-native-runtime.sh services" 2>/dev/null || true
pkill -f "tobii-middleware-spy.py.*--port 4455" 2>/dev/null || true
pkill -f "tobii-middleware-pipe-spy.exe" 2>/dev/null || true

echo "disabled native Tobii launch hook"
echo "launch_script=$launch_script"
echo "backup=$backup"
