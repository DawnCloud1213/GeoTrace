"""GeoTrace 主窗口 — QStackedWidget 视图路由与全局信号连接."""

import json
import logging
from pathlib import Path

from PySide6.QtCore import Qt, QPropertyAnimation, QEasingCurve, QPoint, QThread, Signal, Slot
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QFileDialog,
    QGraphicsOpacityEffect,
    QLabel,
    QMainWindow,
    QMenuBar,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from geotrace.core.spatial import SpatialIndex
from geotrace.database.manager import DatabaseManager
from geotrace.ui.map_widget import MapWidget
from geotrace.ui.photo_grid import PhotoGrid
from geotrace.ui.photo_viewer import PhotoViewer
from geotrace.ui.province_list import ProvinceListPanel
from geotrace.ui.settings_panel import SettingsPanel
from geotrace.ui.theme import Colors, Fonts, panel_shadow_effect

logger = logging.getLogger(__name__)

# 默认数据目录 (项目根下的 data/)
_DEFAULT_DATA_DIR = Path(__file__).parent.parent.parent / "data"
_DEFAULT_GEOJSON = _DEFAULT_DATA_DIR / "china_provinces.geojson"
_DEFAULT_DB = _DEFAULT_DATA_DIR / "geotrace_index.db"


class MainWindow(QMainWindow):
    """GeoTrace 主窗口.

    QStackedWidget 管理两个视图:
        index 0: MapView — ECharts 中国地图 + 省份列表
        index 1: PhotoGrid — 省份照片缩略图网格
    """

    _REQUEST_MAP_REFRESH = Signal()

    def __init__(
        self,
        db_path: str | Path | None = None,
        geojson_path: str | Path | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("GeoTrace (迹点) — 照片地理聚合")
        self.resize(1500, 900)
        self.setMinimumSize(1100, 600)

        # 程序化窗口图标
        icon_pixmap = QPixmap(64, 64)
        icon_pixmap.fill(QColor(Colors.ACCENT_PRIMARY))
        self.setWindowIcon(QIcon(icon_pixmap))

        # 确保数据目录存在
        _DEFAULT_DATA_DIR.mkdir(parents=True, exist_ok=True)

        self._db_path = str(db_path or _DEFAULT_DB)
        self._geojson_path = str(geojson_path or _DEFAULT_GEOJSON)

        # 初始化后端服务
        self._db = DatabaseManager(self._db_path)
        self._spatial_index: SpatialIndex | None = None
        self._init_spatial_index()

        # 初始化 UI
        self._stack = QStackedWidget()
        self._map_view = MapWidget()
        self._photo_grid = PhotoGrid(self._db)

        self._stack.addWidget(self._map_view)     # index 0
        self._stack.addWidget(self._photo_grid)   # index 1

        # 状态栏
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._photo_count_label = QLabel("照片总数: 0")
        self._photo_count_label.setFont(Fonts.ui(9))
        self._photo_count_label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY};")
        self._status_bar.addPermanentWidget(self._photo_count_label)

        # 中央: 单列全窗口
        self._central = QWidget()
        layout = QVBoxLayout(self._central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._stack)
        self.setCentralWidget(self._central)

        # 浮动面板 (叠加在中央 widget 上)
        self._province_list = ProvinceListPanel(self._central)
        self._settings_panel = SettingsPanel(self._central)
        self._init_settings_panel()

        # 菜单栏 (精简)
        self._setup_menus()

        # 连接信号
        self._connect_signals()

        # 初始化地图 (如果 GeoJSON 存在)
        self._init_map()

        # 刷新状态
        self._refresh_stats()

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def _init_spatial_index(self) -> None:
        """初始化空间索引 (GeoJSON 缺失时跳过)."""
        if Path(self._geojson_path).exists():
            try:
                self._spatial_index = SpatialIndex(self._geojson_path)
                logger.info("空间索引加载成功: %s", self._geojson_path)
            except Exception as e:
                logger.error("空间索引初始化失败: %s", e)
                QMessageBox.warning(
                    self, "空间数据错误",
                    f"无法加载地图数据:\n{e}\n\n地图功能将不可用。",
                )
        else:
            logger.warning("GeoJSON 文件不存在: %s, 地图不可用", self._geojson_path)

    def _init_map(self) -> None:
        """初始化原生地图."""
        if Path(self._geojson_path).exists():
            self._map_view.load_map(self._geojson_path)

    def _setup_menus(self) -> None:
        """创建菜单栏."""
        menu_bar = self.menuBar()

        # 文件菜单
        file_menu = menu_bar.addMenu("文件(&F)")

        import_action = file_menu.addAction("导入照片目录(&I)...")
        import_action.setShortcut("Ctrl+I")
        import_action.triggered.connect(self._on_import_directory)

        file_menu.addSeparator()

        exit_action = file_menu.addAction("退出(&X)")
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)

        # 工具菜单
        tools_menu = menu_bar.addMenu("工具(&T)")

        scan_action = tools_menu.addAction("重新扫描所有目录(&R)")
        scan_action.setShortcut("Ctrl+R")
        scan_action.triggered.connect(self._on_rescan_all)

        gen_thumbnails_action = tools_menu.addAction("生成缺失缩略图(&T)")
        gen_thumbnails_action.triggered.connect(self._on_generate_thumbnails)

        # 视图菜单
        view_menu = menu_bar.addMenu("视图(&V)")

        map_action = view_menu.addAction("地图视图(&M)")
        map_action.setShortcut("Ctrl+M")
        map_action.triggered.connect(self.show_map)

        # 帮助菜单
        help_menu = menu_bar.addMenu("帮助(&H)")

        about_action = help_menu.addAction("关于(&A)")
        about_action.triggered.connect(self._on_about)

    # ------------------------------------------------------------------
    # 设置面板初始化
    # ------------------------------------------------------------------

    def _init_settings_panel(self) -> None:
        dirs = [d["path"] for d in self._db.get_directories()]
        self._settings_panel.set_directories(dirs)

    # ------------------------------------------------------------------
    # 信号连接
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        """连接所有跨组件信号."""
        bridge = self._map_view.bridge

        bridge.mapReady.connect(self._on_map_ready)
        bridge.provinceClicked.connect(self._on_province_clicked)
        bridge.returnToMap.connect(self.show_map)
        bridge.unclassifiedClicked.connect(self._on_unclassified_clicked)

        self._photo_grid.returnToMap.connect(self.show_map)
        self._photo_grid.photoDoubleClicked.connect(self._on_photo_double_clicked)

        # 浮动面板 toggle
        self._map_view.toggleProvinceList.connect(self._toggle_province_list)
        self._map_view.toggleSettings.connect(self._toggle_settings)
        self._province_list.closeRequested.connect(self._province_list.hide)
        self._settings_panel.closeRequested.connect(self._settings_panel.hide)

        # 省份列表点击
        self._province_list.provinceClicked.connect(self._on_province_clicked)

        # 设置操作
        self._settings_panel.addDirectory.connect(self._on_add_directory)
        self._settings_panel.removeDirectory.connect(self._on_remove_directory)
        self._settings_panel.rescanRequested.connect(self._on_rescan_all)

    # ------------------------------------------------------------------
    # 浮动面板
    # ------------------------------------------------------------------

    def _toggle_province_list(self) -> None:
        if self._province_list.isVisible():
            self._animate_slide_out(self._province_list, to_left=True)
        else:
            self._settings_panel.hide()
            self._show_panel_with_shadow(self._province_list)
            cw, ch = self._central.width(), self._central.height()
            self._province_list.setGeometry(8, 40, 200, ch - 60)
            self._animate_slide_in(self._province_list, from_left=True)

    def _toggle_settings(self) -> None:
        if self._settings_panel.isVisible():
            self._animate_slide_out(self._settings_panel, to_left=False)
        else:
            self._province_list.hide()
            self._show_panel_with_shadow(self._settings_panel)
            cw, ch = self._central.width(), self._central.height()
            self._settings_panel.setGeometry(cw - 208, 40, 200, ch - 60)
            self._animate_slide_in(self._settings_panel, from_left=False)

    def _show_panel_with_shadow(self, panel) -> None:
        if panel.graphicsEffect() is None:
            panel.setGraphicsEffect(panel_shadow_effect())

    def _animate_slide_in(self, panel, from_left: bool = True) -> None:
        target_geo = panel.geometry()
        if from_left:
            start_geo = target_geo.translated(-target_geo.width(), 0)
        else:
            start_geo = target_geo.translated(target_geo.width(), 0)
        panel.setGeometry(start_geo)
        panel.show()
        panel.raise_()

        anim = QPropertyAnimation(panel, b"geometry")
        anim.setDuration(200)
        anim.setStartValue(start_geo)
        anim.setEndValue(target_geo)
        anim.setEasingCurve(QEasingCurve.OutBack)
        anim.start()
        panel._slide_anim = anim

    def _animate_slide_out(self, panel, to_left: bool = True) -> None:
        current_geo = panel.geometry()
        if to_left:
            end_geo = current_geo.translated(-current_geo.width(), 0)
        else:
            end_geo = current_geo.translated(current_geo.width(), 0)

        anim = QPropertyAnimation(panel, b"geometry")
        anim.setDuration(150)
        anim.setStartValue(current_geo)
        anim.setEndValue(end_geo)
        anim.setEasingCurve(QEasingCurve.InCubic)
        anim.finished.connect(panel.hide)
        anim.start()
        panel._slide_anim = anim

    # ------------------------------------------------------------------
    # 视图切换
    # ------------------------------------------------------------------

    @Slot()
    def show_map(self) -> None:
        """切换到地图视图."""
        self._animate_view_switch(0)
        self._refresh_stats()

    @Slot(str)
    def _on_province_clicked(self, province_name: str) -> None:
        """处理省份点击: 切换到照片网格."""
        if not province_name:
            return
        self._province_list.hide()
        self._settings_panel.hide()
        logger.info("切换到省份: %s", province_name)
        self._photo_grid.load_province(province_name)
        self._animate_view_switch(1)

    @Slot()
    def _on_unclassified_clicked(self) -> None:
        """显示未分类照片."""
        self._photo_grid.load_province("Unclassified")
        self._animate_view_switch(1)

    def _animate_view_switch(self, target_index: int) -> None:
        if self._stack.currentIndex() == target_index:
            return

        effect = QGraphicsOpacityEffect(self._stack)
        self._stack.setGraphicsEffect(effect)

        anim = QPropertyAnimation(effect, b"opacity")
        anim.setDuration(150)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.InCubic)

        def on_faded_out():
            self._stack.setCurrentIndex(target_index)
            anim2 = QPropertyAnimation(effect, b"opacity")
            anim2.setDuration(150)
            anim2.setStartValue(0.0)
            anim2.setEndValue(1.0)
            anim2.setEasingCurve(QEasingCurve.OutCubic)
            anim2.finished.connect(lambda: self._stack.setGraphicsEffect(None))
            anim2.start()
            self._view_fade_anim2 = anim2

        anim.finished.connect(on_faded_out)
        anim.start()
        self._view_fade_anim = anim

    @Slot(str, object, int)
    def _on_photo_double_clicked(
        self, file_path: str, all_paths: list, index: int,
    ) -> None:
        """打开大图查看器."""
        viewer = PhotoViewer(file_path, all_paths, index, self)
        viewer.exec()

    @Slot()
    def _on_map_ready(self) -> None:
        """地图加载完毕, 推送数据."""
        self._refresh_stats()

    # ------------------------------------------------------------------
    # 数据刷新
    # ------------------------------------------------------------------

    def _refresh_stats(self) -> None:
        """刷新省份统计并推送到地图和省份列表."""
        stats = self._db.get_province_stats_as_list()
        self._map_view.update_stats(stats)
        self._province_list.refresh(stats)

        total = self._db.get_total_photo_count()
        self._photo_count_label.setText(f"照片总数: {total}")

    # ------------------------------------------------------------------
    # 菜单动作
    # ------------------------------------------------------------------

    @Slot()
    def _on_import_directory(self) -> None:
        """菜单导入: 打开文件对话框选择目录."""
        directory = QFileDialog.getExistingDirectory(self, "选择照片目录")
        if directory:
            self._on_add_directory(directory)

    @Slot(str)
    def _on_add_directory(self, directory: str) -> None:
        """添加照片目录并开始扫描."""
        if not directory:
            return
        if self._spatial_index is None:
            QMessageBox.warning(
                self, "无法扫描",
                "空间索引未加载, 请先放置 china_provinces.geojson 到 data/ 目录。",
            )
            return

        self._status_bar.showMessage(f"正在扫描: {directory} ...")
        self._run_scan([directory])

    @Slot(str)
    def _on_remove_directory(self, path: str) -> None:
        """移除照片目录及其关联数据."""
        self._db.remove_directory(path)
        self._init_settings_panel()
        self._refresh_stats()

    def _on_rescan_all(self) -> None:
        """重新扫描所有已注册目录."""
        dirs = self._db.get_directories()
        if not dirs:
            QMessageBox.information(self, "提示", "没有已注册的扫描目录, 请先导入照片目录。")
            return

        if self._spatial_index is None:
            QMessageBox.warning(self, "无法扫描", "空间索引未加载。")
            return

        paths = [d["path"] for d in dirs]
        self._status_bar.showMessage("正在重新扫描所有目录...")
        self._run_scan(paths)

    def _run_scan(self, directories: list[str]) -> None:
        """启动异步扫描任务.

        (延迟导入避免循环依赖——workers 依赖本模块的 signal)
        """
        from geotrace.workers.scan import ScanWorker

        self._scan_thread = QThread()
        self._scan_worker = ScanWorker(
            self._db, self._spatial_index, directories,
        )
        self._scan_worker.moveToThread(self._scan_thread)

        self._scan_thread.started.connect(self._scan_worker.run)
        self._scan_worker.finished.connect(self._scan_thread.quit)
        self._scan_worker.finished.connect(self._scan_worker.deleteLater)
        self._scan_thread.finished.connect(self._scan_thread.deleteLater)

        self._scan_worker.progress.connect(self._on_scan_progress)
        self._scan_worker.scanComplete.connect(self._on_scan_complete)
        self._scan_worker.error.connect(self._on_scan_error)

        self._scan_thread.start()

    def _on_generate_thumbnails(self) -> None:
        """生成缺失缩略图."""
        from geotrace.workers.thumbnail import ThumbnailWorker

        self._thumb_thread = QThread()
        self._thumb_worker = ThumbnailWorker(self._db)
        self._thumb_worker.moveToThread(self._thumb_thread)

        self._thumb_thread.started.connect(self._thumb_worker.run)
        self._thumb_worker.finished.connect(self._thumb_thread.quit)
        self._thumb_worker.finished.connect(self._thumb_worker.deleteLater)
        self._thumb_thread.finished.connect(self._thumb_thread.deleteLater)

        self._thumb_worker.progress.connect(self._on_scan_progress)
        self._thumb_worker.error.connect(self._on_scan_error)

        self._thumb_thread.start()

    def _on_about(self) -> None:
        QMessageBox.about(
            self, "关于 GeoTrace (迹点)",
            "<h3>GeoTrace (迹点) v0.1.0</h3>"
            "<p>离线照片地理聚合与检索应用</p>"
            "<p>基于 Python + PySide6 构建，原生 QPainter 手绘地图</p>",
        )

    # ------------------------------------------------------------------
    # Worker 回调 (主线程)
    # ------------------------------------------------------------------

    @Slot(int, int)
    def _on_scan_progress(self, current: int, total: int) -> None:
        self._status_bar.showMessage(f"处理中: {current} / {total}")
        self._settings_panel.set_progress(current, total)

    @Slot(int, int)
    def _on_scan_complete(self, new_count: int, total_count: int) -> None:
        self._status_bar.showMessage(
            f"扫描完成 — 新增 {new_count} 张, 共 {total_count} 张", 5000,
        )
        self._settings_panel.hide_progress()
        self._init_settings_panel()
        self._refresh_stats()

    @Slot(str)
    def _on_scan_error(self, message: str) -> None:
        logger.error("扫描错误: %s", message)
        self._status_bar.showMessage(f"错误: {message}", 10000)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        """窗口关闭时清理资源."""
        self._db.close()
        super().closeEvent(event)
