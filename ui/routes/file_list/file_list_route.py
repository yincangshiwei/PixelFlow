"""FileListRoute —— 左侧文件列表区域 Controller。

职责：
- 左栏控件构建：文件按钮 / 缩略图列表 / 计数 / 选中失败项 / 预览
- ImportCollection 的控件投影：条目文本、缩略图、任务状态前缀与颜色
- 异步缩略图按 entry_id 校验，已删除条目的迟到结果被忽略
- 路径、相对路径、任务状态不保存在 item data role，控件只是状态投影

导入的“决策”（文件对话框、剪贴板、容器抽图、批处理会话）仍在 MainWindow，
本路由通过信号与之协作。
"""
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidgetItem, QSizePolicy,
)
from PySide6.QtGui import QPixmap, QColor

from core.batch_session import (
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    STATUS_FAILED,
    STATUS_CANCELLED,
)
from services.contracts.import_entry import ImportSource
from services.common.file_list import ImportCollection
from services.common.importing import IMAGE_EXTS
from ui.widgets.drop_list_widget import DropListWidget
from .thumbnail_loader import ThumbnailLoader, THUMB_SIZE

# 列表项仅保存 entry_id；其余状态一律来自 ImportCollection
ROLE_ENTRY_ID = Qt.UserRole

# 列表状态前缀（与缩略图并存，一眼可辨）
_STATUS_PREFIX = {
    STATUS_PENDING: "",
    STATUS_RUNNING: "… ",
    STATUS_SUCCESS: "✓ ",
    STATUS_FAILED: "✗ ",
    STATUS_CANCELLED: "⏸ ",
}
_STATUS_COLOR = {
    STATUS_PENDING: QColor(176, 180, 200),
    STATUS_RUNNING: QColor(120, 170, 255),
    STATUS_SUCCESS: QColor(110, 200, 140),
    STATUS_FAILED: QColor(240, 120, 120),
    STATUS_CANCELLED: QColor(230, 190, 100),
}
_STATUS_LABEL = {
    STATUS_PENDING: "未处理",
    STATUS_RUNNING: "处理中",
    STATUS_SUCCESS: "成功",
    STATUS_FAILED: "失败",
    STATUS_CANCELLED: "已取消（未完成）",
}


class FileListRoute(QWidget):
    """左栏文件列表路由（控件容器 + 区域 Controller）。"""

    # ─── 对窗口发布的协作信号 ───
    add_files_requested = Signal()          # 「添加文件」按钮
    add_folder_requested = Signal()         # 「添加文件夹」按钮
    select_failed_requested = Signal()      # 「选中失败项」按钮
    urls_dropped = Signal(list)             # 拖入 QUrl 列表（导入唯一入口）
    files_removed = Signal(list)            # 移除选中后，实际移除的路径
    cleared = Signal()                      # 清空完成（列表与预览已重置）
    current_changed = Signal(object, object)  # (path|None, (w,h)|None) 当前项变化
    selection_changed = Signal()            # 选择集变化（计数已同步刷新）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._collection = ImportCollection()
        self._item_by_path: dict[str, QListWidgetItem] = {}
        self._thumb_loader: ThumbnailLoader | None = None
        self._build_widget()

    # ─── 构建 ───
    def _build_widget(self):
        self.setObjectName("glass_panel")
        self.setFixedWidth(300)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        # 文件按钮
        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        self.btn_add_files = QPushButton("添加文件")
        self.btn_add_folder = QPushButton("添加文件夹")
        self.btn_remove = QPushButton("移除")
        self.btn_clear = QPushButton("清空")
        for b in [self.btn_add_files, self.btn_add_folder, self.btn_remove, self.btn_clear]:
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            btn_row.addWidget(b)
        lay.addLayout(btn_row)

        # 文件列表（缩略图）
        self.file_list = DropListWidget(thumb_size=THUMB_SIZE)
        lay.addWidget(self.file_list, 1)

        self.lbl_file_count = QLabel("共 0 个文件")
        self.lbl_file_count.setStyleSheet("color:#888;font-size:12px;")
        lay.addWidget(self.lbl_file_count)

        # 失败项快捷选择（配合「仅选中」重跑）
        fail_row = QHBoxLayout()
        fail_row.setSpacing(4)
        self.btn_select_failed = QPushButton("选中失败项")
        self.btn_select_failed.setToolTip("选中上一批处理失败的文件，可配合「仅选中」范围重新处理")
        self.btn_select_failed.setEnabled(False)
        self.btn_select_failed.setVisible(False)
        fail_row.addWidget(self.btn_select_failed)
        fail_row.addStretch()
        lay.addLayout(fail_row)

        # 预览
        self.preview_label = QLabel("点击列表预览图片")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setFixedHeight(220)
        self.preview_label.setObjectName("preview_area")
        lay.addWidget(self.preview_label)
        self.lbl_preview_info = QLabel("")
        self.lbl_preview_info.setAlignment(Qt.AlignCenter)
        self.lbl_preview_info.setStyleSheet("color:#888;font-size:11px;")
        lay.addWidget(self.lbl_preview_info)

        # ─── 信号 ───
        self.btn_add_files.clicked.connect(self.add_files_requested)
        self.btn_add_folder.clicked.connect(self.add_folder_requested)
        self.btn_select_failed.clicked.connect(self.select_failed_requested)
        self.btn_remove.clicked.connect(self._on_remove_clicked)
        self.btn_clear.clicked.connect(self._on_clear_clicked)
        self.file_list.urls_dropped.connect(self.urls_dropped)
        self.file_list.currentItemChanged.connect(self._on_current_item_changed)
        self.file_list.itemSelectionChanged.connect(self._on_selection_changed)

    # ─── 只读访问 ───
    @property
    def collection(self) -> ImportCollection:
        return self._collection

    def count(self) -> int:
        return len(self._collection)

    def selected_count(self) -> int:
        return len(self.file_list.selectedItems())

    def current_path(self) -> str | None:
        cur = self.file_list.currentItem()
        if cur is None:
            return None
        entry = self._collection.entry_by_id(cur.data(ROLE_ENTRY_ID))
        return entry.path if entry is not None else None

    def selected_paths(self) -> list[str]:
        out: list[str] = []
        for it in self.file_list.selectedItems():
            entry = self._collection.entry_by_id(it.data(ROLE_ENTRY_ID))
            if entry is not None:
                out.append(entry.path)
        return out

    def all_entries_snapshot(self) -> list[tuple[str, str | None]]:
        """全部条目输入快照 [(完整路径, 相对路径|None), ...]（集合顺序）。"""
        return self._collection.snapshot()

    def selected_entries_snapshot(self) -> list[tuple[str, str | None]]:
        """仅选中条目输入快照（按列表顺序，与重构前 selectedItems 遍历一致）。"""
        return self._collection.snapshot(self.selected_paths())

    # ─── 增删（供导入流程调用）───
    def insert_files(
        self,
        files,
        base_dir=None,
        source: ImportSource = ImportSource.LOCAL_FILE,
    ) -> int:
        """图片入库（去重）+ 列表投影 + 后台缩略图。返回实际新增数。"""
        added = self._collection.add_files(files, base_dir=base_dir, source=source)
        if not added:
            return 0

        # 暂停 UI 更新，提升批量添加性能
        self.file_list.setUpdatesEnabled(False)
        try:
            for entry in added:
                state = self._collection.state_by_id(entry.entry_id)
                item = QListWidgetItem(state.display_name)
                item.setData(ROLE_ENTRY_ID, entry.entry_id)
                item.setToolTip(entry.path)
                self.file_list.addItem(item)
                self._item_by_path[entry.path] = item
            # 对列表项排序（按显示名称，与重构前 sortItems 行为一致）
            self.file_list.sortItems(Qt.AscendingOrder)
            # 排序结果回写集合，保证处理顺序与控件显示一致
            self._collection.reorder([
                self.file_list.item(i).data(ROLE_ENTRY_ID)
                for i in range(self.file_list.count())
            ])
        finally:
            self.file_list.setUpdatesEnabled(True)

        self.refresh_count()

        # 后台加载缩略图（结果按 entry_id 校验迟到）
        tasks = [
            (e.entry_id, e.path)
            for e in added
            if Path(e.path).suffix.lower() in IMAGE_EXTS
        ]
        if tasks:
            self._thumb_loader = ThumbnailLoader(tasks, parent=self)
            self._thumb_loader.loaded.connect(self._on_thumb_loaded)
            self._thumb_loader.start()
        return len(added)

    def remove_selected(self) -> list[str]:
        """移除选中条目，返回实际移除的路径列表。"""
        selected_items = self.file_list.selectedItems()
        if not selected_items:
            return []

        paths: list[str] = []
        # 暂停 UI 更新，大幅提升批量删除性能
        self.file_list.setUpdatesEnabled(False)
        try:
            # 收集要删除的行号，从后往前删除避免索引变化
            rows_to_remove = sorted(
                [self.file_list.row(item) for item in selected_items], reverse=True
            )
            for row in rows_to_remove:
                item = self.file_list.item(row)
                entry = self._collection.entry_by_id(item.data(ROLE_ENTRY_ID))
                self.file_list.takeItem(row)
                if entry is not None:
                    self._item_by_path.pop(entry.path, None)
                    paths.append(entry.path)
            self._collection.remove_paths(paths)
        finally:
            self.file_list.setUpdatesEnabled(True)
        self.refresh_count()
        return paths

    def clear_files(self):
        """清空列表与预览。"""
        self.file_list.clear()
        self._item_by_path.clear()
        self._collection.clear()
        self._reset_preview()
        self.refresh_count()

    # ─── 任务状态投影 ───
    def set_job_status(self, path: str, status: str, error: str = ""):
        if not self._collection.set_job_status(path, status, error=error):
            return
        item = self._item_by_path.get(path)
        if item is not None:
            self._apply_item_job_status(item, status, error=error)

    def reset_job_status(self):
        self._collection.reset_job_status()
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            if item is None:
                continue
            self._apply_item_job_status(item, STATUS_PENDING, error="")

    def set_select_failed_button(self, visible: bool, enabled: bool | None = None):
        self.btn_select_failed.setVisible(visible)
        self.btn_select_failed.setEnabled(visible if enabled is None else enabled)

    def select_paths(self, paths) -> int:
        """清空选择后选中给定路径集合，返回实际选中数。"""
        self.file_list.clearSelection()
        first = None
        n = 0
        for path in paths:
            item = self._item_by_path.get(path)
            if item is None:
                continue
            item.setSelected(True)
            n += 1
            if first is None:
                first = item
        if first is not None:
            self.file_list.setCurrentItem(first)
            self.file_list.scrollToItem(first)
        return n

    # ─── 计数 / 提示 ───
    def refresh_count(self):
        total = len(self._collection)
        selected = len(self.file_list.selectedItems())
        self.lbl_file_count.setText(
            f"共 {total} 个文件" + (f"，已选 {selected}" if selected else "")
        )

    def set_status_tip(self, text: str):
        """临时状态提示（如抽图中），覆盖计数文本。"""
        self.lbl_file_count.setToolTip(text)
        self.lbl_file_count.setText(text)

    def clear_status_tip(self):
        self.lbl_file_count.setToolTip("")
        self.refresh_count()

    # ─── 内部：控件事件 ───
    def _on_remove_clicked(self):
        paths = self.remove_selected()
        if paths:
            self.files_removed.emit(paths)

    def _on_clear_clicked(self):
        self.clear_files()
        self.cleared.emit()

    def _on_selection_changed(self):
        self.refresh_count()
        self.selection_changed.emit()

    def _on_current_item_changed(self, current, _prev):
        if current is None:
            self._reset_preview()
            self.current_changed.emit(None, None)
            return
        entry = self._collection.entry_by_id(current.data(ROLE_ENTRY_ID))
        if entry is None:
            return
        path = entry.path
        pixmap = QPixmap(path)
        if pixmap.isNull():
            # Qt 不解 RAW 等格式：走统一解码
            try:
                from core.image_io import load_image_for_preview
                from ui.routes.file_list.thumbnail_loader import _pil_to_qimage

                pil = load_image_for_preview(path)
                try:
                    qimg = _pil_to_qimage(pil)
                    pixmap = QPixmap.fromImage(qimg)
                finally:
                    pil.close()
            except Exception:
                pixmap = QPixmap()
        if pixmap.isNull():
            self.preview_label.setText("无法加载预览")
            self.lbl_preview_info.setText("")
            self.current_changed.emit(path, None)
            return
        w, h = pixmap.width(), pixmap.height()
        scaled = pixmap.scaled(
            self.preview_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        self.preview_label.setPixmap(scaled)
        self.lbl_preview_info.setText(f"{Path(path).name}  ({w} × {h})")
        self.current_changed.emit(path, (w, h))

    def _reset_preview(self):
        self.preview_label.clear()
        self.preview_label.setText("点击列表预览图片")
        self.lbl_preview_info.setText("")

    def _on_thumb_loaded(self, entry_id: str, path: str, icon):
        # entry_id 校验：条目已移除/路径变化时，迟到结果直接忽略
        entry = self._collection.entry_by_id(entry_id)
        if entry is None or entry.path != path:
            return
        item = self._item_by_path.get(path)
        if item is not None:
            item.setIcon(icon)

    # ─── 内部：状态投影绘制 ───
    def _apply_item_job_status(self, item: QListWidgetItem, status: str, error: str = ""):
        if item is None:
            return
        entry = self._collection.entry_by_id(item.data(ROLE_ENTRY_ID))
        base = self._collection.display_name_of(entry.path) if entry is not None else item.text()
        prefix = _STATUS_PREFIX.get(status, "")
        item.setText(f"{prefix}{base}")
        color = _STATUS_COLOR.get(status, _STATUS_COLOR[STATUS_PENDING])
        item.setForeground(color)
        path = entry.path if entry is not None else ""
        tip_lines = [str(path)] if path else []
        tip_lines.append(f"状态: {_STATUS_LABEL.get(status, status)}")
        if error:
            tip_lines.append(f"错误: {error.splitlines()[0]}")
        item.setToolTip("\n".join(tip_lines))
