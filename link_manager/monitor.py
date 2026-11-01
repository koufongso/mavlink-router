"""Background listener on the router's monitor endpoint: is the vehicle heard, and how much traffic.

Only MAVLink frame headers are parsed (no pymavlink): that's enough to count
messages and spot vehicle heartbeats, and stays cheap at any message rate.
"""

import errno
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Iterator, Optional, Tuple

MAGIC_V1 = 0xFE
MAGIC_V2 = 0xFD
SIGNED_FLAG = 0x01  # MAVLink 2 incompat flag: 13-byte signature after the CRC
HEARTBEAT_ID = 0
HEARTBEAT_AUTOPILOT_OFFSET = 5  # payload: custom_mode(4) type(1) autopilot(1) ...
MAV_AUTOPILOT_INVALID = 8
GCS_SYSID = 255
RATE_WINDOW_S = 5.0
# The port is taken while the other app (GUI or CLI) runs; take over once it closes
BIND_RETRY_S = 1.0


def parse_frames(data: bytes) -> Iterator[Tuple[int, int, bytes]]:
    """(sysid, msgid, payload) for each MAVLink frame in a datagram.

    No CRC check: the router has already validated the frames it forwards.
    """
    i, n = 0, len(data)
    while i < n:
        magic = data[i]
        if magic == MAGIC_V2 and i + 10 <= n:
            length, incompat = data[i + 1], data[i + 2]
            sysid = data[i + 5]
            msgid = data[i + 7] | data[i + 8] << 8 | data[i + 9] << 16
            payload = data[i + 10:i + 10 + length]
            i += 10 + length + 2 + (13 if incompat & SIGNED_FLAG else 0)
        elif magic == MAGIC_V1 and i + 6 <= n:
            length, sysid, msgid = data[i + 1], data[i + 3], data[i + 5]
            payload = data[i + 6:i + 6 + length]
            i += 6 + length + 2
        else:
            return  # not at a frame start; skip the rest of this datagram
        yield sysid, msgid, payload


def is_vehicle_heartbeat(sysid: int, msgid: int, payload: bytes) -> bool:
    # Skip GCS heartbeats (QGC etc. talking through the router)
    return (msgid == HEARTBEAT_ID
            and sysid != GCS_SYSID
            and len(payload) > HEARTBEAT_AUTOPILOT_OFFSET
            and payload[HEARTBEAT_AUTOPILOT_OFFSET] != MAV_AUTOPILOT_INVALID)


class RateWindow:
    """Events per second over a sliding window, with a running total (O(1) per sample)."""

    def __init__(self, window_s: float):
        self._window_s = window_s
        self._samples = deque()  # (time, count)
        self._total = 0

    def add(self, now: float, count: int):
        self._samples.append((now, count))
        self._total += count

    def rate(self, now: float) -> float:
        samples = self._samples
        while samples and now - samples[0][0] > self._window_s:
            self._total -= samples.popleft()[1]
        if len(samples) < 2:
            return 0.0
        span = samples[-1][0] - samples[0][0]
        # The first sample marks the start of the span, so it isn't counted
        return (self._total - samples[0][1]) / span if span > 0 else 0.0


@dataclass
class LinkSnapshot:
    error: str = ""
    port_busy: bool = False  # the monitor port is held by another app, e.g. the GUI while this is the CLI
    sysid: Optional[int] = None
    heartbeat_age_s: Optional[float] = None
    heartbeat_hz: float = 0.0
    msgs_per_s: float = 0.0


class LinkMonitor(threading.Thread):
    def __init__(self, port: int):
        super().__init__(daemon=True)
        self._port = port
        self._lock = threading.Lock()
        self._running = True
        self._error = ""
        self._port_busy = False
        self._sysid: Optional[int] = None
        self._last_hb = 0.0
        self._heartbeats = RateWindow(RATE_WINDOW_S)
        self._messages = RateWindow(RATE_WINDOW_S)

    def stop(self):
        self._running = False

    def _bind(self) -> Optional[socket.socket]:
        """Bind the monitor port, retrying until it's free; None if stopped first."""
        while self._running:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.bind(("127.0.0.1", self._port))
            except OSError as e:
                sock.close()
                with self._lock:
                    self._error = f"monitor port {self._port}: {e.strerror}"
                    self._port_busy = e.errno == errno.EADDRINUSE
                time.sleep(BIND_RETRY_S)
                continue
            with self._lock:
                self._error = ""
                self._port_busy = False
            return sock
        return None

    def run(self):
        sock = self._bind()
        if sock is None:
            return
        sock.settimeout(0.5)

        while self._running:
            try:
                data = sock.recv(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            now = time.monotonic()
            count = 0
            heartbeat_from = None
            for sysid, msgid, payload in parse_frames(data):
                count += 1
                if is_vehicle_heartbeat(sysid, msgid, payload):
                    heartbeat_from = sysid
            if count == 0:
                continue
            with self._lock:
                self._messages.add(now, count)
                if heartbeat_from is not None:
                    self._sysid = heartbeat_from
                    self._last_hb = now
                    self._heartbeats.add(now, 1)
        sock.close()

    def snapshot(self) -> LinkSnapshot:
        now = time.monotonic()
        with self._lock:
            return LinkSnapshot(
                error=self._error,
                port_busy=self._port_busy,
                sysid=self._sysid,
                heartbeat_age_s=(now - self._last_hb) if self._last_hb else None,
                heartbeat_hz=self._heartbeats.rate(now),
                msgs_per_s=self._messages.rate(now),
            )
