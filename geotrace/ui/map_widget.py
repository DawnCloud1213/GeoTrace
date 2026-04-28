"""QWidget + QPainter 直接绘制中国地图 — 不依赖 QGraphicsView.

QGraphicsView 的 Item 事件分发在复杂几何场景下不可靠,
改用最底层 QPainter 手绘 + 直接鼠标事件处理, 保证交互可靠.
"""

import json
import logging
from pathlib import Path

from PySide6.QtCore import Qt, QEasingCurve, QPointF, QRectF, QVariantAnimation, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QTransform,
)
from PySide6.QtWidgets import QPushButton, QVBoxLayout, QWidget

from shapely.geometry import MultiPolygon, Polygon, mapping, shape

from geotrace.ui.bridge import MapBridge

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

# 大陆主体范围 (过滤南海诸岛对初始视图的影响)
_MAINLAND_BOUNDS = (73.5, 18.0, 135.1, 53.6)


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


def _geom_to_painter_path(geom) -> QPainterPath:
    """Shapely Polygon/MultiPolygon → QPainterPath (lng→X, lat→Y)."""

    def _add_rings(path: QPainterPath, poly: Polygon) -> None:
        for ring in [poly.exterior] + list(poly.interiors):
            pts = list(ring.coords)
            if len(pts) < 3:
                continue
            qpf = QPolygonF()
            for x, y in pts:
                qpf.append(QPointF(x, y))
            path.addPolygon(qpf)

    path = QPainterPath()
    path.setFillRule(Qt.OddEvenFill)
    if isinstance(geom, Polygon):
        _add_rings(path, geom)
    elif isinstance(geom, MultiPolygon):
        for poly in geom.geoms:
            _add_rings(path, poly)
    return path


class _MapCanvas(QWidget):
    """手绘地图画布 — 处理绘制 + 所有鼠标交互."""

    provinceClicked = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._provinces: list[tuple[str, QPainterPath]] = []
        self._colors: dict[str, QColor] = {}
        self._hovered: str = ""

        # 省份邻接关系: 全名 → 相邻省份全名集合
        self._neighbors: dict[str, set[str]] = {}
        # 当前受悬停增强的省份集合 (悬停省 + 邻居)
        self._hovered_neighbors: set[str] = set()

        # 视图状态: 场景坐标中心 和 缩放 (像素/度)
        self._cx = (_MAINLAND_BOUNDS[0] + _MAINLAND_BOUNDS[2]) / 2.0
        self._cy = (_MAINLAND_BOUNDS[1] + _MAINLAND_BOUNDS[3]) / 2.0
        self._scale = 1.0
        self._initial_view_set = False
        self._initial_scale: float = 1.0

        # 标签: [(简称, 全名, lng, lat), ...]
        self._labels: list[tuple[str, str, float, float]] = []

        # 动画状态: 当前插值
        self._label_opacity_value: float = 0.0
        self._hover_boost_value: float = 0.0

        # 鼠标状态
        self._press_pos: QPointF | None = None
        self._press_scene_center: tuple[float, float] = (0.0, 0.0)
        self._dragging = False
        self._drag_threshold = 3

        self.setMouseTracking(True)
        self.setCursor(Qt.OpenHandCursor)
        self.setMinimumSize(400, 300)

        # 标签透明度动画 (缩放跨越 110% 阈值时触发)
        self._label_opacity_anim = QVariantAnimation(self)
        self._label_opacity_anim.setDuration(200)
        self._label_opacity_anim.setEasingCurve(QEasingCurve.InOutCubic)
        self._label_opacity_anim.valueChanged.connect(self._on_label_opacity_changed)

        # 悬停增强动画 (鼠标进入/离开省份时触发)
        self._hover_boost_anim = QVariantAnimation(self)
        self._hover_boost_anim.setDuration(200)
        self._hover_boost_anim.setEasingCurve(QEasingCurve.InOutCubic)
        self._hover_boost_anim.valueChanged.connect(self._on_hover_boost_changed)

    # ------------------------------------------------------------------
    # 数据加载
    # ------------------------------------------------------------------

    def load_provinces(self, features: list[dict]) -> None:
        self._provinces.clear()
        self._colors.clear()
        self._labels.clear()
        self._neighbors.clear()

        _temp_geoms: dict[str, object] = {}

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

            _temp_geoms[name] = geom

            path = _geom_to_painter_path(geom)
            self._provinces.append((name, path))
            self._colors[name] = _DEFAULT_FILL

            # 计算标签位置 (省内代表点)
            try:
                rpt = geom.representative_point()
                self._labels.append((_abbreviate(name), name, rpt.x, rpt.y))
            except Exception:
                pass

        # 计算省份邻接关系 (touches + 距离回退)
        names = list(_temp_geoms.keys())
        for i, name1 in enumerate(names):
            g1 = _temp_geoms[name1]
            nb: set[str] = set()
            for j, name2 in enumerate(names):
                if i == j:
                    continue
                g2 = _temp_geoms[name2]
                try:
                    if g1.touches(g2):
                        nb.add(name2)
                    elif g1.distance(g2) < 0.001 and not g1.contains(g2) and not g2.contains(g1):
                        nb.add(name2)
                except Exception:
                    pass
            self._neighbors[name1] = nb

        self._initial_view_set = False
        self._try_compute_initial_view()
        self.update()

    def set_province_colors(self, values: dict[str, int], max_val: int) -> None:
        max_val = max(max_val, 1)
        for name in self._colors:
            val = values.get(name, 0)
            self._colors[name] = _heat_color(val, max_val)
        self.update()

    def highlight(self, name: str) -> None:
        """将指定省份移动到视图中心并放大."""
        for pname, path in self._provinces:
            if pname == name:
                r = path.boundingRect()
                self._cx = r.center().x()
                self._cy = r.center().y()
                # 缩放至适合窗口
                w, h = self.width(), self.height()
                if w > 0 and h > 0 and r.width() > 0:
                    self._scale = min(w / r.width(), h / r.height()) * 0.85
                self._update_label_opacity_target()
                self.update()
                return

    # ------------------------------------------------------------------
    # 坐标变换
    # ------------------------------------------------------------------

    def _make_transform(self) -> QTransform:
        """构建 场景 → 控件 的变换矩阵."""
        t = QTransform()
        t.translate(self.width() / 2.0, self.height() / 2.0)
        t.scale(self._scale, -self._scale)
        t.translate(-self._cx, -self._cy)
        return t

    def _screen_to_scene(self, sx: float, sy: float) -> tuple[float, float]:
        """屏幕坐标 → 场景坐标."""
        scx = (sx - self.width() / 2.0) / self._scale + self._cx
        scy = -(sy - self.height() / 2.0) / self._scale + self._cy
        return scx, scy

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
        if self._initial_scale <= 0:
            target = 0.0
        elif self._scale < self._initial_scale * 1.10:
            target = 0.0
        else:
            target = 60.0 / 255.0

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

    def _compute_initial_view(self) -> None:
        """计算初始视图: 大陆主体填充窗口."""
        min_x, min_y, max_x, max_y = _MAINLAND_BOUNDS
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        rw = max_x - min_x
        rh = max_y - min_y
        if rw <= 0 or rh <= 0:
            return
        self._scale = min(w / rw, h / rh) * 1.05
        self._initial_scale = self._scale
        self._cx = (min_x + max_x) / 2.0
        self._cy = (min_y + max_y) / 2.0
        self._update_label_opacity_target()

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------

    def _try_compute_initial_view(self) -> None:
        """load_provinces 和 resizeEvent 都会调用，确保无论时序如何都能算初始视图."""
        if not self._initial_view_set and self._provinces and self.width() > 100 and self.height() > 100:
            self._compute_initial_view()
            self._initial_view_set = True

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._try_compute_initial_view()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QBrush(_BG_COLOR))

        if not self._provinces:
            return

        t = self._make_transform()
        p.setTransform(t)

        # 笔宽随缩放反比
        pen_w = 0.5 / self._scale

        # Phase 1: 填充所有省份 + 绘制非悬停省份的普通边框
        for name, path in self._provinces:
            color = self._colors.get(name, _DEFAULT_FILL)
            p.fillPath(path, QBrush(color))

            if name != self._hovered:
                p.setPen(QPen(_BORDER_COLOR, pen_w))
                p.drawPath(path)

        # Phase 2: 在最上层绘制悬停省份的发光 + 加粗边框 (避免被相邻省边框覆盖)
        if self._hovered:
            for name, path in self._provinces:
                if name == self._hovered:
                    glow_pen = QPen(_HOVER_GLOW, pen_w * 10)
                    glow_pen.setJoinStyle(Qt.RoundJoin)
                    p.setPen(glow_pen)
                    p.drawPath(path)

                    h_pen = QPen(_HOVER_BORDER, pen_w * 4)
                    h_pen.setJoinStyle(Qt.RoundJoin)
                    p.setPen(h_pen)
                    p.drawPath(path)
                    break

        # 省份简称标签 — 双显示逻辑: 缩放 ≥ 110% 时半透明显示, 悬停省+邻居不透明放大
        if self._labels:
            base_opacity = self._label_opacity_value
            boost = self._hover_boost_value
            base_font_size = max(8, min(16, int(10 * self._scale ** 0.4)))

            if not (base_opacity < 0.004 and boost < 0.004):
                p.resetTransform()

                for abbr, full_name, lng, lat in self._labels:
                    sx = t.map(QPointF(lng, lat))
                    if sx.x() < -60 or sx.x() > self.width() + 60:
                        continue
                    if sx.y() < -40 or sx.y() > self.height() + 40:
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

                    font = p.font()
                    font.setPixelSize(max(1, int(fs)))
                    font.setBold(True)
                    p.setFont(font)

                    shadow_a = min(140, alpha_int)
                    p.setPen(QColor(254, 249, 240, shadow_a))
                    p.drawText(sx + QPointF(1, 1), abbr)
                    p.setPen(QColor(80, 50, 20, alpha_int))
                    p.drawText(sx, abbr)

    # ------------------------------------------------------------------
    # 鼠标事件
    # ------------------------------------------------------------------

    def wheelEvent(self, event) -> None:
        dy = event.angleDelta().y()
        factor = 1.12 ** (dy / 120.0)
        new_scale = self._scale * factor

        if new_scale < 5.0:
            factor = 5.0 / self._scale
        elif new_scale > 800.0:
            factor = 800.0 / self._scale

        # 以鼠标位置为中心缩放
        mx, my = event.position().x(), event.position().y()
        scx, scy = self._screen_to_scene(mx, my)

        self._scale *= factor
        # 调整中心使鼠标下的场景点保持不动
        new_scx, new_scy = self._screen_to_scene(mx, my)
        self._cx += scx - new_scx
        self._cy += scy - new_scy

        self._update_hover(event.position())
        self._update_label_opacity_target()
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._press_pos = event.position()
            self._press_scene_center = (self._cx, self._cy)
            self._dragging = False
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event) -> None:
        if self._press_pos is not None:
            delta = event.position() - self._press_pos
            if not self._dragging and delta.manhattanLength() > self._drag_threshold:
                self._dragging = True
            if self._dragging:
                # 拖拽: 场景中心反向移动
                self._cx = self._press_scene_center[0] - delta.x() / self._scale
                self._cy = self._press_scene_center[1] + delta.y() / self._scale
                self.update()
        else:
            self._update_hover(event.position())

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            if not self._dragging and self._press_pos is not None:
                # 点击检测
                scx, scy = self._screen_to_scene(
                    event.position().x(), event.position().y(),
                )
                for name, path in self._provinces:
                    if path.contains(QPointF(scx, scy)):
                        self.provinceClicked.emit(name)
                        break
            self._press_pos = None
            self._dragging = False
            self.setCursor(Qt.OpenHandCursor)

    def leaveEvent(self, event) -> None:
        if self._hovered:
            self._hovered = ""
            self._hovered_neighbors = set()
            self._update_hover_boost_target()
            self.update()

    # ------------------------------------------------------------------
    # 悬停
    # ------------------------------------------------------------------

    def _update_hover(self, screen_pos: QPointF) -> None:
        scx, scy = self._screen_to_scene(screen_pos.x(), screen_pos.y())
        hit = ""
        for name, path in self._provinces:
            if path.contains(QPointF(scx, scy)):
                hit = name
                break
        if hit != self._hovered:
            self._hovered = hit
            if hit and hit in self._neighbors:
                self._hovered_neighbors = self._neighbors[hit] | {hit}
            elif hit:
                self._hovered_neighbors = {hit}
            else:
                self._hovered_neighbors = set()
            self._update_hover_boost_target()
            self.update()


class MapWidget(QWidget):
    """原生地图组件 — 与旧 MapView 保持相同的 bridge 接口."""

    toggleProvinceList = Signal()
    toggleSettings = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._bridge = MapBridge()
        self._geo_json_loaded = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._canvas = _MapCanvas(self)
        self._canvas.provinceClicked.connect(self._on_province_clicked)
        layout.addWidget(self._canvas)

        # 浮动按钮
        btn_style = """
            QPushButton {
                background: rgba(255,255,255,0.85); border: 1px solid #ccc;
                border-radius: 4px; font-size: 16px; color: #555;
            }
            QPushButton:hover { background: rgba(255,255,255,0.95); border-color: #999; }
        """
        self._btn_provinces = QPushButton("☰", self)
        self._btn_provinces.setFixedSize(32, 32)
        self._btn_provinces.setCursor(Qt.PointingHandCursor)
        self._btn_provinces.setToolTip("省份列表")
        self._btn_provinces.setStyleSheet(btn_style)
        self._btn_provinces.clicked.connect(self.toggleProvinceList.emit)

        self._btn_settings = QPushButton("⚙", self)
        self._btn_settings.setFixedSize(32, 32)
        self._btn_settings.setCursor(Qt.PointingHandCursor)
        self._btn_settings.setToolTip("设置")
        self._btn_settings.setStyleSheet(btn_style)
        self._btn_settings.clicked.connect(self.toggleSettings.emit)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._btn_provinces.move(8, 8)
        self._btn_settings.move(self.width() - 40, 8)
        self._btn_provinces.raise_()
        self._btn_settings.raise_()

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
        logger.info("地图加载完成: %d 个省份", len(self._canvas._provinces))
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

    def highlight(self, province_name: str) -> None:
        self._canvas.highlight(province_name)

    # ------------------------------------------------------------------
    # 内部回调
    # ------------------------------------------------------------------

    def _on_province_clicked(self, name: str) -> None:
        self._bridge.provinceClicked.emit(name)

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
