#!/usr/bin/env bash

sc_find_bin64() {
  if [[ -n "${SC_BIN64:-}" ]]; then
    if [[ -d "$SC_BIN64" ]]; then
      printf '%s\n' "$SC_BIN64"
      return 0
    fi
    echo "warning: SC_BIN64 is set but is not a directory: $SC_BIN64" >&2
    return 1
  fi

  local prefix="${STAR_CITIZEN_PREFIX:-}"
  if [[ -n "$prefix" ]]; then
    local live="$prefix/drive_c/Program Files/Roberts Space Industries/StarCitizen/LIVE/Bin64"
    if [[ -d "$live" ]]; then
      printf '%s\n' "$live"
      return 0
    fi

    local found=""
    found="$(find "$prefix" -path '*/StarCitizen/LIVE/Bin64' -type d 2>/dev/null | sort | head -1 || true)"
    if [[ -n "$found" ]]; then
      printf '%s\n' "$found"
      return 0
    fi

    found="$(find "$prefix" -path '*/StarCitizen/*/Bin64' -type d 2>/dev/null | sort | head -1 || true)"
    if [[ -n "$found" ]]; then
      echo "warning: using non-LIVE Star Citizen channel: $found" >&2
      printf '%s\n' "$found"
      return 0
    fi
    return 1
  fi

  local default_prefix="$HOME/Games/star-citizen"
  local default_live="$default_prefix/drive_c/Program Files/Roberts Space Industries/StarCitizen/LIVE/Bin64"
  if [[ -d "$default_live" ]]; then
    printf '%s\n' "$default_live"
    return 0
  fi

  local live_found=""
  live_found="$(find "$HOME/Games" -path '*/StarCitizen/LIVE/Bin64' -type d 2>/dev/null | sort | head -1 || true)"
  if [[ -n "$live_found" ]]; then
    printf '%s\n' "$live_found"
    return 0
  fi

  local any_found=""
  any_found="$(find "$HOME/Games" -path '*/StarCitizen/*/Bin64' -type d 2>/dev/null | sort | head -1 || true)"
  if [[ -n "$any_found" ]]; then
    echo "warning: using non-LIVE Star Citizen channel: $any_found" >&2
    printf '%s\n' "$any_found"
    return 0
  fi

  return 1
}

sc_require_bin64() {
  local bin64=""
  if ! bin64="$(sc_find_bin64)"; then
    echo "error: Star Citizen Bin64 directory not found" >&2
    echo "set SC_BIN64='/path/to/StarCitizen/LIVE/Bin64' and retry" >&2
    return 1
  fi
  printf '%s\n' "$bin64"
}
