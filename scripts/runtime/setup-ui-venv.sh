#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
python="${MEDIAPIPE_VENV:-.venv/mediapipe}/bin/python"
venv=".venv/ui"
"$python" -m venv "$venv"
"$venv/bin/python" -m pip install --upgrade pip
"$venv/bin/python" -m pip install -r requirements-ui.txt
"$venv/bin/python" - <<'PY'
import tkinter
from PySide6.QtWidgets import QApplication, QSystemTrayIcon
print('Desktop environment ready; Tcl', tkinter.Tcl().eval('info patchlevel'))
PY
