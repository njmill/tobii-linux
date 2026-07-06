#!/usr/bin/env bash
set -euo pipefail

bin64="${SC_BIN64:-}"
if [[ -z "$bin64" ]]; then
  bin64="$(find "$HOME/Games" -path '*/StarCitizen/*/Bin64' -type d 2>/dev/null | sort | tail -1 || true)"
fi

if [[ -z "$bin64" || ! -d "$bin64" ]]; then
  echo "error: Star Citizen Bin64 directory not found" >&2
  echo "set SC_BIN64='/path/to/StarCitizen/LIVE/Bin64' and retry" >&2
  exit 1
fi

target="$bin64/tobii_gameintegration_x64.dll"
backup="$bin64/tobii_gameintegration_x64.dll.original"

if [[ ! -f "$backup" ]]; then
  echo "error: backup not found at $backup" >&2
  exit 1
fi

cp -av "$backup" "$target"
echo "restored=$target"
