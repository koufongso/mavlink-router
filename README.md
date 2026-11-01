# MAVLink Router (GUI)

A small window for starting, stopping and checking [mavlink-router](https://github.com/mavlink-router/mavlink-router)
(`mavlink-routerd`). Pick a mode, click **Start**, and the router fans the vehicle link out to QGC, the
ground-station hub and any other tool over UDP.

The window title shows the router state ("MAVLink Router: running"). Closing the window (or Ctrl+Q) quits the app
after a confirmation and stops the router.
Launching the app again while it's open just brings the existing window to the front.

The router itself runs **detached** under a supervisor, so if the app crashes the link stays up; start the app again
and it picks up the running router.

It also **reconnects by itself**: `mavlink-routerd` exits when its serial device is unplugged, so the app runs it under
a small supervisor (`supervisor.sh`) that waits for the device to come back and starts the router again. You can also
press Start before plugging in the radio.

```
              ┌──────── mavlink-routerd ────────┐
SiK radio ────┤                                 ├─► UDP 14550  QGC
Pixhawk USB ──┤  (one input, chosen by mode)    ├─► UDP 14560  Hub
PX4 SITL ─────┤                                 ├─► UDP 14552  Groundstation
              └─────────────────────────────────┴─► UDP 14599  this app (heartbeat monitor)
```

## Modes

| Mode | Vehicle input | Notes |
|------|---------------|-------|
| Flight (SiK radio) | the ground radio, default `/dev/ttyUSB0` @ 57600 | Also used for **HITL** (see below). Warns if the port looks like a Pixhawk. |
| Bench (Pixhawk USB) | the Pixhawk USB, default `/dev/ttyACM0` | Bench work without radios. |
| SITL (UDP) | UDP server on 14550 (PX4 SITL's GCS link) | The router takes 14550, so QGC moves to **14570**: in QGC add a UDP comm link listening on 14570. |

Pick ports by their `/dev/serial/by-id` name (shown as e.g. "FTDI FT230X Basic UART (ttyUSB0)"): it stays the same
after a replug, while `ttyACM0` can come back as `ttyACM1`.

## QGroundControl setup

QGC should only talk to the router over UDP, never open the serial ports itself. Otherwise it grabs the port as soon as
you plug in, and the router (or the HITL bridge) can't open it.

In QGC: **Application Settings → General → AutoConnect to the following devices**: uncheck **Pixhawk**, **SiK Radio**,
**LibrePilot** and **RTK GPS**; keep **UDP** checked.

If a port is still taken, the app shows who has it: "ttyUSB0 held by QGroundControl (pid ...)".

## HITL

Run HITL like a real flight: the Pixhawk USB carries only the simulation, the ground radio carries operations.

```
Pixhawk USB  (/dev/ttyACM0) ──► hitl_bridge ⇄ Gazebo                  (bridge opens the port directly, no router)
Pixhawk TELEM1 ~ SiK ~ ground SiK (/dev/ttyUSB0) ──► this app, Flight mode ──► QGC / hub
```

## Status

- **Router**: running/stopped, pid and uptime (since the last (re)connect). Orange: waiting to (re)start, or a
  `mavlink-routerd` started outside this app is running.
- **Vehicle port**: the serial device is actually open by the router (or the SITL UDP port); "unplugged, waiting" while
  the device is gone; "held by ..." when another program has it open.
- **Vehicle heartbeat**: the vehicle is actually heard through the router: its sysid and heartbeat rate (GCS heartbeats
  are ignored). Red when no heartbeat for 3 s. Flight mode, arming etc. are left to QGC.
- **Endpoints**: green when something on this machine is listening on that UDP port (e.g. QGC is open).

Problems you need to fix yourself (no permission for the serial port, port busy, UDP port already taken) pop up once
as a message with what to do. Unplugging doesn't: the router just waits and reconnects.

## CLI (SSH / headless)

```bash
python3 -m link_manager cli
```

The GUI in a terminal: pick the mode, port and baud, press **s** to start, and watch the router, vehicle port,
heartbeat, traffic and endpoints update live. Without a display (plain SSH), `python3 -m link_manager` opens it
directly. With the AppImage (once rebuilt): `./MAVLink_Router-x86_64.AppImage cli`.

| Key | |
|-----|---|
| ↑ ↓ / Tab | select a field |
| ← → | change it (mode, port, baud from the list) |
| Enter | type a value (a port by path or name like `ttyUSB0`, any baud, the SITL UDP port) |
| s / x | start / stop the router |
| r | rescan serial ports |
| l | show the router log |
| d | dismiss error messages |
| q | quit: asks whether to stop the router or keep it running |

It only needs Python 3 and `mavlink-routerd` (it uses Python's built-in curses, no PyQt6).

It drives the same detached router, settings and state files as the GUI, so you can mix them: start the router over
SSH and the GUI shows it running, or the other way round. Because the router is detached, a dropped SSH connection
doesn't drop the vehicle link; open the CLI again and it picks up the running router.

Only one program can listen on the monitor port (14599), so with both the GUI and the CLI open, the heartbeat and
traffic show in the one opened first; the other says "port occupied" and takes over within a second
once that one closes. Everything else works the same in both.

## Configuration

Settings live in `~/.config/mavlink-router-app/settings.json` (**Open config folder** in the app); it's written with the
defaults on first run. Edit `endpoints` to add/remove tools; each entry takes `name`, `port`, optional `address`
(default `127.0.0.1`) and optional `sitl_port` (used instead of `port` in SITL mode).

Runtime files (generated `active.conf`, the router output `router.log`, `supervisor.sh`, pid file) are in
`~/.local/state/mavlink-router-app/`.

## Run from source

```bash
pip install -r requirements.txt   # PyQt6
python3 -m link_manager
```

Needs `mavlink-routerd` on `PATH`.

## Build the AppImage

```bash
pip install pyinstaller
# appimagetool: https://github.com/AppImage/appimagetool/releases
APPIMAGETOOL=/path/to/appimagetool ./packaging/build_appimage.sh
```

Produces `MAVLink_Router-x86_64.AppImage`, which bundles the GUI and the `mavlink-routerd` from your `PATH` (or
`ROUTERD=/path/to/mavlink-routerd`). On first start the bundled router is copied to
`~/.cache/mavlink-router-app/` so it keeps running after the AppImage closes.
