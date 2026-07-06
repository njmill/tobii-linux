#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."

uv_dir="${MEDIAPIPE_UV_DIR:-.tmp/uv}"
uv_bin="$uv_dir/uv"
python_dir="${MEDIAPIPE_PYTHON_DIR:-.tmp/uv-python}"
python_version="${MEDIAPIPE_PYTHON_VERSION:-3.12}"

mkdir -p "$uv_dir" "$python_dir"

if [[ ! -x "$uv_bin" ]]; then
  tmpdir="$(mktemp -d)"
  trap 'rm -rf "$tmpdir"' EXIT

  arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64) uv_arch="x86_64-unknown-linux-gnu" ;;
    aarch64|arm64) uv_arch="aarch64-unknown-linux-gnu" ;;
    *)
      echo "error: unsupported architecture for uv bootstrap: $arch" >&2
      exit 1
      ;;
  esac

  url="https://github.com/astral-sh/uv/releases/latest/download/uv-${uv_arch}.tar.gz"
  echo "downloading uv: $url"
  curl -L --fail "$url" -o "$tmpdir/uv.tar.gz"
  tar -xzf "$tmpdir/uv.tar.gz" -C "$tmpdir"
  install -m 755 "$tmpdir/uv-${uv_arch}/uv" "$uv_bin"
fi

echo "using uv: $("$uv_bin" --version)"
"$uv_bin" python install "$python_version"

managed_python="$("$uv_bin" python find "$python_version")"
echo "managed_python=$managed_python"

rm -rf "$python_dir"
"$uv_bin" venv --python "$managed_python" "$python_dir"
"$python_dir/bin/python" --version

echo
echo "MediaPipe-compatible Python ready:"
echo "  $python_dir/bin/python"
echo
echo "Next:"
echo "  MEDIAPIPE_PYTHON=$python_dir/bin/python make mediapipe-venv"
echo
echo "The default setup script also auto-detects:"
echo "  make mediapipe-venv"
