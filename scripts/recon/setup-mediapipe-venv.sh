#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."

venv="${MEDIAPIPE_VENV:-.venv/mediapipe}"
py="${MEDIAPIPE_PYTHON:-}"

if [[ "${EUID:-$(id -u)}" -eq 0 && -n "${SUDO_USER:-}" ]]; then
  cat >&2 <<EOF
error: do not run this target with sudo.

It creates a project-local Python virtualenv that should be owned by your user.
Fix any root-owned partial venv, then rerun without sudo:

  sudo rm -rf "$venv"
  make mediapipe-venv
EOF
  exit 1
fi

if [[ -z "$py" ]]; then
  if [[ -x ".tmp/uv-python/bin/python3.12" ]]; then
    py=".tmp/uv-python/bin/python3.12"
  elif [[ -x ".tmp/uv-python/bin/python" ]]; then
    py=".tmp/uv-python/bin/python"
  fi
fi

if [[ -z "$py" ]]; then
  for candidate in python3.12 python3.11; do
    if command -v "$candidate" >/dev/null 2>&1; then
      py="$candidate"
      break
    fi
  done
fi

if [[ -z "$py" ]]; then
  cat >&2 <<'EOF'
error: no compatible Python found for MediaPipe.

MediaPipe wheels are not expected to work on the system Python 3.13 here.
Install Python 3.11 or 3.12, or rerun with:

  MEDIAPIPE_PYTHON=/path/to/python3.11 make mediapipe-venv

For a project-local Python that avoids changing the system Python, run:

  make mediapipe-python
  make mediapipe-venv
EOF
  exit 1
fi

"$py" -m venv "$venv"
"$venv/bin/python" -m pip install --upgrade pip
"$venv/bin/python" -m pip install -r requirements-mediapipe.txt
"$venv/bin/python" scripts/recon/mediapipe-face-worker.py --self-test

echo "mediapipe venv ready: $venv"
