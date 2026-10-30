#!/bin/sh
# Keeps mavlink-routerd running: waits for the serial device, runs the router,
# and starts it again after it exits (mavlink-routerd exits when its serial
# device is unplugged). Plain sh so it doesn't depend on the GUI or AppImage.
#
# usage: supervisor.sh <mavlink-routerd> <conf file> <conf dir> [<device>]

ROUTERD="$1"
CONF="$2"
CONF_DIR="$3"
DEVICE="$4"
child=""

trap '[ -n "$child" ] && kill "$child" 2>/dev/null; exit 0' TERM INT

stamp() { date '+%Y-%m-%d %H:%M:%S'; }

while true; do
    if [ -n "$DEVICE" ] && [ ! -e "$DEVICE" ]; then
        echo "=== $(stamp) waiting for $DEVICE"
        while [ ! -e "$DEVICE" ]; do sleep 0.5; done
        # Give udev a moment to set the device permissions
        sleep 1
    fi

    echo "=== $(stamp) starting mavlink-routerd"
    "$ROUTERD" -c "$CONF" -d "$CONF_DIR" &
    child=$!
    wait "$child"
    echo "=== $(stamp) mavlink-routerd exited (code $?), restarting"
    child=""
    sleep 1
done
