"""Qt-free helpers shared by the GUI and the CLI: serial ports, UDP listeners, router errors."""

import glob
import os

# Words in a /dev/serial/by-id name that mean it's an autopilot, not a radio
AUTOPILOT_HINTS = ("px4", "pixhawk", "ardupilot", "cubepilot", "fmu")
BAUD_RATES = ["57600", "115200", "230400", "460800", "921600"]
# No vehicle heartbeat for this long means the link is lost
HEARTBEAT_TIMEOUT_S = 3.0


def serial_ports() -> list:
    """(label, path) for serial devices; stable /dev/serial/by-id paths first.

    by-id paths survive a replug (ttyACM0 may come back as ttyACM1), so they
    are the ones to save and to wait for.
    """
    ports = []
    linked = set()
    for path in sorted(glob.glob("/dev/serial/by-id/*")):
        real = os.path.realpath(path)
        linked.add(real)
        ports.append((f"{by_id_label(path)} ({os.path.basename(real)})", path))
    for path in sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")):
        if path not in linked:
            ports.append((path, path))
    return ports


def by_id_label(path: str) -> str:
    """'usb-Holybro_PX4_FMU_v6C.x_3A0029-if00' -> 'Holybro PX4 FMU v6C.x'."""
    name = os.path.basename(path)
    if name.startswith("usb-"):
        name = name[4:]
    name = name.rsplit("-if", 1)[0]
    parts = name.split("_")
    if len(parts) > 1 and len(parts[-1]) >= 6 and parts[-1].isalnum():
        parts = parts[:-1]  # trailing serial number
    return " ".join(parts)


def looks_like_autopilot(device: str) -> bool:
    real = os.path.realpath(device)
    names = [os.path.basename(device)] + [
        os.path.basename(p) for p in glob.glob("/dev/serial/by-id/*") if os.path.realpath(p) == real]
    return any(hint in n.lower() for n in names for hint in AUTOPILOT_HINTS)


def udp_listen_ports() -> set:
    """Local UDP ports something on this machine is bound to."""
    ports = set()
    for path in ("/proc/net/udp", "/proc/net/udp6"):
        try:
            with open(path) as f:
                next(f)
                for line in f:
                    local = line.split()[1]
                    ports.add(int(local.rsplit(":", 1)[1], 16))
        except (OSError, StopIteration, ValueError, IndexError):
            pass
    return ports


def explain_router_error(line: str, device: str):
    """(message, specific) for router errors that need the user to act, else None.

    specific=False is the raw line as a fallback. Unplugging is expected (the
    router restarts on its own), so it isn't reported.
    """
    if "Could not open" in line and "Permission denied" in line:
        return ("No permission to open the serial port.\n\n"
                "Add yourself to the dialout group, then log out and back in:\n"
                "sudo usermod -aG dialout $USER", True)
    if "Could not open" in line and "busy" in line.lower():
        return (f"{device or 'The serial port'} is in use by another program "
                "(often QGC AutoConnect). Close it or turn off AutoConnect for serial devices in QGC.", True)
    if "Error binding" in line and "in use" in line.lower():
        port = line.split(":")[-1].split()[0] if ":" in line else "?"
        return (f"UDP port {port} is already used by another program.\n\n"
                f"In SITL mode the router needs this port for PX4; make sure QGC isn't listening on it "
                "(QGC uses 14570 in SITL mode).", True)
    if "Could not" in line or "Error" in line or "Invalid" in line:
        return line.strip(), False
    return None


def explain_router_errors(lines, device: str) -> list:
    """Messages for the errors in some router output, without repeats.

    One failure often prints several lines; the explained ones win over raw lines.
    """
    found = [e for e in (explain_router_error(line, device) for line in lines) if e]
    if any(specific for _, specific in found):
        found = [e for e in found if e[1]]
    return list(dict.fromkeys(message for message, _ in found))


def describe(holders) -> str:
    return ", ".join(f"{name} (pid {pid})" for pid, name in holders)


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
