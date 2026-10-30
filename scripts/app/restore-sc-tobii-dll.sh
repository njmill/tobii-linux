#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$root_dir/scripts/app/sc-bin64.sh"
bin64="$(sc_require_bin64)"

target="$bin64/tobii_gameintegration_x64.dll"
backup="$bin64/tobii_gameintegration_x64.dll.original"

if [[ ! -f "$backup" ]]; then
  echo "error: backup not found at $backup" >&2
  exit 1
fi

cp -av "$backup" "$target"
echo "restored=$target"
