# MAVLink Router (GUI)

A small window for starting, stopping and checking [mavlink-router](https://github.com/mavlink-router/mavlink-router)
(`mavlink-routerd`). Pick a mode, click **Start**, and the router fans the vehicle link out to QGC, the
ground-station hub and any other tool over UDP.

Closing the window (X) only **minimizes** it; it stays in the dock/taskbar with the state in its title
("MAVLink Router: running"). **Quit** (or Ctrl+Q) exits and asks whether to keep the router running or stop it.
Launching the app again while it's open just brings the existing window to the front.

The router runs **detached**: quitting the app (or the app crashing) never drops the link unless you choose to stop it.
Start the app again and it picks up the running router.

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

## Configuration

Settings live in `~/.config/mavlink-router-app/settings.json` (**Open config folder** in the app); it's written with the
defaults on first run. Edit `endpoints` to add/remove tools; each entry takes `name`, `port`, optional `address`
(default `127.0.0.1`) and optional `sitl_port` (used instead of `port` in SITL mode).

Runtime files (generated `active.conf`, `router.log`, `supervisor.sh`, pid file) are in
`~/.local/state/mavlink-router-app/`.

## Run from source

```bash
pip install -r requirements.txt   # PyQt6, pymavlink
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
