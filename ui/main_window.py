"""
PixelFlow 主窗口
四大区域：① 图片列表  ② 功能菜单+参数  ③ 输出设置  ④ 处理日志
"""
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QLabel, QPushButton, QLineEdit, QComboBox, QCheckBox,
    QFileDialog, QListWidget, QListWidgetItem, QProgressBar,
    QSplitter, QTextEdit, QTextBrowser, QAbstractItemView, QMessageBox,
    QStackedWidget, QSizePolicy, QRadioButton, QButtonGroup,
    QInputDialog, QMenu, QScrollArea, QWidgetAction
)
from PySide6.QtCore import Qt, QSize, QThread, Signal
from PySide6.QtGui import (
    QPixmap, QIcon, QDragEnterEvent, QDropEvent, QImage,
    QPainter, QLinearGradient, QColor, QPaintEvent
)

from config import APP_TITLE, APP_NAME, APP_VERSION, RESOURCES_DIR, APP_COPYRIGHT, APP_COPYRIGHT_URL
from core.base_processor import get_all_processors, BaseProcessor
from core.base_file_processor import get_all_file_processors, BaseFileProcessor
from core.preset_manager import PresetManager
# 导入处理器以触发注册
import core.processors.transparent_processor  # noqa: F401
import core.processors.basic_processor        # noqa: F401
import core.processors.img2doc_processor      # noqa: F401
import core.processors.overlay_processor      # noqa: F401
import core.processors.metadata_processor     # noqa: F401
from core.worker import ProcessWorker
from core.file_worker import FileProcessWorker
from core.log_manager import AppLogManager
from core.batch_session import (
    BatchSession,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    STATUS_FAILED,
    STATUS_CANCELLED,
)
from ui.settings_panel import SettingsPanel

# 图片格式
IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff', '.tif', '.gif'}
# 文档格式
DOC_EXTS = {'.docx', '.pdf'}
# 全部支持的格式（用于拖放判断）
VALID_EXTS = IMAGE_EXTS | DOC_EXTS
THUMB_SIZE = QSize(48, 48)
_RES_DIR = str(RESOURCES_DIR).replace("\\", "/")
# 列表项数据角色：完整路径 / 相对导入根目录的路径（用于保留目录结构）
ROLE_PATH = Qt.UserRole
ROLE_REL_PATH = Qt.UserRole + 1
ROLE_BASE_TEXT = Qt.UserRole + 2  # 不含状态前缀的显示名
ROLE_JOB_STATUS = Qt.UserRole + 3

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


def _get_desktop_path() -> str:
    return str(Path.home() / "Desktop")


class _ThumbnailLoader(QThread):
    """后台加载缩略图"""
    loaded = Signal(str, QIcon)  # path, icon

    def __init__(self, paths: list[str], parent=None):
        super().__init__(parent)
        self._paths = paths

    def run(self):
        for p in self._paths:
            try:
                img = QImage(p)
                if not img.isNull():
                    scaled = img.scaled(THUMB_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    pix = QPixmap.fromImage(scaled)
                    self.loaded.emit(p, QIcon(pix))
            except Exception:
                pass


class GradientBackground(QWidget):
    """绘制深色渐变背景，为毛玻璃效果提供底层氛围"""
    def paintEvent(self, event: QPaintEvent):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        grad = QLinearGradient(0, 0, self.width(), self.height())
        grad.setColorAt(0.0, QColor(18, 18, 36))
        grad.setColorAt(0.4, QColor(22, 22, 42))
        grad.setColorAt(0.7, QColor(28, 24, 48))
        grad.setColorAt(1.0, QColor(20, 20, 38))
        painter.fillRect(self.rect(), grad)
        painter.end()


class DropListWidget(QListWidget):
    """支持拖放、缩略图显示的文件列表"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.NoDragDrop)
        self.setIconSize(THUMB_SIZE)
        self.setSpacing(2)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        urls = event.mimeData().urls()
        for url in urls:
            p = Path(url.toLocalFile())
            paths = []
            base_dir = None
            if p.is_file() and p.suffix.lower() in VALID_EXTS:
                paths.append(str(p))
            elif p.is_dir():
                base_dir = str(p)
                for f in sorted(p.rglob("*")):
                    if f.is_file() and f.suffix.lower() in VALID_EXTS:
                        paths.append(str(f))
            if paths:
                self.window()._insert_files(paths, base_dir=base_dir)
        event.acceptProposedAction()

    def _is_image(self, path: str) -> bool:
        return Path(path).suffix.lower() in IMAGE_EXTS


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)

        # 按屏幕分辨率自适应：初始 85% 屏幕尺寸，最小不低于 1000×650
        from PySide6.QtWidgets import QApplication
        screen = QApplication.primaryScreen().availableGeometry()
        init_w = max(1000, int(screen.width() * 0.75))
        init_h = max(650, int(screen.height() * 0.75))
        min_w = max(1000, int(screen.width() * 0.50))
        min_h = max(650, int(screen.height() * 0.50))
        self.setMinimumSize(min_w, min_h)
        self.resize(init_w, init_h)
        # 居中显示
        self.move(
            screen.x() + (screen.width() - init_w) // 2,
            screen.y() + (screen.height() - init_h) // 2,
        )

        self.worker = None
        self._thumb_loader = None
        # 本批次处理计时（点击「开始处理」起算）
        self._process_t0_mono: float | None = None
        self._process_t0_wall: datetime | None = None
        # 路径到列表项的映射缓存，加速缩略图加载时的查找
        self._path_to_item: dict[str, QListWidgetItem] = {}
        # 最近一次可续跑/重试的批处理会话（内存）
        self._batch_session: BatchSession | None = None
        self._run_mode: str = "full"  # full | continue | retry_failed
        # 图片处理器（BaseProcessor 体系）
        self._processors: list[BaseProcessor] = []
        self._current_processor: BaseProcessor | None = None
        # 文件处理器（BaseFileProcessor 体系）
        self._file_processors: list[BaseFileProcessor] = []
        self._current_file_processor: BaseFileProcessor | None = None
        # 预设管理器（两套体系共用 PresetManager，按 preset_id 区分）
        self._preset_managers: dict[str, PresetManager] = {}
        self._log_manager = AppLogManager()

        self._init_processors()
        self._build_ui()
        self._apply_style()
        self._load_default_presets()

    # ─── 初始化处理器 ───
    def _init_processors(self):
        for cls in get_all_processors():
            p = cls()
            self._processors.append(p)
            self._preset_managers[p.preset_id] = PresetManager(p.preset_id)
        for cls in get_all_file_processors():
            p = cls()
            self._file_processors.append(p)
            self._preset_managers[p.preset_id] = PresetManager(p.preset_id)

    # ─── 构建 UI ───
    def _build_ui(self):
        central = GradientBackground()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(12)

        # ════════ 左栏：图片列表区 ════════
        left = QWidget()
        left.setObjectName("glass_panel")
        left.setFixedWidth(300)
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(8)

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
        left_lay.addLayout(btn_row)

        # 文件列表（缩略图）
        self.file_list = DropListWidget()
        left_lay.addWidget(self.file_list, 1)

        self.lbl_file_count = QLabel("共 0 个文件")
        self.lbl_file_count.setStyleSheet("color:#888;font-size:12px;")
        left_lay.addWidget(self.lbl_file_count)

        # 失败项快捷选择（配合「仅选中」重跑）
        fail_row = QHBoxLayout()
        fail_row.setSpacing(4)
        self.btn_select_failed = QPushButton("选中失败项")
        self.btn_select_failed.setToolTip("选中上一批处理失败的文件，可配合「仅选中」范围重新处理")
        self.btn_select_failed.setEnabled(False)
        self.btn_select_failed.setVisible(False)
        fail_row.addWidget(self.btn_select_failed)
        fail_row.addStretch()
        left_lay.addLayout(fail_row)

        # 预览
        self.preview_label = QLabel("点击列表预览图片")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setFixedHeight(220)
        self.preview_label.setObjectName("preview_area")
        left_lay.addWidget(self.preview_label)
        self.lbl_preview_info = QLabel("")
        self.lbl_preview_info.setAlignment(Qt.AlignCenter)
        self.lbl_preview_info.setStyleSheet("color:#888;font-size:11px;")
        left_lay.addWidget(self.lbl_preview_info)

        root.addWidget(left)

        # ════════ 右栏：Tab切换（图像处理 / 后台日志）+ 输出设置 ════════
        right = QWidget()
        right.setObjectName("glass_panel")
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(12, 12, 12, 12)
        right_lay.setSpacing(0)

        # ── 主内容区（StackedWidget：处理面板 / 日志面板）──
        self.main_stack = QStackedWidget()

        # --- 页面0：图像处理 ---
        proc_page = QWidget()
        proc_page_lay = QVBoxLayout(proc_page)
        proc_page_lay.setContentsMargins(0, 0, 0, 0)
        proc_page_lay.setSpacing(8)

        # Tab 标签栏（嵌入在内容区顶部，作为 GroupBox 标题行）
        tab_row = QHBoxLayout()
        tab_row.setSpacing(0)
        tab_row.setContentsMargins(0, 0, 0, 0)
        self.btn_tab_process = QPushButton("  图像处理  ")
        self.btn_tab_process.setObjectName("tab_active")
        self.btn_tab_process.setMinimumHeight(34)
        self.btn_tab_log = QPushButton("  后台日志  ")
        self.btn_tab_log.setObjectName("tab_inactive")
        self.btn_tab_log.setMinimumHeight(34)
        self.btn_tab_settings = QPushButton("  配置  ")
        self.btn_tab_settings.setObjectName("tab_inactive")
        self.btn_tab_settings.setMinimumHeight(34)
        self.btn_tab_changelog = QPushButton("  版本日志  ")
        self.btn_tab_changelog.setObjectName("tab_inactive")
        self.btn_tab_changelog.setMinimumHeight(34)
        tab_row.addWidget(self.btn_tab_process)
        tab_row.addWidget(self.btn_tab_log)
        tab_row.addWidget(self.btn_tab_settings)
        tab_row.addWidget(self.btn_tab_changelog)
        tab_row.addStretch()
        right_lay.addLayout(tab_row)

        proc_group = QGroupBox()
        proc_group.setObjectName("tab_content_group")
        proc_lay = QVBoxLayout(proc_group)

        # 功能选择栏
        menu_row = QHBoxLayout()
        menu_row.setSpacing(6)
        menu_row.addWidget(QLabel("功能:"))
        self.combo_processor = QComboBox()
        # 图片处理器（data 存 ("image", index)）
        for idx, p in enumerate(self._processors):
            self.combo_processor.addItem(f"{p.icon}  {p.name}", ("image", idx))
        # 文件处理器（data 存 ("file", index)）
        for idx, p in enumerate(self._file_processors):
            self.combo_processor.addItem(f"{p.icon}  {p.name}", ("file", idx))
        self.combo_processor.setMinimumWidth(180)
        menu_row.addWidget(self.combo_processor)
        self.lbl_proc_desc = QLabel("")
        self.lbl_proc_desc.setStyleSheet("color:#888;font-size:12px;")
        menu_row.addWidget(self.lbl_proc_desc, 1)
        proc_lay.addLayout(menu_row)

        # 预设设置栏
        preset_row = QHBoxLayout()
        preset_row.setSpacing(6)
        preset_row.addWidget(QLabel("预设:"))
        self.combo_preset = QComboBox()
        self.combo_preset.setMinimumWidth(160)
        preset_row.addWidget(self.combo_preset)
        self.btn_load_preset_file = QPushButton("📤")
        self.btn_load_preset_file.setToolTip("加载预设")
        self.btn_save_preset = QPushButton("💾")
        self.btn_save_preset.setToolTip("保存预设")
        self.btn_reset_default = QPushButton("🔄")
        self.btn_reset_default.setToolTip("恢复默认")
        self.btn_delete_preset = QPushButton("🗑️")
        self.btn_delete_preset.setToolTip("删除预设")
        self.btn_locate_preset = QPushButton("📂")
        self.btn_locate_preset.setToolTip("定位预设")
        preset_row.addWidget(self.btn_load_preset_file)
        preset_row.addWidget(self.btn_save_preset)
        preset_row.addWidget(self.btn_reset_default)
        preset_row.addWidget(self.btn_delete_preset)
        preset_row.addWidget(self.btn_locate_preset)
        preset_row.addStretch()
        proc_lay.addLayout(preset_row)

        # 参数面板容器（StackedWidget）
        self.panel_stack = QStackedWidget()
        for p in self._processors:
            panel = p.create_panel()
            self.panel_stack.addWidget(panel)
        for p in self._file_processors:
            panel = p.create_panel()
            self.panel_stack.addWidget(panel)
            
        # 将 panel_stack 放入滚动区域
        self.panel_scroll_area = QScrollArea()
        self.panel_scroll_area.setWidgetResizable(True)
        self.panel_scroll_area.setFrameShape(QScrollArea.NoFrame)
        self.panel_scroll_area.setStyleSheet("background: transparent;")
        self.panel_scroll_area.setWidget(self.panel_stack)
        
        proc_lay.addWidget(self.panel_scroll_area, 1)
        proc_page_lay.addWidget(proc_group, 1)

        self.main_stack.addWidget(proc_page)  # index 0

        # --- 页面1：后台日志 ---
        log_page = QWidget()
        log_page.setObjectName("tab_content_group")
        log_page_lay = QVBoxLayout(log_page)
        log_page_lay.setContentsMargins(12, 12, 12, 12)
        log_page_lay.setSpacing(8)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        log_page_lay.addWidget(self.log_text, 1)

        log_btn_row = QHBoxLayout()
        log_btn_row.addStretch()
        self.btn_clear_log = QPushButton("清空日志")
        log_btn_row.addWidget(self.btn_clear_log)
        log_page_lay.addLayout(log_btn_row)

        self.main_stack.addWidget(log_page)  # index 1

        # --- 页面2：配置（懒加载，避免启动时扫描 Python/环境卡顿）---
        settings_page = QWidget()
        settings_page.setObjectName("tab_content_group")
        settings_page_lay = QVBoxLayout(settings_page)
        settings_page_lay.setContentsMargins(12, 12, 12, 12)
        settings_page_lay.setSpacing(8)
        self._settings_page_lay = settings_page_lay
        self.settings_panel = None  # 首次切入配置 Tab 时再创建
        self._settings_placeholder = QLabel("正在加载配置…")
        self._settings_placeholder.setAlignment(Qt.AlignCenter)
        self._settings_placeholder.setStyleSheet("color:#8a90b0;")
        settings_page_lay.addWidget(self._settings_placeholder, 1)
        self.main_stack.addWidget(settings_page)  # index 2

        # --- 页面3：版本日志 ---
        changelog_page = QWidget()
        changelog_page.setObjectName("tab_content_group")
        changelog_page_lay = QVBoxLayout(changelog_page)
        changelog_page_lay.setContentsMargins(12, 12, 12, 12)
        changelog_page_lay.setSpacing(8)

        self.changelog_browser = QTextBrowser()
        self.changelog_browser.setOpenExternalLinks(False)
        self.changelog_browser.setReadOnly(True)
        self._load_changelog()
        changelog_page_lay.addWidget(self.changelog_browser, 1)

        self.main_stack.addWidget(changelog_page)  # index 3

        right_lay.addWidget(self.main_stack, 1)
        right_lay.addSpacing(8)

        # ── 输出设置 ──
        out_group = QGroupBox("输出设置")
        out_lay = QVBoxLayout(out_group)
        out_lay.setSpacing(8)

        # 路径模式
        path_row = QHBoxLayout()
        path_row.setSpacing(10)
        self.rb_desktop = QRadioButton("桌面路径")
        self.rb_custom = QRadioButton("自定义路径")
        self.rb_overwrite = QRadioButton("原图路径 (覆盖原图)")
        self.rb_copy = QRadioButton("原图路径 (另存副本)")
        self.rb_desktop.setChecked(True)

        self.path_group = QButtonGroup(self)
        for i, rb in enumerate([self.rb_desktop, self.rb_custom, self.rb_overwrite, self.rb_copy]):
            self.path_group.addButton(rb, i)
            path_row.addWidget(rb)
        path_row.addStretch()
        out_lay.addLayout(path_row)

        # 路径输入行（桌面/自定义共用）
        path_input_row = QHBoxLayout()
        self.txt_output_dir = QLineEdit()
        self.txt_output_dir.setText(_get_desktop_path())
        path_input_row.addWidget(self.txt_output_dir, 1)
        self.btn_browse = QPushButton("浏览...")
        self.btn_browse.setVisible(False)  # 默认桌面模式不显示浏览
        path_input_row.addWidget(self.btn_browse)
        out_lay.addLayout(path_input_row)

        # 原图路径提示标签（覆盖/副本模式时显示）
        self.lbl_src_hint = QLabel("输出到每张图片的原始所在目录")
        self.lbl_src_hint.setStyleSheet("color:#888;font-size:12px;font-style:italic;")
        self.lbl_src_hint.setVisible(False)
        out_lay.addWidget(self.lbl_src_hint)

        # 自动创建文件夹 + 保留目录结构 + 覆盖同名 + 保留抠图（水平并排）
        out_opt_row = QHBoxLayout()
        out_opt_row.setSpacing(16)
        self.chk_auto_folder = QCheckBox("在该路径下自动创建文件夹保存")
        self.chk_auto_folder.setChecked(True)
        self.chk_auto_folder.setToolTip("在所选输出路径下自动创建 PixelFlow_output 子文件夹")
        out_opt_row.addWidget(self.chk_auto_folder)
        self.chk_keep_structure = QCheckBox("保留目录结构")
        self.chk_keep_structure.setChecked(True)
        self.chk_keep_structure.setToolTip(
            "添加文件夹时，按图片相对导入根目录的路径，\n"
            "在输出目录下重建对应子文件夹（仅桌面/自定义路径模式有效）。\n"
            "单独添加的文件或无相对路径时仍平铺输出。"
        )
        out_opt_row.addWidget(self.chk_keep_structure)
        self.chk_overwrite_file = QCheckBox("覆盖同名文件")
        self.chk_overwrite_file.setChecked(False)
        self.chk_overwrite_file.setToolTip(
            "勾选后若输出目录已存在同名文件则直接覆盖；\n"
            "未勾选时自动在文件名后追加 _1、_2… 后缀避免覆盖。\n"
            "仅桌面路径 / 自定义路径模式有效。"
        )
        out_opt_row.addWidget(self.chk_overwrite_file)
        # 透明图 + AI 抠图时显示：额外保留一份抠图结果
        self.chk_keep_matting = QCheckBox("保留抠图结果")
        self.chk_keep_matting.setChecked(False)
        self.chk_keep_matting.setVisible(False)
        self.chk_keep_matting.setToolTip(
            "额外保存一份 AI 抠图后的透明图（PNG）。\n"
            "若同时开启「裁剪透明边缘」，保留的是裁剪后的透明主体。\n"
            "文件名在主输出名后追加 _matted（例如 photo.png → photo_matted.png，\n"
            "若主输出因重名变为 photo_1.png，则为 photo_1_matted.png）。"
        )
        out_opt_row.addWidget(self.chk_keep_matting)
        out_opt_row.addStretch()
        out_lay.addLayout(out_opt_row)

        # 处理范围（全局：所有功能共用）
        scope_row = QHBoxLayout()
        scope_row.setSpacing(12)
        scope_row.addWidget(QLabel("处理范围:"))
        self.rb_scope_all = QRadioButton("全部文件")
        self.rb_scope_selected = QRadioButton("仅选中")
        self.rb_scope_all.setChecked(True)
        self.rb_scope_all.setToolTip("处理左侧列表中的全部文件")
        self.rb_scope_selected.setToolTip(
            "只处理左侧当前选中的文件（可多选）。\n"
            "适合单张微调；元数据编辑在选中单图时会自动回读属性。"
        )
        self.scope_group = QButtonGroup(self)
        self.scope_group.addButton(self.rb_scope_all, 0)
        self.scope_group.addButton(self.rb_scope_selected, 1)
        scope_row.addWidget(self.rb_scope_all)
        scope_row.addWidget(self.rb_scope_selected)
        self.lbl_scope_hint = QLabel("")
        self.lbl_scope_hint.setStyleSheet("color:#8a90b0;font-size:11px;")
        scope_row.addWidget(self.lbl_scope_hint, 1)
        out_lay.addLayout(scope_row)

        right_lay.addWidget(out_group)
        right_lay.addSpacing(8)

        # ── 底部操作栏 ──
        bottom = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%v / %m")
        bottom.addWidget(self.progress_bar, 1)
        self.btn_continue = QPushButton("继续")
        self.btn_continue.setObjectName("btn_resume")
        self.btn_continue.setMinimumHeight(38)
        self.btn_continue.setVisible(False)
        self.btn_continue.setToolTip(
            "从取消后剩余的未完成文件继续处理（含取消时中断的那张）。\n"
            "使用该批次保存的参数与输出设置。"
        )
        bottom.addWidget(self.btn_continue)
        self.btn_retry_failed = QPushButton("重试失败")
        self.btn_retry_failed.setObjectName("btn_resume")
        self.btn_retry_failed.setMinimumHeight(38)
        self.btn_retry_failed.setVisible(False)
        self.btn_retry_failed.setToolTip(
            "仅重新处理上一批失败的文件。\n"
            "使用该批次保存的参数与输出设置。"
        )
        bottom.addWidget(self.btn_retry_failed)
        self.btn_start = QPushButton("  开始处理  ")
        self.btn_start.setObjectName("btn_start")
        self.btn_start.setMinimumHeight(38)
        bottom.addWidget(self.btn_start)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setVisible(False)
        self.btn_cancel.setMinimumHeight(38)
        bottom.addWidget(self.btn_cancel)
        right_lay.addLayout(bottom)

        # ── 版权信息 ──
        copyright_label = QLabel(
            f'软件版权归：'
            f'<a href="{APP_COPYRIGHT_URL}" style="color:#5a6080;text-decoration:none;font-size:11px;">'
            f'@{APP_COPYRIGHT}</a>'
            f'  所有'
        )
        copyright_label.setAlignment(Qt.AlignCenter)
        copyright_label.setOpenExternalLinks(True)
        copyright_label.setToolTip(APP_COPYRIGHT_URL)
        copyright_label.setCursor(Qt.PointingHandCursor)
        right_lay.addWidget(copyright_label)

        root.addWidget(right, 1)

        # ─── 菜单栏（关于）───
        self._build_menubar()

        # ─── 信号 ───
        self.btn_add_files.clicked.connect(self._add_files)
        self.btn_add_folder.clicked.connect(self._add_folder)
        self.btn_clear.clicked.connect(self._clear_files)
        self.btn_remove.clicked.connect(self._remove_selected)
        self.btn_browse.clicked.connect(self._browse_output)
        self.btn_start.clicked.connect(self._start_process)
        self.btn_continue.clicked.connect(self._continue_process)
        self.btn_retry_failed.clicked.connect(self._retry_failed_process)
        self.btn_cancel.clicked.connect(self._cancel_process)
        self.btn_select_failed.clicked.connect(self._select_failed_files)
        self.file_list.currentItemChanged.connect(self._on_file_selected)
        self.file_list.itemSelectionChanged.connect(self._on_selection_changed)
        self.file_list.model().rowsInserted.connect(self._update_file_count)
        self.file_list.model().rowsRemoved.connect(self._update_file_count)
        self.file_list.model().modelReset.connect(self._update_file_count)
        self.combo_processor.currentIndexChanged.connect(self._on_processor_changed)
        self.path_group.idToggled.connect(self._on_path_mode_changed)
        self.scope_group.idToggled.connect(self._on_scope_changed)
        self.combo_preset.currentIndexChanged.connect(self._on_preset_selected)
        self.btn_load_preset_file.clicked.connect(self._load_preset_file)
        self.btn_save_preset.clicked.connect(self._save_preset)
        self.btn_reset_default.clicked.connect(self._reset_default)
        self.btn_delete_preset.clicked.connect(self._delete_preset)
        self.btn_locate_preset.clicked.connect(self._locate_preset)
        self.btn_tab_process.clicked.connect(lambda: self._switch_tab(0))
        self.btn_tab_log.clicked.connect(lambda: self._switch_tab(1))
        self.btn_tab_settings.clicked.connect(lambda: self._switch_tab(2))
        self.btn_tab_changelog.clicked.connect(lambda: self._switch_tab(3))
        self.btn_clear_log.clicked.connect(self._clear_current_log)

        # 透明图 AI 抠图开关变化时，同步「保留抠图结果」可见性
        for p in self._processors:
            if getattr(p, "preset_id", "") == "transparent_image":
                grp = getattr(p, "_grp_matting", None)
                if grp is not None:
                    grp.toggled.connect(lambda _checked: self._update_output_extra_opts())

        # 初始状态
        if self._processors:
            self._on_processor_changed(0)
        else:
            self._update_output_extra_opts()

    # ─── 样式 ───
    def _apply_style(self):
        self.setStyleSheet(f"""
            /* ═══ 全局基础 ═══ */
            * {{
                color: #e0e4f0;
                font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
                font-size: 13px;
            }}
            QMainWindow {{
                background: transparent;
            }}
            QWidget {{
                background: transparent;
            }}

            /* ═══ 毛玻璃面板 ═══ */
            QWidget#glass_panel {{
                background: rgba(30, 30, 52, 160);
                border: 1px solid rgba(120, 130, 180, 0.15);
                border-radius: 14px;
            }}

            /* ═══ GroupBox — 内嵌毛玻璃卡片 ═══ */
            QGroupBox {{
                font-weight: bold; font-size: 13px; color: #c0c6d8;
                border: 1px solid rgba(100, 110, 170, 0.18);
                border-radius: 10px;
                margin-top: 14px; padding: 18px 12px 12px 12px;
                background: rgba(38, 38, 62, 140);
            }}
            QGroupBox::title {{
                subcontrol-origin: margin; left: 14px; padding: 2px 10px;
                background: rgba(50, 50, 80, 180);
                border-radius: 6px;
                color: #a8b0d0;
            }}
            QGroupBox::indicator {{ width: 18px; height: 18px; }}
            QGroupBox::indicator:checked {{ image: url({_RES_DIR}/check_on.svg); }}
            QGroupBox::indicator:unchecked {{ image: url({_RES_DIR}/check_off.svg); }}

            /* ═══ 按钮 — 磨砂质感 ═══ */
            QPushButton {{
                padding: 6px 14px;
                border: 1px solid rgba(100, 110, 170, 0.25);
                border-radius: 7px;
                background: rgba(50, 50, 80, 140);
                color: #d0d4e0;
            }}
            QPushButton:hover {{
                background: rgba(70, 75, 120, 160);
                border-color: rgba(100, 140, 255, 0.5);
                color: #fff;
            }}
            QPushButton:pressed {{
                background: rgba(40, 40, 70, 180);
            }}

            /* 开始处理按钮 — 渐变发光 */
            QPushButton#btn_start {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1,
                    stop:0 rgba(91,138,245,220), stop:1 rgba(140,110,255,220));
                color: #fff; font-weight: bold; font-size: 14px;
                border: 1px solid rgba(140, 160, 255, 0.3);
                border-radius: 9px; padding: 8px 28px;
            }}
            QPushButton#btn_start:hover {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1,
                    stop:0 rgba(107,154,255,240), stop:1 rgba(155,130,255,240));
                border-color: rgba(160, 180, 255, 0.5);
            }}
            QPushButton#btn_start:disabled {{
                background: rgba(50, 50, 70, 140);
                color: #555;
                border-color: rgba(80, 80, 100, 0.2);
            }}

            /* 继续 / 重试失败 */
            QPushButton#btn_resume {{
                background: rgba(55, 70, 120, 160);
                color: #d8e0ff;
                border: 1px solid rgba(100, 140, 255, 0.35);
                border-radius: 8px;
                padding: 6px 14px;
            }}
            QPushButton#btn_resume:hover {{
                background: rgba(70, 90, 150, 190);
                border-color: rgba(130, 170, 255, 0.55);
                color: #fff;
            }}
            QPushButton#btn_resume:disabled {{
                background: rgba(40, 40, 60, 100);
                color: #555;
                border-color: rgba(80, 80, 100, 0.15);
            }}

            /* Tab 按钮 */
            QPushButton#tab_active {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 rgba(91,138,245,200), stop:1 rgba(120,110,245,200));
                color: #fff; font-weight: bold;
                border: 1px solid rgba(130, 150, 255, 0.25);
                border-bottom: 2px solid rgba(38, 38, 62, 0);
                border-radius: 8px 8px 0 0; padding: 8px 20px;
            }}
            QPushButton#tab_inactive {{
                background: rgba(42, 42, 68, 120);
                color: #777; font-weight: normal;
                border: 1px solid rgba(80, 80, 120, 0.2);
                border-bottom: none;
                border-radius: 8px 8px 0 0; padding: 8px 20px;
            }}
            QPushButton#tab_inactive:hover {{
                color: #bbb;
                background: rgba(55, 55, 90, 140);
            }}

            /* ═══ 菜单栏 ═══ */
            QMenuBar {{
                background: rgba(22, 22, 40, 200);
                color: #b8bcd0;
                border-bottom: 1px solid rgba(100, 110, 170, 0.12);
                padding: 2px 8px;
                font-size: 12px;
            }}
            QMenuBar::item {{
                padding: 4px 10px;
                border-radius: 5px;
            }}
            QMenuBar::item:hover {{
                background: rgba(60, 65, 110, 100);
                color: #e0e4f0;
            }}
            QMenu {{
                background-color: rgba(30, 30, 55, 230);
                border: 1px solid rgba(100, 110, 170, 0.3);
                border-radius: 8px;
                padding: 4px;
                color: #e0e4f0;
            }}
            QMenu::item {{
                padding: 6px 24px;
                border-radius: 5px;
            }}
            QMenu::item:hover {{
                background-color: rgba(60, 65, 110, 100);
            }}
            QMenu::separator {{
                height: 1px;
                background: rgba(100, 110, 170, 0.2);
                margin: 4px 8px;
            }}

            /* Tab 内容区 — 与 Tab 按钮无缝衔接的卡片 */
            QWidget#tab_content_group, QGroupBox#tab_content_group {{
                background: rgba(38, 38, 62, 140);
                border: 1px solid rgba(100, 110, 170, 0.18);
                border-radius: 0 10px 10px 10px;
                margin: 0; padding: 12px;
            }}
            QGroupBox#tab_content_group {{
                margin-top: 0;
                padding-top: 12px;
            }}

            /* ═══ 输入控件 — 内凹磨砂 ═══ */
            QSpinBox, QComboBox, QLineEdit {{
                padding: 5px 10px;
                border: 1px solid rgba(90, 100, 160, 0.25);
                border-radius: 7px;
                background: rgba(22, 22, 40, 160);
                color: #e0e0e0;
                selection-background-color: #5b8af5;
            }}
            QSpinBox:focus, QComboBox:focus, QLineEdit:focus {{
                border-color: rgba(100, 150, 255, 0.6);
            }}
            QSpinBox::up-button, QSpinBox::down-button {{
                background: rgba(55, 55, 85, 140);
                border: none; width: 20px;
            }}
            QSpinBox::up-arrow {{
                border-left:4px solid transparent; border-right:4px solid transparent;
                border-bottom:5px solid #aaa;
            }}
            QSpinBox::down-arrow {{
                border-left:4px solid transparent; border-right:4px solid transparent;
                border-top:5px solid #aaa;
            }}
            QComboBox::drop-down {{
                border: none;
                background: rgba(55, 55, 85, 140);
                width: 24px; border-radius: 0 7px 7px 0;
            }}
            QComboBox::down-arrow {{
                border-left:5px solid transparent; border-right:5px solid transparent;
                border-top:5px solid #aaa;
            }}
            /* 下拉列表样式 - 确保背景色不被全局透明覆盖 */
            QComboBox QAbstractItemView {{
                background-color: rgba(30, 30, 55, 230);
                color: #e0e0e0;
                border: 1px solid rgba(100, 110, 170, 0.3);
                selection-background-color: rgba(91, 138, 245, 180);
                selection-color: #fff;
                border-radius: 6px;
                outline: none;
            }}
            QComboBox QAbstractItemView::item {{
                padding: 5px 10px;
                background-color: transparent;
            }}
            QComboBox QAbstractItemView::item:hover {{
                background-color: rgba(60, 65, 110, 100);
            }}
            QComboBox QAbstractItemView::item:selected {{
                background-color: rgba(91, 138, 245, 180);
                color: #fff;
            }}

            /* ═══ 列表 — 半透明底 ═══ */
            QListWidget {{
                border: 1px solid rgba(90, 100, 160, 0.2);
                border-radius: 8px;
                background: rgba(18, 18, 34, 140);
                color: #d0d4e0;
                outline: none;
            }}
            QListWidget::item {{
                padding: 4px 6px; border-radius: 5px;
            }}
            QListWidget::item:hover {{
                background: rgba(60, 65, 110, 100);
            }}
            QListWidget::item:selected {{
                background: rgba(80, 100, 180, 120);
                color: #fff;
            }}

            /* ═══ 日志文本框 ═══ */
            QTextEdit {{
                border: 1px solid rgba(90, 100, 160, 0.2);
                border-radius: 8px;
                background: rgba(14, 14, 28, 160);
                color: #a8b8d4;
                font-family: Consolas, "Courier New", monospace;
                font-size: 12px;
            }}

            /* ═══ 版本日志浏览器 ═══ */
            QTextBrowser {{
                border: 1px solid rgba(90, 100, 160, 0.2);
                border-radius: 8px;
                background: rgba(14, 14, 28, 160);
                color: #d0d8f0;
                font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
                font-size: 13px;
                padding: 8px;
            }}

            /* ═══ 进度条 — 发光条 ═══ */
            QProgressBar {{
                border: 1px solid rgba(90, 100, 160, 0.2);
                border-radius: 7px;
                text-align: center; height: 24px;
                background: rgba(18, 18, 36, 160);
                color: #e0e0e0;
            }}
            QProgressBar::chunk {{
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 rgba(91,138,245,220), stop:0.5 rgba(120,115,250,220), stop:1 rgba(160,120,255,200));
                border-radius: 6px;
            }}

            /* ═══ 预览区 ═══ */
            #preview_area {{
                background: rgba(18, 18, 36, 140);
                border: 1px dashed rgba(100, 110, 170, 0.3);
                border-radius: 8px;
                color: #556;
            }}

            /* ═══ Radio / CheckBox ═══ */
            QRadioButton {{ spacing: 6px; color: #c0c6d8; }}
            QRadioButton::indicator {{ width: 18px; height: 18px; }}
            QRadioButton::indicator:checked {{ image: url({_RES_DIR}/radio_on.svg); }}
            QRadioButton::indicator:unchecked {{ image: url({_RES_DIR}/radio_off.svg); }}

            QCheckBox {{ spacing: 6px; color: #c0c6d8; }}
            QCheckBox::indicator {{ width: 18px; height: 18px; }}
            QCheckBox::indicator:checked {{ image: url({_RES_DIR}/check_on.svg); }}
            QCheckBox::indicator:unchecked {{ image: url({_RES_DIR}/check_off.svg); }}

            /* ═══ 标签 & Tooltip ═══ */
            QLabel {{ color: #b8bcd0; background: transparent; }}
            QToolTip {{
                background: rgba(30, 30, 55, 230);
                color: #e0e0e0;
                border: 1px solid rgba(100, 140, 255, 0.4);
                padding: 5px; border-radius: 6px;
            }}

            /* ═══ 滚动条 — 细线发光 ═══ */
            QScrollBar:vertical {{
                background: transparent;
                width: 8px;
                margin: 4px 0;
                border-radius: 4px;
            }}
            QScrollBar::handle:vertical {{
                background: rgba(100, 110, 170, 80);
                min-height: 30px;
                border-radius: 4px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: rgba(120, 140, 220, 120);
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: transparent;
            }}
            QScrollBar:horizontal {{
                background: transparent;
                height: 8px;
                margin: 0 4px;
                border-radius: 4px;
            }}
            QScrollBar::handle:horizontal {{
                background: rgba(100, 110, 170, 80);
                min-width: 30px;
                border-radius: 4px;
            }}
            QScrollBar::handle:horizontal:hover {{
                background: rgba(120, 140, 220, 120);
            }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
                width: 0;
            }}
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
                background: transparent;
            }}
        """)

    # ─── 处理器切换 ───
    def _on_processor_changed(self, combo_idx):
        data = self.combo_processor.itemData(combo_idx)
        if data is None:
            return
        kind, idx = data
        # panel_stack 索引：图片处理器在前，文件处理器在后
        if kind == "image":
            self._current_processor = self._processors[idx]
            self._current_file_processor = None
            self.panel_stack.setCurrentIndex(idx)
            self.lbl_proc_desc.setText(self._current_processor.description)
            self._log_manager.switch_feature(self._current_processor.preset_id, clear_current=True)
        else:
            self._current_processor = None
            self._current_file_processor = self._file_processors[idx]
            self.panel_stack.setCurrentIndex(len(self._processors) + idx)
            self.lbl_proc_desc.setText(self._current_file_processor.description)
            self._log_manager.switch_feature(self._current_file_processor.preset_id, clear_current=True)
        # 重置滚动条到顶部
        self._scroll_to_top()
        self._refresh_preset_list()
        # 切换功能后刷新面板状态（如抠图模型就绪提示）
        proc = self._get_current_any_processor()
        if proc is not None and hasattr(proc, "on_panel_activated"):
            try:
                proc.on_panel_activated()
            except Exception:
                pass
        # 切换功能后，把当前选中图推给新处理器（元数据回读等）
        cur = self.file_list.currentItem()
        path = cur.data(ROLE_PATH) if cur is not None else None
        self._notify_processor_selection(path)
        # 输出区：保留抠图等选项随功能切换显隐
        self._update_output_extra_opts()

    # ─── Tab 切换 ───
    def _switch_tab(self, idx):
        self.main_stack.setCurrentIndex(idx)
        tab_btns = [
            self.btn_tab_process,
            self.btn_tab_log,
            self.btn_tab_settings,
            self.btn_tab_changelog,
        ]
        for i, btn in enumerate(tab_btns):
            btn.setObjectName("tab_active" if i == idx else "tab_inactive")
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        # 配置页首次进入时再构建，避免拖慢启动
        if idx == 2:
            self._ensure_settings_panel()
        # 回到图像处理时刷新当前面板状态（如抠图模型就绪提示）
        elif idx == 0:
            proc = self._get_current_any_processor()
            if proc is not None and hasattr(proc, "on_panel_activated"):
                try:
                    proc.on_panel_activated()
                except Exception:
                    pass

    def _ensure_settings_panel(self):
        if self.settings_panel is not None:
            return
        # 先让出事件循环，保证 Tab 切换动画/重绘先完成
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._build_settings_panel)

    def _build_settings_panel(self):
        if self.settings_panel is not None:
            return
        panel = SettingsPanel(lazy=True)
        self.settings_panel = panel
        panel.log_begin.connect(self._on_settings_log_begin)
        panel.log_line.connect(self._on_settings_log_line)
        if self._settings_placeholder is not None:
            self._settings_page_lay.removeWidget(self._settings_placeholder)
            self._settings_placeholder.deleteLater()
            self._settings_placeholder = None
        self._settings_page_lay.addWidget(panel, 1)
        # 若有待跳转的配置子菜单（如从透明图处理点链接）
        pending = getattr(self, "_pending_settings_menu", None)
        if pending is not None:
            self._pending_settings_menu = None
            panel.open_menu(int(pending))

    def open_settings(self, menu_row: int = 0):
        """打开「配置」Tab，并切换到指定左侧菜单（0=开发环境，1=抠图模型）。"""
        self._pending_settings_menu = menu_row
        self._switch_tab(2)
        if self.settings_panel is not None:
            self._pending_settings_menu = None
            self.settings_panel.open_menu(menu_row)

    def _on_settings_log_begin(self, feature_id: str, title: str):
        """配置中心长任务开始：切换后台日志并写入标题。"""
        try:
            self._log_manager.switch_feature(feature_id or "settings_runtime", clear_current=True)
        except Exception:
            pass
        self.log_text.clear()
        self._switch_tab(1)
        self._log(title)
        self._log("─" * 50)

    def _on_settings_log_line(self, text: str):
        """配置中心进度/详情写入后台日志。"""
        if text:
            self._log(text)

    # ─── 菜单栏 ───
    def _build_menubar(self):
        """构建顶部菜单栏（关于在最右侧）"""
        menubar = self.menuBar()

        # 用伸缩控件把"关于"推到最右边
        stretch = QWidget()
        stretch.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        stretch_action = QWidgetAction(menubar)
        stretch_action.setDefaultWidget(stretch)
        menubar.addAction(stretch_action)

        about_menu = menubar.addMenu("关于")

        act_update = about_menu.addAction("系统更新")
        act_update.triggered.connect(lambda: QMessageBox.information(
            self, "系统更新", "系统更新（开发中）"))

        about_menu.addSeparator()

        act_about = about_menu.addAction("版本信息")
        act_about.triggered.connect(lambda: QMessageBox.about(
            self, f"关于 {APP_NAME}",
            f"<b>{APP_TITLE}</b><br><br>"
            f"版本: {APP_VERSION}<br>"
            f"版权: {APP_COPYRIGHT}<br>"
            f"<a href='{APP_COPYRIGHT_URL}'>{APP_COPYRIGHT_URL}</a>"
        ))

    # ─── 版本日志加载 ───
    def _load_changelog(self):
        """读取 resources/CHANGELOG.md 并渲染到 changelog_browser"""
        changelog_path = RESOURCES_DIR / "CHANGELOG.md"
        try:
            text = changelog_path.read_text(encoding="utf-8")
        except Exception:
            text = "_版本日志文件未找到_"
        self.changelog_browser.setMarkdown(text)

    # ─── 滚动条重置 ───
    def _scroll_to_top(self):
        """将参数面板的滚动条重置到顶部"""
        self.panel_scroll_area.verticalScrollBar().setValue(0)

    # ─── 输出路径模式 ───
    def _on_path_mode_changed(self, btn_id, checked):
        if not checked:
            return
        # 0=桌面, 1=自定义, 2=覆盖原图, 3=副本原图
        show_input = btn_id in (0, 1)
        show_browse = btn_id == 1
        show_src_hint = btn_id in (2, 3)

        self.txt_output_dir.setVisible(show_input)
        self.btn_browse.setVisible(show_browse)
        self.lbl_src_hint.setVisible(show_src_hint)

        if btn_id == 0:
            self.txt_output_dir.setText(_get_desktop_path())
        elif btn_id == 1:
            if self.txt_output_dir.text() == _get_desktop_path():
                self.txt_output_dir.setText("")
                self.txt_output_dir.setPlaceholderText("选择或输入自定义输出目录...")

        self._update_output_extra_opts()

    def _update_output_extra_opts(self):
        """按路径模式 / 当前功能，刷新输出区附加选项可见性。"""
        mode_id = self.path_group.checkedId()
        show_out_opts = mode_id in (0, 1)
        self.chk_auto_folder.setVisible(show_out_opts)
        self.chk_keep_structure.setVisible(show_out_opts)
        self.chk_overwrite_file.setVisible(show_out_opts)

        show_keep_matting = False
        proc = self._current_processor
        if (
            proc is not None
            and getattr(proc, "preset_id", "") == "transparent_image"
        ):
            grp = getattr(proc, "_grp_matting", None)
            if grp is not None and grp.isChecked():
                show_keep_matting = True
        self.chk_keep_matting.setVisible(show_keep_matting)

    def _resolve_output_dir(self, src_path: str) -> tuple[str, bool]:
        """根据当前路径模式，返回 (output_dir, is_src_overwrite)。

        is_src_overwrite 仅表示「原图路径(覆盖原图)」模式，
        与桌面/自定义下的「覆盖同名文件」勾选无关。
        """
        mode_id = self.path_group.checkedId()
        if mode_id == 0:  # 桌面
            return self.txt_output_dir.text().strip() or _get_desktop_path(), False
        elif mode_id == 1:  # 自定义
            d = self.txt_output_dir.text().strip()
            return (d if d else _get_desktop_path()), False
        elif mode_id == 2:  # 原图路径覆盖
            return str(Path(src_path).parent), True
        else:  # 原图路径另存副本
            return str(Path(src_path).parent), False

    def _resolve_file_overwrite(self, is_src_overwrite: bool) -> bool:
        """是否允许覆盖同名输出文件。"""
        if is_src_overwrite:
            return True
        mode_id = self.path_group.checkedId()
        if mode_id in (0, 1):
            return self.chk_overwrite_file.isChecked()
        return False

    # ─── 文件操作 ───
    def _add_files(self):
        desktop_path = os.path.join(os.path.expanduser('~'), 'Desktop')
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择文件", desktop_path,
            "支持的文件 (*.png *.jpg *.jpeg *.webp *.bmp *.tiff *.tif *.gif *.docx *.pdf);;"
            "图片文件 (*.png *.jpg *.jpeg *.webp *.bmp *.tiff *.tif *.gif);;"
            "文档文件 (*.docx *.pdf)"
        )
        if files:
            self._insert_files(files)

    def _add_folder(self):
        desktop_path = os.path.join(os.path.expanduser('~'), 'Desktop')
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹", desktop_path)
        if folder:
            files = [str(f) for f in sorted(Path(folder).rglob("*"))
                     if f.is_file() and f.suffix.lower() in VALID_EXTS]
            # 传入 base_dir 以便显示相对路径
            self._insert_files(files, base_dir=folder)

    def _insert_files(self, files, base_dir=None):
        existing = {self.file_list.item(i).data(ROLE_PATH) for i in range(self.file_list.count())}
        new_image_paths = []
        
        # 暂停 UI 更新，提升批量添加性能
        self.file_list.setUpdatesEnabled(False)
        try:
            for f in files:
                if f not in existing:
                    # 如果提供了 base_dir，则显示并记录相对路径，供「保留目录结构」使用
                    rel_path = None
                    if base_dir:
                        try:
                            rel_path = str(Path(f).relative_to(base_dir)).replace("\\", "/")
                            display_name = rel_path
                        except ValueError:
                            display_name = Path(f).name
                    else:
                        # 对于单个添加的文件，尽量显示其父目录+文件名以便区分
                        display_name = f"{Path(f).parent.name}/{Path(f).name}"
                        
                    base_text = display_name
                    if Path(f).suffix.lower() in DOC_EXTS:
                        ext = Path(f).suffix.lower()
                        base_text = f"{'📄' if ext == '.pdf' else '📝'}  {display_name}"
                    item = QListWidgetItem(base_text)
                    item.setData(ROLE_PATH, f)
                    item.setData(ROLE_REL_PATH, rel_path)
                    item.setData(ROLE_BASE_TEXT, base_text)
                    item.setData(ROLE_JOB_STATUS, STATUS_PENDING)
                    item.setToolTip(f)
                    self.file_list.addItem(item)
                    # 缓存路径到项的映射
                    self._path_to_item[f] = item
                    existing.add(f)
                    if Path(f).suffix.lower() in IMAGE_EXTS:
                        new_image_paths.append(f)
            
            # 对列表项进行排序（按显示名称排序，从而实现按目录及文件名排序）
            self.file_list.sortItems(Qt.AscendingOrder)
        finally:
            # 恢复 UI 更新
            self.file_list.setUpdatesEnabled(True)
        
        # 后台加载图片缩略图
        if new_image_paths:
            self._thumb_loader = _ThumbnailLoader(new_image_paths)
            self._thumb_loader.loaded.connect(self._on_thumb_loaded)
            self._thumb_loader.start()

    def _on_thumb_loaded(self, path, icon):
        # 使用缓存字典快速查找，O(1) 复杂度
        item = self._path_to_item.get(path)
        if item:
            item.setIcon(icon)

    def _clear_files(self):
        self.file_list.clear()
        # 清空路径缓存
        self._path_to_item.clear()
        self.preview_label.clear()
        self.preview_label.setText("点击列表预览图片")
        self.lbl_preview_info.setText("")
        self._clear_batch_session()

    def _remove_selected(self):
        selected_items = self.file_list.selectedItems()
        if not selected_items:
            return
        
        # 暂停 UI 更新，大幅提升批量删除性能
        self.file_list.setUpdatesEnabled(False)
        try:
            # 收集要删除的行号，从后往前删除避免索引变化
            rows_to_remove = sorted([self.file_list.row(item) for item in selected_items], reverse=True)
            for row in rows_to_remove:
                item = self.file_list.item(row)
                # 从缓存中移除
                path = item.data(ROLE_PATH)
                self._path_to_item.pop(path, None)
                self.file_list.takeItem(row)
                # 同步会话：移除的文件不再参与续跑
                if self._batch_session is not None:
                    job = self._batch_session.get(path)
                    if job is not None:
                        self._batch_session.files = [
                            f for f in self._batch_session.files if f.path != path
                        ]
                        self._batch_session._index.pop(path, None)
        finally:
            # 恢复 UI 更新
            self.file_list.setUpdatesEnabled(True)
        self._update_resume_buttons()
        
        # 更新文件计数
        self._update_file_count()

    def _update_file_count(self):
        total = self.file_list.count()
        selected = len(self.file_list.selectedItems())
        self.lbl_file_count.setText(f"共 {total} 个文件" + (f"，已选 {selected}" if selected else ""))
        self._update_scope_hint()

    def _on_scope_changed(self, *_args):
        self._update_scope_hint()

    def _update_scope_hint(self):
        if not hasattr(self, "lbl_scope_hint"):
            return
        if self.rb_scope_selected.isChecked():
            n = len(self.file_list.selectedItems())
            self.lbl_scope_hint.setText(f"将处理选中的 {n} 个文件" if n else "请先在左侧选中文件")
        else:
            n = self.file_list.count()
            self.lbl_scope_hint.setText(f"将处理全部 {n} 个文件")

    def _collect_process_files(self) -> list[str]:
        """按处理范围收集待处理文件路径。"""
        return [p for p, _ in self._collect_process_entries()]

    def _collect_process_entries(self) -> list[tuple[str, str | None]]:
        """按处理范围收集 (完整路径, 相对路径|None)。相对路径用于保留目录结构。"""
        if self.rb_scope_selected.isChecked():
            items = self.file_list.selectedItems()
        else:
            items = [self.file_list.item(i) for i in range(self.file_list.count())]
        entries: list[tuple[str, str | None]] = []
        for it in items:
            path = it.data(ROLE_PATH) if it is not None else None
            if not path:
                continue
            rel = it.data(ROLE_REL_PATH)
            entries.append((path, rel if rel else None))
        return entries

    def _notify_processor_selection(self, path: str | None):
        """通知当前图片处理器：选中项变化（用于单图回读等）。"""
        proc = self._current_processor
        if proc is None:
            return
        if hasattr(proc, "on_selected_image"):
            try:
                # 文档不传给图片处理器回读
                if path and Path(path).suffix.lower() in DOC_EXTS:
                    proc.on_selected_image(None)
                else:
                    proc.on_selected_image(path)
            except Exception:
                pass

    def _on_selection_changed(self):
        self._update_file_count()

    def _on_file_selected(self, current, _prev):
        if current is None:
            self._notify_processor_selection(None)
            return
        path = current.data(ROLE_PATH)
        # 文档文件不做图片预览
        if Path(path).suffix.lower() in DOC_EXTS:
            self.preview_label.clear()
            ext = Path(path).suffix.upper().lstrip(".")
            icon_char = "📄" if ext == "PDF" else "📝"
            self.preview_label.setText(f"{icon_char}\n{ext} 文档")
            size_kb = Path(path).stat().st_size // 1024 if Path(path).exists() else 0
            self.lbl_preview_info.setText(f"{Path(path).name}  ({size_kb} KB)")
            self._notify_processor_selection(None)
            return
        pixmap = QPixmap(path)
        if pixmap.isNull():
            self.preview_label.setText("无法加载预览")
            self.lbl_preview_info.setText("")
            self._notify_processor_selection(path)
            return
        w, h = pixmap.width(), pixmap.height()
        # 同步底图尺寸给叠加处理器（用于宫格坐标定位）
        if (self._current_processor is not None 
                and hasattr(self._current_processor, 'set_base_image_size')):
            self._current_processor.set_base_image_size(w, h)
        scaled = pixmap.scaled(self.preview_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.preview_label.setPixmap(scaled)
        self.lbl_preview_info.setText(f"{Path(path).name}  ({w} × {h})")
        self._notify_processor_selection(path)

    def _browse_output(self):
        folder = QFileDialog.getExistingDirectory(self, "选择输出目录", _get_desktop_path())
        if folder:
            self.txt_output_dir.setText(folder)

    # ─── 批处理会话 / 列表状态 ───
    def _clear_batch_session(self):
        self._batch_session = None
        self._run_mode = "full"
        self._reset_list_job_status()
        self._update_resume_buttons()

    def _reset_list_job_status(self):
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            if item is None:
                continue
            self._apply_item_job_status(item, STATUS_PENDING, error="")

    def _apply_item_job_status(self, item: QListWidgetItem, status: str, error: str = ""):
        if item is None:
            return
        base = item.data(ROLE_BASE_TEXT)
        if not base:
            # 兼容旧项：去掉已知前缀
            text = item.text()
            for pref in _STATUS_PREFIX.values():
                if pref and text.startswith(pref):
                    text = text[len(pref):]
                    break
            base = text
            item.setData(ROLE_BASE_TEXT, base)
        prefix = _STATUS_PREFIX.get(status, "")
        item.setText(f"{prefix}{base}")
        item.setData(ROLE_JOB_STATUS, status)
        color = _STATUS_COLOR.get(status, _STATUS_COLOR[STATUS_PENDING])
        item.setForeground(color)
        path = item.data(ROLE_PATH) or ""
        tip_lines = [str(path)] if path else []
        status_label = {
            STATUS_PENDING: "未处理",
            STATUS_RUNNING: "处理中",
            STATUS_SUCCESS: "成功",
            STATUS_FAILED: "失败",
            STATUS_CANCELLED: "已取消（未完成）",
        }.get(status, status)
        tip_lines.append(f"状态: {status_label}")
        if error:
            tip_lines.append(f"错误: {error.splitlines()[0]}")
        item.setToolTip("\n".join(tip_lines))

    def _set_path_job_status(self, path: str, status: str, error: str = ""):
        item = self._path_to_item.get(path)
        if item is not None:
            self._apply_item_job_status(item, status, error=error)

    def _sync_list_from_session(self):
        sess = self._batch_session
        if sess is None:
            return
        for job in sess.files:
            self._set_path_job_status(job.path, job.status, error=job.error)

    def _update_resume_buttons(self):
        """
        底部按钮按需显示，避免常态冗余：
        - 「继续」：仅用户取消后且仍有未完成时显示
        - 「重试失败」：仅存在失败项时显示
        """
        busy = self.worker is not None and self.worker.isRunning()
        sess = self._batch_session
        # 继续：必须是取消过的会话，且还有 pending/cancelled
        show_cont = (
            (not busy)
            and sess is not None
            and sess.supports_resume
            and sess.user_cancelled
            and sess.can_continue()
        )
        # 重试失败：有失败即可（取消后若也有失败可一并显示）
        show_retry = (
            (not busy)
            and sess is not None
            and sess.supports_resume
            and sess.can_retry_failed()
        )
        self.btn_continue.setVisible(show_cont)
        self.btn_continue.setEnabled(show_cont)
        self.btn_retry_failed.setVisible(show_retry)
        self.btn_retry_failed.setEnabled(show_retry)

        has_failed = sess is not None and bool(sess.failed_paths())
        self.btn_select_failed.setVisible(has_failed)
        self.btn_select_failed.setEnabled(has_failed and not busy)

        if sess and sess.supports_resume:
            s = sess.summary()
            unfinished = s["pending"] + s["cancelled"]
            self.btn_continue.setText(
                f"继续({unfinished})" if unfinished else "继续"
            )
            self.btn_retry_failed.setText(
                f"重试失败({s['failed']})" if s["failed"] else "重试失败"
            )
        else:
            self.btn_continue.setText("继续")
            self.btn_retry_failed.setText("重试失败")

    def _select_failed_files(self):
        sess = self._batch_session
        if sess is None:
            return
        failed = set(sess.failed_paths())
        if not failed:
            return
        self.file_list.clearSelection()
        first = None
        for path in failed:
            item = self._path_to_item.get(path)
            if item is None:
                continue
            item.setSelected(True)
            if first is None:
                first = item
        if first is not None:
            self.file_list.setCurrentItem(first)
            self.file_list.scrollToItem(first)
        self.rb_scope_selected.setChecked(True)
        self._log(f"已选中 {len(failed)} 个失败文件，处理范围已切换为「仅选中」")

    def _resolve_processor_for_session(self, sess: BatchSession):
        if sess.kind == "image":
            for p in self._processors:
                if p.preset_id == sess.processor_preset_id:
                    return p
        else:
            for p in self._file_processors:
                if p.preset_id == sess.processor_preset_id:
                    return p
        return None

    def _processor_supports_resume(self, proc) -> bool:
        """批量合并类不支持按文件续跑。"""
        if proc is None:
            return False
        return not bool(getattr(proc, "is_batch_processor", False))

    # ─── 处理逻辑 ───
    def _start_process(self):
        self._begin_process(mode="full")

    def _continue_process(self):
        self._begin_process(mode="continue")

    def _retry_failed_process(self):
        self._begin_process(mode="retry_failed")

    def _begin_process(self, mode: str = "full"):
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.warning(self, "提示", "已有任务正在处理，请等待完成或取消后再试")
            return

        mode_names = ["桌面路径", "自定义路径", "原图路径(覆盖)", "原图路径(副本)"]

        # ── 续跑 / 重试失败：复用会话快照 ──
        if mode in ("continue", "retry_failed"):
            sess = self._batch_session
            if sess is None or not sess.supports_resume:
                QMessageBox.information(self, "提示", "当前没有可续跑的批处理任务")
                return
            paths = sess.pending_paths() if mode == "continue" else sess.failed_paths()
            if not paths:
                tip = "没有未完成的文件" if mode == "continue" else "没有失败的文件"
                QMessageBox.information(self, "提示", tip)
                return
            proc = self._resolve_processor_for_session(sess)
            if proc is None:
                QMessageBox.warning(
                    self, "提示",
                    f"找不到原功能「{sess.processor_name}」，无法续跑。\n请重新选择功能后点「开始处理」。"
                )
                return

            # 切回对应功能（不改用户面板参数；Worker 用会话 options）
            self._activate_processor_by_preset(sess.processor_preset_id, sess.kind)

            sess.reset_for_retry(paths)
            self._sync_list_from_session()
            self._run_mode = mode
            file_list = list(paths)
            rel_map = {
                p: sess.rel_path_map[p]
                for p in file_list
                if sess.keep_structure and p in sess.rel_path_map
            }
            file_index_map = sess.order_map()
            output_dir = sess.output_dir
            auto_folder = sess.auto_subfolder
            is_src_overwrite = sess.overwrite
            file_overwrite = bool(getattr(sess, "file_overwrite", is_src_overwrite))
            options = dict(sess.options)
            count = len(file_list)

            proc_for_log = proc
            if proc_for_log is not None:
                self._log_manager.switch_feature(proc_for_log.preset_id, clear_current=False)
            self._switch_tab(1)
            self.progress_bar.setMaximum(count)
            self.progress_bar.setValue(0)
            self.btn_start.setEnabled(False)
            self.btn_continue.setVisible(False)
            self.btn_retry_failed.setVisible(False)
            self.btn_cancel.setVisible(True)
            self.btn_cancel.setEnabled(True)

            self._process_t0_mono = time.monotonic()
            self._process_t0_wall = datetime.now()
            start_text = self._process_t0_wall.strftime("%Y-%m-%d %H:%M:%S")
            action = "继续未完成" if mode == "continue" else "重试失败"
            icon = getattr(proc, "icon", "")
            self._log("─" * 50)
            self._log(f"▶ {action}: {icon}  {proc.name}  共 {count} 个文件")
            self._log(f"输出: {mode_names[sess.path_mode_id]}  →  {output_dir}")
            self._log(f"参数: 沿用该批次快照")
            if options.get("enable_matting"):
                self._log(self._format_matting_start_log(options))
            if options.get("keep_matting"):
                self._log("保留抠图结果: 是（文件名追加 _matted）")
            self._log(f"开始时间: {start_text}")
            self._log("─" * 50)

            if sess.kind == "image":
                self.worker = ProcessWorker(
                    file_list, output_dir, proc, options,
                    auto_subfolder=auto_folder,
                    overwrite=is_src_overwrite,
                    file_overwrite=file_overwrite,
                    rel_path_map=rel_map,
                    file_index_map=file_index_map,
                )
                self.worker.progress.connect(self._on_progress)
                self.worker.image_done.connect(self._on_image_done)
                self.worker.finished.connect(self._on_worker_finished)
                self.worker.debug.connect(self._on_worker_debug)
                self.worker.start()
            else:
                self.worker = FileProcessWorker(
                    file_list, output_dir, proc, options,
                    auto_subfolder=auto_folder,
                    rel_path_map=rel_map,
                    file_index_map=file_index_map,
                )
                self.worker.progress.connect(self._on_progress)
                self.worker.file_done.connect(self._on_file_done)
                self.worker.finished.connect(self._on_worker_finished)
                self.worker.debug.connect(self._on_worker_debug)
                self.worker.start()
            return

        # ── 全新整批 ──
        if self.file_list.count() == 0:
            QMessageBox.warning(self, "提示", "请先添加要处理的文件")
            return
        if self._current_processor is None and self._current_file_processor is None:
            QMessageBox.warning(self, "提示", "请选择处理功能")
            return

        # 若有未完成/失败会话，确认是否开新批次
        old = self._batch_session
        if old is not None and old.supports_resume and (
            old.can_continue() or old.can_retry_failed()
        ):
            s = old.summary()
            unfinished = s["pending"] + s["cancelled"]
            ret = QMessageBox.question(
                self,
                "开始新任务",
                (
                    f"上一批仍有未完成 {unfinished} 个、失败 {s['failed']} 个。\n"
                    f"开始新任务将清除续跑记录（已成功输出的文件不受影响）。\n\n"
                    f"是否继续开始新任务？"
                ),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if ret != QMessageBox.Yes:
                return

        # 验证自定义路径
        mode_id = self.path_group.checkedId()
        if mode_id == 1 and not self.txt_output_dir.text().strip():
            QMessageBox.warning(self, "提示", "请选择自定义输出目录")
            return

        entries = self._collect_process_entries()
        file_list = [p for p, _ in entries]
        scope_selected = self.rb_scope_selected.isChecked()
        if not file_list:
            if scope_selected:
                QMessageBox.warning(self, "提示", "「仅选中」模式下请先在左侧列表选中要处理的文件")
            else:
                QMessageBox.warning(self, "提示", "请先添加要处理的文件")
            return

        count = len(file_list)
        output_dir, is_src_overwrite = self._resolve_output_dir(file_list[0])
        allow_file_overwrite = self._resolve_file_overwrite(is_src_overwrite)

        proc_for_log = self._current_processor or self._current_file_processor
        if proc_for_log is not None:
            self._log_manager.switch_feature(proc_for_log.preset_id, clear_current=True)
        self.log_text.clear()
        self._switch_tab(1)
        scope_name = "仅选中" if scope_selected else "全部文件"
        # 原图路径覆盖模式不建子文件夹；桌面/自定义下「覆盖同名」不影响自动文件夹
        auto_folder = self.chk_auto_folder.isChecked() and not is_src_overwrite
        # 仅桌面/自定义路径模式支持按相对路径重建子目录
        keep_structure = (
            self.chk_keep_structure.isChecked()
            and not is_src_overwrite
            and mode_id in (0, 1)
        )
        rel_map = {p: rel for p, rel in entries if rel} if keep_structure else {}

        self.progress_bar.setMaximum(count)
        self.progress_bar.setValue(0)
        self.btn_start.setEnabled(False)
        self.btn_continue.setVisible(False)
        self.btn_retry_failed.setVisible(False)
        self.btn_select_failed.setVisible(False)
        self.btn_cancel.setVisible(True)
        self.btn_cancel.setEnabled(True)

        # 批次计时：以点击「开始处理」为准
        self._process_t0_mono = time.monotonic()
        self._process_t0_wall = datetime.now()
        start_text = self._process_t0_wall.strftime("%Y-%m-%d %H:%M:%S")
        self._run_mode = "full"

        if self._current_processor is not None:
            # ── 图片处理 Worker ──
            proc = self._current_processor
            options = proc.gather_options()
            options["_output_format"] = proc.get_output_format()
            # 输出设置：保留抠图（仅透明图 + 已开 AI 抠图时生效）
            if (
                getattr(proc, "preset_id", "") == "transparent_image"
                and options.get("enable_matting")
                and self.chk_keep_matting.isVisible()
                and self.chk_keep_matting.isChecked()
            ):
                options["keep_matting"] = True
            else:
                options["keep_matting"] = False
            supports = self._processor_supports_resume(proc)
            self._batch_session = BatchSession.create(
                kind="image",
                processor_preset_id=proc.preset_id,
                processor_name=proc.name,
                supports_resume=supports,
                options=options,
                output_dir=output_dir,
                auto_subfolder=auto_folder,
                overwrite=is_src_overwrite,
                file_overwrite=allow_file_overwrite,
                keep_structure=keep_structure,
                path_mode_id=mode_id,
                entries=entries,
            )
            self._reset_list_job_status()
            for p, _ in entries:
                self._set_path_job_status(p, STATUS_PENDING)

            self._log(f"功能: {proc.icon}  {proc.name}")
            self._log(f"处理范围: {scope_name}  共 {count} 个文件")
            self._log(f"输出模式: {mode_names[mode_id]}  →  {output_dir}")
            if mode_id in (0, 1):
                self._log(
                    f"同名文件: {'覆盖' if allow_file_overwrite else '自动加后缀 (_1/_2…)'}"
                )
            if keep_structure:
                self._log(f"保留目录结构: 是（{len(rel_map)} 个文件含相对路径）")
            if options.get("keep_matting"):
                self._log("保留抠图结果: 是（文件名追加 _matted）")
            if not supports:
                self._log("说明: 当前为批量合并功能，不支持中途续跑/按文件重试")
            # AI 抠图开启时，启动摘要先写一版（Worker 内会再写设备/batch 实测值）
            if options.get("enable_matting"):
                self._log(self._format_matting_start_log(options))
            self._log(f"开始时间: {start_text}")
            self._log("─" * 50)
            self.worker = ProcessWorker(
                file_list, output_dir, proc, options,
                auto_subfolder=auto_folder,
                overwrite=is_src_overwrite,
                file_overwrite=allow_file_overwrite,
                rel_path_map=rel_map,
                file_index_map=self._batch_session.order_map(),
            )
            self.worker.progress.connect(self._on_progress)
            self.worker.image_done.connect(self._on_image_done)
            self.worker.finished.connect(self._on_worker_finished)
            self.worker.debug.connect(self._on_worker_debug)
            self.worker.start()
        else:
            # ── 文件处理 Worker ──
            proc = self._current_file_processor
            options = proc.gather_options()
            supports = True  # 文件处理器均为逐文件
            self._batch_session = BatchSession.create(
                kind="file",
                processor_preset_id=proc.preset_id,
                processor_name=proc.name,
                supports_resume=supports,
                options=options,
                output_dir=output_dir,
                auto_subfolder=auto_folder,
                overwrite=is_src_overwrite,
                file_overwrite=allow_file_overwrite,
                keep_structure=keep_structure,
                path_mode_id=mode_id,
                entries=entries,
            )
            self._reset_list_job_status()
            for p, _ in entries:
                self._set_path_job_status(p, STATUS_PENDING)

            self._log(f"功能: {proc.icon}  {proc.name}")
            self._log(f"处理范围: {scope_name}  共 {count} 个文件")
            self._log(f"输出模式: {mode_names[mode_id]}  →  {output_dir}")
            if mode_id in (0, 1):
                self._log(
                    f"同名文件: {'覆盖' if allow_file_overwrite else '自动加后缀 (_1/_2…)'}"
                )
            if keep_structure:
                self._log(f"保留目录结构: 是（{len(rel_map)} 个文件含相对路径）")
            self._log(f"开始时间: {start_text}")
            self._log("─" * 50)
            self.worker = FileProcessWorker(
                file_list, output_dir, proc, options,
                auto_subfolder=auto_folder,
                rel_path_map=rel_map,
                file_index_map=self._batch_session.order_map(),
            )
            self.worker.progress.connect(self._on_progress)
            self.worker.file_done.connect(self._on_file_done)
            self.worker.finished.connect(self._on_worker_finished)
            self.worker.debug.connect(self._on_worker_debug)
            self.worker.start()

    def _activate_processor_by_preset(self, preset_id: str, kind: str):
        """切换功能下拉到指定处理器（续跑时保证 UI 与任务一致，不清空日志）。"""
        combo = self.combo_processor
        procs = self._processors if kind == "image" else self._file_processors
        target_idx = None
        for i, p in enumerate(procs):
            if p.preset_id == preset_id:
                target_idx = i
                break
        if target_idx is None:
            return
        for i in range(combo.count()):
            data = combo.itemData(i)
            if data != (kind, target_idx):
                continue
            if combo.currentIndex() == i:
                # 已选中时仍同步 current 引用
                if kind == "image":
                    self._current_processor = self._processors[target_idx]
                    self._current_file_processor = None
                else:
                    self._current_processor = None
                    self._current_file_processor = self._file_processors[target_idx]
                return
            # 阻断 currentIndexChanged，避免 switch_feature(clear) 清掉续跑日志
            combo.blockSignals(True)
            combo.setCurrentIndex(i)
            combo.blockSignals(False)
            if kind == "image":
                self._current_processor = self._processors[target_idx]
                self._current_file_processor = None
                self.panel_stack.setCurrentIndex(target_idx)
                self.lbl_proc_desc.setText(self._current_processor.description)
            else:
                self._current_processor = None
                self._current_file_processor = self._file_processors[target_idx]
                self.panel_stack.setCurrentIndex(len(self._processors) + target_idx)
                self.lbl_proc_desc.setText(self._current_file_processor.description)
            self._scroll_to_top()
            self._refresh_preset_list()
            return

    def _cancel_process(self):
        if self.worker:
            # 先标记当前 running，避免 cancel 抢在 image_done 前
            cur = getattr(self.worker, "current_path", None)
            if self._batch_session is not None and cur:
                self._batch_session.mark_running(cur)
            self.worker.cancel()
            if self._batch_session is not None:
                self._batch_session.mark_cancelled_running()
                self._sync_list_from_session()
            self._log("⚠ 用户取消处理（已成功的文件保留；可用「继续」处理剩余）")

    def _on_progress(self, current, total, filename):
        # 动态同步最大值（批量处理器上报的 total 是文件总数，可能与初始 count 不同）
        if total > 0 and self.progress_bar.maximum() != total:
            self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)
        if filename and filename != "完成":
            self._log(f"  ▶ {filename}")
        # 标记 running（用 worker.current_path 更准）
        if self.worker is not None and self._batch_session is not None:
            cur = getattr(self.worker, "current_path", None)
            if cur:
                self._batch_session.mark_running(cur)
                self._set_path_job_status(cur, STATUS_RUNNING)

    def _record_result_to_session(self, result):
        sess = self._batch_session
        if sess is None or not sess.supports_resume:
            return
        path = getattr(result, "input_path", "") or ""
        if not path or path.startswith("分组:") or path == "批量处理":
            return
        if result.success:
            sess.mark_success(path, getattr(result, "output_path", "") or "")
            self._set_path_job_status(path, STATUS_SUCCESS)
        else:
            err = str(getattr(result, "error", "") or "")
            sess.mark_failed(path, err)
            self._set_path_job_status(path, STATUS_FAILED, error=err)

    def _on_image_done(self, result):
        self._record_result_to_session(result)
        name = Path(result.input_path).name
        if result.success:
            d = result.details or {}
            # 批量合并处理器（如排版导出）的结果
            if result.input_path.startswith("分组:"):
                group_label = result.input_path  # e.g. "分组: 排版导出"
                files_count = d.get("files_count", "?")
                pages = d.get("pages", "?")
                out_name = Path(result.output_path).name if result.output_path else ""
                self._log(f"✓ {group_label}  共 {files_count} 张图 → {pages} 页  →  {out_name}")
                return
            parts = [f"✓ {name}"]
            if "matting_model" in d:
                refine = "+精炼" if d.get("matting_refine") else ""
                batch_n = d.get("matting_batch")
                dev = str(d.get("matting_device") or "").upper()
                path = d.get("matting_path") or ""
                matting_bits = [f"抠图→{d['matting_model']}{refine}"]
                if batch_n:
                    matting_bits.append(f"batch={batch_n}")
                if dev:
                    matting_bits.append(dev)
                if path == "mask_prescale":
                    matting_bits.append("mask")
                elif path == "refine_rgba":
                    matting_bits.append("精炼全尺寸")
                if d.get("matting_pipeline"):
                    matting_bits.append("流水线")
                parts.append(" · ".join(matting_bits))
            if "trimmed_size" in d:
                parts.append(f"裁剪→{d['trimmed_size'][0]}×{d['trimmed_size'][1]}")
            if d.get("matting_keep_path"):
                parts.append(f"抠图副本→{Path(d['matting_keep_path']).name}")
            elif d.get("matting_keep_error"):
                parts.append(f"抠图副本失败:{d['matting_keep_error']}")
            if "layout_display_size" in d:
                size = d["layout_display_size"]
                parts.append(
                    f"主体→{size[0]}×{size[1]}（画布占比 {d.get('subject_percent', '?')}%）"
                )
            elif "resized_size" in d:
                parts.append(f"缩放→{d['resized_size'][0]}×{d['resized_size'][1]}")
            if "canvas_size" in d:
                parts.append(f"画布→{d['canvas_size'][0]}×{d['canvas_size'][1]}")
            if "compress_info" in d:
                parts.append(f"压缩→{d['compress_info']}")
            if "dpi" in d:
                parts.append(f"DPI→{d['dpi']}")
            if "output_format" in d:
                parts.append(f"格式→{d['output_format'].upper()}")
            if d.get("fake_extension") or d.get("format_note"):
                parts.append(f"格式识别→{d.get('format_note') or d.get('true_format', '?')}")
            if d.get("converted"):
                parts.append(f"转换→{(d.get('output_format') or d.get('format', '?')).upper()}")
            elif d.get("convert_skipped"):
                parts.append("转换→已是目标格式(跳过重编码)")
            if d.get("skipped"):
                parts.append(f"跳过→{d.get('skip_reason', '不支持')}")
            elif d.get("cleared"):
                parts.append("元数据→已清除")
            elif d.get("fields_written"):
                parts.append("元数据→" + ", ".join(d["fields_written"]))
            if d.get("fields_dropped"):
                parts.append("忽略字段→" + "/".join(d["fields_dropped"]))
            if d.get("warning"):
                parts.append(f"⚠{d['warning']}")
            self._log("  ".join(parts))
        else:
            error = str(result.error)
            self._log(f"✗ {name}  错误: {error.splitlines()[0] if error else ''}")
            if "\n" in error:
                self._on_worker_debug(f"{name} 完整错误信息:\n{error}")

    def _on_file_done(self, result):
        self._record_result_to_session(result)
        name = Path(result.input_path).name
        if result.success:
            d = result.details or {}
            parts = [f"✓ {name}"]
            if "paragraphs" in d:
                parts.append(f"段落数: {d['paragraphs']}")
            if "tables" in d:
                parts.append(f"表格数: {d['tables']}")
            if "pages" in d:
                parts.append(f"页数: {d['pages']}")
            if "chars" in d:
                parts.append(f"字符数: {d['chars']}")
            self._log("  ".join(parts))
        else:
            error = str(result.error)
            self._log(f"✗ {name}  错误: {error.splitlines()[0] if error else ''}")
            if "\n" in error:
                self._on_worker_debug(f"{name} 完整错误信息:\n{error}")

    def _on_worker_debug(self, text: str):
        try:
            self._log_manager.write(text)
        except Exception:
            pass

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """将秒数格式化为可读时长，如 12.3 秒 / 1 分 05.2 秒 / 1 小时 02 分 03.1 秒。"""
        if seconds < 0:
            seconds = 0.0
        total_ms = int(round(seconds * 1000))
        s_whole, ms = divmod(total_ms, 1000)
        h, rem = divmod(s_whole, 3600)
        m, s = divmod(rem, 60)
        frac = f"{s}.{ms:03d}".rstrip("0").rstrip(".")
        if h > 0:
            return f"{h} 小时 {m:02d} 分 {frac} 秒（共 {seconds:.3f} 秒）"
        if m > 0:
            return f"{m} 分 {frac} 秒（共 {seconds:.3f} 秒）"
        return f"{frac} 秒"

    def _on_worker_finished(self):
        """仅在 QThread 已完全退出后结算，避免销毁仍处于清理阶段的线程对象。"""
        worker = self.worker
        if worker is None:
            return
        results = list(getattr(worker, "results", []) or [])
        self._on_all_done(results)

    def _on_all_done(self, results):
        # 取消时可能仍有 running 未落到 cancelled
        if self._batch_session is not None and self._batch_session.user_cancelled:
            self._batch_session.mark_cancelled_running()
            self._sync_list_from_session()

        sess = self._batch_session
        if sess is not None and sess.supports_resume:
            s = sess.summary()
            success = s["success"]
            fail = s["failed"]
            unfinished = s["pending"] + s["cancelled"]
            run_success = sum(1 for r in results if r.success)
            run_fail = len(results) - run_success
        else:
            success = sum(1 for r in results if r.success)
            fail = len(results) - success
            unfinished = 0
            run_success, run_fail = success, fail
            s = None

        end_wall = datetime.now()
        elapsed = None
        if self._process_t0_mono is not None:
            elapsed = time.monotonic() - self._process_t0_mono
        start_text = (
            self._process_t0_wall.strftime("%Y-%m-%d %H:%M:%S")
            if self._process_t0_wall is not None
            else "—"
        )
        end_text = end_wall.strftime("%Y-%m-%d %H:%M:%S")
        self._log("─" * 50)
        if sess is not None and sess.supports_resume:
            self._log(
                f"处理结束!  本轮成功: {run_success}  本轮失败: {run_fail}"
                f"  |  累计成功: {success}  失败: {fail}  未完成: {unfinished}"
            )
            if sess.user_cancelled:
                self._log("状态: 用户已取消")
        else:
            self._log(f"处理完成!  成功: {success}  失败: {fail}")
        self._log(f"开始时间: {start_text}")
        self._log(f"结束时间: {end_text}")
        if elapsed is not None:
            self._log(f"处理耗时: {self._format_duration(elapsed)}")
        self._process_t0_mono = None
        self._process_t0_wall = None
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setVisible(False)
        self.worker = None
        self._update_resume_buttons()

        # 全部成功且无未完成
        if fail == 0 and unfinished == 0:
            if elapsed is not None:
                QMessageBox.information(
                    self, "完成",
                    f"全部 {success} 个文件处理成功!\n耗时: {self._format_duration(elapsed)}"
                )
            else:
                QMessageBox.information(self, "完成", f"全部 {success} 个文件处理成功!")
            return

        # 取消：仅提示结果，不在弹窗里放继续/重试（需要时用底部按钮）
        lines = []
        if sess is not None and sess.supports_resume and s is not None:
            lines.append(f"累计成功: {success}    失败: {fail}    未完成: {unfinished}")
        else:
            lines.append(f"成功: {success}    失败: {fail}")
        if elapsed is not None:
            lines.append(f"耗时: {self._format_duration(elapsed)}")

        if sess is not None and sess.user_cancelled:
            lines.append("任务已取消。如需接着处理，可使用底部「继续」。")
            QMessageBox.information(self, "已取消", "\n".join(lines))
            return

        # 自然结束且有失败：提示可用底部「重试失败」
        lines.append("可查看后台日志；失败项可用底部「重试失败」重新处理。")
        QMessageBox.warning(self, "完成", "\n".join(lines))

    @staticmethod
    def _format_matting_start_log(options: dict) -> str:
        """点击开始时写 AI 抠图摘要（含配置里的推理设备偏好）。"""
        mid = options.get("matting_model", "ben2")
        refine = bool(options.get("matting_refine", False))
        try:
            from core.matting.model_manager import get_matting_manager
            pref = (
                get_matting_manager().get_device_preference() or "auto"
            ).lower()
        except Exception:
            pref = "auto"
        labels = {
            "auto": "自动(优先GPU)",
            "cuda": "CUDA(GPU)",
            "cpu": "CPU",
        }
        pref_desc = labels.get(pref, pref)
        return (
            f"AI 抠图: 已开启  模型={mid}  "
            f"推理设备={pref_desc}  "
            f"边缘精炼={'开' if refine else '关'}  "
            f"（实际运行设备与 batch 见处理线程日志）"
        )

    def _log(self, text: str):
        self.log_text.append(text)
        try:
            self._log_manager.write(text)
        except Exception:
            pass

    def _clear_current_log(self):
        self.log_text.clear()
        try:
            self._log_manager.clear_current()
        except Exception:
            pass

    # ─── 预设管理 ───

    def _get_current_preset_mgr(self) -> PresetManager | None:
        if self._current_processor is not None:
            return self._preset_managers.get(self._current_processor.preset_id)
        if self._current_file_processor is not None:
            return self._preset_managers.get(self._current_file_processor.preset_id)
        return None

    def _get_current_any_processor(self):
        """返回当前激活的处理器（图片或文件，任一）"""
        return self._current_processor or self._current_file_processor

    def _load_default_presets(self):
        """启动时：为每个处理器确保 default.json 存在，并加载默认预设"""
        all_procs = list(self._processors) + list(self._file_processors)
        for p in all_procs:
            mgr = self._preset_managers[p.preset_id]
            mgr.ensure_default(p.default_options())
            data = mgr.load_default()
            if data:
                p.apply_options(data)
        self._refresh_preset_list()

    def _refresh_preset_list(self):
        """刷新当前处理器的预设下拉列表"""
        self.combo_preset.clear()
        mgr = self._get_current_preset_mgr()
        if mgr is None:
            return
        for name in mgr.list_presets():
            display = f"[默认] {name}" if name == "default" else name
            self.combo_preset.addItem(display, name)

    def _on_preset_selected(self, combo_idx):
        """下拉框选择预设时直接应用参数"""
        mgr = self._get_current_preset_mgr()
        if mgr is None:
            return
        name = self.combo_preset.currentData()
        if name is None:
            return
        data = mgr.load_preset(name)
        if data is None:
            return
        proc = self._get_current_any_processor()
        proc.apply_options(data)
        self._update_output_extra_opts()
        display = "默认" if name == "default" else name
        self._log(f"已加载预设: {display}")

    def _load_preset_file(self):
        """从外部文件加载预设"""
        mgr = self._get_current_preset_mgr()
        if mgr is None:
            return
        
        # 获取当前功能的预设目录路径作为默认打开路径
        default_dir = str(mgr.preset_dir)
        
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择预设文件", default_dir, "JSON Files (*.json)"
        )
        if not file_path:
            return
        
        # 读取预设文件
        try:
            import json
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            QMessageBox.warning(self, "错误", f"无法读取预设文件: {e}")
            return
        
        # 获取文件名（不含扩展名）
        preset_name = Path(file_path).stem
        
        # 检查是否重名
        existing = mgr.list_user_presets()
        if preset_name in existing:
            # 重名处理：让用户选择重命名或覆盖
            msg = QMessageBox(self)
            msg.setWindowTitle("预设重名")
            msg.setText(f"预设 '{preset_name}' 已存在，请选择操作：")
            msg.setIcon(QMessageBox.Warning)
            
            rename_btn = msg.addButton("重命名", QMessageBox.AcceptRole)
            overwrite_btn = msg.addButton("覆盖", QMessageBox.DestructiveRole)
            msg.addButton("取消", QMessageBox.RejectRole)
            
            msg.exec()
            clicked_btn = msg.clickedButton()
            
            if clicked_btn == rename_btn:
                # 重命名
                new_name, ok = QInputDialog.getText(self, "重命名预设", "请输入新的预设名称:", text=preset_name)
                if not ok or not new_name.strip():
                    return
                preset_name = new_name.strip()
                if preset_name == "default":
                    QMessageBox.warning(self, "提示", "不能使用 'default' 作为预设名称，该名称为系统保留")
                    return
                if preset_name in existing:
                    QMessageBox.warning(self, "提示", f"预设 '{preset_name}' 已存在，请重新选择名称")
                    return
            elif clicked_btn == overwrite_btn:
                # 覆盖
                pass
            else:
                # 取消
                return
        
        # 保存预设
        mgr.save_preset(preset_name, data)
        self._refresh_preset_list()
        
        # 选中刚加载的预设
        for i in range(self.combo_preset.count()):
            if self.combo_preset.itemData(i) == preset_name:
                self.combo_preset.setCurrentIndex(i)
                break
        
        # 应用参数
        proc = self._get_current_any_processor()
        proc.apply_options(data)
        self._update_output_extra_opts()

        display = "默认" if preset_name == "default" else preset_name
        self._log(f"已从文件加载预设: {display}")
        QMessageBox.information(self, "成功", f"预设 '{display}' 加载成功！")

    def _save_preset(self):
        mgr = self._get_current_preset_mgr()
        if mgr is None:
            return
        name, ok = QInputDialog.getText(self, "保存预设", "请输入预设名称:")
        if not ok or not name.strip():
            return
        name = name.strip()
        if name == "default":
            QMessageBox.warning(self, "提示", "不能使用 'default' 作为预设名称，该名称为系统保留")
            return
        existing = mgr.list_user_presets()
        if name in existing:
            ret = QMessageBox.question(
                self, "覆盖确认",
                f"预设 '{name}' 已存在，是否覆盖？",
                QMessageBox.Yes | QMessageBox.No
            )
            if ret != QMessageBox.Yes:
                return
        proc = self._get_current_any_processor()
        data = proc.gather_options()
        mgr.save_preset(name, data)
        self._refresh_preset_list()
        for i in range(self.combo_preset.count()):
            if self.combo_preset.itemData(i) == name:
                self.combo_preset.setCurrentIndex(i)
                break
        self._log(f"已保存预设: {name}")

    def _reset_default(self):
        proc = self._get_current_any_processor()
        if proc is None:
            return
        mgr = self._get_current_preset_mgr()
        if mgr is None:
            return
        defaults = proc.default_options()
        mgr.save_default(defaults)
        proc.apply_options(defaults)
        self._update_output_extra_opts()
        for i in range(self.combo_preset.count()):
            if self.combo_preset.itemData(i) == "default":
                self.combo_preset.setCurrentIndex(i)
                break
        self._log("已恢复默认设置")

    def _delete_preset(self):
        mgr = self._get_current_preset_mgr()
        if mgr is None:
            return
        name = self.combo_preset.currentData()
        if name is None:
            return
        if name == "default":
            QMessageBox.warning(self, "提示", "默认预设不能删除")
            return
        ret = QMessageBox.question(
            self, "删除确认",
            f"确定删除预设 '{name}'？",
            QMessageBox.Yes | QMessageBox.No
        )
        if ret == QMessageBox.Yes:
            mgr.delete_preset(name)
            self._refresh_preset_list()
            self._log(f"已删除预设: {name}")

    def _locate_preset(self):
        """打开当前功能的预设目录"""
        mgr = self._get_current_preset_mgr()
        if mgr is None:
            return
        
        preset_dir = mgr.preset_dir
        
        # 确保目录存在
        if not preset_dir.exists():
            preset_dir.mkdir(parents=True, exist_ok=True)
        
        # 跨平台打开文件夹
        try:
            if sys.platform == 'win32':
                os.startfile(str(preset_dir))
            elif sys.platform == 'darwin':  # macOS
                subprocess.run(['open', str(preset_dir)])
            else:  # Linux
                subprocess.run(['xdg-open', str(preset_dir)])
            self._log(f"已打开预设目录: {preset_dir}")
        except Exception as e:
            QMessageBox.warning(self, "错误", f"无法打开预设目录: {e}")
