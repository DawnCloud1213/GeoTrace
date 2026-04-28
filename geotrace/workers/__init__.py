"""异步任务模块 - 基于 QThread 的后台 Workers."""

from PySide6.QtCore import QObject, Signal


class Worker(QObject):
    """所有 Worker 的基类."""

    finished = Signal()
    error = Signal(str)

    def run(self):
        raise NotImplementedError
