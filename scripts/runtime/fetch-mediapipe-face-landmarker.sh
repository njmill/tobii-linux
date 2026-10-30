#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."

url="${MEDIAPIPE_FACE_MODEL_URL:-https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task}"
out="assets/mediapipe/face_landmarker.task"
checksum_file="assets/mediapipe/face_landmarker.task.sha256"

mkdir -p "$(dirname "$out")"
tmp="${out}.tmp"
trap 'rm -f "$tmp"' EXIT

if [[ ! -f "$checksum_file" ]]; then
  echo "error: missing committed checksum: $checksum_file" >&2
  exit 1
fi

if command -v curl >/dev/null 2>&1; then
  curl -L --fail --progress-bar "$url" -o "$tmp"
elif command -v wget >/dev/null 2>&1; then
  wget -O "$tmp" "$url"
else
  echo "error: need curl or wget to fetch $url" >&2
  exit 1
fi

expected="$(awk '{print $1}' "$checksum_file")"
actual="$(sha256sum "$tmp" | awk '{print $1}')"
if [[ "$actual" != "$expected" ]]; then
  echo "error: checksum mismatch for $url" >&2
  echo "expected=$expected" >&2
  echo "actual=$actual" >&2
  exit 1
fi

mv "$tmp" "$out"
trap - EXIT
echo "$actual  $out"
echo "wrote $out"
