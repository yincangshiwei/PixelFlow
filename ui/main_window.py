"""
PixelFlow 主窗口
四大区域：① 图片列表  ② 功能菜单+参数  ③ 输出设置  ④ 处理日志
"""
import os
import subprocess
import sys
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
from core.worker import ProcessWorker
from core.file_worker import FileProcessWorker
from core.log_manager import AppLogManager

# 图片格式
IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff', '.tif', '.gif'}
# 文档格式
DOC_EXTS = {'.docx', '.pdf'}
# 全部支持的格式（用于拖放判断）
VALID_EXTS = IMAGE_EXTS | DOC_EXTS
THUMB_SIZE = QSize(48, 48)
_RES_DIR = str(RESOURCES_DIR).replace("\\", "/")


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
        # 路径到列表项的映射缓存，加速缩略图加载时的查找
        self._path_to_item: dict[str, QListWidgetItem] = {}
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
        self.btn_tab_changelog = QPushButton("  版本日志  ")
        self.btn_tab_changelog.setObjectName("tab_inactive")
        self.btn_tab_changelog.setMinimumHeight(34)
        tab_row.addWidget(self.btn_tab_process)
        tab_row.addWidget(self.btn_tab_log)
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

        # --- 页面2：版本日志 ---
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

        self.main_stack.addWidget(changelog_page)  # index 2

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

        # 自动创建文件夹
        self.chk_auto_folder = QCheckBox("在该路径下自动创建文件夹保存")
        self.chk_auto_folder.setChecked(True)
        out_lay.addWidget(self.chk_auto_folder)

        right_lay.addWidget(out_group)
        right_lay.addSpacing(8)

        # ── 底部操作栏 ──
        bottom = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%v / %m")
        bottom.addWidget(self.progress_bar, 1)
        self.btn_start = QPushButton("  开始处理  ")
        self.btn_start.setObjectName("btn_start")
        self.btn_start.setMinimumHeight(38)
        bottom.addWidget(self.btn_start)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setEnabled(False)
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
        self.btn_cancel.clicked.connect(self._cancel_process)
        self.file_list.currentItemChanged.connect(self._on_file_selected)
        self.file_list.model().rowsInserted.connect(self._update_file_count)
        self.file_list.model().rowsRemoved.connect(self._update_file_count)
        self.file_list.model().modelReset.connect(self._update_file_count)
        self.combo_processor.currentIndexChanged.connect(self._on_processor_changed)
        self.path_group.idToggled.connect(self._on_path_mode_changed)
        self.combo_preset.currentIndexChanged.connect(self._on_preset_selected)
        self.btn_load_preset_file.clicked.connect(self._load_preset_file)
        self.btn_save_preset.clicked.connect(self._save_preset)
        self.btn_reset_default.clicked.connect(self._reset_default)
        self.btn_delete_preset.clicked.connect(self._delete_preset)
        self.btn_locate_preset.clicked.connect(self._locate_preset)
        self.btn_tab_process.clicked.connect(lambda: self._switch_tab(0))
        self.btn_tab_log.clicked.connect(lambda: self._switch_tab(1))
        self.btn_tab_changelog.clicked.connect(lambda: self._switch_tab(2))
        self.btn_clear_log.clicked.connect(self._clear_current_log)

        # 初始状态
        if self._processors:
            self._on_processor_changed(0)

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

    # ─── Tab 切换 ───
    def _switch_tab(self, idx):
        self.main_stack.setCurrentIndex(idx)
        tab_btns = [self.btn_tab_process, self.btn_tab_log, self.btn_tab_changelog]
        for i, btn in enumerate(tab_btns):
            btn.setObjectName("tab_active" if i == idx else "tab_inactive")
            btn.style().unpolish(btn)
            btn.style().polish(btn)

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
        self.chk_auto_folder.setVisible(btn_id in (0, 1))

        if btn_id == 0:
            self.txt_output_dir.setText(_get_desktop_path())
        elif btn_id == 1:
            if self.txt_output_dir.text() == _get_desktop_path():
                self.txt_output_dir.setText("")
                self.txt_output_dir.setPlaceholderText("选择或输入自定义输出目录...")

    def _resolve_output_dir(self, src_path: str) -> tuple[str, bool]:
        """根据当前路径模式，返回 (output_dir, is_overwrite)"""
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
        existing = {self.file_list.item(i).data(Qt.UserRole) for i in range(self.file_list.count())}
        new_image_paths = []
        
        # 暂停 UI 更新，提升批量添加性能
        self.file_list.setUpdatesEnabled(False)
        try:
            for f in files:
                if f not in existing:
                    # 如果提供了 base_dir，则显示相对路径，否则显示绝对路径（或文件名）
                    if base_dir:
                        try:
                            display_name = str(Path(f).relative_to(base_dir))
                        except ValueError:
                            display_name = Path(f).name
                    else:
                        # 对于单个添加的文件，尽量显示其父目录+文件名以便区分
                        display_name = f"{Path(f).parent.name}/{Path(f).name}"
                        
                    item = QListWidgetItem(display_name)
                    item.setData(Qt.UserRole, f)
                    item.setToolTip(f)
                    # 文档文件显示文字图标，不加载缩略图
                    if Path(f).suffix.lower() in DOC_EXTS:
                        ext = Path(f).suffix.lower()
                        item.setText(f"{'📄' if ext == '.pdf' else '📝'}  {display_name}")
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
                path = item.data(Qt.UserRole)
                self._path_to_item.pop(path, None)
                self.file_list.takeItem(row)
        finally:
            # 恢复 UI 更新
            self.file_list.setUpdatesEnabled(True)
        
        # 更新文件计数
        self._update_file_count()

    def _update_file_count(self):
        self.lbl_file_count.setText(f"共 {self.file_list.count()} 个文件")

    def _on_file_selected(self, current, _prev):
        if current is None:
            return
        path = current.data(Qt.UserRole)
        # 文档文件不做图片预览
        if Path(path).suffix.lower() in DOC_EXTS:
            self.preview_label.clear()
            ext = Path(path).suffix.upper().lstrip(".")
            icon_char = "📄" if ext == "PDF" else "📝"
            self.preview_label.setText(f"{icon_char}\n{ext} 文档")
            size_kb = Path(path).stat().st_size // 1024 if Path(path).exists() else 0
            self.lbl_preview_info.setText(f"{Path(path).name}  ({size_kb} KB)")
            return
        pixmap = QPixmap(path)
        if pixmap.isNull():
            self.preview_label.setText("无法加载预览")
            self.lbl_preview_info.setText("")
            return
        w, h = pixmap.width(), pixmap.height()
        # 同步底图尺寸给叠加处理器（用于宫格坐标定位）
        if (self._current_processor is not None 
                and hasattr(self._current_processor, 'set_base_image_size')):
            self._current_processor.set_base_image_size(w, h)
        scaled = pixmap.scaled(self.preview_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.preview_label.setPixmap(scaled)
        self.lbl_preview_info.setText(f"{Path(path).name}  ({w} × {h})")

    def _browse_output(self):
        folder = QFileDialog.getExistingDirectory(self, "选择输出目录", _get_desktop_path())
        if folder:
            self.txt_output_dir.setText(folder)

    # ─── 处理逻辑 ───
    def _start_process(self):
        count = self.file_list.count()
        if count == 0:
            QMessageBox.warning(self, "提示", "请先添加要处理的文件")
            return
        if self._current_processor is None and self._current_file_processor is None:
            QMessageBox.warning(self, "提示", "请选择处理功能")
            return

        # 验证自定义路径
        mode_id = self.path_group.checkedId()
        if mode_id == 1 and not self.txt_output_dir.text().strip():
            QMessageBox.warning(self, "提示", "请选择自定义输出目录")
            return

        file_list = [self.file_list.item(i).data(Qt.UserRole) for i in range(count)]
        output_dir, is_overwrite = self._resolve_output_dir(file_list[0])

        proc_for_log = self._current_processor or self._current_file_processor
        if proc_for_log is not None:
            self._log_manager.switch_feature(proc_for_log.preset_id, clear_current=True)
        self.log_text.clear()
        self._switch_tab(1)
        mode_names = ["桌面路径", "自定义路径", "原图路径(覆盖)", "原图路径(副本)"]
        auto_folder = self.chk_auto_folder.isChecked() and not is_overwrite

        self.progress_bar.setMaximum(count)
        self.progress_bar.setValue(0)
        self.btn_start.setEnabled(False)
        self.btn_cancel.setEnabled(True)

        if self._current_processor is not None:
            # ── 图片处理 Worker ──
            proc = self._current_processor
            options = proc.gather_options()
            self._log(f"功能: {proc.icon}  {proc.name}")
            self._log(f"共 {count} 个文件")
            self._log(f"输出模式: {mode_names[mode_id]}  →  {output_dir}")
            self._log("─" * 50)
            self.worker = ProcessWorker(
                file_list, output_dir, proc, options,
                auto_subfolder=auto_folder
            )
            self.worker.progress.connect(self._on_progress)
            self.worker.image_done.connect(self._on_image_done)
            self.worker.all_done.connect(self._on_all_done)
            self.worker.debug.connect(self._on_worker_debug)
            self.worker.start()
        else:
            # ── 文件处理 Worker ──
            proc = self._current_file_processor
            options = proc.gather_options()
            self._log(f"功能: {proc.icon}  {proc.name}")
            self._log(f"共 {count} 个文件")
            self._log(f"输出模式: {mode_names[mode_id]}  →  {output_dir}")
            self._log("─" * 50)
            self.worker = FileProcessWorker(
                file_list, output_dir, proc, options,
                auto_subfolder=auto_folder
            )
            self.worker.progress.connect(self._on_progress)
            self.worker.file_done.connect(self._on_file_done)
            self.worker.all_done.connect(self._on_all_done)
            self.worker.debug.connect(self._on_worker_debug)
            self.worker.start()

    def _cancel_process(self):
        if self.worker:
            self.worker.cancel()
            self._log("⚠ 用户取消处理")

    def _on_progress(self, current, total, filename):
        # 动态同步最大值（批量处理器上报的 total 是文件总数，可能与初始 count 不同）
        if total > 0 and self.progress_bar.maximum() != total:
            self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)
        if filename and filename != "完成":
            self._log(f"  ▶ {filename}")

    def _on_image_done(self, result):
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
            if "trimmed_size" in d:
                parts.append(f"裁剪→{d['trimmed_size'][0]}×{d['trimmed_size'][1]}")
            if "resized_size" in d:
                parts.append(f"缩放→{d['resized_size'][0]}×{d['resized_size'][1]}")
            if "canvas_size" in d:
                parts.append(f"画布→{d['canvas_size'][0]}×{d['canvas_size'][1]}")
            if "compress_info" in d:
                parts.append(f"压缩→{d['compress_info']}")
            if "output_format" in d:
                parts.append(f"格式→{d['output_format'].upper()}")
            self._log("  ".join(parts))
        else:
            error = str(result.error)
            self._log(f"✗ {name}  错误: {error.splitlines()[0] if error else ''}")
            if "\n" in error:
                self._on_worker_debug(f"{name} 完整错误信息:\n{error}")

    def _on_file_done(self, result):
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

    def _on_all_done(self, results):
        success = sum(1 for r in results if r.success)
        fail = len(results) - success
        self._log("─" * 50)
        self._log(f"处理完成!  成功: {success}  失败: {fail}")
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.worker = None
        if fail == 0:
            QMessageBox.information(self, "完成", f"全部 {success} 个文件处理成功!")
        else:
            QMessageBox.warning(self, "完成", f"成功: {success}  失败: {fail}\n请查看日志")

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
