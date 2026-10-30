#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$root_dir/scripts/app/sc-bin64.sh"
bin64="$(sc_require_bin64)"

target="$bin64/tobii_gameintegration_x64.dll"
backup="$bin64/tobii_gameintegration_x64.dll.original"
shim="$root_dir/.tmp/sc-tobii-gameintegration/tobii_gameintegration_x64.dll"

if [[ ! -f "$target" ]]; then
  echo "error: Tobii game integration DLL not found: $target" >&2
  exit 1
fi

if [[ -f "$shim" ]] && cmp -s "$target" "$shim"; then
  if [[ ! -f "$backup" ]]; then
    echo "error: installed Tobii DLL matches local replacement shim, but backup is missing: $backup" >&2
    echo "restore the stock game DLL through the launcher before starting stock runtime" >&2
    exit 1
  fi
  echo "detected replacement Tobii DLL; restoring stock DLL first"
  SC_BIN64="$bin64" "$root_dir/scripts/app/restore-sc-tobii-dll.sh" >/dev/null
fi

if [[ -f "$shim" ]] && cmp -s "$target" "$shim"; then
  echo "error: Tobii DLL still matches local replacement shim after restore attempt" >&2
  exit 1
fi

echo "stock_dll=ok path=$target"
