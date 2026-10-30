"""Link modes, persisted settings and mavlink-router config generation."""

import copy
import json
import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "mavlink-router-app"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "mavlink-router-app"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

# Extra endpoint the GUI itself listens on for the heartbeat monitor
MONITOR_PORT = 14599

MODES = {
    "flight": "Flight (SiK radio)",
    "bench": "Bench (Pixhawk USB)",
    "sitl": "SITL (UDP)",
}
SERIAL_MODES = ("flight", "bench")

DEFAULT_SETTINGS = {
    "mode": "flight",
    "serial": {
        "flight": {"device": "/dev/ttyUSB0", "baud": 57600},
        "bench": {"device": "/dev/ttyACM0", "baud": 115200},
    },
    # PX4 SITL sends its GCS link to UDP 14550, so the router takes that port
    # in SITL mode and QGC moves to its "sitl_port"
    "sitl": {"listen_port": 14550},
    # Where the router sends MAVLink. "address" defaults to 127.0.0.1;
    # "sitl_port" (optional) replaces "port" in SITL mode.
    "endpoints": [
        {"name": "QGC", "port": 14550, "sitl_port": 14570},
        {"name": "Hub", "port": 14560},
        {"name": "Groundstation", "port": 14552},
    ],
}


def load_settings() -> dict:
    """Load settings, filling in defaults; writes the defaults on first run."""
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE) as f:
            user = json.load(f)
        for key, value in user.items():
            if isinstance(value, dict) and isinstance(settings.get(key), dict):
                settings[key].update(value)
            else:
                settings[key] = value
    except FileNotFoundError:
        save_settings(settings)
    except (OSError, ValueError) as e:
        print(f"[settings] ignoring unreadable {SETTINGS_FILE}: {e}")
    return settings


def save_settings(settings: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")


def endpoints_for(settings: dict, mode: str) -> list:
    """Resolve the endpoint list for a mode as (name, address, port) tuples."""
    resolved = []
    for ep in settings["endpoints"]:
        port = ep.get("sitl_port", ep["port"]) if mode == "sitl" else ep["port"]
        resolved.append((ep["name"], ep.get("address", "127.0.0.1"), int(port)))
    return resolved


def build_conf(settings: dict, mode: str, device: str = "", baud: int = 0) -> str:
    """Build a mavlink-routerd .conf for the given mode."""
    lines = ["[General]", "TcpServerPort = 0", ""]

    if mode in SERIAL_MODES:
        lines += ["[UartEndpoint vehicle]", f"Device = {device}", f"Baud = {baud}", ""]
    else:
        lines += [
            "[UdpEndpoint vehicle]",
            "Mode = Server",
            "Address = 0.0.0.0",
            f"Port = {int(settings['sitl']['listen_port'])}",
            "",
        ]

    endpoints = endpoints_for(settings, mode) + [("monitor", "127.0.0.1", MONITOR_PORT)]
    for i, (name, address, port) in enumerate(endpoints):
        lines += [
            f"[UdpEndpoint out{i}]",
            f"# {name}",
            "Mode = Normal",
            f"Address = {address}",
            f"Port = {port}",
            "",
        ]
    return "\n".join(lines)
