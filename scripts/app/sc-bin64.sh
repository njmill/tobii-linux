#!/usr/bin/env bash

sc_config_file() {
  printf '%s\n' "${XDG_CONFIG_HOME:-$HOME/.config}/tobii-linux/runtime.env"
}

sc_shell_quote() {
  local value="$1"
  printf "'%s'" "${value//\'/\'\\\'\'}"
}

sc_load_config() {
  local config_file
  config_file="$(sc_config_file)"
  [[ -f "$config_file" ]] || return 0

  local explicit_sc_bin64="${SC_BIN64-}"
  local explicit_prefix="${STAR_CITIZEN_PREFIX-}"
  local explicit_wine="${STAR_CITIZEN_WINE-}"

  # shellcheck disable=SC1090
  source "$config_file"

  [[ -n "$explicit_sc_bin64" ]] && SC_BIN64="$explicit_sc_bin64"
  [[ -n "$explicit_prefix" ]] && STAR_CITIZEN_PREFIX="$explicit_prefix"
  [[ -n "$explicit_wine" ]] && STAR_CITIZEN_WINE="$explicit_wine"
}

sc_prefix_from_bin64() {
  local bin64="$1"
  local marker="/drive_c/"
  if [[ "$bin64" == *"$marker"* ]]; then
    printf '%s\n' "${bin64%%$marker*}"
    return 0
  fi
  return 1
}

sc_find_in_root() {
  local root="$1"
  local found=""
  [[ -d "$root" ]] || return 1

  found="$(find "$root" -maxdepth 12 -path '*/StarCitizen/LIVE/Bin64' -type d 2>/dev/null | sort | head -1 || true)"
  if [[ -n "$found" ]]; then
    printf '%s\n' "$found"
    return 0
  fi

  found="$(find "$root" -maxdepth 12 -path '*/StarCitizen/*/Bin64' -type d 2>/dev/null | sort | head -1 || true)"
  if [[ -n "$found" ]]; then
    echo "warning: using non-LIVE Star Citizen channel: $found" >&2
    printf '%s\n' "$found"
    return 0
  fi

  return 1
}

sc_find_bin64() {
  local explicit_sc_bin64=0
  [[ -n "${SC_BIN64:-}" ]] && explicit_sc_bin64=1
  sc_load_config

  if [[ -n "${SC_BIN64:-}" ]]; then
    if [[ -d "$SC_BIN64" ]]; then
      printf '%s\n' "$SC_BIN64"
      return 0
    fi
    echo "warning: SC_BIN64 is set but is not a directory: $SC_BIN64" >&2
    if [[ "$explicit_sc_bin64" -eq 1 ]]; then
      return 1
    fi
    unset SC_BIN64
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

  local roots=(
    "$HOME/Games/star-citizen"
    "$HOME/Games"
    "$HOME/.local/share/lutris"
    "$HOME/.var/app/net.lutris.Lutris/data/lutris"
    "$HOME/.local/share/bottles"
    "$HOME/.var/app/com.usebottles.bottles/data/bottles"
    "$HOME/.local/share/Steam/steamapps/compatdata"
    "$HOME/.steam/steam/steamapps/compatdata"
    "$HOME/.local/share/heroic"
    "$HOME/.var/app/com.heroicgameslauncher.hgl/config/heroic"
  )

  local root
  for root in "${roots[@]}"; do
    sc_find_in_root "$root" && return 0
  done

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

sc_write_runtime_config() {
  local bin64="$1"
  local prefix="${2:-}"
  local wine="${3:-}"
  local config_file
  config_file="$(sc_config_file)"
  mkdir -p "$(dirname "$config_file")"

  if [[ -z "$prefix" ]]; then
    prefix="$(sc_prefix_from_bin64 "$bin64" || true)"
  fi

  {
    echo "# Written by tobii-linux install.sh"
    echo "# Edit this file or override these variables in your shell if Star Citizen moves."
    echo "export SC_BIN64=$(sc_shell_quote "$bin64")"
    if [[ -n "$prefix" ]]; then
      echo "export STAR_CITIZEN_PREFIX=$(sc_shell_quote "$prefix")"
    fi
    if [[ -n "$wine" ]]; then
      echo "export STAR_CITIZEN_WINE=$(sc_shell_quote "$wine")"
    fi
  } >"$config_file"
  chmod 0600 "$config_file"
  printf '%s\n' "$config_file"
}
