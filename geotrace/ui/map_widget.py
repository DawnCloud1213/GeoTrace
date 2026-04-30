"""QWidget + QPainter 绘制中国地图 — Web Mercator 瓦片 + 省份叠加 + 聚类.

V2.0 架构 (三层绘制):
  1. 底层: TileManager 拼接 XYZ 瓦片 (Mercator 像素坐标系).
  2. 中层: QPainterPath 省份多边形 (WGS84 → Mercator 投影后缓存).
  3. 上层: GridClusterer 照片聚类 (屏幕像素坐标系绘制).

坐标系约定:
  - 场景坐标 = Mercator 像素 (于当前 zoom 层级).
  - 屏幕坐标 = widget 本地像素.
  - 变换: screen = scene - center_px + viewport_center
          (因为 1 Mercator 像素 ≡ 1 屏幕像素, 仅做平移).
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path

from PySide6.QtCore import Qt, QEasingCurve, QPointF, QRectF, QVariantAnimation, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF,
)
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from shapely.geometry import MultiPolygon, Point, Polygon, mapping, shape

from geotrace.ui.bridge import MapBridge
from geotrace.ui.map_animation import MapViewAnimator, compute_fit_zoom_and_center
from geotrace.ui.map_core import MercatorProjection, TileManager, MAX_ZOOM, MIN_ZOOM
from geotrace.ui.marker_cluster import ClusterRenderer, GridClusterer, ThumbnailManager
from geotrace.ui.theme import Colors

logger = logging.getLogger(__name__)

# 暖色热力图色阶
_HEAT_COLORS = [
    QColor(0xFF, 0xF3, 0xE0),
    QColor(0xFF, 0xCC, 0x80),
    QColor(0xFF, 0x98, 0x00),
    QColor(0xE6, 0x51, 0x00),
]

_BG_COLOR = QColor(0xFE, 0xF9, 0xF0)
_DEFAULT_FILL = _HEAT_COLORS[0]
_BORDER_COLOR = QColor(200, 184, 152)
_HOVER_BORDER = QColor(0xFF, 0x70, 0x43)
_HOVER_GLOW = QColor(0xFF, 0x70, 0x43, 40)

# 大陆主体 WGS84 范围 (用于初始视图)
_MAINLAND_BOUNDS = (73.5, 18.0, 135.1, 53.6)

# 标签显隐 zoom 阈值 (全国初始视图 zoom 约 4, 需在此以下即可显示)
_LABEL_ZOOM_THRESHOLD = 3.5


def _heat_color(value: int, max_val: int) -> QColor:
    if max_val <= 0:
        return _DEFAULT_FILL
    t = min(value / max_val, 1.0)
    segments = len(_HEAT_COLORS) - 1
    idx = min(int(t * segments), segments - 1)
    local_t = (t * segments) - idx
    c0, c1 = _HEAT_COLORS[idx], _HEAT_COLORS[idx + 1]
    return QColor(
        int(c0.red() + (c1.red() - c0.red()) * local_t),
        int(c0.green() + (c1.green() - c0.green()) * local_t),
        int(c0.blue() + (c1.blue() - c0.blue()) * local_t),
    )


_SUFFIXES = [
    "维吾尔自治区", "壮族自治区", "回族自治区",
    "特别行政区", "自治区", "省", "市",
]


def _abbreviate(name: str) -> str:
    for suf in _SUFFIXES:
        if name.endswith(suf):
            return name[:-len(suf)]
    return name


class _MapCanvas(QWidget):
    """手绘地图画布 — Mercator 坐标系 + 瓦片 + 省份 + 聚类."""

    provinceClicked = Signal(str)
    hoveredChanged = Signal(str)
    clusterClicked = Signal(list)  # list[int] 照片 id 列表

    def __init__(self, tile_manager: TileManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tile_manager = tile_manager

        # ── 省份数据 ──
        # WGS84 Shapely 几何体 (用于碰撞检测和投影缓存键)
        self._province_geoms: dict[str, object] = {}
        # Mercator 像素路径缓存 (key = name, value = QPainterPath)
        self._province_paths: dict[str, QPainterPath] = {}
        self._cached_zoom: float = -1.0
        self._province_colors: dict[str, QColor] = {}
        self._province_neighbors: dict[str, set[str]] = {}
        # (简称, 全名, lng, lat) — WGS84 代表点
        self._labels: list[tuple[str, str, float, float]] = []

        # ── 视图状态 (Mercator 像素) ──
        self._zoom: float = 4.0
        self._center_px: float = 0.0
        self._center_py: float = 0.0
        self._initial_view_set = False

        # ── 悬停 ──
        self._hovered: str = ""
        self._hovered_neighbors: set[str] = set()

        # ── 动画 ──
        self._label_opacity_value: float = 0.0
        self._hover_boost_value: float = 0.0
        self._label_opacity_anim = QVariantAnimation(self)
        self._label_opacity_anim.setDuration(200)
        self._label_opacity_anim.setEasingCurve(QEasingCurve.InOutCubic)
        self._label_opacity_anim.valueChanged.connect(self._on_label_opacity_changed)

        self._hover_boost_anim = QVariantAnimation(self)
        self._hover_boost_anim.setDuration(200)
        self._hover_boost_anim.setEasingCurve(QEasingCurve.InOutCubic)
        self._hover_boost_anim.valueChanged.connect(self._on_hover_boost_changed)

        self._animator = MapViewAnimator(self)
        self._animator.set_callback(self._on_anim_frame)
        self._animator.finished.connect(self._invalidate_paths)

        # ── 聚类 ──
        self._clusterer = GridClusterer(cell_px=50)
        self._cluster_renderer = ClusterRenderer(mode="badge")
        self._photo_coords: list[dict] = []  # 当前渲染的照片坐标集

        # 缩略图异步加载完成后自动触发重绘
        ThumbnailManager().thumbnailReady.connect(self.update)

        # ── 视图模式 ──
        self._view_mode: str = "national"  # "national" | "province"
        self._current_province: str | None = None

        # ── 鼠标 ──
        self._press_pos: QPointF | None = None
        self._press_center: tuple[float, float] = (0.0, 0.0)
        self._dragging = False
        self._drag_threshold = 3

        self.setMouseTracking(True)
        self.setCursor(Qt.OpenHandCursor)
        self.setMinimumSize(400, 300)

    # ------------------------------------------------------------------
    # 数据加载
    # ------------------------------------------------------------------

    def load_provinces(self, features: list[dict]) -> None:
        self._province_geoms.clear()
        self._province_paths.clear()
        self._cached_zoom = -1.0
        self._province_colors.clear()
        self._province_neighbors.clear()
        self._labels.clear()

        for feat in features:
            props = feat.get("properties", {})
            geom_data = feat.get("geometry")
            if not geom_data:
                continue
            name = (props.get("name") or props.get("NAME") or "").strip()
            if not name:
                continue
            try:
                geom = shape(geom_data)
                if geom.is_empty:
                    continue
                if not geom.is_valid:
                    geom = geom.buffer(0)
            except Exception as e:
                logger.warning("解析 '%s' 几何体失败: %s", name, e)
                continue

            self._province_geoms[name] = geom
            self._province_colors[name] = _DEFAULT_FILL

            try:
                rpt = geom.representative_point()
                self._labels.append((_abbreviate(name), name, rpt.x, rpt.y))
            except Exception:
                pass

        # 邻接关系
        names = list(self._province_geoms.keys())
        for i, name1 in enumerate(names):
            g1 = self._province_geoms[name1]
            nb: set[str] = set()
            for j, name2 in enumerate(names):
                if i == j:
                    continue
                g2 = self._province_geoms[name2]
                try:
                    if g1.touches(g2):
                        nb.add(name2)
                    elif g1.distance(g2) < 0.001 and not g1.contains(g2) and not g2.contains(g1):
                        nb.add(name2)
                except Exception:
                    pass
            self._province_neighbors[name1] = nb

        self._initial_view_set = False
        self._try_compute_initial_view()
        self.update()

    def set_province_colors(self, values: dict[str, int], max_val: int) -> None:
        max_val = max(max_val, 1)
        for name in self._province_colors:
            val = values.get(name, 0)
            self._province_colors[name] = _heat_color(val, max_val)
        self.update()

    def set_photo_coords(self, photos: list[dict]) -> None:
        """设置当前应渲染聚类的照片坐标列表."""
        self._photo_coords = photos
        self._clusterer.load_photos(photos)
        self.update()

    def highlight(self, name: str) -> None:
        """平滑飞行到指定省份的 Bounding Box."""
        geom = self._province_geoms.get(name)
        if geom is None:
            return
        bounds = geom.bounds  # (min_lng, min_lat, max_lng, max_lat)
        target_px, target_py, target_zoom = compute_fit_zoom_and_center(
            bounds[0], bounds[1], bounds[2], bounds[3],
            self.width(), self.height(),
        )
        self._animator.fly_to(
            self._center_px, self._center_py, self._zoom,
            target_px, target_py, target_zoom,
        )

    # ------------------------------------------------------------------
    # 动画帧回调
    # ------------------------------------------------------------------

    def _on_anim_frame(self, px: float, py: float, zoom: float) -> None:
        self._center_px = px
        self._center_py = py
        self._zoom = zoom
        # 动画期间不复建路径，依靠 paintEvent 中的 scale 补偿；
        # finished 信号已连接 _invalidate_paths，动画结束后自动重建精确路径。
        self._update_label_opacity_target()
        self.update()

    # ------------------------------------------------------------------
    # 路径缓存
    # ------------------------------------------------------------------

    def _invalidate_paths(self) -> None:
        self._province_paths.clear()
        self._cached_zoom = -1.0

    def _get_province_paths(self) -> dict[str, QPainterPath]:
        # 只在路径为空时重建；zoom 变化期间依靠 paintEvent 的 scale 补偿，
        # 避免动画/连续缩放时每帧重建带来的巨大开销。
        if not self._province_paths:
            self._province_paths.clear()
            for name, geom in self._province_geoms.items():
                self._province_paths[name] = self._build_mercator_path(geom, self._zoom)
            self._cached_zoom = self._zoom
        return self._province_paths

    def _build_mercator_path(self, geom, zoom: float) -> QPainterPath:
        """Shapely → QPainterPath (顶点为 Mercator 像素)."""

        def _add_rings(path: QPainterPath, poly: Polygon) -> None:
            for ring in [poly.exterior] + list(poly.interiors):
                pts = list(ring.coords)
                if len(pts) < 3:
                    continue
                qpf = QPolygonF()
                for lng, lat in pts:
                    px, py = MercatorProjection.lnglat_to_pixel(lng, lat, zoom)
                    qpf.append(QPointF(px, py))
                path.addPolygon(qpf)

        path = QPainterPath()
        path.setFillRule(Qt.OddEvenFill)
        if isinstance(geom, Polygon):
            _add_rings(path, geom)
        elif isinstance(geom, MultiPolygon):
            for poly in geom.geoms:
                _add_rings(path, poly)
        return path

    # ------------------------------------------------------------------
    # 初始视图
    # ------------------------------------------------------------------

    def _try_compute_initial_view(self) -> None:
        if (not self._initial_view_set
                and self._province_geoms
                and self.width() > 100
                and self.height() > 100):
            self._compute_initial_view()
            self._initial_view_set = True

    def _compute_initial_view(self) -> None:
        min_lng, min_lat, max_lng, max_lat = _MAINLAND_BOUNDS
        self._center_px, self._center_py, self._zoom = compute_fit_zoom_and_center(
            min_lng, min_lat, max_lng, max_lat,
            self.width(), self.height(),
        )
        self._update_label_opacity_target()

    # ------------------------------------------------------------------
    # 标签动画
    # ------------------------------------------------------------------

    def _on_label_opacity_changed(self, value: float) -> None:
        self._label_opacity_value = value
        self.update()

    def _on_hover_boost_changed(self, value: float) -> None:
        self._hover_boost_value = value
        self.update()

    def _update_label_opacity_target(self) -> None:
        target = 60.0 / 255.0 if self._zoom >= _LABEL_ZOOM_THRESHOLD else 0.0
        current = self._label_opacity_value
        if abs(current - target) < 0.001:
            return
        self._label_opacity_anim.stop()
        self._label_opacity_anim.setStartValue(current)
        self._label_opacity_anim.setEndValue(target)
        self._label_opacity_anim.start()

    def _update_hover_boost_target(self) -> None:
        target = 1.0 if self._hovered else 0.0
        current = self._hover_boost_value
        if abs(current - target) < 0.001:
            return
        self._hover_boost_anim.stop()
        self._hover_boost_anim.setStartValue(current)
        self._hover_boost_anim.setEndValue(target)
        self._hover_boost_anim.start()

    # ------------------------------------------------------------------
    # 绘制 (三层)
    # ------------------------------------------------------------------

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._try_compute_initial_view()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        # ── Layer 1: 瓦片底层 ──
        self._tile_manager.paint_tiles(
            p, self.rect(), self._zoom, self._center_px, self._center_py,
        )

        if not self._province_geoms:
            p.end()
            return

        # 中层与上层共享同一视口平移变换
        p.save()
        paths = self._get_province_paths()
        pen_w = 0.5  # 像素单位

        # 若路径缓存 zoom 与当前 zoom 不一致（动画/连续缩放期间），
        # 使用 painter scale 补偿，避免每帧重建 QPainterPath。
        if paths and abs(self._cached_zoom - self._zoom) > 0.001:
            scale = 2.0 ** (self._zoom - self._cached_zoom)
            p.translate(self.width() / 2.0, self.height() / 2.0)
            p.scale(scale, scale)
            p.translate(-self._center_px / scale, -self._center_py / scale)
        else:
            p.translate(self.width() / 2.0 - self._center_px,
                        self.height() / 2.0 - self._center_py)

        # ── Layer 2a: 填充 + 普通边框 ──
        for name, path in paths.items():
            color = self._province_colors.get(name, _DEFAULT_FILL)
            p.fillPath(path, QBrush(color))
            if name != self._hovered:
                pen = QPen(_BORDER_COLOR, pen_w)
                pen.setCosmetic(True)
                p.setPen(pen)
                p.drawPath(path)

        # ── Layer 2b: 悬停发光 (最上层防止被邻省遮挡) ──
        if self._hovered:
            for name, path in paths.items():
                if name == self._hovered:
                    glow_pen = QPen(_HOVER_GLOW, pen_w * 10)
                    glow_pen.setJoinStyle(Qt.RoundJoin)
                    glow_pen.setCosmetic(True)
                    p.setPen(glow_pen)
                    p.drawPath(path)

                    h_pen = QPen(_HOVER_BORDER, pen_w * 4)
                    h_pen.setJoinStyle(Qt.RoundJoin)
                    h_pen.setCosmetic(True)
                    p.setPen(h_pen)
                    p.drawPath(path)
                    break

        p.restore()

        # ── Layer 2c: 省份标签 (屏幕坐标系, 不走场景变换) ──
        self._paint_labels(p)

        # ── Layer 3: 聚类顶层 (屏幕坐标系, 不走场景变换) ──
        if self._photo_coords:
            clusters = self._clusterer.cluster(
                self._zoom,
                self.width(), self.height(),
                self._center_px, self._center_py,
            )
            self._cluster_renderer.paint(p, clusters)

    def _paint_labels(self, painter: QPainter) -> None:
        if not self._labels:
            return
        base_opacity = self._label_opacity_value
        boost = self._hover_boost_value
        if base_opacity < 0.004 and boost < 0.004:
            return

        # 字体大小随 zoom 对数增长
        base_font_size = max(8, min(16, int(10 * self._zoom ** 0.4)))

        painter.setRenderHint(QPainter.Antialiasing, False)
        for abbr, full_name, lng, lat in self._labels:
            px, py = MercatorProjection.lnglat_to_pixel(lng, lat, self._zoom)
            sx = px - self._center_px + self.width() / 2.0
            sy = py - self._center_py + self.height() / 2.0

            if sx < -60 or sx > self.width() + 60:
                continue
            if sy < -40 or sy > self.height() + 40:
                continue

            is_highlighted = (
                self._hovered != ""
                and (full_name == self._hovered
                     or full_name in self._hovered_neighbors)
            )

            if is_highlighted:
                label_alpha_f = base_opacity + boost * (1.0 - base_opacity)
                fs = base_font_size * (1.0 + 0.12 * boost)
            else:
                label_alpha_f = base_opacity
                fs = base_font_size

            alpha_int = int(label_alpha_f * 255)
            if alpha_int < 2:
                continue

            font = painter.font()
            font.setPixelSize(max(1, int(fs)))
            font.setBold(True)
            painter.setFont(font)

            shadow_a = min(140, alpha_int)
            painter.setPen(QColor(254, 249, 240, shadow_a))
            painter.drawText(QPointF(sx + 1, sy + 1), abbr)
            painter.setPen(QColor(80, 50, 20, alpha_int))
            painter.drawText(QPointF(sx, sy), abbr)

        painter.setRenderHint(QPainter.Antialiasing, True)

    # ------------------------------------------------------------------
    # 鼠标事件
    # ------------------------------------------------------------------

    def wheelEvent(self, event) -> None:
        dy = event.angleDelta().y()
        factor = 1.12 ** (dy / 120.0)
        delta_zoom = math.log2(factor)
        new_zoom = self._zoom + delta_zoom
        new_zoom = max(MIN_ZOOM, min(MAX_ZOOM, new_zoom))

        # 以鼠标锚点缩放: 保持鼠标下地理点不变
        mx, my = event.position().x(), event.position().y()
        old_mpx = self._center_px + (mx - self.width() / 2.0)
        old_mpy = self._center_py + (my - self.height() / 2.0)
        lng, lat = MercatorProjection.pixel_to_lnglat(old_mpx, old_mpy, self._zoom)
        new_mpx, new_mpy = MercatorProjection.lnglat_to_pixel(lng, lat, new_zoom)

        self._center_px = new_mpx - (mx - self.width() / 2.0)
        self._center_py = new_mpy - (my - self.height() / 2.0)
        self._zoom = new_zoom

        self._invalidate_paths()
        self._update_hover(event.position())
        self._update_label_opacity_target()
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._press_pos = event.position()
            self._press_center = (self._center_px, self._center_py)
            self._dragging = False
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event) -> None:
        if self._press_pos is not None:
            delta = event.position() - self._press_pos
            if not self._dragging and delta.manhattanLength() > self._drag_threshold:
                self._dragging = True
            if self._dragging:
                # 1:1 平移 (Mercator 像素 = 屏幕像素)
                self._center_px = self._press_center[0] - delta.x()
                self._center_py = self._press_center[1] - delta.y()
                self.update()
        else:
            self._update_hover(event.position())

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            if not self._dragging and self._press_pos is not None:
                mx, my = event.position().x(), event.position().y()

                # 先检测聚类点击
                if self._photo_coords:
                    clusters = self._clusterer.cluster(
                        self._zoom,
                        self.width(), self.height(),
                        self._center_px, self._center_py,
                    )
                    for c in clusters:
                        dx = mx - c.screen_x
                        dy = my - c.screen_y
                        if c.count > 1:
                            radius = max(14, int(10 + math.log2(c.count + 1) * 6))
                            if dx * dx + dy * dy <= radius * radius:
                                self.clusterClicked.emit(c.ids)
                                self._press_pos = None
                                self.setCursor(Qt.OpenHandCursor)
                                return
                        else:
                            if dx * dx + dy * dy <= (SINGLE_SIZE / 2.0 + 4) ** 2:
                                self.clusterClicked.emit(c.ids)
                                self._press_pos = None
                                self.setCursor(Qt.OpenHandCursor)
                                return

            self._press_pos = None
            self._dragging = False
            self.setCursor(Qt.OpenHandCursor)

    def leaveEvent(self, event) -> None:
        if self._hovered:
            self._hovered = ""
            self._hovered_neighbors = set()
            self._update_hover_boost_target()
            self.hoveredChanged.emit("")
            self.update()

    # ------------------------------------------------------------------
    # 悬停检测 (WGS84 Shapely 精确碰撞)
    # ------------------------------------------------------------------

    def _update_hover(self, screen_pos: QPointF) -> None:
        mpx = self._center_px + (screen_pos.x() - self.width() / 2.0)
        mpy = self._center_py + (screen_pos.y() - self.height() / 2.0)
        lng, lat = MercatorProjection.pixel_to_lnglat(mpx, mpy, self._zoom)
        point = Point(lng, lat)

        hit = ""
        for name, geom in self._province_geoms.items():
            try:
                if geom.contains(point) or point.within(geom):
                    hit = name
                    break
            except Exception:
                continue

        if hit != self._hovered:
            self._hovered = hit
            if hit and hit in self._province_neighbors:
                self._hovered_neighbors = self._province_neighbors[hit] | {hit}
            elif hit:
                self._hovered_neighbors = {hit}
            else:
                self._hovered_neighbors = set()
            self._update_hover_boost_target()
            self.hoveredChanged.emit(hit)
            self.update()

    # ------------------------------------------------------------------
    # 视图模式
    # ------------------------------------------------------------------

    def set_cluster_mode(self, mode: str) -> None:
        self._cluster_renderer.set_mode(mode)
        self.update()

    def enter_province_view(self, province_name: str, photos: list[dict]) -> None:
        self._view_mode = "province"
        self._current_province = province_name
        self._clusterer.cell_px = 30  # 省份视图下更小聚合粒度
        self.set_cluster_mode("thumbnail")
        self.set_photo_coords(photos)
        self.highlight(province_name)

    def exit_province_view(self) -> None:
        self._view_mode = "national"
        self._current_province = None
        self._clusterer.cell_px = 50  # 恢复默认
        self.set_cluster_mode("badge")
        self._photo_coords = []
        self._clusterer.load_photos([])
        self._compute_initial_view()
        self.update()

    # ------------------------------------------------------------------
    # 双击事件
    # ------------------------------------------------------------------

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            mx, my = event.position().x(), event.position().y()
            mpx = self._center_px + (mx - self.width() / 2.0)
            mpy = self._center_py + (my - self.height() / 2.0)
            lng, lat = MercatorProjection.pixel_to_lnglat(mpx, mpy, self._zoom)
            point = Point(lng, lat)
            for name, geom in self._province_geoms.items():
                try:
                    if geom.contains(point) or point.within(geom):
                        self.provinceClicked.emit(name)
                        break
                except Exception:
                    continue


# 复用 marker_cluster 常量
from geotrace.ui.marker_cluster import SINGLE_SIZE  # noqa: E402


class MapWidget(QWidget):
    """原生地图组件 — Mercator V2."""

    toggleProvinceList = Signal()
    toggleSettings = Signal()
    clusterClicked = Signal(list)  # 透传 canvas 信号
    provinceViewEntered = Signal(str)
    backToNational = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._bridge = MapBridge()
        self._geo_json_loaded = False
        self._stats_max_val = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # TileManager (支持多套 MBTiles: standard + satellite)
        data_dir = Path(__file__).parent.parent.parent / "data"
        providers: dict[str, str] = {}
        std_path = data_dir / "tiles.mbtiles"
        sat_path = data_dir / "satellite.mbtiles"
        if std_path.exists():
            providers["standard"] = str(std_path)
        if sat_path.exists():
            providers["satellite"] = str(sat_path)
        self._tile_manager = TileManager(
            mbtiles_providers=providers if providers else None,
            enable_network=True,
            placeholder_color=_BG_COLOR,
            parent=self,
        )
        self._tile_manager.tileLoaded.connect(self.update)

        self._canvas = _MapCanvas(self._tile_manager, self)
        self._canvas.provinceClicked.connect(self._on_province_clicked)
        self._canvas.hoveredChanged.connect(self._on_hovered_changed)
        self._canvas.clusterClicked.connect(self.clusterClicked.emit)
        layout.addWidget(self._canvas)

        # 浮动按钮
        self._btn_provinces = QPushButton("☰", self)
        self._btn_provinces.setFixedSize(36, 36)
        self._btn_provinces.setCursor(Qt.PointingHandCursor)
        self._btn_provinces.setToolTip("省份列表")
        self._btn_provinces.setProperty("cssClass", "mapOverlay")
        self._btn_provinces.clicked.connect(self.toggleProvinceList.emit)

        self._btn_settings = QPushButton("⚙", self)
        self._btn_settings.setFixedSize(36, 36)
        self._btn_settings.setCursor(Qt.PointingHandCursor)
        self._btn_settings.setToolTip("设置")
        self._btn_settings.setProperty("cssClass", "mapOverlay")
        self._btn_settings.clicked.connect(self.toggleSettings.emit)

        # 地图样式切换 (有多套 MBTiles 时才显示)
        self._btn_style = QPushButton("🗺", self)
        self._btn_style.setFixedSize(36, 36)
        self._btn_style.setCursor(Qt.PointingHandCursor)
        self._btn_style.setProperty("cssClass", "mapOverlay")
        if self._tile_manager.can_switch:
            self._btn_style.setToolTip("切换底图 (当前: 标准地图)")
            self._btn_style.setVisible(True)
        else:
            self._btn_style.setToolTip("切换底图 (无可用底图数据)")
            self._btn_style.setVisible(False)
        self._btn_style.clicked.connect(self._toggle_tile_style)

        # 返回全国视图按钮 (仅在省份视图显示)
        self._btn_back = QPushButton("←", self)
        self._btn_back.setFixedSize(36, 36)
        self._btn_back.setCursor(Qt.PointingHandCursor)
        self._btn_back.setToolTip("返回全国视图")
        self._btn_back.setProperty("cssClass", "mapOverlay")
        self._btn_back.hide()
        self._btn_back.clicked.connect(self._on_back_clicked)

        # 悬停提示
        self._hover_tooltip = QLabel(self)
        self._hover_tooltip.setStyleSheet(f"""
            QLabel {{
                background: rgba(255,255,255,0.90);
                border: 1px solid {Colors.BORDER_MEDIUM};
                border-radius: 4px;
                padding: 2px 8px;
                font-size: 12px;
                color: {Colors.TEXT_PRIMARY};
            }}
        """)
        self._hover_tooltip.setVisible(False)

        # 色阶图例
        self._legend = self._create_legend()

    def _create_legend(self) -> QFrame:
        from geotrace.ui.theme import Fonts as F
        legend = QFrame(self)
        legend.setObjectName("mapLegend")
        legend.setStyleSheet(f"""
            QFrame#mapLegend {{
                background: rgba(255,255,255,0.85);
                border: 1px solid {Colors.BORDER_LIGHT};
                border-radius: 6px;
                padding: 8px;
            }}
        """)

        vl = QVBoxLayout(legend)
        vl.setContentsMargins(8, 6, 8, 6)
        vl.setSpacing(2)

        title = QLabel("照片数")
        title.setFont(F.caption(9))
        title.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; border: none; background: transparent;")
        vl.addWidget(title)

        self._legend_bar = QWidget()
        self._legend_bar.setFixedSize(120, 12)
        self._legend_bar.setStyleSheet(f"""
            background: qlineargradient(x1:0, x2:1,
                stop:0 {Colors.MAP_HEAT_1},
                stop:0.33 {Colors.MAP_HEAT_2},
                stop:0.66 {Colors.MAP_HEAT_3},
                stop:1 {Colors.MAP_HEAT_4});
            border-radius: 2px;
        """)
        vl.addWidget(self._legend_bar)

        labels_row = QHBoxLayout()
        self._legend_min = QLabel("0")
        self._legend_max = QLabel("0")
        for lbl in (self._legend_min, self._legend_max):
            lbl.setFont(F.caption(8))
            lbl.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; border: none; background: transparent;")
        labels_row.addWidget(self._legend_min)
        labels_row.addStretch()
        labels_row.addWidget(self._legend_max)
        vl.addLayout(labels_row)

        legend.setVisible(False)
        return legend

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._btn_provinces.move(8, 8)
        self._btn_settings.move(self.width() - 44, 8)
        self._btn_style.move(self.width() - 44, 52)
        self._btn_back.move(8, 52)
        self._hover_tooltip.move(50, self.height() - 50)
        self._legend.move(self.width() - 150, self.height() - 100)
        self._btn_provinces.raise_()
        self._btn_settings.raise_()
        self._btn_style.raise_()
        self._btn_back.raise_()
        self._hover_tooltip.raise_()
        self._legend.raise_()

    @property
    def bridge(self) -> MapBridge:
        return self._bridge

    # ------------------------------------------------------------------
    # 地图加载
    # ------------------------------------------------------------------

    def load_map(self, geojson_path: str | Path) -> bool:
        geojson_path = Path(geojson_path)
        if not geojson_path.exists():
            logger.error("GeoJSON 不存在: %s", geojson_path)
            return False
        try:
            with open(geojson_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("GeoJSON 解析失败: %s", e)
            return False

        data = self._simplify_geojson(data, tolerance=0.02)
        self._canvas.load_provinces(data.get("features", []))
        self._geo_json_loaded = True
        logger.info("地图加载完成: %d 个省份", len(self._canvas._province_geoms))
        self._bridge.mapReady.emit()
        return True

    # ------------------------------------------------------------------
    # 数据更新
    # ------------------------------------------------------------------

    def update_stats(self, stats: list[dict]) -> None:
        if not self._geo_json_loaded:
            return
        max_val = 1
        values: dict[str, int] = {}
        for s in stats:
            name = s.get("name", "")
            val = s.get("value", 0)
            values[name] = val
            if val > max_val:
                max_val = val
        self._canvas.set_province_colors(values, max_val)

        total_photos = sum(values.values())
        self._stats_max_val = max_val
        if total_photos > 0 and max_val > 0:
            self._legend_min.setText("0")
            self._legend_max.setText(str(max_val))
            self._legend.setVisible(True)
        else:
            self._legend.setVisible(False)

    def set_photo_coords(self, photos: list[dict]) -> None:
        """向地图注入照片坐标以渲染聚类."""
        self._canvas.set_photo_coords(photos)

    def highlight(self, province_name: str) -> None:
        self._canvas.highlight(province_name)

    def enter_province_view(self, province_name: str, photos: list[dict]) -> None:
        self._canvas.enter_province_view(province_name, photos)
        self._btn_back.show()
        self._btn_back.raise_()
        self.provinceViewEntered.emit(province_name)

    def exit_province_view(self) -> None:
        self._canvas.exit_province_view()
        self._btn_back.hide()

    def _on_back_clicked(self) -> None:
        self.backToNational.emit()

    # ------------------------------------------------------------------
    # 内部回调
    # ------------------------------------------------------------------

    def _on_province_clicked(self, name: str) -> None:
        self._bridge.provinceClicked.emit(name)

    def _on_hovered_changed(self, name: str) -> None:
        if name:
            self._hover_tooltip.setText(name)
            self._hover_tooltip.adjustSize()
            self._hover_tooltip.setVisible(True)
        else:
            self._hover_tooltip.setVisible(False)

    def _toggle_tile_style(self) -> None:
        """在在线/标准/卫星底图之间循环切换."""
        if not self._tile_manager.can_switch:
            return
        next_key = self._tile_manager.cycle_provider()
        if next_key == "satellite":
            self._tile_manager._placeholder = QColor("#1A1A1A")
        else:
            self._tile_manager._placeholder = _BG_COLOR
        name_map = {
            "online": "在线地图",
            "satellite": "卫星影像",
            "standard": "标准地图",
            "placeholder": "无底图",
        }
        self._btn_style.setToolTip(
            f"切换底图 (当前: {name_map.get(next_key, next_key)})"
        )
        self.update()

    # ------------------------------------------------------------------
    # GeoJSON 简化
    # ------------------------------------------------------------------

    @staticmethod
    def _simplify_geojson(geojson: dict, tolerance: float = 0.01) -> dict:
        simplified_features = []
        for feature in geojson.get("features", []):
            geom_data = feature.get("geometry")
            if not geom_data:
                simplified_features.append(feature)
                continue
            try:
                geom = shape(geom_data)
                if geom.is_empty:
                    simplified_features.append(feature)
                    continue
                if not geom.is_valid:
                    geom = geom.buffer(0)
                simplified = geom.simplify(tolerance, preserve_topology=True)
                new_feature = dict(feature)
                new_feature["geometry"] = mapping(simplified)
            except Exception:
                new_feature = dict(feature)
            simplified_features.append(new_feature)
        return {"type": "FeatureCollection", "features": simplified_features}
