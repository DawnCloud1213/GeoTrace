"""GeoTrace V2.0 主窗口 — 全屏地图 + 浮动毛玻璃侧边栏.

布局:
  - CentralWidget: MapWidget (全屏地图，始终可见)
  - 浮动: FloatingSidebar (左侧毛玻璃面板，QTabWidget 包含省份列表/照片网格)
  - 浮动: SettingsPanel (右侧毛玻璃面板，叠加在地图右上角)

视图路由变化:
  - provinceClicked → 地图平滑飞行 + 左侧切到照片 Tab + 加载瀑布流.
  - clusterClicked → 左侧切到照片 Tab + 按 ID 加载.
"""

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QPropertyAnimation, QEasingCurve, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QStatusBar,
    QWidget,
)

from geotrace.core.spatial import SpatialIndex
from geotrace.database.manager import DatabaseManager
from geotrace.ui.floating_sidebar import FloatingSidebar
from geotrace.ui.map_widget import MapWidget
from geotrace.ui.photo_viewer import PhotoViewer
from geotrace.ui.settings_panel import SettingsPanel
from geotrace.ui.theme import Colors, Fonts, liquid_glass_shadow_effect

logger = logging.getLogger(__name__)

_DEFAULT_DATA_DIR = Path(__file__).parent.parent.parent / "data"
_DEFAULT_GEOJSON = _DEFAULT_DATA_DIR / "china_provinces.geojson"
_DEFAULT_DB = _DEFAULT_DATA_DIR / "geotrace_index.db"


class MainWindow(QMainWindow):
    """GeoTrace V2.0 主窗口 — 全屏地图 + 浮动侧边栏."""

    _REQUEST_MAP_REFRESH = Signal()

    def __init__(
        self,
        db_path: str | Path | None = None,
        geojson_path: str | Path | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("GeoTrace (迹点) V2.0 — 照片地理聚合")
        self.resize(1600, 900)
        self.setMinimumSize(1200, 600)

        # 程序化窗口图标
        icon_pixmap = QPixmap(64, 64)
        icon_pixmap.fill(QColor(Colors.ACCENT_PRIMARY))
        self.setWindowIcon(QIcon(icon_pixmap))

        # 确保数据目录存在
        _DEFAULT_DATA_DIR.mkdir(parents=True, exist_ok=True)

        self._db_path = str(db_path or _DEFAULT_DB)
        self._geojson_path = str(geojson_path or _DEFAULT_GEOJSON)

        # 后端服务
        self._db = DatabaseManager(self._db_path)
        self._spatial_index: SpatialIndex | None = None
        self._init_spatial_index()

        # ── 中央地图 (始终可见) ──
        self._map_view = MapWidget()
        self.setCentralWidget(self._map_view)

        # ── 浮动左侧面板 ──
        self._floating_sidebar = FloatingSidebar(self._db, self._map_view)
        self._floating_sidebar.set_frosted_alpha(self._floating_sidebar._tier.tint_alpha)

        # ── 浮动设置面板 (parent=地图, 叠加在地图右上角) ──
        self._settings_panel = SettingsPanel(self._map_view)
        self._init_settings_panel()
        self._settings_panel.set_frosted_alpha(self._settings_panel._tier.tint_alpha)


        # 状态栏
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._photo_count_label = QLabel("照片总数: 0")
        self._photo_count_label.setFont(Fonts.ui(9))
        self._photo_count_label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY};")
        self._status_bar.addPermanentWidget(self._photo_count_label)

        # 菜单栏
        self._setup_menus()

        # 信号连接
        self._connect_signals()

        # 初始化地图
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
                    self, "地图数据错误",
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
        toggle_sidebar_action = view_menu.addAction("切换侧边栏(&B)")
        toggle_sidebar_action.setShortcut("Ctrl+B")
        toggle_sidebar_action.triggered.connect(self._toggle_floating_sidebar)
        map_action = view_menu.addAction("聚焦地图(&M)")
        map_action.setShortcut("Ctrl+M")
        map_action.triggered.connect(self._focus_map)

        # 帮助菜单
        help_menu = menu_bar.addMenu("帮助(&H)")
        about_action = help_menu.addAction("关于(&A)")
        about_action.triggered.connect(self._on_about)

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
        bridge.provinceClicked.connect(self._enter_province_view)
        bridge.unclassifiedClicked.connect(self._on_unclassified_clicked)

        # 照片网格双击打开大图
        self._floating_sidebar._photo_grid.photoDoubleClicked.connect(
            self._on_photo_double_clicked
        )

        # 照片网格返回全国视图
        self._floating_sidebar._photo_grid.returnToMap.connect(
            self._exit_province_view
        )

        # 地图浮动按钮
        self._map_view.toggleProvinceList.connect(self._toggle_floating_sidebar)
        self._map_view.toggleSettings.connect(self._toggle_settings)
        self._settings_panel.closeRequested.connect(self._settings_panel.hide)
        self._floating_sidebar.closeRequested.connect(
            self._floating_sidebar.hide
        )

        # 省份列表点击
        self._floating_sidebar.provinceClicked.connect(
            self._enter_province_view
        )

        # 地图返回按钮
        self._map_view.backToNational.connect(self._exit_province_view)

        # 设置操作
        self._settings_panel.addDirectory.connect(self._on_add_directory)
        self._settings_panel.removeDirectory.connect(self._on_remove_directory)
        self._settings_panel.rescanRequested.connect(self._on_rescan_all)
        self._settings_panel.thumbnailToggleChanged.connect(
            self._on_thumbnail_toggle_changed
        )
        self._settings_panel.frostedAlphaChanged.connect(
            self._on_frosted_alpha_changed
        )

        # 聚类点击
        self._map_view.clusterClicked.connect(self._on_cluster_clicked)

        self._map_view.viewChanged.connect(self._on_view_changed)

    # ------------------------------------------------------------------
    # 浮动侧边栏控制
    # ------------------------------------------------------------------

    def _toggle_floating_sidebar(self) -> None:
        """切换左侧浮动侧边栏的显示/隐藏."""
        if self._floating_sidebar.isVisible():
            self._animate_slide_out(self._floating_sidebar, to_left=True)
        else:
            self._show_panel_with_shadow(self._floating_sidebar)
            mh = self._map_view.height()
            mw = self._map_view.width()
            sidebar_w = self._floating_sidebar.width() or 300
            self._floating_sidebar.setGeometry(
                8, 40, sidebar_w, max(mh - 60, 300)
            )
            self._animate_slide_in(self._floating_sidebar, from_left=True)

    def _focus_map(self) -> None:
        """收起浮动侧边栏聚焦地图."""
        if self._floating_sidebar.isVisible():
            self._animate_slide_out(self._floating_sidebar, to_left=True)

    # ------------------------------------------------------------------
    # 浮动面板 (设置面板)
    # ------------------------------------------------------------------

    def _toggle_settings(self) -> None:
        if self._settings_panel.isVisible():
            self._animate_slide_out(self._settings_panel, to_left=False)
        else:
            self._show_panel_with_shadow(self._settings_panel)
            mw = self._map_view.width()
            mh = self._map_view.height()
            self._settings_panel.setGeometry(
                mw - 258, 40, 250, max(mh - 60, 200)
            )
            self._animate_slide_in(self._settings_panel, from_left=False)

    def _show_panel_with_shadow(self, panel: QWidget) -> None:
        if panel.graphicsEffect() is None:
            panel.setGraphicsEffect(liquid_glass_shadow_effect())

    def _animate_slide_in(self, panel: QWidget, from_left: bool = True) -> None:
        target_geo = panel.geometry()
        if from_left:
            start_geo = target_geo.translated(-target_geo.width(), 0)
        else:
            start_geo = target_geo.translated(target_geo.width(), 0)
        panel.setGeometry(start_geo)
        panel.show()
        panel.raise_()

        anim = QPropertyAnimation(panel, b"geometry")
        anim.setDuration(350)
        anim.setStartValue(start_geo)
        anim.setEndValue(target_geo)
        # OutBack = spring-like overshoot (Apple HIG: ~0.825 damping)
        curve = QEasingCurve(QEasingCurve.OutBack)
        anim.setEasingCurve(curve)
        anim.finished.connect(lambda: panel.capture_backdrop_now())
        anim.start()
        panel._slide_anim = anim  # type: ignore[attr-defined]

    def _animate_slide_out(self, panel: QWidget, to_left: bool = True) -> None:
        current_geo = panel.geometry()
        if to_left:
            end_geo = current_geo.translated(-current_geo.width(), 0)
        else:
            end_geo = current_geo.translated(current_geo.width(), 0)

        anim = QPropertyAnimation(panel, b"geometry")
        anim.setDuration(250)
        anim.setStartValue(current_geo)
        anim.setEndValue(end_geo)
        # InCubic = smooth exit, no bounce (Apple HIG: exit faster than enter)
        anim.setEasingCurve(QEasingCurve.InCubic)
        anim.finished.connect(panel.hide)
        anim.start()
        panel._slide_anim = anim  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # 视图交互
    # ------------------------------------------------------------------

    @Slot(str)
    def _enter_province_view(self, province_name: str) -> None:
        """进入省份放大视图: 地图飞行 + 加载缩略图标记 + 侧边栏照片 Tab."""
        if not province_name:
            return
        logger.info("进入省份视图: %s", province_name)
        self._settings_panel.hide()
        photos = self._db.get_photo_coords(province_name)
        self._map_view.enter_province_view(province_name, photos)
        self._floating_sidebar._photo_grid.load_province(province_name)
        self._floating_sidebar.switch_to_photos_tab()
        if not self._floating_sidebar.isVisible():
            self._toggle_floating_sidebar()

    @Slot()
    def _exit_province_view(self) -> None:
        """返回全国地图视图: 隐藏设置面板，清空照片聚类."""
        logger.info("返回全国视图")
        self._settings_panel.hide()
        self._map_view.exit_province_view()
        self._floating_sidebar.switch_to_provinces_tab()
        if self._map_view._canvas._force_thumbnail_mode:
            try:
                photos = self._db.get_photo_coords()
                self._map_view.set_photo_coords(photos)
                self._map_view._canvas.set_cluster_mode("thumbnail")
            except Exception as e:
                logger.warning("加载全国照片坐标失败: %s", e)

    @Slot()
    def _on_unclassified_clicked(self) -> None:
        """显示未分类照片."""
        self._floating_sidebar._photo_grid.load_province("Unclassified")
        self._floating_sidebar.switch_to_photos_tab()
        if not self._floating_sidebar.isVisible():
            self._toggle_floating_sidebar()

    @Slot(bool)
    def _on_thumbnail_toggle_changed(self, enabled: bool) -> None:
        self._map_view._canvas.set_force_thumbnail_mode(enabled)
        if self._map_view._canvas._view_mode != "national":
            return
        if enabled:
            photos = self._db.get_photo_coords()
            if not photos:
                return
            self._map_view.set_photo_coords(photos)
        else:
            self._map_view.set_photo_coords([])

    @Slot()
    def _on_view_changed(self) -> None:
        """Map drag/zoom — real-time Liquid Glass refresh every frame.

        When both floating panels are visible, one grabFramebuffer read
        serves both — halving the GPU→CPU bandwidth per frame.
        """
        side_vis = self._floating_sidebar.isVisible()
        sett_vis = self._settings_panel.isVisible()

        if side_vis and sett_vis:
            self._capture_shared_live_backdrop()
        else:
            if sett_vis:
                self._settings_panel.request_backdrop_live()
            if side_vis:
                self._floating_sidebar.request_backdrop_live()

    def _capture_shared_live_backdrop(self) -> None:
        """One grabFramebuffer → crop → refract for both floating panels.

        Normalises the DPR-scaled framebuffer capture to widget pixels
        so that crop rects (which are in widget coords) align correctly.
        """
        from PySide6.QtGui import QPixmap
        from PySide6.QtCore import QPoint, QRect, QSize

        canvas = self._map_view._canvas
        if not canvas or not canvas.isVisible():
            return

        side = self._floating_sidebar
        sett = self._settings_panel

        # Frame skip: shared path also skips every other frame
        if not hasattr(self, '_shared_live_frame'):
            self._shared_live_frame = 0
        self._shared_live_frame += 1
        if self._shared_live_frame % 2 == 0:
            return

        # Union rect in map_widget coords
        side_geo = side.geometry()
        sett_geo = sett.geometry()
        union = side_geo.united(sett_geo)
        if union.isEmpty():
            return

        # Map union to GL widget coords
        gl_offset = canvas.mapFrom(self._map_view, QPoint(0, 0))
        gl_union = QRect(union.topLeft() + gl_offset, union.size())
        gl_union = gl_union.intersected(canvas.rect())
        if gl_union.isEmpty():
            return

        # One framebuffer grab for the union region
        try:
            canvas.makeCurrent()
            fb = canvas.grabFramebuffer()
            canvas.doneCurrent()
        except Exception:
            return

        if fb.isNull():
            return

        fb = fb.mirrored(False, True)  # GL origin → image origin
        dpr = fb.devicePixelRatioF() or 1.0

        # Crop at framebuffer (DPR) resolution, then normalise to widget px
        if dpr != 1.0:
            fb_src = QRect(
                int(gl_union.x() * dpr), int(gl_union.y() * dpr),
                int(gl_union.width() * dpr), int(gl_union.height() * dpr))
            raw = QPixmap.fromImage(fb.copy(fb_src))
            # Scale to widget pixels so crop rects match
            raw = raw.scaled(gl_union.size(),
                             Qt.IgnoreAspectRatio,
                             Qt.SmoothTransformation)
        else:
            raw = QPixmap.fromImage(fb.copy(gl_union))

        if raw.isNull():
            return

        # Crop & deliver to each panel (all coords now in widget pixels)
        # -- Sidebar (left panel) --
        side_src = QRect(
            side_geo.topLeft() - union.topLeft(), side_geo.size())
        side_crop = raw.copy(side_src.intersected(raw.rect()))
        if not side_crop.isNull():
            side.apply_live_backdrop(side_crop, side_geo.size())

        # -- Settings panel (right panel) --
        sett_src = QRect(
            sett_geo.topLeft() - union.topLeft(), sett_geo.size())
        sett_crop = raw.copy(sett_src.intersected(raw.rect()))
        if not sett_crop.isNull():
            sett.apply_live_backdrop(sett_crop, sett_geo.size())

    @Slot(int)
    def _on_frosted_alpha_changed(self, value: int) -> None:
        """毛玻璃透明度滑块调节 — 作为各 tier 基础 alpha 的乘数."""
        multiplier = value / 100.0
        sidebar_alpha = self._floating_sidebar._tier.tint_alpha * multiplier
        settings_alpha = self._settings_panel._tier.tint_alpha * multiplier
        self._map_view.set_frosted_alpha(sidebar_alpha)
        self._settings_panel.set_frosted_alpha(settings_alpha)
        self._floating_sidebar.set_frosted_alpha(sidebar_alpha)

    @Slot(list)
    def _on_cluster_clicked(self, ids: list[int]) -> None:
        """聚类点击：加载对应照片到 sidebar 照片 Tab."""
        if not ids:
            return
        self._floating_sidebar._photo_grid.load_photo_ids(
            ids, title="选中区域"
        )
        self._floating_sidebar.switch_to_photos_tab()
        if not self._floating_sidebar.isVisible():
            self._toggle_floating_sidebar()

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
        self._floating_sidebar.refresh_province_list(stats)

        total = self._db.get_total_photo_count()
        self._photo_count_label.setText(f"照片总数: {total}")

    def _refresh_map_photos(self) -> None:
        """根据当前视图模式刷新地图上的照片标记."""
        current = self._map_view._canvas._current_province
        if current:
            try:
                photos = self._db.get_photo_coords(current)
                self._map_view.set_photo_coords(photos)
            except Exception as e:
                logger.warning("加载省份照片坐标失败: %s", e)
        elif self._map_view._canvas._force_thumbnail_mode:
            try:
                photos = self._db.get_photo_coords()
                self._map_view.set_photo_coords(photos)
            except Exception as e:
                logger.warning("加载全国照片坐标失败: %s", e)
        else:
            self._map_view.set_photo_coords([])

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
        self._refresh_map_photos()

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
        """启动异步扫描任务."""
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
            "<h3>GeoTrace (迹点) V2.0</h3>"
            "<p>离线照片地理聚合与检索应用</p>"
            "<p>基于 Python + PySide6 构建，Web Mercator 瓦片引擎 + 原生 QPainter 叠加</p>",
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
        self._refresh_map_photos()
        # 扫描完成后自动生成缺失缩略图，确保 thumbnail_path 被填充
        self._on_generate_thumbnails()

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
