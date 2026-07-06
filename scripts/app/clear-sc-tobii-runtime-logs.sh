#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
work_dir="${SC_TOBII_NATIVE_RUNTIME_DIR:-$root_dir/.tmp/sc-tobii-native-runtime}"

mkdir -p "$work_dir"
shopt -s nullglob
for log in "$work_dir"/*.log; do
  : >"$log"
  echo "cleared $log"
done

if ! compgen -G "$work_dir/*.log" >/dev/null; then
  echo "no runtime logs existed under $work_dir"
fi
