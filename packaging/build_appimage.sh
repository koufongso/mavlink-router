#!/usr/bin/env bash
# Build MAVLink_Router-x86_64.AppImage: the GUI (PyInstaller bundle) plus mavlink-routerd.
#
# Needs: pyinstaller (pip install pyinstaller), appimagetool (on PATH or
# APPIMAGETOOL=/path/to/appimagetool), mavlink-routerd (on PATH or
# ROUTERD=/path/to/mavlink-routerd).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$ROOT/build"
APPDIR="$BUILD/AppDir"
APPIMAGETOOL="${APPIMAGETOOL:-appimagetool}"
ROUTERD="${ROUTERD:-$(command -v mavlink-routerd || true)}"

[[ -x "$ROUTERD" ]] || { echo "mavlink-routerd not found; set ROUTERD=/path/to/mavlink-routerd" >&2; exit 1; }
command -v "$APPIMAGETOOL" >/dev/null || { echo "appimagetool not found; set APPIMAGETOOL=..." >&2; exit 1; }
command -v pyinstaller >/dev/null || { echo "pyinstaller not found; pip install pyinstaller" >&2; exit 1; }

rm -rf "$BUILD"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib"

cd "$ROOT"
pyinstaller --noconfirm --clean --windowed \
    --name mavlink-router-app \
    --distpath "$BUILD/dist" --workpath "$BUILD/work" --specpath "$BUILD" \
    --add-data "$ROOT/link_manager/icon.svg:link_manager" \
    --paths "$ROOT" \
    packaging/entry.py

cp -r "$BUILD/dist/mavlink-router-app" "$APPDIR/usr/lib/"
cp "$ROUTERD" "$APPDIR/usr/bin/mavlink-routerd"
cp packaging/mavlink-router.desktop "$APPDIR/"
cp link_manager/icon.svg "$APPDIR/mavlink-router.svg"

cat > "$APPDIR/AppRun" <<'APPRUN'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
export APPDIR="${APPDIR:-$HERE}"
exec "$HERE/usr/lib/mavlink-router-app/mavlink-router-app" "$@"
APPRUN
chmod +x "$APPDIR/AppRun"

ARCH=x86_64 "$APPIMAGETOOL" "$APPDIR" "$ROOT/MAVLink_Router-x86_64.AppImage"
echo "Built $ROOT/MAVLink_Router-x86_64.AppImage"
