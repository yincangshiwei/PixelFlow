"""ImportCoordinator —— 文件导入协调（P5：自 MainWindow 迁出）。

职责（窗口级导入事件入口）：
- 添加文件 / 文件夹对话框（默认桌面路径）
- 拖放 QUrl / 程序化本地路径导入
- 窗口级粘贴（焦点规则 + 剪贴板动作链）
- HTML/DOCX/PDF 容器异步抽图（Worker 所有权、0.35s 防抖、忙状态光标）

导入决策在 services.common.importing.FileImportService（纯逻辑）；
Qt 剪贴板读取在 ui.adapters.clipboard_adapter；本协调器只执行计划。
"""
from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from services.common.importing import (
    ACTION_EXTRACT_HTML,
    ACTION_IMPORT_PATHS,
    ACTION_SAVE_IMAGE,
    ImportPlan,
    image_dialog_filter,
    run_extract_jobs,
)
from ui.adapters.clipboard_adapter import (
    grab_clipboard_image_path,
    read_clipboard_payload,
    urls_to_local_paths,
)


def _get_desktop_path() -> str:
    return str(Path.home() / "Desktop")


class ContainerImageExtractWorker(QThread):
    """
    后台从容器提取图片并落盘。
    - container_paths: 本地 HTML/DOCX/PDF 路径列表
    - html_jobs: 剪贴板 HTML 任务 list of (refs, html_base_str|None, page_url)
    """
    finished_ok = Signal(list)       # 本地路径列表
    finished_fail = Signal(str)      # 错误说明

    def __init__(
        self,
        container_paths: list[str] | None = None,
        html_jobs: list[tuple[list[str], str | None, str]] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._containers = list(container_paths or [])
        self._html_jobs = list(html_jobs or [])

    def run(self):
        try:
            all_paths = run_extract_jobs(self._containers, self._html_jobs)
            if all_paths:
                self.finished_ok.emit(all_paths)
            else:
                self.finished_fail.emit("未能提取到图片（文档无内嵌图、链接失效或格式不支持）")
        except Exception as e:
            self.finished_fail.emit(f"提取图片失败：{e}")


class ImportCoordinator:
    """导入协调器（非 QObject；由壳持有，生命周期与窗口一致）。"""

    def __init__(self, *, window, file_list_route, import_service):
        """
        :param window: 主窗口（对话框 / 弹窗父级）
        :param file_list_route: ui.routes.file_list.FileListRoute
        :param import_service: services.common.importing.FileImportService
        """
        self._window = window
        self._file_list_route = file_list_route
        self._import_service = import_service
        self._extract_worker: ContainerImageExtractWorker | None = None
        self._extract_last_ts = 0.0

    # ─── 对话框入口 ───

    def add_files_dialog(self):
        desktop_path = _get_desktop_path()
        files, _ = QFileDialog.getOpenFileNames(
            self._window, "选择文件", desktop_path,
            image_dialog_filter(),
        )
        if files:
            # 图片直接入库；HTML/DOCX/PDF 自动抽图
            self.import_local_paths(files)

    def add_folder_dialog(self):
        desktop_path = _get_desktop_path()
        folder = QFileDialog.getExistingDirectory(self._window, "选择文件夹", desktop_path)
        if folder:
            # 文件夹：图片 + 容器抽图 统一走导入
            self.import_local_paths([folder])

    # ─── 拖放 / 程序化导入 ───

    def import_urls(self, urls) -> int:
        """从 QUrl 列表导入本地文件/文件夹，返回直接加入列表的图片数。"""
        return self.import_local_paths(urls_to_local_paths(urls))

    def import_local_paths(self, local_paths: list[str | Path]) -> int:
        """
        导入本地路径：
        - 图片直接加入列表
        - HTML/DOCX/PDF 异步抽取内嵌图片后加入列表（文档本身不入库）
        返回直接加入列表的图片数（不含异步抽图）。
        """
        return self._execute_import_plan(
            self._import_service.plan_local_import(local_paths)
        )

    def _execute_import_plan(self, plan: ImportPlan) -> int:
        """执行导入计划：图片分组入库 + 容器异步抽图。返回直接入库数。"""
        total = 0
        for files, base_dir in plan.groups:
            total += len(files)
            self._file_list_route.insert_files(files, base_dir=base_dir)
        if plan.container_paths:
            self.start_container_extract(
                container_paths=plan.container_paths,
                busy_tip=plan.busy_tip,
            )
        return total

    # ─── 粘贴 ───

    @staticmethod
    def _focus_wants_native_paste(focus) -> bool:
        """焦点控件是否应使用原生文本粘贴（而非导入文件列表）。"""
        if focus is None:
            return False
        # 可编辑下拉内部焦点通常是 QLineEdit
        if focus.inherits("QLineEdit"):
            return not focus.isReadOnly()
        if focus.inherits("QAbstractSpinBox"):
            return True
        if focus.inherits("QTextEdit") or focus.inherits("QPlainTextEdit"):
            return not focus.isReadOnly()
        if focus.inherits("QComboBox"):
            return bool(focus.isEditable())
        return False

    def on_paste_shortcut(self):
        """窗口级粘贴：可编辑文本控件转发原生 paste，否则导入文件列表。"""
        focus = QApplication.focusWidget()
        # QShortcut 可能先于控件截获 Ctrl+V，对可编辑控件手动转发
        if self._focus_wants_native_paste(focus):
            if focus.inherits("QAbstractSpinBox"):
                le = focus.lineEdit()
                if le is not None:
                    le.paste()
                    return
            if hasattr(focus, "paste"):
                focus.paste()
            return
        self.paste_from_clipboard()

    def paste_from_clipboard(self):
        """
        从剪贴板导入（动作链由 FileImportService 决策，按序尝试、成功即止）：
        1) 文件/文件夹/HTML/DOCX/PDF 路径
        2) 位图数据 → 临时 PNG（落盘失败则继续后续动作）
        3) 剪贴板 HTML 中的图片（URL / base64）
        4) 纯文本本地路径
        """
        actions = self._import_service.plan_clipboard(read_clipboard_payload())
        for action in actions:
            if action.kind == ACTION_IMPORT_PATHS and action.import_plan is not None:
                self._execute_import_plan(action.import_plan)
                return
            if action.kind == ACTION_SAVE_IMAGE:
                saved = grab_clipboard_image_path()
                if saved:
                    self._file_list_route.insert_files([saved])
                    return
                continue
            if action.kind == ACTION_EXTRACT_HTML:
                self.start_container_extract(
                    html_jobs=[(action.html_refs, None, action.html_page_url)],
                    busy_tip=action.busy_tip,
                )
                return

    # ─── 容器抽图（异步 Worker 所有权）───

    def start_container_extract(
        self,
        *,
        container_paths: list[str] | None = None,
        html_jobs: list[tuple[list[str], str | None | Path, str]] | None = None,
        busy_tip: str = "正在提取图片…",
    ):
        """后台从 HTML/DOCX/PDF 或剪贴板 HTML 抽图并加入列表。"""
        if self._extract_worker is not None and self._extract_worker.isRunning():
            self._file_list_route.set_status_tip("正在提取图片，请稍候再试…")
            QTimer.singleShot(2500, self._update_file_count)
            return
        now = time.monotonic()
        if now - self._extract_last_ts < 0.35:
            return
        self._extract_last_ts = now

        containers = [str(p) for p in (container_paths or []) if p]
        norm_jobs = self._import_service.normalize_html_jobs(html_jobs)

        if not containers and not norm_jobs:
            return

        self._set_extract_busy(True, busy_tip)
        worker = ContainerImageExtractWorker(
            container_paths=containers,
            html_jobs=norm_jobs,
            parent=self._window,
        )
        self._extract_worker = worker
        worker.finished_ok.connect(self._on_extract_ok)
        worker.finished_fail.connect(self._on_extract_fail)
        worker.finished.connect(self._on_extract_finished)
        worker.start()

    def _set_extract_busy(self, busy: bool, tip: str = ""):
        """抽图期间的轻量状态提示。"""
        try:
            if busy:
                QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))
                if tip:
                    self._file_list_route.set_status_tip(tip)
            else:
                while QApplication.overrideCursor() is not None:
                    QApplication.restoreOverrideCursor()
                self._file_list_route.clear_status_tip()
                self._update_file_count()
        except Exception:
            pass

    def _on_extract_ok(self, paths: list):
        if paths:
            self._file_list_route.insert_files(list(paths))

    def _on_extract_fail(self, message: str):
        try:
            self._file_list_route.set_status_tip(message or "抽图失败")
            QTimer.singleShot(3500, self._update_file_count)
        except Exception:
            pass
        QMessageBox.information(self._window, "提取图片", message or "未能提取到图片")

    def _on_extract_finished(self):
        self._set_extract_busy(False)
        w = self._extract_worker
        self._extract_worker = None
        if w is not None:
            w.deleteLater()

    def _update_file_count(self):
        self._file_list_route.refresh_count()

    # ─── 窗口关闭 ───

    def wait_extract_worker(self, wait_ms: int = 3000) -> None:
        """关闭时有界等待抽图线程，避免销毁运行中的 QThread。"""
        if self._extract_worker is not None and self._extract_worker.isRunning():
            self._extract_worker.wait(wait_ms)
