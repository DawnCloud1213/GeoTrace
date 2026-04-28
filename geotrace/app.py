"""GeoTrace 应用入口 — QApplication 初始化与主窗口启动."""

import logging
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from geotrace.ui.main_window import MainWindow
from geotrace.ui.theme import GLOBAL_QSS

# 应用元数据
APP_NAME = "GeoTrace"
APP_VERSION = "0.1.0"
ORG_NAME = "GeoTrace"


def setup_logging(level: int = logging.INFO) -> None:
    """配置全局日志."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
        ],
    )
    logging.getLogger("PIL").setLevel(logging.WARNING)
    logging.getLogger("shapely").setLevel(logging.WARNING)


def main() -> None:
    """应用主入口."""
    setup_logging()
    logger = logging.getLogger(__name__)

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(ORG_NAME)
    app.setStyleSheet(GLOBAL_QSS)

    window = MainWindow()
    window.show()

    logger.info("GeoTrace (迹点) v%s 已启动", APP_VERSION)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
