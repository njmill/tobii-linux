#!/usr/bin/env bash
set -euo pipefail

tobii_begin="# tobii-linux-native-tobii-hook begin"
tobii_end="# tobii-linux-native-tobii-hook end"
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$root_dir/scripts/app/sc-bin64.sh"
sc_load_config
if [[ -z "${STAR_CITIZEN_PREFIX:-}" && -n "${SC_BIN64:-}" ]]; then
  STAR_CITIZEN_PREFIX="$(sc_prefix_from_bin64 "$SC_BIN64" || true)"
fi
prefix="${STAR_CITIZEN_PREFIX:-$HOME/Games/star-citizen}"
launch_script="${STAR_CITIZEN_LAUNCH_SCRIPT:-$prefix/sc-launch.sh}"

# Upgrade migration only: remove our marked block, preserving the launcher and service.
python3 - "$launch_script" <<'PYREMOVE'
from datetime import datetime
import os
from pathlib import Path
import shutil
import tempfile
import sys

path = Path(sys.argv[1]).resolve()
if not path.is_file():
    print(f'No legacy launch hook to remove: {path}')
    raise SystemExit(0)
original = path.read_bytes()
lines = original.splitlines(keepends=True)
begin = b'# tobii-linux-native-tobii-hook begin'
end = b'# tobii-linux-native-tobii-hook end'
kept = []
inside = False
found = False
for line in lines:
    marker = line.strip()
    if marker == begin:
        if inside:
            raise SystemExit('Malformed nested launch hook; launcher left unchanged')
        inside = found = True
    elif marker == end:
        if not inside:
            raise SystemExit('Unmatched launch hook end; launcher left unchanged')
        inside = False
    elif not inside:
        kept.append(line)
if inside:
    raise SystemExit('Unterminated launch hook; launcher left unchanged')
if not found:
    print(f'No legacy launch hook present: {path}')
    raise SystemExit(0)
backup = path.with_name(path.name + '.bak-remove-tobii-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
shutil.copy2(path, backup)
fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
try:
    with os.fdopen(fd, 'wb') as output:
        output.write(b''.join(kept))
        os.fchmod(output.fileno(), path.stat().st_mode & 0o7777)
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
print(f'Removed legacy Tobii launch hook: {path}')
print(f'Backup: {backup}')
PYREMOVE
