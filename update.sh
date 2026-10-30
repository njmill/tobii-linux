#!/usr/bin/env bash
set -euo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root_dir"
preflight=0
for option in "$@"; do
  case "$option" in
    --preflight) preflight=1 ;;
    --no-system-packages|--no-udev|--no-desktop|--no-service|--no-build|--no-sc-check|--no-launch-hook) ;;
    -h|--help)
      cat <<'EOF'
Usage: ./update.sh [installer options]

Run after replacing this folder's source files with a new release or checkout.
Stops the service, backs up saved runtime settings under .tmp/backups/, and
reruns install.sh. Existing settings and calibration files are preserved.

--preflight prints the install plan without stopping the service or making backups.
All other install.sh options are accepted; see ./install.sh --help.
EOF
      exit 0 ;;
    *) echo "unknown option: $option (see ./update.sh --help)" >&2; exit 2 ;;
  esac
done
if [[ "$preflight" -eq 1 ]]; then
  exec ./install.sh "$@"
fi
if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  echo "error: run ./update.sh as your normal user; the installer requests sudo when needed." >&2
  exit 1
fi
source "$root_dir/scripts/app/sc-bin64.sh"
sc_load_config
./scripts/app/manage-tracking-service.py stop
python3 - <<'PY'
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile

root = Path.cwd()
work = Path(os.environ.get('SC_TOBII_NATIVE_RUNTIME_DIR', root/'.tmp/sc-tobii-native-runtime'))
settings = {
    'sc-tuning.json': 'SC_TUNING_FILE', 'sc-window.json': 'SC_WINDOW_STATE_FILE',
    'screen-calibration.json': 'SC_SCREEN_CALIBRATION_FILE',
    'gaze-calibration.json': 'SC_GAZE_CALIBRATION_FILE',
    'head-position.json': 'SC_HEAD_POSITION_PROFILE',
    'native-head-position.json': 'SC_NATIVE_HEAD_POSITION_PROFILE',
}
sources = {'runtime/'+name: Path(os.environ.get(variable, work/name)) for name, variable in settings.items()}
sources['config/runtime.env'] = Path(os.environ.get('XDG_CONFIG_HOME', Path.home()/'.config'))/'tobii-linux/runtime.env'
contents = {name: (path, path.read_bytes()) for name, path in sources.items() if path.is_file()}
if contents:
    folder = root/'.tmp/backups'
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    backup = folder/f'runtime-state-{stamp}.tar.gz'
    metadata = json.dumps({name: dict(source=str(path.resolve()), sha256=hashlib.sha256(data).hexdigest())
        for name, (path, data) in contents.items()}, indent=2).encode()
    # Create private from the first byte, including when a permissive umask is set.
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        with tarfile.open(fileobj=stream, mode='w:gz') as tar:
            for name, data in [('backup.json', metadata), *[(name, data) for name, (_, data) in contents.items()]]:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o600
                tar.addfile(info, io.BytesIO(data))
    print(f'Saved settings backup: {backup}', flush=True)
else:
    print('No saved settings found; continuing with installation.', flush=True)
PY
exec ./install.sh "$@"
