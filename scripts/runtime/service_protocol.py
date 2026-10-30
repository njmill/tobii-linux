"""Versioned, user-local JSON control protocol; no tracking or GUI dependencies."""
from collections import deque
import json
import math
import os
from pathlib import Path
import queue
import socket
import threading
import time

PROTOCOL_VERSION = 1
MAX_MESSAGE = 1024 * 1024


def default_socket():
    root = Path(__file__).resolve().parents[2]
    work = Path(os.environ.get('SC_TOBII_NATIVE_RUNTIME_DIR', root / '.tmp/sc-tobii-native-runtime'))
    if not work.is_absolute():work=root/work
    path=Path(os.environ.get('SC_TOBII_CONTROL_SOCKET', work / 'control.sock'))
    return path if path.is_absolute() else root/path


def json_safe(value):
    from dataclasses import asdict, is_dataclass
    if is_dataclass(value):
        return json_safe(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, deque)):
        return [json_safe(v) for v in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f'unsupported protocol value {type(value).__name__}')


def encode(value):
    return json.dumps(json_safe(value), allow_nan=False, separators=(',', ':')).encode() + b'\n'


class RpcConnection:
    def __init__(self, path, timeout=5.):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(timeout)
        try:
            self.socket.connect(str(path))
        except BaseException:
            self.socket.close()
            raise
        self.buffer = bytearray()

    def request(self, op, **values):
        self.socket.sendall(encode(dict(version=PROTOCOL_VERSION, op=op, **values)))
        while b'\n' not in self.buffer:
            data = self.socket.recv(65536)
            if not data:
                raise ConnectionError('tracking service disconnected')
            self.buffer.extend(data)
            if len(self.buffer) > MAX_MESSAGE:
                raise ValueError('service response exceeds message limit')
        line, _, rest = self.buffer.partition(b'\n')
        self.buffer = bytearray(rest)
        reply = json.loads(line)
        if reply.get('version') != PROTOCOL_VERSION:
            raise ValueError('unsupported tracking-service protocol')
        if not reply.get('ok'):
            raise ValueError(reply.get('error', 'service command failed'))
        return reply.get('result')

    def close(self):
        self.socket.close()


class ServiceClient:
    """Background RPC so device startup/shutdown cannot block the UI thread."""
    def __init__(self, path, preview=False):
        self.preview = preview
        self.path = path
        self.commands = queue.Queue(maxsize=128)
        self.lock = threading.Lock()
        self.snapshot = None
        self.connected = False
        self.error = ''
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name='tracking-service-client', daemon=True)
        self.thread.start()

    def command(self, name, **values):
        try:
            self.commands.put_nowait((name, values))
        except queue.Full:
            raise ValueError("tracking service is busy; try again") from None

    def read(self):
        with self.lock:
            return self.snapshot, self.connected, self.error

    def _run(self):
        connection = None
        command_error = ""
        while not self.stop.is_set():
            try:
                if connection is None:
                    connection = RpcConnection(self.path)
                while not self.commands.empty():
                    name, values = self.commands.get_nowait()
                    # Never replay an uncertain calibration/action after reconnect.
                    try:
                        connection.request('command', name=name, values=values)
                        command_error = ''
                    except ValueError as exc:
                        command_error = str(exc)
                snapshot = connection.request('snapshot', preview=self.preview)
                with self.lock:
                    self.snapshot, self.connected, self.error = snapshot, True, command_error
                self.stop.wait(.05)
            except (OSError, ValueError, ConnectionError) as exc:
                with self.lock:
                    self.connected, self.error = False, str(exc)
                if connection is not None:
                    connection.close()
                    connection = None
                # Actions issued against a disconnected service are not deferred.
                while not self.commands.empty():
                    try:
                        self.commands.get_nowait()
                    except queue.Empty:
                        break
                self.stop.wait(.5)
        if connection is not None:
            connection.close()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=6.)
