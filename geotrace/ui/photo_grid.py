"""照片网格视图 — 按省份分页浏览照片缩略图.

基于 QListView + 自定义模型, 集成 QPixmapCache 内存缓存
和磁盘缩略图缓存, 支持万张级别照片流畅浏览.
"""

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QSize,
    Qt,
    QThread,
    Signal,
    Slot,
)
from PySide6.QtGui import QPixmap, QPixmapCache
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListView,
    QPushButton,
    QStyledItemDelegate,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from geotrace.database.manager import DatabaseManager

logger = logging.getLogger(__name__)

PAGE_SIZE = 200
THUMBNAIL_SIZE = QSize(280, 210)


class PhotoListModel(QAbstractListModel):
    """照片列表模型 — 管理分页数据."""

    def __init__(self, db: DatabaseManager, parent=None) -> None:
        super().__init__(parent)
        self._db = db
        self._photos: list[dict] = []
        self._total = 0
        self._province_name = ""
        self._page = 1

    # ------------------------------------------------------------------
    # QAbstractListModel 接口
    # ------------------------------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._photos) if not parent.isValid() else 0

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        photo = self._photos[index.row()]

        if role == Qt.DisplayRole:
            return photo.get("file_name", "")
        if role == Qt.UserRole:
            return photo  # 返回完整 dict
        if role == Qt.UserRole + 1:
            return photo.get("file_path", "")
        if role == Qt.UserRole + 2:
            return photo.get("thumbnail_path", "")
        if role == Qt.UserRole + 3:
            return photo.get("id", 0)
        return None

    # ------------------------------------------------------------------
    # 数据加载
    # ------------------------------------------------------------------

    def load_province(self, province_name: str) -> None:
        """加载指定省份的第一页照片."""
        self._province_name = province_name
        self._page = 1
        self._load_page()

    def has_more(self) -> bool:
        return len(self._photos) < self._total

    def load_next_page(self) -> int:
        """加载下一页, 返回新增数量."""
        if not self.has_more():
            return 0
        self._page += 1
        return self._load_page(append=True)

    def _load_page(self, append: bool = False) -> int:
        self.beginResetModel()

        if self._province_name == "Unclassified":
            photos, self._total = self._db.get_unclassified_photos(
                page=self._page, page_size=PAGE_SIZE,
            )
        elif self._province_name:
            photos, self._total = self._db.query_by_province(
                self._province_name, page=self._page, page_size=PAGE_SIZE,
            )
        else:
            photos, self._total = [], 0

        if append:
            self._photos.extend(photos)
        else:
            self._photos = photos

        self.endResetModel()
        return len(photos)

    @property
    def total(self) -> int:
        return self._total

    @property
    def province_name(self) -> str:
        return self._province_name


class ThumbnailDelegate(QStyledItemDelegate):
    """照片缩略图绘制代理 — 集成 QPixmapCache 内存缓存."""

    PLACEHOLDER_KEY = "__geotrace_placeholder__"

    def paint(self, painter, option, index: QModelIndex) -> None:
        if not index.isValid():
            return

        file_path = index.data(Qt.UserRole + 1)
        thumbnail_path = index.data(Qt.UserRole + 2)
        photo_name = index.data(Qt.DisplayRole)

        # 绘制选中/悬停背景
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
        elif option.state & QStyle.State_MouseOver:
            painter.fillRect(option.rect, option.palette.light())

        # 计算缩略图区域
        margin = 8
        img_rect = option.rect.adjusted(margin, margin, -margin, -margin)
        # 底部留文字空间
        text_height = 20
        img_rect.setHeight(option.rect.height() - margin * 2 - text_height)

        # 加载缩略图
        pixmap = self._load_thumbnail(file_path, thumbnail_path, img_rect.size())
        if pixmap:
            painter.drawPixmap(img_rect, pixmap)
        else:
            painter.fillRect(img_rect, option.palette.mid())
            painter.setPen(option.palette.text().color())
            painter.drawText(img_rect, Qt.AlignCenter, "无预览")

        # 绘制文件名
        text_rect = option.rect.adjusted(margin, 0, -margin, -4)
        text_rect.setTop(img_rect.bottom() + 2)
        painter.setPen(option.palette.text().color())
        elided = painter.fontMetrics().elidedText(photo_name, Qt.ElideRight, text_rect.width())
        painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, elided)

    def sizeHint(self, option, index: QModelIndex) -> QSize:
        return QSize(THUMBNAIL_SIZE.width(), THUMBNAIL_SIZE.height() + 24)

    def _load_thumbnail(
        self, file_path: str, thumbnail_path: str, size: QSize,
    ) -> QPixmap | None:
        """从缓存或磁盘加载缩略图 QPixmap."""
        cache_key = file_path

        # QPixmapCache 内存缓存
        pixmap = QPixmap()
        if QPixmapCache.find(cache_key, pixmap):
            return self._scaled_if_needed(pixmap, size)

        # 尝试从磁盘缩略图缓存加载
        source = thumbnail_path or file_path
        if not Path(source).exists():
            return None

        if pixmap.load(source):
            scaled = self._scaled_if_needed(pixmap, size)
            # 只有缩略图缓存才放进 QPixmapCache (原图太大)
            if thumbnail_path:
                QPixmapCache.insert(cache_key, scaled)
            return scaled

        return None

    @staticmethod
    def _scaled_if_needed(pixmap: QPixmap, size: QSize) -> QPixmap:
        if pixmap.size().width() > size.width() or pixmap.size().height() > size.height():
            return pixmap.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        return pixmap


class PhotoGrid(QWidget):
    """照片网格视图 — 按省份分页浏览照片.

    包含返回按钮、省份标题、照片列表和翻页控制.
    """

    photoDoubleClicked = Signal(str)  # file_path
    returnToMap = Signal()

    def __init__(self, db: DatabaseManager, parent=None) -> None:
        super().__init__(parent)
        self._db = db

        # UI 组件
        self._back_btn = QPushButton("← 返回地图")
        self._province_label = QLabel()
        self._province_label.setStyleSheet("font-size: 18px; font-weight: bold; padding: 8px;")

        self._model = PhotoListModel(db, self)
        self._list_view = QListView()
        self._list_view.setModel(self._model)
        self._list_view.setItemDelegate(ThumbnailDelegate(self._list_view))
        self._list_view.setViewMode(QListView.IconMode)
        self._list_view.setIconSize(THUMBNAIL_SIZE)
        self._list_view.setGridSize(QSize(THUMBNAIL_SIZE.width() + 12, THUMBNAIL_SIZE.height() + 30))
        self._list_view.setResizeMode(QListView.Adjust)
        self._list_view.setWrapping(True)
        self._list_view.setBatchSize(30)
        self._list_view.setUniformItemSizes(True)
        self._list_view.setSpacing(4)

        self._load_more_btn = QPushButton("加载更多...")
        self._load_more_btn.setVisible(False)

        # 布局
        top_bar = QHBoxLayout()
        top_bar.addWidget(self._back_btn)
        top_bar.addWidget(self._province_label, 1)

        layout = QVBoxLayout(self)
        layout.addLayout(top_bar)
        layout.addWidget(self._list_view, 1)
        layout.addWidget(self._load_more_btn)

        # 信号连接
        self._back_btn.clicked.connect(self.returnToMap.emit)
        self._list_view.doubleClicked.connect(self._on_double_clicked)
        self._load_more_btn.clicked.connect(self._load_more)

        # 滚动到底部检测
        self._list_view.verticalScrollBar().valueChanged.connect(self._on_scroll)

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def load_province(self, province_name: str) -> None:
        """加载指定省份的照片."""
        self._province_label.setText(f"{province_name} (加载中...)")
        self._model.load_province(province_name)
        self._update_display()

    def clear(self) -> None:
        """清除当前显示."""
        self._model.load_province("")
        self._province_label.setText("")
        self._load_more_btn.setVisible(False)

    # ------------------------------------------------------------------
    # 内部逻辑
    # ------------------------------------------------------------------

    def _update_display(self) -> None:
        count = self._model.rowCount()
        total = self._model.total
        self._province_label.setText(
            f"{self._model.province_name} (第 {min(count, total)} / {total} 张)"
        )
        self._load_more_btn.setVisible(self._model.has_more())
        QPixmapCache.clear()  # 切换省份时清理内存缓存

    def _load_more(self) -> None:
        self._load_more_btn.setEnabled(False)
        self._load_more_btn.setText("加载中...")
        # 使用 QThread 避免阻塞 UI (快速操作, 但保持体验)
        added = self._model.load_next_page()
        self._update_display()
        self._load_more_btn.setEnabled(self._model.has_more())
        self._load_more_btn.setText("加载更多...")
        if added == 0:
            self._load_more_btn.setVisible(False)

    def _on_scroll(self, value: int) -> None:
        sb = self._list_view.verticalScrollBar()
        if sb.value() >= sb.maximum() - 100 and self._model.has_more():
            self._load_more()

    def _on_double_clicked(self, index: QModelIndex) -> None:
        file_path = index.data(Qt.UserRole + 1)
        if file_path:
            self.photoDoubleClicked.emit(file_path)
