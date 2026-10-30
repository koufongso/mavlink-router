import sys
from pathlib import Path

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from .app import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("mavlink-router-app")
    app.setDesktopFileName("mavlink-router")
    icon = Path(__file__).parent / "icon.svg"
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
