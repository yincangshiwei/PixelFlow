"""PixelFlow 主窗口 —— 应用壳 / Composition Root（P5）。

壳只负责：
- 布局骨架（左栏文件列表 + 右栏 Tab / 输出设置 / 操作栏）
- 显式创建依赖（AppContext）并挂载各区域 Route
- 跨区域信号连线与 Tab 导航
- 窗口关闭协调（§5.4 规则 5）

业务规则均在 services 层；导入协调在 ImportCoordinator；
日志 / 版本日志 / 配置中心分别为独立 Route。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QPushButton, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
    QWidgetAction,
)

from config import (
    APP_COPYRIGHT,
    APP_COPYRIGHT_URL,
    APP_NAME,
    APP_TITLE,
    APP_VERSION,
)
from core.log_manager import AppLogManager
from services.common.batch_orchestrator import BatchOrchestrator
from services.common.importing import FileImportService
from ui.routes.changelog import ChangelogRoute
from ui.routes.file_list import FileListRoute
from ui.routes.log import LogRoute
from ui.routes.process import ActionBarRoute, OutputSettingsRoute, ProcessTabRoute
from ui.routes.settings import SettingsRoute
from ui.widgets.gradient_background import GradientBackground

from .app_context import AppContext
from .import_coordinator import ImportCoordinator
from .styles import build_global_stylesheet


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        # 主窗口也接受拖放，便于拖到左栏按钮/预览等区域
        self.setAcceptDrops(True)

        # 按屏幕分辨率自适应：初始 75% 屏幕尺寸，最小不低于 1000×650
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

        # 组合根：只读依赖容器（不保存页面 / 任务可变状态）
        self._ctx = AppContext(
            orchestrator=BatchOrchestrator(),
            import_service=FileImportService(),
            log_manager=AppLogManager(),
        )

        # 各区域 Route
        self._file_list_route: FileListRoute | None = None
        self._process_tab: ProcessTabRoute | None = None
        self._output_route: OutputSettingsRoute | None = None
        self._action_bar: ActionBarRoute | None = None
        self._log_route: LogRoute | None = None
        self._changelog_route: ChangelogRoute | None = None
        self._settings_route: SettingsRoute | None = None  # 懒加载
        self._settings_page_lay = None
        self._settings_placeholder = None
        self._pending_settings_menu: int | None = None
        self._import_coordinator: ImportCoordinator | None = None

        self._build_ui()
        self.setStyleSheet(build_global_stylesheet())

    # ─── 构建 UI ───
    def _build_ui(self):
        central = GradientBackground()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(12)

        # ════════ 左栏：图片列表区（FileListRoute）════════
        self._file_list_route = FileListRoute()
        root.addWidget(self._file_list_route)

        # ════════ 右栏：Tab切换 + 输出设置 ════════
        right = QWidget()
        right.setObjectName("glass_panel")
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(12, 12, 12, 12)
        right_lay.setSpacing(0)

        # ── 主内容区（StackedWidget）──
        self.main_stack = QStackedWidget()

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

        # --- 页面0：图像处理（ProcessTabRoute：功能/预设/面板）---
        self._process_tab = ProcessTabRoute()
        self.main_stack.addWidget(self._process_tab)  # index 0

        # --- 页面1：后台日志（LogRoute）---
        self._log_route = LogRoute(log_manager=self._ctx.log_manager)
        self.main_stack.addWidget(self._log_route)  # index 1

        # --- 页面2：配置（懒加载，避免启动时扫描 Python/环境卡顿）---
        settings_page = QWidget()
        settings_page.setObjectName("tab_content_group")
        settings_page_lay = QVBoxLayout(settings_page)
        settings_page_lay.setContentsMargins(12, 12, 12, 12)
        settings_page_lay.setSpacing(8)
        self._settings_page_lay = settings_page_lay
        self._settings_placeholder = QLabel("正在加载配置…")
        self._settings_placeholder.setAlignment(Qt.AlignCenter)
        self._settings_placeholder.setStyleSheet("color:#8a90b0;")
        settings_page_lay.addWidget(self._settings_placeholder, 1)
        self.main_stack.addWidget(settings_page)  # index 2

        # --- 页面3：版本日志（ChangelogRoute）---
        self._changelog_route = ChangelogRoute()
        self.main_stack.addWidget(self._changelog_route)  # index 3

        right_lay.addWidget(self.main_stack, 1)
        right_lay.addSpacing(8)

        # ── 输出设置（OutputSettingsRoute）──
        self._output_route = OutputSettingsRoute(scope_counts=self._scope_counts)
        right_lay.addWidget(self._output_route)
        right_lay.addSpacing(8)

        # ── 底部操作栏（ActionBarRoute：进度条 + 开始/取消/续跑）──
        self._action_bar = ActionBarRoute(
            orchestrator=self._ctx.orchestrator,
            process_tab=self._process_tab,
            output_route=self._output_route,
            file_list_route=self._file_list_route,
            log_manager=self._ctx.log_manager,
            log_writer=self._log_route.log,
            log_clear=self._log_route.clear_text,
            debug_writer=self._log_route.debug,
            switch_to_log_tab=lambda: self._switch_tab(1),
        )
        right_lay.addWidget(self._action_bar)

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

        # ─── 导入协调（对话框 / 拖放 / 粘贴 / 容器抽图）───
        self._import_coordinator = ImportCoordinator(
            window=self,
            file_list_route=self._file_list_route,
            import_service=self._ctx.import_service,
        )

        # ─── 菜单栏（关于）───
        self._build_menubar()

        # ─── 信号 ───
        flr = self._file_list_route
        coord = self._import_coordinator
        flr.add_files_requested.connect(coord.add_files_dialog)
        flr.add_folder_requested.connect(coord.add_folder_dialog)
        flr.cleared.connect(self._action_bar.on_file_list_cleared)
        flr.files_removed.connect(self._on_files_removed)
        flr.select_failed_requested.connect(self._action_bar.select_failed_files)
        flr.urls_dropped.connect(coord.import_urls)
        flr.current_changed.connect(self._on_file_current_changed)
        flr.selection_changed.connect(self._update_scope_hint)
        # 全局粘贴快捷键（焦点在输入框等控件时仍可用，文本控件自身会优先处理）
        self._shortcut_paste = QShortcut(QKeySequence.Paste, self)
        self._shortcut_paste.setContext(Qt.WindowShortcut)
        self._shortcut_paste.activated.connect(coord.on_paste_shortcut)
        self.btn_tab_process.clicked.connect(lambda: self._switch_tab(0))
        self.btn_tab_log.clicked.connect(lambda: self._switch_tab(1))
        self.btn_tab_settings.clicked.connect(lambda: self._switch_tab(2))
        self.btn_tab_changelog.clicked.connect(lambda: self._switch_tab(3))

        # ProcessTabRoute 协作信号
        pt = self._process_tab
        if pt is not None:
            pt.log_message.connect(self._log_route.log)
            pt.feature_changed.connect(self._on_process_feature_changed)
            pt.output_opts_need_refresh.connect(self._refresh_output_extra_opts)
            # 初始：同步当前功能到日志管理器与输出区
            self._on_process_feature_changed(
                pt.current_feature_id or "",
                pt.current_processor(),
            )
        else:
            self._refresh_output_extra_opts()

    # ─── 处理 Tab / 功能切换（ProcessTabRoute）───
    def _on_process_feature_changed(self, feature_id: str, proc):
        """ProcessTabRoute.feature_changed：日志分区、选中回读、输出区联动。"""
        fid = feature_id or (
            getattr(proc, "preset_id", "") if proc is not None else ""
        )
        if fid:
            self._log_route.switch_feature(fid, clear_current=True)
        if self._file_list_route is not None:
            self._notify_processor_selection(self._file_list_route.current_path())
        self._refresh_output_extra_opts()

    def _refresh_output_extra_opts(self):
        """按当前功能 / 抠图开关刷新输出区附加选项可见性。"""
        pt = self._process_tab
        if pt is None or self._output_route is None:
            return
        grp = pt.matting_group_widget()
        self._output_route.refresh_extra_opts(
            pt.current_feature_id or "",
            bool(grp is not None and grp.isChecked()),
        )

    def _scope_counts(self) -> tuple[int, int]:
        """输出设置区的处理范围提示数据源：(全部文件数, 选中文件数)。"""
        if self._file_list_route is None:
            return 0, 0
        return self._file_list_route.count(), self._file_list_route.selected_count()

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
            self._ensure_settings_route()
        # 回到图像处理时刷新当前面板状态（如抠图模型就绪提示）
        elif idx == 0 and self._process_tab is not None:
            route = self._process_tab.current_route()
            if route is not None and hasattr(route, "on_panel_activated"):
                try:
                    route.on_panel_activated()
                except Exception:
                    pass

    def _ensure_settings_route(self):
        if self._settings_route is not None:
            return
        # 先让出事件循环，保证 Tab 切换动画/重绘先完成
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._build_settings_route)

    def _build_settings_route(self):
        if self._settings_route is not None:
            return
        route = SettingsRoute(lazy=True)
        self._settings_route = route
        route.log_begin.connect(self._on_settings_log_begin)
        route.log_line.connect(self._on_settings_log_line)
        if self._settings_placeholder is not None:
            self._settings_page_lay.removeWidget(self._settings_placeholder)
            self._settings_placeholder.deleteLater()
            self._settings_placeholder = None
        self._settings_page_lay.addWidget(route, 1)
        # 若有待跳转的配置子菜单（如从透明图处理点链接）
        if self._pending_settings_menu is not None:
            pending = self._pending_settings_menu
            self._pending_settings_menu = None
            route.open_menu(int(pending))

    def open_settings(self, menu_row: int = 0):
        """打开「配置」Tab，并切换到指定左侧菜单
        （0=开发环境，1=抠图模型，2=高清放大引擎）。"""
        self._pending_settings_menu = menu_row
        self._switch_tab(2)
        if self._settings_route is not None:
            self._pending_settings_menu = None
            self._settings_route.open_menu(menu_row)

    def _on_settings_log_begin(self, feature_id: str, title: str):
        """配置中心长任务开始：切换后台日志并写入标题。"""
        try:
            self._log_route.switch_feature(feature_id or "settings_runtime", clear_current=True)
        except Exception:
            pass
        self._log_route.clear_text()
        self._switch_tab(1)
        self._log_route.log(title)
        self._log_route.log("─" * 50)

    def _on_settings_log_line(self, text: str):
        """配置中心进度/详情写入后台日志。"""
        if text:
            self._log_route.log(text)

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

    # ─── 拖放（导入决策在 ImportCoordinator）───
    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent):
        self._import_coordinator.import_urls(event.mimeData().urls())
        event.acceptProposedAction()

    # ─── 左栏协作（列表状态真相在 FileListRoute.collection）───
    def _on_files_removed(self, paths: list):
        """移除条目后同步会话：移除的文件不再参与续跑。"""
        if self._action_bar is not None:
            self._action_bar.on_files_removed(paths)
        self._update_scope_hint()

    def _on_file_current_changed(self, path: str | None, size):
        """当前条目变化：预览已由路由绘制，这里同步底图尺寸给叠加功能。"""
        # ProcessTabRoute 内部会同步给叠加 Route / Processor（用于宫格坐标定位）
        if path is not None and size is not None and self._process_tab is not None:
            self._process_tab.set_base_image_size(size[0], size[1])
        self._notify_processor_selection(path)

    def _update_scope_hint(self):
        if self._output_route is not None:
            self._output_route.update_scope_hint()

    def _notify_processor_selection(self, path: str | None):
        """通知当前功能：选中项变化（用于单图回读等）。"""
        if self._process_tab is not None:
            self._process_tab.notify_selected_image(path)

    # ─── 窗口关闭（§5.4 规则 5：禁止新任务 → 请求取消 → 有界等待）───
    def closeEvent(self, event):
        if self._action_bar is not None and not self._action_bar.request_shutdown(wait_ms=15000):
            event.ignore()
            QMessageBox.information(
                self, "提示",
                "后台任务仍在清理中，请等待其完成或取消后再关闭窗口。",
            )
            return
        # 配置中心后台线程：有界等待，避免退出时销毁运行中的 QThread
        if self._settings_route is not None:
            self._settings_route.request_shutdown(wait_ms=3000)
        # 容器抽图辅助线程：有界等待
        if self._import_coordinator is not None:
            self._import_coordinator.wait_extract_worker(3000)
        event.accept()
