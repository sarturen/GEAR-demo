"""Application entry point."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from .config import load_settings
from .ui.common import ui_font
from .ui.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("GEAR 测试台")
    app.setOrganizationName("GEAR")
    app.setFont(ui_font())
    window = MainWindow(load_settings())
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
