#!/usr/bin/env python3
"""Create a portable source bundle from the public working tree."""
import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[2]
ROOT_FILES = ('README.md', 'INSTALL.md', 'Makefile', 'install.sh', 'update.sh',
              'uninstall.sh', 'requirements-mediapipe.txt', '.gitignore')
PUBLIC_DIRS = ('scripts/app', 'scripts/runtime', 'tools/app', 'configs/udev',
               'configs/systemd', 'docs')
SUFFIXES = {'.py', '.sh', '.c', '.in', '.rules', '.md', '.puml'}
REQUIRED = ('scripts/runtime/tracking-service.py', 'scripts/runtime/tracking_engine.py',
    'scripts/runtime/tracking_core.py', 'scripts/runtime/runtime_config.py',
    'scripts/runtime/service_protocol.py', 'scripts/runtime/capture_control.py',
    'scripts/runtime/eye-pose-dashboard.py', 'scripts/runtime/mediapipe-face-worker.py',
    'scripts/app/manage-tracking-service.py', 'configs/systemd/tobii-linux.service.in',
    'tools/app/tobii-ttp-mux.c', 'tools/app/tobii-middleware-pipe-spy.c')


def collect_files(root):
    paths = {Path(name) for name in ROOT_FILES}
    paths.add(Path('assets/mediapipe/face_landmarker.task.sha256'))
    for directory in PUBLIC_DIRS:
        for path in (root / directory).rglob('*'):
            if '__pycache__' in path.parts:
                continue
            if path.is_symlink():
                raise ValueError(f'public source must be a regular file/directory: {path}')
            if path.is_file() and path.suffix in SUFFIXES:
                paths.add(path.relative_to(root))
    for name in ('LICENSE', 'LICENSE.md', 'COPYING', 'NOTICE'):
        if (root / name).is_file():
            paths.add(Path(name))
    for name in (*paths, *(Path(p) for p in REQUIRED)):
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f'missing or invalid public source: {name}')
    return sorted(paths)


def git_metadata(root):
    try:
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root,
            stderr=subprocess.DEVNULL, text=True).strip()
        dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=root,
            stderr=subprocess.DEVNULL, text=True).strip())
        return revision, dirty
    except (OSError, subprocess.CalledProcessError):
        return None, None


def build(root, output, version, epoch):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', version):
        raise ValueError('version must contain only letters, digits, dots, underscores or hyphens')
    paths = collect_files(root)
    contents = {p.as_posix(): (root / p).read_bytes() for p in paths}
    revision, dirty = git_metadata(root)
    contents['RELEASE.json'] = (json.dumps(dict(format_version=1, version=version,
        kind='source', created_utc=datetime.fromtimestamp(epoch, timezone.utc).isoformat(),
        source_revision=revision, working_tree_dirty=dirty), indent=2, sort_keys=True)+'\n').encode()
    contents['MANIFEST.sha256'] = ''.join(f'{hashlib.sha256(data).hexdigest()}  {name}\n'
        for name, data in sorted(contents.items())).encode()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f'tobii-linux-{version}.tar.gz'
    temporary = archive.with_suffix('.gz.tmp')
    try:
        with temporary.open('wb') as raw:
            with gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=epoch) as compressed:
                with tarfile.open(mode='w', fileobj=compressed, format=tarfile.PAX_FORMAT) as tar:
                    for name, data in sorted(contents.items()):
                        info = tarfile.TarInfo('tobii-linux/' + name)
                        info.size = len(data)
                        info.mode = 0o755 if name in contents and (root / name).is_file() and os.access(root / name, os.X_OK) else 0o644
                        info.mtime = epoch
                        tar.addfile(info, io.BytesIO(data))
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)
    checksum = archive.with_name(archive.name+'.sha256')
    checksum.write_text(f'{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n')
    return archive, checksum, len(contents)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', help='Release label; defaults to a UTC snapshot timestamp.')
    parser.add_argument('--output-dir', type=Path, default=ROOT/'.tmp/releases')
    args = parser.parse_args()
    try:
        epoch = int(os.environ.get('SOURCE_DATE_EPOCH', datetime.now(timezone.utc).timestamp()))
        version = args.version or datetime.fromtimestamp(epoch, timezone.utc).strftime('snapshot-%Y%m%d-%H%M%S')
        archive, checksum, count = build(ROOT, args.output_dir, version, epoch)
    except (OSError, ValueError) as exc:
        parser.exit(1, f'package failed: {exc}\n')
    print(f'Archive: {archive}\nSHA-256: {checksum}\nFiles: {count}; size: {archive.stat().st_size:,} bytes')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
