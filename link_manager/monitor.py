"""Background listener on the router's monitor endpoint: vehicle heartbeat and traffic rate."""

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

from pymavlink import mavutil

GCS_SYSID = 255
RATE_WINDOW_S = 5.0

# PX4 custom_mode: main mode in bits 16-23, sub mode in bits 24-31
PX4_MAIN_MODES = {1: "MANUAL", 2: "ALTCTL", 3: "POSCTL", 5: "ACRO", 6: "OFFBOARD",
                  7: "STABILIZED", 8: "RATTITUDE", 10: "TERMINATION", 11: "ALTITUDE_CRUISE"}
PX4_AUTO_SUB_MODES = {1: "READY", 2: "TAKEOFF", 3: "LOITER", 4: "MISSION", 5: "RTL",
                      6: "LAND", 8: "FOLLOW", 9: "PRECLAND", 10: "VTOL_TAKEOFF"}
PX4_MAIN_MODE_AUTO = 4


def px4_mode_name(custom_mode: int) -> str:
    main = (custom_mode >> 16) & 0xFF
    sub = (custom_mode >> 24) & 0xFF
    if main == PX4_MAIN_MODE_AUTO:
        return PX4_AUTO_SUB_MODES.get(sub, f"AUTO({sub})")
    return PX4_MAIN_MODES.get(main, f"MODE({main})")


def _rate(times) -> float:
    """Messages per second over the samples in the window."""
    if len(times) < 2:
        return 0.0
    span = times[-1] - times[0]
    return (len(times) - 1) / span if span > 0 else 0.0


@dataclass
class LinkSnapshot:
    error: str = ""
    sysid: Optional[int] = None
    heartbeat_age_s: Optional[float] = None
    heartbeat_hz: float = 0.0
    msgs_per_s: float = 0.0
    mode: str = ""
    armed: bool = False


class LinkMonitor(threading.Thread):
    def __init__(self, port: int):
        super().__init__(daemon=True)
        self._port = port
        self._lock = threading.Lock()
        self._running = True
        self._error = ""
        self._sysid: Optional[int] = None
        self._last_hb = 0.0
        self._hb_times = deque()
        self._msg_times = deque()
        self._mode = ""
        self._armed = False

    def stop(self):
        self._running = False

    def run(self):
        try:
            conn = mavutil.mavlink_connection(f"udpin:127.0.0.1:{self._port}")
        except OSError as e:
            with self._lock:
                self._error = f"monitor port {self._port}: {e}"
            return

        while self._running:
            msg = conn.recv_match(blocking=True, timeout=0.5)
            if msg is None or msg.get_type() == "BAD_DATA":
                continue
            now = time.monotonic()
            with self._lock:
                self._msg_times.append(now)
                if msg.get_type() == "HEARTBEAT" and self._is_vehicle(msg):
                    self._sysid = msg.get_srcSystem()
                    self._last_hb = now
                    self._hb_times.append(now)
                    self._armed = bool(msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
                    if msg.autopilot == mavutil.mavlink.MAV_AUTOPILOT_PX4:
                        self._mode = px4_mode_name(msg.custom_mode)
                    else:
                        self._mode = mavutil.mode_string_v10(msg)
        conn.close()

    @staticmethod
    def _is_vehicle(msg) -> bool:
        # Skip GCS heartbeats (QGC etc. talking through the router)
        return (msg.get_srcSystem() != GCS_SYSID
                and msg.autopilot != mavutil.mavlink.MAV_AUTOPILOT_INVALID)

    def snapshot(self) -> LinkSnapshot:
        now = time.monotonic()
        with self._lock:
            for times in (self._hb_times, self._msg_times):
                while times and now - times[0] > RATE_WINDOW_S:
                    times.popleft()
            return LinkSnapshot(
                error=self._error,
                sysid=self._sysid,
                heartbeat_age_s=(now - self._last_hb) if self._last_hb else None,
                heartbeat_hz=_rate(self._hb_times),
                msgs_per_s=_rate(self._msg_times),
                mode=self._mode,
                armed=self._armed,
            )
