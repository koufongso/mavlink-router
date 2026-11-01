import os
import sys
from pathlib import Path

from . import cli

# One instance per user: launching again shows the existing window
INSTANCE_NAME = f"mavlink-router-app-{os.getuid()}"


def show_running_instance() -> bool:
    """Ask an already running instance to show itself. True if there was one."""
    from PyQt6.QtNetwork import QLocalSocket

    socket = QLocalSocket()
    socket.connectToServer(INSTANCE_NAME)
    if not socket.waitForConnected(500):
        return False
    socket.write(b"show")
    socket.waitForBytesWritten(500)
    socket.disconnectFromServer()
    return True


def run_gui():
    # Qt is imported here so the CLI works without it (and without a display)
    from PyQt6.QtGui import QIcon
    from PyQt6.QtNetwork import QLocalServer
    from PyQt6.QtWidgets import QApplication

    from .app import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("mavlink-router-app")
    app.setDesktopFileName("mavlink-router")
    if show_running_instance():
        return

    icon = Path(__file__).parent / "icon.svg"
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    window = MainWindow()

    server = QLocalServer(app)
    QLocalServer.removeServer(INSTANCE_NAME)  # stale socket from a crashed instance
    server.listen(INSTANCE_NAME)

    def on_second_launch():
        connection = server.nextPendingConnection()
        connection.disconnected.connect(connection.deleteLater)
        window.show_existing()

    server.newConnection.connect(on_second_launch)

    window.show()
    sys.exit(app.exec())


USAGE = """usage: python3 -m link_manager [cli]

  (no argument)  the GUI; the terminal UI when there's no display (e.g. over SSH)
  cli            the terminal UI"""


def main():
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        return
    # No display: e.g. over SSH without X forwarding
    if args[:1] == ["cli"] or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            sys.exit("The terminal UI needs a terminal (with ssh, a command needs ssh -t).")
        sys.exit(cli.run())
    run_gui()


if __name__ == "__main__":
    main()
