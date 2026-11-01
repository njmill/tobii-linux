#!/usr/bin/env python3
"""Maintain passive Tobii pipes in Wine prefixes, independently of capture."""
import argparse
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import socket
import subprocess
import time
import uuid


RUNNER_ENVIRONMENT = {
    'WINEESYNC', 'WINEFSYNC', 'WINEARCH', 'WINESERVER', 'WINELOADER',
    'WINEDLLOVERRIDES', 'WINE_CPU_TOPOLOGY', 'WINE_LARGE_ADDRESS_AWARE',
    'WINE_DISABLE_FAST_SYNC', 'WINE_DISABLE_KERNEL_WRITEWATCH',
    'PATH', 'LD_LIBRARY_PATH', 'SteamAppId', 'SteamGameId',
}
WINE_INFRASTRUCTURE = {
    'services.exe', 'winedevice.exe', 'plugplay.exe', 'explorer.exe',
    'svchost.exe', 'rpcss.exe', 'lsass.exe', 'conhost.exe', 'wineboot.exe',
    'tabtip.exe', 'start.exe', 'winemenubuilder.exe', 'rundll32.exe',
    'tobii-middleware-pipe-spy.exe',
}


def application_process(executable, command):
    if Path(executable).name.startswith('wineserver'):
        return False
    for argument in command.split(b'\0'):
        name = argument.decode(errors='replace').replace('\\', '/').rsplit('/', 1)[-1].lower()
        if name.endswith('.exe'):
            return name not in WINE_INFRASTRUCTURE
    return True


@dataclass(eq=True)
class WineTarget:
    prefix: str
    wine: str
    environment: dict = field(default_factory=dict)


def runner_from_executable(executable):
    """Use the actual Wine installation, including Proton's files/bin layout."""
    path = Path(executable)
    if path.name not in ('wine', 'wine64', 'wine-preloader', 'wine64-preloader', 'wineserver'):
        return None
    directories = [path.parent] if path.parent.name == 'bin' else []
    directories.extend(parent / 'bin' for parent in list(path.parents)[:4])
    directories.append(path.parent)
    for directory in directories:
        for name in ('wine', 'wine64'):
            candidate = directory / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def discover_targets(proc=Path('/proc')):
    """Inspect this user's Wine processes; no game-name list or disk crawl."""
    targets = {}
    for entry in proc.iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            # A host Wine executable cannot join a Flatpak/container Wine server.
            root_stat = (entry / 'root').stat()
            host_stat = Path('/').stat()
            if (root_stat.st_dev, root_stat.st_ino) != (host_stat.st_dev, host_stat.st_ino):
                continue
            executable = os.readlink(entry / 'exe')
            wine = runner_from_executable(executable)
            if not wine or not application_process(executable, (entry / 'cmdline').read_bytes()):
                continue
            raw = (entry / 'environ').read_bytes()
            environment = dict(part.decode(errors='surrogateescape').split('=', 1)
                               for part in raw.split(b'\0') if b'=' in part)
            if environment.get('TOBII_LINUX_BRIDGE') == '1':
                continue
            prefix = environment.get('WINEPREFIX')
            if not prefix and environment.get('STEAM_COMPAT_DATA_PATH'):
                prefix = str(Path(environment['STEAM_COMPAT_DATA_PATH']) / 'pfx')
            prefix = str(Path(prefix or Path.home() / '.wine').resolve())
            if not (Path(prefix) / 'system.reg').is_file():
                continue
            # Never forward private child bootstrap state such as WINESERVERSOCKET:
            # its descriptor does not exist in a newly spawned Wine process.
            forwarded = {key: value for key, value in environment.items()
                         if key.startswith(('STEAM_COMPAT_', 'PROTON_'))
                         or key in RUNNER_ENVIRONMENT}
            target = WineTarget(prefix, wine, forwarded)
            targets[prefix] = target
        except (OSError, ValueError):
            # Processes can exit between any two /proc reads.
            continue
    return targets


class Bridge:
    def __init__(self, target, directory, executable, sesp_args, discovery_args):
        self.target = target
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.executable = executable
        self.sesp_args = sesp_args
        self.discovery_args = discovery_args
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
            reservation.bind(('127.0.0.1', 0))
            self.port = reservation.getsockname()[1]
        self.children = {}
        self.retries = {}
        self.started = {}
        self.ready_children = set()

    def commands(self):
        yield 'middleware-pipe-spy', ['--sesp-headpose-udp', str(self.port), *self.sesp_args]
        yield from self.discovery_args

    def tick(self, now):
        for name, args in self.commands():
            child = self.children.get(name)
            if child is not None and child.poll() is None:
                if name not in self.ready_children and now - self.started[name] > 30:
                    print(f'bridge readiness timeout prefix={self.target.prefix!r} pipe={name}', flush=True)
                    child.kill()
                continue
            retry_at, failures = self.retries.get(name, (0, 0))
            if child is not None:
                print(f'bridge exited prefix={self.target.prefix!r} pipe={name} rc={child.returncode}', flush=True)
                self.children.pop(name)
                self.ready_children.discard(name)
                failures = 0 if now - self.started[name] > 5 else failures + 1
                retry_at = now + min(.1 * 2 ** min(failures, 6), 5)
                self.retries[name] = (retry_at, failures)
            if now < retry_at:
                continue
            environment = {**os.environ, **self.target.environment,
                           'WINEPREFIX': self.target.prefix,
                           'WINEDEBUG': os.environ.get('SC_TOBII_PIPE_WINEDEBUG', '-all'),
                           'TOBII_LINUX_BRIDGE': '1',
                           'TOBII_LINUX_BRIDGE_INSTANCE': uuid.uuid4().hex}
            try:
                with (self.directory / f'{name}.stdout').open('w') as output:
                    child = subprocess.Popen([self.target.wine, self.executable,
                        '--seconds', os.environ.get('SC_TOBII_RUNTIME_SECONDS', '0'),
                        '--log', str(self.directory / f'{name}.log'), *args],
                        cwd=self.directory, env=environment, stdin=subprocess.DEVNULL,
                        stdout=output, stderr=subprocess.STDOUT)
                self.children[name] = child
                self.started[name] = now
                print(f'bridge started prefix={self.target.prefix!r} pipe={name} pid={child.pid}', flush=True)
            except OSError as error:
                print(f'bridge start failed prefix={self.target.prefix!r}: {error}', flush=True)
                self.retries[name] = (now + 5, failures + 1)

    def close(self):
        # Never kill a prefix's wineserver: it may also own the user's game.
        for child in self.children.values():
            if child.poll() is None:
                child.terminate()
        deadline = time.monotonic() + 2
        for child in self.children.values():
            try:
                child.wait(timeout=max(.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        self.children.clear()
        self.ready_children.clear()

    def snapshot(self):
        processes = {name: child.pid for name, child in self.children.items() if child.poll() is None}
        ready = len(processes) == len(list(self.commands()))
        for name in processes:
            if name in self.ready_children:
                continue
            try:
                with (self.directory / f'{name}.stdout').open('rb') as output_file:
                    output = output_file.read(65536).decode(errors='replace')
                child_ready = 'pipe_listening name=' in output
                if name == 'middleware-pipe-spy':
                    child_ready = child_ready and 'live_pose_udp_listening port=' in output
                if child_ready:
                    self.ready_children.add(name)
                ready = ready and child_ready
            except OSError:
                ready = False
        return dict(prefix=self.target.prefix, wine=self.target.wine, port=self.port,
                    ready=bool(ready), processes=processes, logs=str(self.directory))


def serve(args, stop):
    args.work_dir.mkdir(parents=True, exist_ok=True)
    primary = str(Path(args.prefix).expanduser().resolve()) if args.prefix else None
    configured = WineTarget(primary, shutil.which(args.wine) or args.wine) if primary else None
    bridges = {}
    last_seen = {}
    last_scan = last_status = 0
    state_path = args.work_dir / 'wine-bridges.json'
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as relay, selectors.DefaultSelector() as selector:
        relay.bind(('127.0.0.1', args.pose_port))
        relay.setblocking(False)
        selector.register(relay, selectors.EVENT_READ)
        try:
            while not stop():
                now = time.monotonic()
                if now - last_scan >= .25:
                    targets = {} if args.no_discovery else discover_targets()
                    for prefix in targets:
                        last_seen[prefix] = now
                    if configured and Path(primary, 'system.reg').is_file():
                        targets.setdefault(primary, bridges[primary].target if primary in bridges else configured)
                    for prefix, target in targets.items():
                        current = bridges.get(prefix)
                        # Replace only the affected prefix when its runner settings change.
                        if current and (current.target.wine != target.wine or
                                        current.target.environment != target.environment):
                            current.close()
                            del bridges[prefix]
                        if prefix not in bridges:
                            directory = args.work_dir if prefix == primary else args.work_dir / 'wine-prefixes' / hashlib.sha256(prefix.encode()).hexdigest()[:16]
                            bridges[prefix] = Bridge(target, directory, args.executable, args.sesp_args, args.discovery_args)
                    for prefix in list(bridges):
                        if prefix != primary and now - last_seen.get(prefix, now) > 5:
                            bridges.pop(prefix).close()
                            print(f'bridge released prefix={prefix!r}', flush=True)
                    last_scan = now
                for bridge in bridges.values():
                    bridge.tick(now)
                if now - last_status >= .25:
                    temporary = state_path.with_suffix('.json.tmp')
                    temporary.write_text(json.dumps(dict(pid=os.getpid(), updated=time.time(),
                        prefixes=[bridge.snapshot() for bridge in bridges.values()])) + '\n')
                    temporary.replace(state_path)
                    last_status = now
                if selector.select(.05):
                    # Bound each batch so a busy producer cannot starve recovery or shutdown.
                    for _ in range(128):
                        try:
                            packet, _ = relay.recvfrom(65535)
                        except BlockingIOError:
                            break
                        for bridge in bridges.values():
                            try:
                                relay.sendto(packet, ('127.0.0.1', bridge.port))
                            except BlockingIOError:
                                pass  # A subsequent pose supersedes a dropped packet.
        finally:
            for bridge in bridges.values():
                bridge.close()
            state_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', default='')
    parser.add_argument('--wine', default='wine')
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--executable', required=True)
    parser.add_argument('--pose-port', type=int, default=4243)
    parser.add_argument('--no-discovery', action='store_true',
                        help='Only prepare the explicitly configured prefix')
    parser.add_argument('--sesp-args', type=json.loads, default=[])
    parser.add_argument('--discovery-args', type=json.loads, default=[])
    args = parser.parse_args()
    args.work_dir = args.work_dir.expanduser().resolve()
    args.executable = str(Path(args.executable).expanduser().resolve())
    stopped = False
    def stop_handler(*_):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    serve(args, lambda: stopped)


if __name__ == '__main__':
    main()
