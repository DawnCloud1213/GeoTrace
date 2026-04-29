"""地图视角平滑动画 — 使用 QVariantAnimation 对 center / zoom 插值.

触发场景:
  - 侧边栏点击省份 → 计算该省 Bounding Box 的适配 zoom 和中心点 → 600ms 缓动飞行.
  - 双击地图或点击聚类簇 → 近距离平滑过渡.
"""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import (
    QEasingCurve,
    QObject,
    QPointF,
    QPropertyAnimation,
    QVariantAnimation,
)

from geotrace.ui.map_core import MercatorProjection

logger = logging.getLogger(__name__)


class MapViewAnimator(QVariantAnimation):
    """对地图中心点 (pixel) 和 zoom 进行平滑插值.

    使用单一 float progress (0.0 → 1.0) 驱动，
    在 valueChanged 中手动 lerp 计算 center / zoom，
    以规避 PySide6 QVariantAnimation 不支持 Python tuple 插值的问题.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.setDuration(600)
        self.setEasingCurve(QEasingCurve.InOutQuart)
        self.valueChanged.connect(self._on_value_changed)

        self._callback: Callable[[float, float, float], None] | None = None

        self._start_lng: float = 0.0
        self._start_lat: float = 0.0
        self._start_zoom: float = 4.0
        self._target_lng: float = 0.0
        self._target_lat: float = 0.0
        self._target_zoom: float = 4.0

    def set_callback(
        self,
        callback: Callable[[float, float, float], None],
    ) -> None:
        """设置每帧回调: callback(center_px, center_py, zoom)."""
        self._callback = callback

    def fly_to(
        self,
        start_center_px: float,
        start_center_py: float,
        start_zoom: float,
        target_center_px: float,
        target_center_py: float,
        target_zoom: float,
    ) -> None:
        """启动飞行动画.

        为了避免不同 zoom 层级下 Mercator 像素坐标尺度不一致导致的插值畸变，
         internally 将中心点转换为经纬度进行插值，每帧再转回当前 zoom 的像素坐标。
        """
        self.stop()
        self._start_zoom = max(MIN_ZOOM, start_zoom)
        self._target_zoom = max(MIN_ZOOM, min(MAX_ZOOM, target_zoom))

        # 统一到与 zoom 无关的经纬度坐标系插值
        self._start_lng, self._start_lat = MercatorProjection.pixel_to_lnglat(
            start_center_px, start_center_py, start_zoom
        )
        self._target_lng, self._target_lat = MercatorProjection.pixel_to_lnglat(
            target_center_px, target_center_py, target_zoom
        )

        self.setStartValue(0.0)
        self.setEndValue(1.0)
        self.start()

    def _on_value_changed(self, value) -> None:
        progress = float(value)
        lng = self._start_lng + (self._target_lng - self._start_lng) * progress
        lat = self._start_lat + (self._target_lat - self._start_lat) * progress
        zoom = self._start_zoom + (self._target_zoom - self._start_zoom) * progress
        px, py = MercatorProjection.lnglat_to_pixel(lng, lat, zoom)
        if self._callback:
            self._callback(px, py, zoom)


# 复用 map_core 的常量
from geotrace.ui.map_core import MAX_ZOOM, MIN_ZOOM


def compute_fit_zoom_and_center(
    min_lng: float,
    min_lat: float,
    max_lng: float,
    max_lat: float,
    viewport_width: int,
    viewport_height: int,
    padding: float = 1.15,
) -> tuple[float, float, float]:
    """计算让给定地理范围适配视口的 zoom 和中心像素.

    Returns:
        (center_px, center_py, zoom)
    """
    # 从较高 zoom 向下搜索适配 zoom
    for z in range(MAX_ZOOM, MIN_ZOOM - 1, -1):
        zf = float(z)
        p1x, p1y = MercatorProjection.lnglat_to_pixel(min_lng, min_lat, zf)
        p2x, p2y = MercatorProjection.lnglat_to_pixel(max_lng, max_lat, zf)
        pw = abs(p2x - p1x) * padding
        ph = abs(p2y - p1y) * padding
        if pw <= viewport_width and ph <= viewport_height:
            center_px = (p1x + p2x) / 2.0
            center_py = (p1y + p2y) / 2.0
            return center_px, center_py, zf

    # 最小 zoom 也放不下，使用最小 zoom 并居中
    zf = float(MIN_ZOOM)
    p1x, p1y = MercatorProjection.lnglat_to_pixel(min_lng, min_lat, zf)
    p2x, p2y = MercatorProjection.lnglat_to_pixel(max_lng, max_lat, zf)
    return (p1x + p2x) / 2.0, (p1y + p2y) / 2.0, zf
