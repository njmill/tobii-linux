#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$root_dir/scripts/app/sc-bin64.sh"
sc_load_config
if [[ -z "${STAR_CITIZEN_PREFIX:-}" && -n "${SC_BIN64:-}" ]]; then
  STAR_CITIZEN_PREFIX="$(sc_prefix_from_bin64 "$SC_BIN64" || true)"
fi
prefix="${STAR_CITIZEN_PREFIX:-$HOME/Games/star-citizen}"
launch_script="${STAR_CITIZEN_LAUNCH_SCRIPT:-$prefix/sc-launch.sh}"
log_dir="$root_dir/.tmp/sc-tobii-native-runtime"
hook_log="$log_dir/launch-hook.log"
capability_mode="${SC_TOBII_TTP_CAPABILITY_MODE:-headpose}"
unsolicited_presence="${SC_TOBII_TTP_UNSOLICITED_PRESENCE:-0}"
tobii_begin="# tobii-linux-native-tobii-hook begin"
tobii_end="# tobii-linux-native-tobii-hook end"
trackir_begin="# tobii-linux-trackir-hook begin"
trackir_end="# tobii-linux-trackir-hook end"

if [[ ! -f "$launch_script" ]]; then
  echo "error: launch script not found: $launch_script" >&2
  exit 1
fi

mkdir -p "$log_dir"
backup="$launch_script.bak-tobii-native-$(date +%Y%m%d-%H%M%S)"
cp -f "$launch_script" "$backup"

tmp="$(mktemp)"
awk -v root="$root_dir" \
    -v hook_log="$hook_log" \
    -v hook_log_q="$(sc_shell_quote "$hook_log")" \
    -v hook_dir_q="$(sc_shell_quote "$log_dir")" \
    -v service_command="$(sc_shell_quote "$root_dir/scripts/app/manage-tracking-service.py")" \
    -v ready_command="$(sc_shell_quote "$root_dir/scripts/app/wait-sc-tobii-runtime-ready.sh")" \
    -v capability_mode="$capability_mode" \
    -v unsolicited_presence="$unsolicited_presence" \
    -v tobii_begin="$tobii_begin" \
    -v tobii_end="$tobii_end" \
    -v trackir_begin="$trackir_begin" \
    -v trackir_end="$trackir_end" '
  BEGIN { skip = 0; inserted = 0 }
  $0 == tobii_begin { skip = 1; next }
  $0 == tobii_end { skip = 0; next }
  $0 == trackir_begin { skip = 1; next }
  $0 == trackir_end { skip = 0; next }
  skip { next }
  {
    print
    if (!inserted && $0 ~ /wineserver[[:space:]]+-k/) {
      print ""
      print tobii_begin
      print "mkdir -p " hook_dir_q
      print "# client-driven background service v1"
      print service_command " restart >>" hook_log_q " 2>&1 || true"
      print "SC_TOBII_READY_TIMEOUT=\"${SC_TOBII_READY_TIMEOUT:-15}\" " ready_command " >>" hook_log_q " 2>&1 || true"
      print tobii_end
      inserted = 1
    }
  }
  END {
    if (!inserted) {
      print "error: could not find wineserver -k insertion point" > "/dev/stderr"
      exit 2
    }
  }
' "$launch_script" >"$tmp"

mv "$tmp" "$launch_script"
chmod +x "$launch_script"

echo "installed native Tobii launch hook"
echo "launch_script=$launch_script"
echo "backup=$backup"
echo "hook_log=$hook_log"
echo "capability_mode=$capability_mode"
echo "unsolicited_presence=$unsolicited_presence"
echo "status command: make sc-tobii-stock-runtime-status"
