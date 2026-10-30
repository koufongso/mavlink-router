import os
import sys
from pathlib import Path

from PyQt6.QtGui import QIcon
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication

from .app import MainWindow

# One instance per user: launching again shows the existing window
INSTANCE_NAME = f"mavlink-router-app-{os.getuid()}"


def show_running_instance() -> bool:
    """Ask an already running instance to show itself. True if there was one."""
    socket = QLocalSocket()
    socket.connectToServer(INSTANCE_NAME)
    if not socket.waitForConnected(500):
        return False
    socket.write(b"show")
    socket.waitForBytesWritten(500)
    socket.disconnectFromServer()
    return True


def main():
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


if __name__ == "__main__":
    main()
