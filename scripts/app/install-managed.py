#!/usr/bin/env python3
"""Install/update isolated source versions; keep runtime settings outside code."""
import argparse
import base64
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

REPOSITORY = 'njmill/tobii-linux-pub'
SETTINGS = ('sc-tuning.json', 'sc-window.json', 'screen-calibration.json',
            'gaze-calibration.json', 'head-position.json', 'native-head-position.json')


def download(url):
    if urllib.parse.urlparse(url).scheme != 'https':
        raise ValueError('Remote downloads require HTTPS')
    headers = {'User-Agent': 'tobii-linux-installer', 'Accept': 'application/vnd.github+json'}
    if urllib.parse.urlparse(url).hostname == 'api.github.com' and os.environ.get('GITHUB_TOKEN'):
        headers['Authorization'] = 'Bearer ' + os.environ['GITHUB_TOKEN']
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
                data = response.read(256 * 1024 * 1024 + 1)
                if len(data) > 256 * 1024 * 1024:
                    raise ValueError('Download exceeds source archive size limit')
                return data
        except urllib.error.HTTPError as error:
            if error.code < 500 and error.code != 429:
                raise
            if attempt == 2:
                raise
        except OSError:
            if attempt == 2:
                raise
        time.sleep(attempt + 1)


def resolve_source(args):
    if args.archive:
        archive = args.archive.expanduser().resolve()
        checksum = args.sha256
        if checksum is None:
            checksum = Path(str(archive) + '.sha256').read_text().split()[0]
        if not re.fullmatch(r'[0-9a-fA-F]{64}', checksum):
            raise ValueError('A valid --sha256 or archive.sha256 sidecar is required')
        return dict(kind='archive', version=archive.name, url=str(archive), checksum=checksum.lower(), id=checksum.lower())
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repo):
        raise ValueError('Repository must be OWNER/NAME')
    api = 'https://api.github.com/repos/' + args.repo
    reference = args.ref
    release = None
    if reference is None:
        endpoint = '/releases/latest' if args.version == 'latest' else '/releases/tags/' + urllib.parse.quote(args.version, safe='')
        try:
            release = json.loads(download(api + endpoint))
            reference = release['tag_name']
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            reference = 'main' if args.version == 'latest' else args.version
            if args.version == 'latest':
                print('No published release; resolving the current main commit.', flush=True)
    commit = json.loads(download(api + '/commits/' + urllib.parse.quote(reference, safe='')))['sha']
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('GitHub returned an invalid commit ID')
    if release:
        assets = {asset['name']: asset for asset in release.get('assets', [])}
        name = 'tobii-linux-' + reference.lstrip('v') + '.tar.gz'
        if name in assets:
            if name + '.sha256' not in assets:
                raise ValueError('Release source bundle has no checksum sidecar')
            checksum = download(assets[name + '.sha256']['browser_download_url']).decode().split()[0]
            if not re.fullmatch(r'[0-9a-fA-F]{64}', checksum):
                raise ValueError('Invalid release checksum')
            return dict(kind='release', version=reference, commit=commit,
                        url=assets[name]['browser_download_url'], checksum=checksum.lower(), id=checksum.lower())
    return dict(kind='commit', version=reference + '@' + commit[:12], commit=commit,
                url='https://codeload.github.com/' + args.repo + '/tar.gz/' + commit, id=commit)


def extract_source(archive, target):
    """Accept a single source root and regular files only; never follow tar links."""
    with tarfile.open(archive, 'r:gz') as source:
        count = total = 0
        top = None
        seen = set()
        for member in source:
            count += 1
            total += member.size
            if count > 10000 or total > 256 * 1024 * 1024:
                raise ValueError('Source archive exceeds extraction limits')
            name = PurePosixPath(member.name)
            if name.is_absolute() or '..' in name.parts or '\\' in member.name or not name.parts:
                raise ValueError('Unsafe source archive path: ' + member.name)
            if top is None:
                top = name.parts[0]
            if top != name.parts[0] or not (member.isfile() or member.isdir()):
                raise ValueError('Source archive must contain one root and no links/special files')
            if member.isdir():
                continue
            relative = Path(*name.parts[1:])
            if len(name.parts) < 2 or relative in seen:
                raise ValueError('Duplicate or invalid archive member')
            seen.add(relative)
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as incoming, path.open('xb') as output:
                shutil.copyfileobj(incoming, output)
            path.chmod(0o755 if member.mode & 0o111 else 0o644)
    for required in ('install.sh', 'Makefile', 'scripts/app/manage-tracking-service.py', 'scripts/app/install-managed.py'):
        if not (target / required).is_file():
            raise ValueError('Source is missing ' + required)
    manifest = target / 'MANIFEST.sha256'
    if manifest.exists():
        listed = set()
        for line in manifest.read_text().splitlines():
            digest, separator, name = line.partition('  ')
            path = PurePosixPath(name)
            if not separator or not re.fullmatch(r'[0-9a-f]{64}', digest) or path.is_absolute() or '..' in path.parts:
                raise ValueError('Invalid source manifest')
            if name in listed or hashlib.sha256((target / name).read_bytes()).hexdigest() != digest:
                raise ValueError('Source manifest mismatch: ' + name)
            listed.add(name)
        actual = {p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file()}
        if actual != listed | {'MANIFEST.sha256'}:
            raise ValueError('Source manifest does not cover all extracted files')


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w') as output:
        json.dump(value, output, indent=2)
        output.write('\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def link_version(base, name, target):
    path = base / name
    temporary = base / (name + '.new')
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    temporary.replace(path)


def python_for(root):
    candidate = root / '.venv/ui/bin/python'
    return str(candidate) if candidate.exists() else sys.executable


def run_manager(root, action, check=True):
    return subprocess.run([python_for(root), str(root / 'scripts/app/manage-tracking-service.py'), action], check=check)


def runtime_state(root):
    return (root / '.tmp/sc-tobii-native-runtime').resolve()


def snapshot(root):
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(1)
            connection.connect(str(runtime_state(root) / 'control.sock'))
            connection.sendall(b'{"version":1,"op":"snapshot"}\n')
            response = b''
            while b'\n' not in response and len(response) < 1024 * 1024:
                chunk = connection.recv(65536)
                if not chunk:
                    return None
                response += chunk
            return json.loads(response.split(b'\n')[0]).get('result')
    except (OSError, ValueError):
        return None


def stop_components(root):
    scripts = {str(root / 'scripts/runtime' / name) for name in
               ('tracking-tray.py', 'eye-pose-dashboard.py', 'inband-landmark-dashboard.py')}
    children = []
    for process in Path('/proc').iterdir():
        try:
            if not process.name.isdecimal() or process.stat().st_uid != os.getuid():
                continue
            arguments = set(os.fsdecode(part) for part in (process / 'cmdline').read_bytes().split(b'\0'))
            if scripts & arguments:
                pid = int(process.name)
                os.kill(pid, signal.SIGTERM)
                children.append(pid)
        except (OSError, ValueError):
            pass
    run_manager(root, 'stop')
    deadline = time.monotonic() + 6
    while any(Path('/proc', str(pid)).exists() for pid in children) and time.monotonic() < deadline:
        time.sleep(.05)
    # Do not force-kill a desktop client with unsaved state.
    if any(Path('/proc', str(pid)).exists() for pid in children):
        raise RuntimeError('Close the Tobii dashboard/tray and retry the update')


def integration_paths(state, bin_dir):
    config = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
    data = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share'))
    paths = [config / 'systemd/user/tobii-linux.service',
             config / 'systemd/user/graphical-session.target.wants/tobii-linux.service',
             config / 'systemd/user/default.target.wants/tobii-linux.service',
             config / 'autostart/tobii-linux-service.desktop', config / 'autostart/tobii-linux-tray.desktop',
             config / 'tobii-linux/runtime.env', data / 'applications/tobii-dashboard.desktop',
             data / 'applications/tobii-tray.desktop', bin_dir / 'tobii-dashboard', bin_dir / 'tobii']
    return paths + [state / name for name in SETTINGS]


def backup_files(paths, destination):
    records = []
    for path in paths:
        record = dict(path=str(path), kind='absent')
        if path.is_symlink():
            record.update(kind='link', target=os.readlink(path))
        elif path.is_file():
            record.update(kind='file', mode=stat.S_IMODE(path.stat().st_mode),
                          data=base64.b64encode(path.read_bytes()).decode())
        elif path.exists():
            raise ValueError('Expected a file, not a directory: ' + str(path))
        records.append(record)
    atomic_json(destination, records)
    return records


def restore_files(records):
    for record in records:
        path = Path(record['path'])
        path.unlink(missing_ok=True)
        if record['kind'] == 'absent':
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if record['kind'] == 'link':
            path.symlink_to(record['target'])
        else:
            path.write_bytes(base64.b64decode(record['data']))
            path.chmod(record['mode'])


def write_command(base, bin_dir):
    bin_dir.mkdir(parents=True, exist_ok=True)
    root = shlex.quote(str(base / 'current'))
    install = shlex.quote(str(base))
    script = ('#!/bin/sh\nset -eu\nroot=' + root + '\n'
              'python="$root/.venv/ui/bin/python"\n'
              'action="${1:-dashboard}"\n[ "$#" -eq 0 ] || shift\n'
              'case "$action" in\n'
              ' update) exec "$python" "$root/scripts/app/install-managed.py" --install-dir ' + install + ' "$@" ;;\n'
              ' rollback) exec "$python" "$root/scripts/app/install-managed.py" --install-dir ' + install + ' --rollback "$@" ;;\n'
              ' uninstall) exec bash "$root/uninstall.sh" "$@" ;;\n'
              ' version) cat ' + install + '/install.json ;;\n'
              ' dashboard|tray|start|stop|status) exec "$python" "$root/scripts/app/manage-tracking-service.py" "$action" "$@" ;;\n'
              ' *) echo "Usage: tobii [dashboard|tray|start|stop|status|version|update|rollback|uninstall]"; exit 2 ;;\n'
              'esac\n')
    (bin_dir / 'tobii').write_text(script)
    (bin_dir / 'tobii').chmod(0o755)


def activate(root, args):
    command = ['bash', str(root / 'install.sh'), '--activate-only']
    if args.no_udev:
        command.append('--no-udev')
    subprocess.run(command, cwd=root, check=True)


def start(root, launch, ensure=True):
    if ensure:
        run_manager(root, 'start')
    if launch and (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        log = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'tobii-linux/desktop.log'
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('a') as output:
            for action in ('tray', 'dashboard'):
                subprocess.Popen([python_for(root), str(root / 'scripts/app/manage-tracking-service.py'), action],
                                 cwd=root, stdin=subprocess.DEVNULL, stdout=output,
                                 stderr=subprocess.STDOUT, start_new_session=True)


def perform(args, base, source):
    base.mkdir(parents=True, exist_ok=True)
    with (base / 'install.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = base / 'current'
        old = current.resolve() if current.is_symlink() else args.migrate_from
        receipt_path = base / 'install.json'
        receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
        if not args.rollback and not args.force and receipt.get('source', {}).get('id') == source['id'] and current.is_symlink() and (current / '.venv/ui/bin/python').exists():
            print('Already current:', receipt.get('source', {}).get('version'))
            return
        old_snapshot = snapshot(old) if old else None
        if old_snapshot and old_snapshot.get('capture_running'):
            raise RuntimeError('Capture is active. Close the game or stop capture, then rerun the installer.')
        config = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
        if not old and any(path.exists() for path in (config / 'systemd/user/tobii-linux.service',
                config / 'autostart/tobii-linux-service.desktop', config / 'autostart/tobii-linux-tray.desktop')):
            raise RuntimeError('An existing source installation is registered. Use --migrate-from /path/to/checkout to preserve its settings.')
        state = base / 'state/runtime'
        state.mkdir(parents=True, mode=0o700, exist_ok=True)
        bin_dir = Path(receipt.get('bin_dir', os.environ.get('TOBII_BIN_DIR', str(Path.home() / '.local/bin')))).expanduser().resolve()
        if args.rollback:
            previous = base / 'previous'
            if not previous.is_symlink() or not previous.resolve().is_dir():
                raise ValueError('There is no previous installed version')
            candidate = previous.resolve()
            candidate_source = json.loads((candidate / '.managed-install.json').read_text())['source']
        else:
            versions = base / 'versions'
            versions.mkdir(exist_ok=True)
            candidate = versions / (source['id'][:12] + '-' + uuid.uuid4().hex[:8])
            candidate.mkdir()
            candidate_source = source
            print('Downloading and preparing', source['version'], flush=True)
            data = Path(source['url']).read_bytes() if source['kind'] == 'archive' else download(source['url'])
            digest = hashlib.sha256(data).hexdigest()
            if source.get('checksum') and digest != source['checksum']:
                raise ValueError('Source archive checksum mismatch')
            with tempfile.NamedTemporaryFile(suffix='.tar.gz') as archive:
                archive.write(data)
                archive.flush()
                extract_source(archive.name, candidate)
            (candidate / '.tmp').mkdir(exist_ok=True)
            (candidate / '.tmp/sc-tobii-native-runtime').symlink_to(state)
            atomic_json(candidate / '.managed-install.json', dict(source=source, base=str(base), archive_sha256=digest))
            command = ['bash', str(candidate / 'install.sh'), '--prepare-only']
            if args.no_system_packages:
                command.append('--no-system-packages')
            subprocess.run(command, cwd=candidate, check=True)
        # Nothing above this point stops the old service or rewrites integration files.
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
        records = backup_files(integration_paths(state, bin_dir) + [receipt_path, base / 'previous'], base / 'backups' / (stamp + '.json'))
        old_running = bool(old_snapshot)
        should_start = not args.no_start and (old_running or old is None)
        old_link = os.readlink(current) if current.is_symlink() else None
        try:
            if old:
                stop_components(old)
            if args.migrate_from and not receipt:
                for name in SETTINGS:
                    original = runtime_state(args.migrate_from) / name
                    if original.is_file() and not (state / name).exists():
                        shutil.copy2(original, state / name)
            print('Activating prepared version; preserving settings and startup preference.', flush=True)
            link_version(base, 'current', candidate)
            activate(candidate, args)
            write_command(base, bin_dir)
            if should_start:
                start(candidate, launch=False)
            atomic_json(receipt_path, dict(repo=args.repo, source=candidate_source,
                current=str(candidate), previous=str(old) if old else None, bin_dir=str(bin_dir)))
            if old and old != candidate and old.parent == base / 'versions':
                link_version(base, 'previous', old)
        except BaseException:
            run_manager(candidate, 'stop', check=False)
            restore_files(records)
            current.unlink(missing_ok=True)
            if old_link:
                current.symlink_to(old_link)
            try:
                subprocess.run(['systemctl', '--user', 'daemon-reload'], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=False)
            except OSError:
                pass
            if old_running:
                run_manager(old, 'start', check=False)
            print('Activation failed. Previous integration files and settings restored.', file=sys.stderr)
            raise
        if should_start:
            start(candidate, launch=True, ensure=False)
        print('\nInstalled:', candidate_source['version'])
        print('Command:', bin_dir / 'tobii')
        if str(bin_dir) not in os.environ.get('PATH', '').split(os.pathsep):
            print('Add this directory to PATH, or use the full command path:', bin_dir)
        print('Update: tobii update (or rerun the same one-line installer)')
        print('Rollback: tobii rollback')
        print('Settings:', state)
        print('Replug the tracker if USB permissions were just installed. The tray is optional on desktops without a tray host.')
        print('Open Tobii Dashboard for screen/gaze calibration. Capture starts only on client demand or manual Start Capture.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-dir', type=Path, default=Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'tobii-linux')
    parser.add_argument('--repo')
    parser.add_argument('--version', default='latest')
    parser.add_argument('--ref')
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--sha256')
    parser.add_argument('--migrate-from', type=lambda p: Path(p).expanduser().resolve())
    parser.add_argument('--preflight', action='store_true')
    parser.add_argument('--rollback', action='store_true')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--no-start', action='store_true')
    parser.add_argument('--no-system-packages', action='store_true')
    parser.add_argument('--no-udev', action='store_true')
    args = parser.parse_args()
    try:
        if platform.system() != 'Linux' or platform.machine() != 'x86_64':
            raise ValueError('This runtime currently supports x86-64 Linux only')
        if os.geteuid() == 0:
            raise ValueError('Run as your normal user, without sudo')
        if Path('/run/ostree-booted').exists() and not args.no_system_packages:
            raise ValueError('Immutable OSTree system: provision host dependencies first, then use --no-system-packages. Automatic package layering/reboots are not supported.')
        libc, libc_version = platform.libc_ver()
        if libc != 'glibc' or tuple(int(n) for n in libc_version.split('.')[:2]) < (2, 28):
            raise ValueError('The pinned desktop/ML wheels require glibc 2.28 or newer')
        base = args.install_dir.expanduser().resolve()
        if len(os.fsencode(base / 'state/runtime/control.sock')) > 107:
            raise ValueError('Installation path is too long for a Unix control socket; choose a shorter --install-dir')
        if args.migrate_from and not (args.migrate_from / 'scripts/app/manage-tracking-service.py').is_file():
            raise ValueError('--migrate-from must name an existing Tobii source installation')
        if (base / '.git').exists():
            raise ValueError('Choose a managed installation folder, not an existing Git checkout')
        if (base / 'current').exists() and not (base / 'current').is_symlink():
            raise ValueError('The managed current path is not an installer-owned symlink')
        receipt = base / 'install.json'
        args.repo = args.repo or (json.loads(receipt.read_text()).get('repo') if receipt.exists() else None) or REPOSITORY
        if args.rollback and (args.archive or args.ref or args.version != 'latest'):
            raise ValueError('--rollback cannot be combined with a source selection')
        if args.sha256 and not args.archive:
            raise ValueError('--sha256 requires --archive')
        source = None if args.rollback else resolve_source(args)
        if args.preflight:
            print('Plan:', 'rollback' if args.rollback else source['version'])
            print('Install folder:', base)
            print('Persistent settings:', base / 'state/runtime')
            print('Prepare separate version, then update service/menu integration; retain previous version.')
            print('Supports apt/dnf/pacman families; uses XDG desktop entries and systemd when available.')
            print('No persistent changes made.')
            return 0
        perform(args, base, source)
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.CalledProcessError) as error:
        print('Installation failed:', error, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
