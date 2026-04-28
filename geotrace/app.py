"""GeoTrace 应用入口 — QApplication 初始化与主窗口启动."""

import logging
import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from geotrace.ui.main_window import MainWindow

# 应用元数据
APP_NAME = "GeoTrace"
APP_VERSION = "0.1.0"
ORG_NAME = "GeoTrace"

# 资源目录
_RESOURCES_DIR = Path(__file__).parent / "ui" / "resources"


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
    # 降低第三方库日志级别
    logging.getLogger("PIL").setLevel(logging.WARNING)
    logging.getLogger("shapely").setLevel(logging.WARNING)


def check_resources() -> list[str]:
    """检查必要的资源文件是否存在.

    Returns:
        缺失文件的描述列表 (为空表示一切就绪).
    """
    missing: list[str] = []

    echarts_js = _RESOURCES_DIR / "echarts.min.js"
    if not echarts_js.exists():
        missing.append(
            f"ECharts JS 库: {echarts_js}\n"
            "请从 https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js 下载"
        )

    return missing


def main() -> None:
    """应用主入口."""
    setup_logging()
    logger = logging.getLogger(__name__)

    # QWebEngine GPU 加速 (必须在 QApplication 创建前设置)
    gpu_flags = (
        "--enable-gpu-rasterization "
        "--enable-accelerated-2d-canvas "
        "--ignore-gpu-blocklist "
        "--enable-features=UseSkiaRenderer "
        "--disable-software-rasterizer"
    )
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", gpu_flags)
    if os.environ.get("GEOTRACE_DEBUG"):
        os.environ["QTWEBENGINE_REMOTE_DEBUGGING"] = "9222"

    # Qt 高 DPI 支持
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(ORG_NAME)

    # 资源检查
    missing = check_resources()
    if missing:
        msg = "以下资源文件缺失, 地图功能可能不可用:\n\n" + "\n\n".join(missing)
        logger.warning(msg)
        QMessageBox.warning(None, "资源缺失", msg)

    # 启动主窗口
    window = MainWindow()
    window.show()

    logger.info("GeoTrace (迹点) v%s 已启动", APP_VERSION)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
