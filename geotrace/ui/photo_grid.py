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
    QObject,
    QSize,
    Qt,
    QThread,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QImageReader,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPixmapCache,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
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
from geotrace.ui.theme import Colors, Fonts
from geotrace.ui.photo_viewer import PhotoViewer

logger = logging.getLogger(__name__)

PAGE_SIZE = 200
# 侧边栏模式下使用紧凑缩略图
THUMBNAIL_SIZE = QSize(140, 105)
GRID_SPACING = QSize(THUMBNAIL_SIZE.width() + 12, THUMBNAIL_SIZE.height() + 32)


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

    def set_page_data(self, photos: list[dict], total: int,
                      append: bool = False) -> int:
        """填入查询结果 (主线程调用)."""
        self._total = total
        if append:
            start = len(self._photos)
            self._photos.extend(photos)
            end = len(self._photos) - 1
            if start <= end:
                self.beginInsertRows(QModelIndex(), start, end)
                self.endInsertRows()
        else:
            self.beginResetModel()
            self._photos = photos
            self.endResetModel()
        return len(photos)

    def has_more(self) -> bool:
        return len(self._photos) < self._total

    @property
    def total(self) -> int:
        return self._total

    @property
    def province_name(self) -> str:
        return self._province_name

    @property
    def page(self) -> int:
        return self._page


class PhotoQueryWorker(QObject):
    """后台线程执行 DB 照片查询."""
    finished = Signal(list, int)  # photos, total
    error = Signal(str)

    def __init__(self, db: DatabaseManager, province_name: str,
                 page: int, page_size: int, parent=None) -> None:
        super().__init__(parent)
        self._db = db
        self._province_name = province_name
        self._page = page
        self._page_size = page_size

    @Slot()
    def run(self) -> None:
        try:
            if self._province_name == "Unclassified":
                photos, total = self._db.get_unclassified_photos(
                    page=self._page, page_size=self._page_size,
                )
            elif self._province_name:
                photos, total = self._db.query_by_province(
                    self._province_name, page=self._page,
                    page_size=self._page_size,
                )
            else:
                photos, total = [], 0
            self.finished.emit(photos, total)
        except Exception as e:
            self.error.emit(str(e))


class PhotoIdsQueryWorker(QObject):
    """后台线程通过 ID 列表查询照片."""
    finished = Signal(list)
    error = Signal(str)

    def __init__(self, db: DatabaseManager, ids: list[int], parent=None) -> None:
        super().__init__(parent)
        self._db = db
        self._ids = ids

    @Slot()
    def run(self) -> None:
        try:
            photos = self._db.get_photos_by_ids(self._ids)
            self.finished.emit(photos)
        except Exception as e:
            self.error.emit(str(e))


class ThumbnailDelegate(QStyledItemDelegate):
    """照片缩略图绘制代理 — 卡片风格 + QPixmapCache 内存缓存."""

    def paint(self, painter, option, index: QModelIndex) -> None:
        if not index.isValid():
            return

        file_path = index.data(Qt.UserRole + 1)
        thumbnail_path = index.data(Qt.UserRole + 2)
        photo_name = index.data(Qt.DisplayRole)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        # 卡片边距和圆角
        card_margin = 6
        card_rect = option.rect.adjusted(card_margin, card_margin, -card_margin, -card_margin)

        # 卡片投影
        shadow_color = QColor(128, 100, 70, 30)
        shadow_rect = card_rect.translated(0, 2)
        painter.setPen(Qt.NoPen)
        painter.setBrush(shadow_color)
        painter.drawRoundedRect(shadow_rect, 8, 8)

        # 卡片主体背景
        if option.state & QStyle.State_Selected:
            card_bg = QColor(Colors.ACCENT_SELECTED)
        elif option.state & QStyle.State_MouseOver:
            card_bg = QColor(Colors.ACCENT_HOVER_LIGHT)
        else:
            card_bg = QColor(Colors.CARD_BG)

        painter.setBrush(card_bg)
        painter.setPen(QPen(QColor(Colors.BORDER_LIGHT), 1))
        painter.drawRoundedRect(card_rect, 8, 8)

        # 图片区域(顶部圆角裁剪)
        img_margin = 8
        img_rect = card_rect.adjusted(img_margin, img_margin, -img_margin, -img_margin)
        text_height = 22
        img_rect.setHeight(max(0, card_rect.height() - img_margin * 2 - text_height))

        pixmap = self._load_thumbnail(file_path, thumbnail_path, img_rect.size())
        if pixmap and img_rect.isValid():
            clip_path = QPainterPath()
            clip_path.addRoundedRect(img_rect, 6, 6)
            painter.setClipPath(clip_path)
            painter.drawPixmap(img_rect, pixmap)
            painter.setClipping(False)
        elif img_rect.isValid():
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(Colors.WINDOW_BG))
            painter.drawRoundedRect(img_rect, 6, 6)
            painter.setPen(QColor(Colors.TEXT_DISABLED))
            painter.drawText(img_rect, Qt.AlignCenter, "无预览")

        # 文件名
        text_rect = card_rect.adjusted(img_margin + 4, 0, -img_margin - 4, -6)
        text_rect.setTop(img_rect.bottom() + 4)
        painter.setPen(QColor(Colors.TEXT_PRIMARY))
        font = painter.font()
        font.setPixelSize(11)
        painter.setFont(font)
        elided = painter.fontMetrics().elidedText(photo_name, Qt.ElideRight, text_rect.width())
        painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, elided)

        painter.restore()

    def sizeHint(self, option, index: QModelIndex) -> QSize:
        return QSize(THUMBNAIL_SIZE.width(), THUMBNAIL_SIZE.height() + 30)

    def _load_thumbnail(
        self, file_path: str, thumbnail_path: str, size: QSize,
    ) -> QPixmap | None:
        """从缓存或磁盘加载缩略图 QPixmap."""
        cache_key = file_path

        pixmap = QPixmap()
        if QPixmapCache.find(cache_key, pixmap):
            return self._scaled_if_needed(pixmap, size)

        source = thumbnail_path or file_path
        if not Path(source).exists():
            return None

        reader = QImageReader(source)
        reader.setAutoTransform(True)
        reader.setScaledSize(size * 2)
        pixmap = QPixmap.fromImageReader(reader)
        if not pixmap.isNull():
            scaled = self._scaled_if_needed(pixmap, size)
            QPixmapCache.insert(cache_key, scaled)
            return scaled

        return None

    @staticmethod
    def _scaled_if_needed(pixmap: QPixmap, size: QSize) -> QPixmap:
        if pixmap.size().width() > size.width() or pixmap.size().height() > size.height():
            return pixmap.scaled(size, Qt.KeepAspectRatio, Qt.FastTransformation)
        return pixmap


class PhotoGrid(QWidget):
    """照片网格视图 — 按省份分页浏览照片.

    包含返回按钮、省份标题、照片列表和翻页控制.
    """

    photoDoubleClicked = Signal(str, object, int)  # file_path, all_paths, index
    returnToMap = Signal()

    def __init__(self, db: DatabaseManager, parent=None) -> None:
        super().__init__(parent)
        self._db = db
        QPixmapCache.setCacheLimit(51200)
        self._is_loading = False

        # UI 组件
        self._back_btn = QPushButton("← 返回地图")
        self._back_btn.setProperty("cssClass", "ghost")

        self._province_label = QLabel()
        self._province_label.setFont(Fonts.title(16))
        self._province_label.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; padding: 12px 8px;")

        self._model = PhotoListModel(db, self)
        self._list_view = QListView()
        self._list_view.setModel(self._model)
        self._list_view.setItemDelegate(ThumbnailDelegate(self._list_view))
        self._list_view.setViewMode(QListView.IconMode)
        self._list_view.setIconSize(THUMBNAIL_SIZE)
        self._list_view.setGridSize(GRID_SPACING)
        self._list_view.setResizeMode(QListView.Adjust)
        self._list_view.setWrapping(True)
        self._list_view.setBatchSize(30)
        self._list_view.setUniformItemSizes(True)
        self._list_view.setSpacing(8)
        self._list_view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._list_view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self._list_view.verticalScrollBar().setSingleStep(40)
        self._list_view.setStyleSheet(f"""
            QListView {{
                border: none;
                background-color: {Colors.WINDOW_BG};
            }}
        """)

        self._empty_label = QLabel("该省份暂无照片")
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setFont(Fonts.title(14))
        self._empty_label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; padding: 40px;")
        self._empty_label.setVisible(False)

        # 布局
        top_bar = QHBoxLayout()
        top_bar.addWidget(self._back_btn)
        top_bar.addWidget(self._province_label, 1)

        layout = QVBoxLayout(self)
        layout.addLayout(top_bar)
        layout.addWidget(self._list_view, 1)
        layout.addWidget(self._empty_label, 1)

        # 信号连接
        self._back_btn.clicked.connect(self.returnToMap.emit)
        self._list_view.doubleClicked.connect(self._on_double_clicked)

        # 滚动到底部自动加载
        self._list_view.verticalScrollBar().valueChanged.connect(self._on_scroll)

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def load_province(self, province_name: str) -> None:
        """加载指定省份的照片."""
        province_changed = (
            self._model.province_name
            and self._model.province_name != province_name
        )
        if province_changed:
            QPixmapCache.clear()
            self._model.set_page_data([], 0)
        self._model._province_name = province_name
        self._model._page = 1
        self._province_label.setText(f"{province_name} (加载中...)")
        self._empty_label.setVisible(False)
        self._run_query(page=1, append=False)

    def clear(self) -> None:
        """清除当前显示."""
        self._model._province_name = ""
        self._model.set_page_data([], 0)
        self._province_label.setText("")

    def load_photo_ids(self, ids: list[int], title: str = "选中照片") -> None:
        """通过 ID 列表加载照片 (聚类点击等场景)."""
        QPixmapCache.clear()
        self._model.set_page_data([], 0)
        self._model._province_name = title
        self._province_label.setText(f"{title} (加载中...)")
        self._empty_label.setVisible(False)
        self._is_loading = True
        self._query_thread = QThread()
        self._query_worker = PhotoIdsQueryWorker(self._db, ids)
        self._query_worker.moveToThread(self._query_thread)
        self._query_thread.started.connect(self._query_worker.run)
        self._query_worker.finished.connect(
            lambda photos: self._on_ids_query_finished(photos),
        )
        self._query_worker.finished.connect(self._query_thread.quit)
        self._query_worker.finished.connect(self._query_worker.deleteLater)
        self._query_thread.finished.connect(self._query_thread.deleteLater)
        self._query_thread.start()

    def _on_ids_query_finished(self, photos: list[dict]) -> None:
        total = len(photos)
        self._model.set_page_data(photos, total, append=False)
        self._update_display()
        self._is_loading = False

    # ------------------------------------------------------------------
    # 内部逻辑
    # ------------------------------------------------------------------

    def _update_display(self) -> None:
        count = self._model.rowCount()
        total = self._model.total
        if total == 0 and self._model.province_name:
            self._province_label.setText(self._model.province_name)
            self._list_view.setVisible(False)
            self._empty_label.setVisible(True)
        else:
            self._empty_label.setVisible(False)
            self._list_view.setVisible(True)
            self._province_label.setText(
                f"{self._model.province_name} (第 {min(count, total)} / {total} 张)"
            )

    def _run_query(self, page: int, append: bool) -> None:
        self._is_loading = True
        province = self._model.province_name
        self._query_thread = QThread()
        self._query_worker = PhotoQueryWorker(
            self._db, province, page, PAGE_SIZE,
        )
        self._query_worker.moveToThread(self._query_thread)
        self._query_thread.started.connect(self._query_worker.run)
        self._query_worker.finished.connect(
            lambda photos, total, p=province, a=append: (
                self._on_query_finished(photos, total, a)
                if self._model.province_name == p
                else setattr(self, '_is_loading', False)
            ),
        )
        self._query_worker.finished.connect(self._query_thread.quit)
        self._query_worker.finished.connect(self._query_worker.deleteLater)
        self._query_thread.finished.connect(self._query_thread.deleteLater)
        self._query_thread.start()

    def _on_query_finished(
        self, photos: list[dict], total: int, append: bool,
    ) -> None:
        self._model.set_page_data(photos, total, append=append)
        self._update_display()
        self._is_loading = False

    def _on_scroll(self, value: int) -> None:
        sb = self._list_view.verticalScrollBar()
        if sb.maximum() <= 0:
            return
        progress = sb.value() / sb.maximum()
        if (progress >= 0.50
                and self._model.has_more()
                and not self._is_loading):
            self._model._page += 1
            self._run_query(page=self._model.page, append=True)

    def _on_double_clicked(self, idx: QModelIndex) -> None:
        file_path = idx.data(Qt.UserRole + 1)
        if not file_path:
            return
        all_paths = [
            self._model.index(r, 0).data(Qt.UserRole + 1)
            for r in range(self._model.rowCount())
        ]
        viewer = PhotoViewer(file_path, all_paths, idx.row(), self)
        viewer.exec()
