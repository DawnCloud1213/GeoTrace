"""照片地理坐标聚类渲染 — R-Tree 视口剔除 + 屏幕网格聚合 + 异步缩略图加载.

约束:
  - 界面同时绘制元素不超过 500 个 (维持 60FPS).
  - 宏观视角: 相邻照片合并为计数 Badge.
  - 微观视角: zoom 放大后拆分为真实缩略图锚点.
  - 缩略图加载必须在后台线程完成，主线程仅绘制缓存命中项或占位符.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QRectF, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QImageReader,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPixmapCache,
)

from geotrace.ui.map_core import MercatorProjection
from geotrace.ui.theme import Colors

logger = logging.getLogger(__name__)

# 聚类参数
DEFAULT_CELL_PX = 50  # 网格单元像素
MAX_ELEMENTS = 500
BADGE_BG = QColor(230, 126, 34, 220)  # 暖橙半透明
BADGE_BORDER = QColor(255, 255, 255, 180)
BADGE_TEXT = QColor(255, 255, 255)
SINGLE_ANCHOR_BG = QColor(230, 126, 34)
SINGLE_ANCHOR_BORDER = QColor(255, 255, 255)
SINGLE_SIZE = 24  # 单个锚点直径 (像素)
BADGE_MIN_RADIUS = 14
THUMBNAIL_SIZE = 44  # 缩略图模式下标记尺寸
THUMBNAIL_RADIUS = 6  # 缩略图圆角
SHADOW_COLOR = QColor(80, 50, 20, 40)  # 暖棕阴影


@dataclass(frozen=True)
class ClusterItem:
    """聚类结果项."""

    screen_x: float
    screen_y: float
    count: int
    ids: list[int]
    thumbnail_path: str | None = None
    file_path: str | None = None


# ------------------------------------------------------------------------------
# 异步缩略图加载基础设施
# ------------------------------------------------------------------------------

class AsyncImageLoaderSignals(QObject):
    """跨线程图像加载信号集.

    loaded 携带 QImage (非 QPixmap, 因 QPixmap 只能在 GUI 线程创建).
    """

    loaded = Signal(str, object)  # cache_key, QImage
    failed = Signal(str, str)     # cache_key, error_msg


class ImageLoadTask(QThreadPool):
    """后台 QImage 加载任务 (QRunnable).

    在子线程中读取磁盘文件并解码为 QImage，完成后通过 Signal 回传主线程.
    """

    def __init__(
        self,
        cache_key: str,
        file_path: str,
        target_size: QSize,
        signals: AsyncImageLoaderSignals,
    ) -> None:
        super().__init__()
        self._cache_key = cache_key
        self._file_path = file_path
        self._target_size = target_size
        self._signals = signals

    def run(self) -> None:
        try:
            if not Path(self._file_path).exists():
                self._signals.failed.emit(self._cache_key, "File not found")
                return

            reader = QImageReader(self._file_path)
            reader.setAutoTransform(True)
            if self._target_size.isValid():
                # 读取 2x 尺寸以保留 HiDPI 清晰度，主线程再缩放到目标尺寸
                reader.setScaledSize(self._target_size * 2)

            image = reader.read()
            if image.isNull():
                self._signals.failed.emit(
                    self._cache_key, f"QImageReader failed: {reader.errorString()}"
                )
                return

            self._signals.loaded.emit(self._cache_key, image)
        except Exception as e:
            logger.warning("ImageLoadTask 异常 %s: %s", self._cache_key, e)
            self._signals.failed.emit(self._cache_key, str(e))


class ThumbnailManager(QObject):
    """缩略图异步管理器 — 单例.

    内部维护 QThreadPool (max 4 线程)，通过 ImageLoadTask 后台加载 QImage。
    主线程收到 loaded Signal 后转为 QPixmap 写入 QPixmapCache，并 emit thumbnailReady。

    使用方式:
        mgr = ThumbnailManager()
        mgr.request_thumbnail(cache_key, file_path, QSize(44, 44))
        pixmap = mgr.get_pixmap(cache_key)  # 可能为 None (未加载完成)
    """

    _instance: ThumbnailManager | None = None
    _lock = threading.Lock()

    thumbnailReady = Signal(str)  # cache_key

    def __new__(cls) -> ThumbnailManager:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        super().__init__()
        self._initialized = True

        self._thread_pool = QThreadPool()
        self._thread_pool.setMaxThreadCount(4)

        self._signals = AsyncImageLoaderSignals()
        self._signals.loaded.connect(self._on_loaded)
        self._signals.failed.connect(self._on_failed)

        self._pending: set[str] = set()

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def request_thumbnail(
        self, cache_key: str, file_path: str | None, target_size: QSize
    ) -> None:
        """幂等地请求缩略图加载.

        若已存在于 QPixmapCache 或已在 pending 队列，则忽略.
        """
        if not file_path:
            return
        if not Path(file_path).exists():
            return

        cached = QPixmap()
        if QPixmapCache.find(cache_key, cached) and not cached.isNull():
            return
        if cache_key in self._pending:
            return

        self._pending.add(cache_key)
        task = ImageLoadTask(cache_key, file_path, target_size, self._signals)
        self._thread_pool.start(task)

    def get_pixmap(self, cache_key: str) -> QPixmap | None:
        """从 QPixmapCache 读取已就绪的缩略图."""
        if not cache_key:
            return None
        cached = QPixmap()
        if QPixmapCache.find(cache_key, cached) and not cached.isNull():
            return cached
        return None

    def clear_pending(self) -> None:
        """清空 pending 队列 (用于切换视图时放弃旧请求)."""
        self._pending.clear()

    # ------------------------------------------------------------------
    # 内部回调 (均在主线程执行，因 Signal 自动排队到接收者线程)
    # ------------------------------------------------------------------

    def _on_loaded(self, cache_key: str, image: QImage) -> None:
        self._pending.discard(cache_key)
        if image.isNull():
            return
        try:
            pixmap = QPixmap.fromImage(image)
            if pixmap.isNull():
                return
            QPixmapCache.insert(cache_key, pixmap)
            self.thumbnailReady.emit(cache_key)
        except Exception as e:
            logger.warning("缩略图 QPixmap 转换失败 %s: %s", cache_key, e)

    def _on_failed(self, cache_key: str, error: str) -> None:
        self._pending.discard(cache_key)
        logger.debug("缩略图加载失败 %s: %s", cache_key, error)


# ------------------------------------------------------------------------------
# R-Tree 视口剔除 + 屏幕网格聚类
# ------------------------------------------------------------------------------

class GridClusterer:
    """基于 R-Tree 视口剔除 + 屏幕像素网格的动态聚类.

    初始化时调用 load_photos() 构建内存 R-Tree 索引；
    每次 cluster() 先将屏幕视口转为 WGS84 BBox，通过 rtree.intersection()
    快速获取可视范围内照片 ID，仅对这部分数据执行网格聚合.
    """

    def __init__(self, cell_px: int = DEFAULT_CELL_PX) -> None:
        self.cell_px = cell_px
        self._rtree: object | None = None
        self._photo_map: dict[int, dict] = {}

    def load_photos(self, photos: list[dict]) -> None:
        """重建空间索引.

        Args:
            photos: 包含 id, latitude, longitude, thumbnail_path, file_path 的 dict 列表.
        """
        try:
            from rtree import index
        except ImportError:
            logger.error("rtree 未安装，无法构建空间索引")
            self._rtree = None
            self._photo_map = {}
            return

        self._photo_map = {}
        # rtree.Index() 创建内存索引
        rtree_idx = index.Index()
        for p in photos:
            photo_id = p.get("id")
            lat = p.get("latitude")
            lng = p.get("longitude")
            if photo_id is None or lat is None or lng is None:
                continue
            # 点数据的 BBox: (lng, lat, lng, lat)
            rtree_idx.insert(photo_id, (lng, lat, lng, lat))
            self._photo_map[photo_id] = p

        self._rtree = rtree_idx
        logger.debug("R-Tree 索引已重建: %d 张照片", len(self._photo_map))

    def cluster(
        self,
        zoom: float,
        viewport_width: int,
        viewport_height: int,
        center_px: float,
        center_py: float,
    ) -> list[ClusterItem]:
        """聚合并返回当前视口内的 ClusterItem 列表.

        时间复杂度: O(log N + M), N 为总照片数, M 为视口内照片数.
        """
        if not self._photo_map:
            return []
        if self._rtree is None:
            # 降级: 无 rtree 时遍历全部 (兼容 rtree 缺失场景)
            return self._cluster_fallback(
                zoom, viewport_width, viewport_height, center_px, center_py
            )

        half_w = viewport_width / 2.0
        half_h = viewport_height / 2.0
        min_px = center_px - half_w
        max_px = center_px + half_w
        min_py = center_py - half_h
        max_py = center_py + half_h

        # 视口四角 → WGS84 BBox (注意 Mercator 的 Y 轴与纬度方向相反)
        min_lng, max_lat = MercatorProjection.pixel_to_lnglat(min_px, min_py, zoom)
        max_lng, min_lat = MercatorProjection.pixel_to_lnglat(max_px, max_py, zoom)

        # 扩大一圈以包含屏幕边缘附近的照片
        lng_pad = abs(max_lng - min_lng) * 0.05
        lat_pad = abs(max_lat - min_lat) * 0.05
        bbox = (
            min_lng - lng_pad,
            min_lat - lat_pad,
            max_lng + lng_pad,
            max_lat + lat_pad,
        )

        try:
            visible_ids = list(self._rtree.intersection(bbox))
        except Exception as e:
            logger.warning("R-Tree intersection 失败: %s", e)
            return []

        if not visible_ids:
            return []

        # 仅对可视范围内照片执行屏幕网格聚类
        return self._cluster_visible(
            visible_ids, zoom, viewport_width, viewport_height, center_px, center_py
        )

    def _cluster_visible(
        self,
        visible_ids: list[int],
        zoom: float,
        viewport_width: int,
        viewport_height: int,
        center_px: float,
        center_py: float,
    ) -> list[ClusterItem]:
        """对已知可见照片 ID 列表执行屏幕网格聚类."""
        half_w = viewport_width / 2.0
        half_h = viewport_height / 2.0
        min_px = center_px - half_w
        max_px = center_px + half_w
        min_py = center_py - half_h
        max_py = center_py + half_h

        cell_px = self.cell_px
        max_iterations = 5

        for _ in range(max_iterations):
            clusters: dict[tuple[int, int], list[dict]] = {}
            for photo_id in visible_ids:
                p = self._photo_map.get(photo_id)
                if p is None:
                    continue
                lat = p.get("latitude")
                lng = p.get("longitude")
                if lat is None or lng is None:
                    continue
                px, py = MercatorProjection.lnglat_to_pixel(lng, lat, zoom)
                if not (min_px - cell_px <= px <= max_px + cell_px):
                    continue
                if not (min_py - cell_px <= py <= max_py + cell_px):
                    continue
                sx = px - min_px
                sy = py - min_py
                cell_x = int(sx // cell_px)
                cell_y = int(sy // cell_px)
                key = (cell_x, cell_y)
                clusters.setdefault(key, []).append({
                    "sx": sx,
                    "sy": sy,
                    "id": photo_id,
                    "thumbnail_path": p.get("thumbnail_path"),
                    "file_path": p.get("file_path"),
                })

            result: list[ClusterItem] = []
            for items in clusters.values():
                count = len(items)
                avg_sx = sum(it["sx"] for it in items) / count
                avg_sy = sum(it["sy"] for it in items) / count
                ids = [it["id"] for it in items]
                thumb = items[0]["thumbnail_path"]
                fpath = items[0]["file_path"]
                result.append(ClusterItem(avg_sx, avg_sy, count, ids, thumb, fpath))

            if len(result) <= MAX_ELEMENTS:
                return result
            cell_px = int(cell_px * 1.5) + 1

        result.sort(key=lambda c: c.count, reverse=True)
        return result[:MAX_ELEMENTS]

    def _cluster_fallback(
        self,
        zoom: float,
        viewport_width: int,
        viewport_height: int,
        center_px: float,
        center_py: float,
    ) -> list[ClusterItem]:
        """无 R-Tree 时的降级全量遍历 (O(N))."""
        all_photos = list(self._photo_map.values())
        half_w = viewport_width / 2.0
        half_h = viewport_height / 2.0
        min_px = center_px - half_w
        max_px = center_px + half_w
        min_py = center_py - half_h
        max_py = center_py + half_h

        cell_px = self.cell_px
        max_iterations = 5

        for _ in range(max_iterations):
            clusters: dict[tuple[int, int], list[dict]] = {}
            for p in all_photos:
                lat = p.get("latitude")
                lng = p.get("longitude")
                if lat is None or lng is None:
                    continue
                px, py = MercatorProjection.lnglat_to_pixel(lng, lat, zoom)
                if not (min_px - cell_px <= px <= max_px + cell_px):
                    continue
                if not (min_py - cell_px <= py <= max_py + cell_px):
                    continue
                sx = px - min_px
                sy = py - min_py
                key = (int(sx // cell_px), int(sy // cell_px))
                clusters.setdefault(key, []).append({
                    "sx": sx,
                    "sy": sy,
                    "id": p.get("id", 0),
                    "thumbnail_path": p.get("thumbnail_path"),
                    "file_path": p.get("file_path"),
                })

            result: list[ClusterItem] = []
            for items in clusters.values():
                count = len(items)
                avg_sx = sum(it["sx"] for it in items) / count
                avg_sy = sum(it["sy"] for it in items) / count
                ids = [it["id"] for it in items]
                thumb = items[0]["thumbnail_path"]
                fpath = items[0]["file_path"]
                result.append(ClusterItem(avg_sx, avg_sy, count, ids, thumb, fpath))

            if len(result) <= MAX_ELEMENTS:
                return result
            cell_px = int(cell_px * 1.5) + 1

        result.sort(key=lambda c: c.count, reverse=True)
        return result[:MAX_ELEMENTS]


# ------------------------------------------------------------------------------
# 聚类渲染器 — 对接 ThumbnailManager
# ------------------------------------------------------------------------------

class ClusterRenderer:
    """负责在 QPainter 上绘制 ClusterItem 列表.

    对接 ThumbnailManager 单例: paint() 阶段对未缓存的缩略图发起异步请求，
    后台加载完成后通过 ThumbnailManager.thumbnailReady 触发 MapCanvas update().
    """

    def __init__(self, mode: str = "badge") -> None:
        self._mode = mode
        self._thumb_manager = ThumbnailManager()
        self._badge_font = QFont('"Microsoft YaHei UI", "Segoe UI", "SimSun"', 9)
        self._badge_font.setBold(True)
        self._thumb_font = QFont('"Microsoft YaHei UI", "Segoe UI", "SimSun"', 8)
        self._thumb_font.setBold(True)

    def set_mode(self, mode: str) -> None:
        self._mode = mode

    def paint(self, painter: QPainter, clusters: list[ClusterItem]) -> None:
        """绘制所有聚类元素."""
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        for cluster in clusters:
            if self._mode == "thumbnail":
                self._paint_thumbnail_cluster(painter, cluster)
            elif cluster.count > 1:
                self._paint_badge(painter, cluster)
            else:
                self._paint_single(painter, cluster)

        painter.restore()

    def _paint_badge(self, painter: QPainter, cluster: ClusterItem) -> None:
        """绘制计数 Badge (圆形 + 数字)."""
        import math

        radius = max(BADGE_MIN_RADIUS, int(10 + math.log2(cluster.count + 1) * 6))
        x = cluster.screen_x - radius
        y = cluster.screen_y - radius

        # 阴影
        shadow_rect = QRectF(x + 1, y + 2, radius * 2, radius * 2)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(SHADOW_COLOR))
        painter.drawEllipse(shadow_rect)

        # 主体
        rect = QRectF(x, y, radius * 2, radius * 2)
        painter.setBrush(QBrush(BADGE_BG))
        painter.setPen(QPen(BADGE_BORDER, 2))
        painter.drawEllipse(rect)

        # 文本
        text = str(cluster.count)
        painter.setPen(QColor(BADGE_TEXT))
        painter.setFont(self._badge_font)
        painter.drawText(rect, Qt.AlignCenter, text)

    def _paint_single(self, painter: QPainter, cluster: ClusterItem) -> None:
        """绘制单个照片锚点 (缩略图圆形裁剪 或 纯色圆点)."""
        size = SINGLE_SIZE
        x = cluster.screen_x - size / 2.0
        y = cluster.screen_y - size / 2.0

        pixmap = self._get_cached_pixmap(cluster, size)
        if pixmap and not pixmap.isNull():
            clip = QPainterPath()
            clip.addEllipse(x, y, size, size)
            painter.setClipPath(clip)
            painter.drawPixmap(int(x), int(y), pixmap)
            painter.setClipping(False)
        else:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(SINGLE_ANCHOR_BG))
            painter.drawEllipse(QRectF(x, y, size, size))

        # 边框
        painter.setPen(QPen(SINGLE_ANCHOR_BORDER, 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(QRectF(x, y, size, size))

    def _paint_thumbnail_cluster(self, painter: QPainter, cluster: ClusterItem) -> None:
        """绘制缩略图模式标记 (圆角矩形缩略图 + 可选数字角标)."""
        size = THUMBNAIL_SIZE
        x = cluster.screen_x - size / 2.0
        y = cluster.screen_y - size / 2.0

        # 阴影
        shadow_rect = QRectF(x + 1, y + 2, size, size)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(SHADOW_COLOR))
        painter.drawRoundedRect(shadow_rect, THUMBNAIL_RADIUS, THUMBNAIL_RADIUS)

        rect = QRectF(x, y, size, size)

        pixmap = self._get_cached_pixmap(cluster, size)
        if pixmap and not pixmap.isNull():
            clip = QPainterPath()
            clip.addRoundedRect(rect, THUMBNAIL_RADIUS, THUMBNAIL_RADIUS)
            painter.setClipPath(clip)
            painter.drawPixmap(rect.toRect(), pixmap)
            painter.setClipping(False)
        else:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(Colors.ACCENT_PRIMARY)))
            painter.drawRoundedRect(rect, THUMBNAIL_RADIUS, THUMBNAIL_RADIUS)

        # 边框
        painter.setPen(QPen(QColor(Colors.BORDER_MEDIUM), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(rect, THUMBNAIL_RADIUS, THUMBNAIL_RADIUS)

        # 数字角标 (count > 1)
        if cluster.count > 1:
            badge_r = 8
            badge_x = x + size - badge_r * 2
            badge_y = y + size - badge_r * 2
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(Colors.ACCENT_PRIMARY)))
            painter.drawEllipse(QRectF(badge_x, badge_y, badge_r * 2, badge_r * 2))
            painter.setPen(QColor(Colors.TEXT_ON_ACCENT))
            painter.setFont(self._thumb_font)
            painter.drawText(
                QRectF(badge_x, badge_y, badge_r * 2, badge_r * 2),
                Qt.AlignCenter,
                str(cluster.count),
            )

    def _get_cached_pixmap(
        self, cluster: ClusterItem, target_size: int
    ) -> QPixmap | None:
        """优先读 QPixmapCache，未命中则发起异步加载请求."""
        cache_key = cluster.file_path or cluster.thumbnail_path
        if not cache_key:
            return None

        # 先查缓存
        pixmap = self._thumb_manager.get_pixmap(cache_key)
        if pixmap is not None:
            if pixmap.width() != target_size or pixmap.height() != target_size:
                pixmap = pixmap.scaled(
                    QSize(target_size, target_size),
                    Qt.KeepAspectRatioByExpanding,
                    Qt.SmoothTransformation,
                )
            return pixmap

        # 未命中: 发起后台加载 (幂等)
        self._thumb_manager.request_thumbnail(
            cache_key, cluster.file_path or cluster.thumbnail_path, QSize(target_size, target_size)
        )
        return None


import math  # noqa: E402
