"""QWebChannel 桥接对象 — Python 与 ECharts 的双向通信通道.

Signal  = Python -> JavaScript (单向推送)
Slot    = JavaScript -> Python (调用), 内部触发 Signal 供 MainWindow 监听
"""

from PySide6.QtCore import QObject, Signal, Slot


class MapBridge(QObject):
    """ECharts 地图与 Python 后端的桥接对象.

    通过 QWebChannel 注册为 "bridge" 后:
    - JS 侧调用 @Slot 方法 (如 onProvinceClicked)
    - Slot 内部 emit 对应的 Signal
    - MainWindow 通过 connect Signal 来响应事件
    """

    # ---- Signals: Python -> JS (由 Python emit, JS connect 监听) ----

    provinceStatsChanged = Signal(str)
    """省份统计更新. payload: JSON '[{"name":"四川省","value":42},...]'"""

    highlightProvince = Signal(str)
    """高亮省份. payload: 省份名."""

    # ---- Signals: JS -> Python 代理 (由 Slot 内部 emit, MainWindow connect 监听) ----

    mapReady = Signal()
    """地图就绪."""

    provinceClicked = Signal(str)
    """省份被点击. payload: 省份名."""

    returnToMap = Signal()
    """返回地图."""

    unclassifiedClicked = Signal()
    """未分类被点击."""

    # ---- Slots: JS -> Python (JS 直接调用, 内部转发为 Signal) ----

    @Slot()
    def onMapReady(self):
        """ECharts 初始化完成 (JS 调用)."""
        self.mapReady.emit()

    @Slot(str)
    def onProvinceClicked(self, province_name: str):
        """用户点击地图省份 (JS 调用)."""
        if province_name:
            self.provinceClicked.emit(province_name)

    @Slot()
    def onReturnToMap(self):
        """用户返回地图 (JS 调用)."""
        self.returnToMap.emit()

    @Slot(str)
    def onUnclassifiedClicked(self, _placeholder: str = ""):
        """用户点击未分类 (JS 调用)."""
        self.unclassifiedClicked.emit()
