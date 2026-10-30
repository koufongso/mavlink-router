# MAVLink Router (GUI)

A small window for starting, stopping and checking [mavlink-router](https://github.com/mavlink-router/mavlink-router)
(`mavlink-routerd`). Pick a mode, click **Start**, and the router fans the vehicle link out to QGC, the
ground-station hub and any other tool over UDP.

The router runs **detached**: closing this window (or the window crashing) never drops the link. Reopen it and
it picks up the running router again.

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
| Flight (SiK radio) | serial, default `/dev/ttyUSB0` @ 57600 | |
| Bench (Pixhawk USB) | serial, default `/dev/ttyACM0` | |
| SITL (UDP) | UDP server on 14550 (PX4 SITL's GCS link) | The router takes 14550, so QGC moves to **14570**: in QGC add a UDP comm link listening on 14570. |

## Status

- **Router**: running/stopped, pid and uptime. Orange means a `mavlink-routerd` started outside this app is running.
- **Vehicle port**: the serial device is actually open by the router (or the SITL UDP port).
- **Vehicle heartbeat**: the vehicle is actually heard through the router: its sysid and heartbeat rate (GCS heartbeats
  are ignored). Red when no heartbeat for 3 s. Flight mode, arming etc. are left to QGC.
- **Endpoints**: green when something on this machine is listening on that UDP port (e.g. QGC is open).

## Configuration

Settings live in `~/.config/mavlink-router-app/settings.json` (**Open config folder** in the app); it's written with the
defaults on first run. Edit `endpoints` to add/remove tools; each entry takes `name`, `port`, optional `address`
(default `127.0.0.1`) and optional `sitl_port` (used instead of `port` in SITL mode).

Runtime files (generated `active.conf`, `router.log`, pid file) are in `~/.local/state/mavlink-router-app/`.

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
