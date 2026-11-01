#!/usr/bin/env bash
set -euo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
work_dir="${SC_TOBII_NATIVE_RUNTIME_DIR:-$root_dir/.tmp/sc-tobii-native-runtime}"
# Check current process state, rather than stale listening lines from a previous run.
python3 - "$work_dir" "${SC_TOBII_READY_TIMEOUT:-15}" <<'PYREADY'
import json
import os
from pathlib import Path
import socket
import sys
import time

work = Path(sys.argv[1])
deadline = time.monotonic() + float(sys.argv[2])
while time.monotonic() <= deadline:
    try:
        state = json.loads((work / 'wine-bridges.json').read_text())
        os.kill(state['pid'], 0)
        ready = time.time() - state['updated'] < 2 and bool(state['prefixes'])
        for prefix in state['prefixes']:
            ready = ready and prefix['ready']
            for pid in prefix['processes'].values():
                os.kill(pid, 0)
        if ready:
            with socket.create_connection(('127.0.0.1', int(os.environ.get('SC_TOBII_MIDDLEWARE_PORT', '4455'))), timeout=.2):
                pass
            print('native Tobii runtime ready')
            raise SystemExit(0)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    time.sleep(.1)
raise SystemExit('Wine bridges not ready; inspect wine-bridges.stdout and wine-bridges.json')
PYREADY
