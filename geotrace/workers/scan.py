"""目录扫描异步 Worker — 全量磁盘扫描、EXIF 提取、空间定位、入库.

在 QThread 中运行, 通过 Signal/Slot 与主线程通信.
"""

import logging
import os
from pathlib import Path

from PySide6.QtCore import QThread, Signal, Slot

from geotrace.core.extractor import EXIFExtractor
from geotrace.core.models import PhotoMetadata
from geotrace.core.spatial import SpatialIndex
from geotrace.database.manager import DatabaseManager
from geotrace.workers import Worker

logger = logging.getLogger(__name__)

# 批量提交大小
BATCH_SIZE = 100


class ScanWorker(Worker):
    """照片目录扫描 Worker.

    工作流:
        1. os.walk 遍历目录
        2. 增量检查 (file_path + file_mtime)
        3. EXIF 提取
        4. 空间逆地理编码
        5. 批量 UPSERT 入库
        6. 进度信号实时推送

    Signals:
        progress(int, int): 进度 (current, total).
        fileProcessed(str): 当前处理的文件名.
        scanComplete(int, int): 扫描完成 (new_count, total_count).
        error(str): 错误消息.
        finished(): 任务完成.
    """

    progress = Signal(int, int)
    fileProcessed = Signal(str)
    scanComplete = Signal(int, int)

    def __init__(
        self,
        db: DatabaseManager,
        spatial_index: SpatialIndex,
        directories: list[str],
    ) -> None:
        super().__init__()
        self._db = db
        self._spatial_index = spatial_index
        self._directories = directories
        self._cancelled = False

    @Slot()
    def run(self) -> None:
        """在线程中执行扫描 (由 QThread.started 信号触发)."""
        try:
            self._do_scan()
        except Exception as e:
            logger.exception("扫描过程发生未处理异常")
            self.error.emit(str(e))
        finally:
            self.finished.emit()

    def cancel(self) -> None:
        """取消扫描."""
        self._cancelled = True

    # ------------------------------------------------------------------
    # 核心扫描逻辑
    # ------------------------------------------------------------------

    def _do_scan(self) -> None:
        extractor = EXIFExtractor()
        new_count = 0
        processed = 0

        for directory in self._directories:
            if self._cancelled:
                break

            self._db.add_directory(directory)

            # 第一阶段: 收集所有文件路径
            file_paths = self._collect_files(directory)
            total_in_dir = len(file_paths)
            logger.info("目录 '%s' 中共发现 %d 个候选文件", directory, total_in_dir)

            if total_in_dir == 0:
                self._db.update_directory_scan(directory, 0)
                continue

            # 第二阶段: 逐个处理
            batch: list[PhotoMetadata] = []
            dir_new = 0

            for i, file_path in enumerate(file_paths):
                if self._cancelled:
                    break

                self.progress.emit(processed + 1, total_in_dir)
                self.fileProcessed.emit(os.path.basename(file_path))

                # 增量检查
                try:
                    mtime = os.path.getmtime(file_path)
                except OSError:
                    continue

                if not self._db.photo_needs_update(file_path, mtime):
                    processed += 1
                    continue

                # EXIF 提取
                meta = extractor.extract(file_path)

                # 空间逆地理编码
                if meta.latitude is not None and meta.longitude is not None:
                    result = self._spatial_index.locate(meta.longitude, meta.latitude)
                    if result:
                        meta.province_code = result["code"]
                        meta.province_name = result["name"]
                    else:
                        meta.province_code = ""
                        meta.province_name = "Unclassified"
                else:
                    meta.province_code = ""
                    meta.province_name = "Unclassified"

                batch.append(meta)
                processed += 1

                # 批量提交
                if len(batch) >= BATCH_SIZE:
                    dir_new += self._db.batch_upsert_photos(batch)
                    batch.clear()

            # 提交剩余
            if batch:
                dir_new += self._db.batch_upsert_photos(batch)

            new_count += dir_new
            self._db.update_directory_scan(directory, total_in_dir)

        total = self._db.get_total_photo_count()
        self.scanComplete.emit(new_count, total)
        logger.info("扫描完成: 新增 %d 张, 总计 %d 张", new_count, total)

    # ------------------------------------------------------------------
    # 文件收集
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_files(directory: str) -> list[str]:
        """遍历目录收集所有支持的图片文件路径."""
        files: list[str] = []
        try:
            for root, _, filenames in os.walk(directory):
                for name in filenames:
                    if EXIFExtractor.is_supported(name):
                        files.append(os.path.join(root, name))
        except PermissionError as e:
            logger.warning("无权限访问目录: %s", e)
        return files
