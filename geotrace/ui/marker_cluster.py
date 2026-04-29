"""照片地理坐标聚类渲染 — 基于屏幕像素网格的动态聚合.

约束:
  - 界面同时绘制元素不超过 500 个 (维持 60FPS).
  - 宏观视角: 相邻照片合并为计数 Badge.
  - 微观视角: zoom 放大后拆分为真实缩略图锚点.

复用 ThumbnailDelegate._load_thumbnail 的 QPixmapCache 策略。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
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


class GridClusterer:
    """基于屏幕像素网格的动态聚类.

    输入: 照片坐标列表 + 当前 zoom + 画布尺寸 + 视口中心像素.
    输出: ClusterItem 列表.
    """

    def __init__(self, cell_px: int = DEFAULT_CELL_PX) -> None:
        self.cell_px = cell_px

    def cluster(
        self,
        photos: list[dict],
        zoom: float,
        viewport_width: int,
        viewport_height: int,
        center_px: float,
        center_py: float,
    ) -> list[ClusterItem]:
        """聚合并返回当前视口内的 ClusterItem 列表.

        Args:
            photos: 包含 latitude, longitude, id, thumbnail_path 的 dict 列表.
            zoom: 当前 zoom 层级.
            viewport_width, viewport_height: widget 尺寸.
            center_px, center_py: 视口中心对应的场景像素.

        Returns:
            ClusterItem 列表 (屏幕坐标已计算).
        """
        if not photos:
            return []

        half_w = viewport_width / 2.0
        half_h = viewport_height / 2.0
        min_px = center_px - half_w
        max_px = center_px + half_w
        min_py = center_py - half_h
        max_py = center_py + half_h

        # 动态调整 cell_px: 若初步聚类结果超过 MAX_ELEMENTS, 扩大网格.
        cell_px = self.cell_px
        max_iterations = 5
        for _ in range(max_iterations):
            clusters: dict[tuple[int, int], list[dict]] = {}
            for p in photos:
                lat = p.get("latitude")
                lng = p.get("longitude")
                if lat is None or lng is None:
                    continue
                px, py = MercatorProjection.lnglat_to_pixel(lng, lat, zoom)
                if not (min_px - cell_px <= px <= max_px + cell_px):
                    continue
                if not (min_py - cell_px <= py <= max_py + cell_px):
                    continue
                # 屏幕坐标
                sx = px - min_px
                sy = py - min_py
                cell_x = int(sx // cell_px)
                cell_y = int(sy // cell_px)
                key = (cell_x, cell_y)
                if key not in clusters:
                    clusters[key] = []
                clusters[key].append({
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

        # 仍然超限, 强制只保留 MAX_ELEMENTS 个最大的簇
        result.sort(key=lambda c: c.count, reverse=True)
        return result[:MAX_ELEMENTS]


class ClusterRenderer:
    """负责在 QPainter 上绘制 ClusterItem 列表."""

    def __init__(self, mode: str = "badge") -> None:
        self._mode = mode
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

        # 尝试加载缩略图 (优先使用 file_path 作为缓存 key 以复用 PhotoGrid 缓存)
        pixmap: QPixmap | None = None
        cache_key = cluster.file_path or cluster.thumbnail_path
        if cache_key:
            cached = QPixmap()
            if QPixmapCache.find(cache_key, cached):
                pixmap = cached.scaled(QSize(size, size), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            else:
                # 异步不在此处处理; 若未缓存则先画纯色圆点
                pass

        if pixmap and not pixmap.isNull():
            clip = QPainterPath()
            clip.addEllipse(x, y, size, size)
            painter.setClipPath(clip)
            painter.drawPixmap(int(x), int(y), pixmap)
            painter.setClipping(False)
        else:
            # 纯色圆点
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

        # 尝试加载缩略图 (优先使用 file_path 作为缓存 key 以复用 PhotoGrid 缓存)
        pixmap: QPixmap | None = None
        cache_key = cluster.file_path or cluster.thumbnail_path
        if cache_key:
            cached = QPixmap()
            if QPixmapCache.find(cache_key, cached):
                pixmap = cached.scaled(
                    QSize(size, size),
                    Qt.KeepAspectRatioByExpanding,
                    Qt.SmoothTransformation,
                )

        if pixmap and not pixmap.isNull():
            clip = QPainterPath()
            clip.addRoundedRect(rect, THUMBNAIL_RADIUS, THUMBNAIL_RADIUS)
            painter.setClipPath(clip)
            painter.drawPixmap(rect.toRect(), pixmap)
            painter.setClipping(False)
        else:
            # 无缓存时绘制醒目占位块
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


import math  # noqa: E402 放在文件尾部避免循环导入不影响实际运行
