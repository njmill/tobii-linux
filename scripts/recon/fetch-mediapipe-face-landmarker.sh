#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."

url="${MEDIAPIPE_FACE_MODEL_URL:-https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task}"
out="assets/mediapipe/face_landmarker.task"

mkdir -p "$(dirname "$out")"
tmp="${out}.tmp"

if command -v curl >/dev/null 2>&1; then
  curl -L --fail --progress-bar "$url" -o "$tmp"
elif command -v wget >/dev/null 2>&1; then
  wget -O "$tmp" "$url"
else
  echo "error: need curl or wget to fetch $url" >&2
  exit 1
fi

mv "$tmp" "$out"
sha256sum "$out" | tee assets/mediapipe/face_landmarker.task.sha256
echo "wrote $out"
