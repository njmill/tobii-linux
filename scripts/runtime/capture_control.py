"""Loopback demand snapshots and explicit dashboard capture policy.

Producers periodically publish their current subscription count. Sequence numbers
reject reordered UDP, and leases expire if a compatibility process disappears.
"""
import os
import socket
import time
import uuid


class DemandPublisher:
    def __init__(self, target, source='tcp'):
        self.address = None
        if target:
            host, port = target.rsplit(':', 1)
            if host != '127.0.0.1':
                raise ValueError('capture demand must use IPv4 loopback')
            self.address = (host, int(port))
        self.source = f'{source}:{os.getpid()}:{uuid.uuid4().hex}'
        self.sequence = 0
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def publish(self, count):
        if self.address is None:
            return
        self.sequence += 1
        data = f'tobii-demand-v1 {self.source} {self.sequence} {count}'.encode('ascii')
        self.socket.sendto(data, self.address)

    def close(self):
        try:
            self.publish(0)
        finally:
            self.socket.close()


class CaptureDemand:
    def __init__(self, lease_s=2.0, stop_grace_s=1.0):
        self.lease_s = lease_s
        self.stop_grace_s = stop_grace_s
        self.sources = {}
        self.manual = None
        self.last_demand = None

    def receive(self, data, now):
        try:
            protocol, source, sequence, count = data.decode('ascii').split()
            sequence, count = int(sequence), int(count)
            if protocol != 'tobii-demand-v1' or not source.startswith(('tcp:', 'sesp:')):
                return False
            if len(source) > 128 or sequence < 1 or not 0 <= count <= 100000:
                return False
        except (ValueError, UnicodeError):
            return False
        previous = self.sources.get(source)
        if previous is not None and sequence <= previous[0]:
            return False
        self.sources[source] = (sequence, count, now)
        return True

    def client_count(self, now):
        return sum(count for _, count, seen in self.sources.values() if now - seen <= self.lease_s)

    def wanted(self, now):
        active = self.client_count(now) > 0
        if active:
            self.last_demand = now
        if self.manual is not None:
            return self.manual
        return active or (self.last_demand is not None and now - self.last_demand < self.stop_grace_s)


class DemandReceiver:
    def __init__(self, port):
        self.policy = CaptureDemand()
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.socket.bind(('127.0.0.1', port))
            self.socket.setblocking(False)
        except BaseException:
            self.socket.close()
            raise

    def poll(self, now):
        for _ in range(256):
            try:
                data, address = self.socket.recvfrom(512)
            except BlockingIOError:
                break
            if address[0] == '127.0.0.1':
                self.policy.receive(data, now)
        return self.policy.wanted(now)

    def close(self):
        self.socket.close()
