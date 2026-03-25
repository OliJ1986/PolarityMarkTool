"""
app.py
──────
Entry point for the PolarityMark desktop application.

Run directly:
    python app.py

Or via main.py:
    python main.py
"""
import sys
import os

# Ensure the project root is on sys.path so all package imports resolve
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication

from gui.main_window import MainWindow


def main() -> None:
    # Enable high-DPI scaling (Qt6 does this automatically, but be explicit)
    app = QApplication(sys.argv)
    app.setApplicationName("PolarityMark")
    app.setApplicationDisplayName("PolarityMark – PCB Polarity Marker Detector")
    app.setOrganizationName("PolarityMarkTool")

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()


