"""SettingsRoute —— 配置中心壳（P5：自 settings_panel 拆出）。

左侧菜单（开发环境 → 抠图模型配置）+ 右侧内容栈；
子页拆分为 DevEnvRoute / MattingModelRoute，本路由只做装配与跨页连线：
- 开发环境状态变化 → 重估模型页门禁
- 模型页跳转 / 安装 uv / 刷新开发页标签 → 委托开发环境页
- 两个子页的后台日志信号统一转发给壳（写入「后台日志」页）
"""
from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QStackedWidget,
    QVBoxLayout, QWidget,
)

from services.common.matting_config_service import get_matting_config_service
from services.common.runtime_facade import get_runtime_facade

from .dev_env_route import DevEnvRoute
from .matting_model_route import MattingModelRoute


class SettingsRoute(QWidget):
    """主窗口「配置」Tab 的内容区。"""

    # 后台日志：由主窗口连接，写入「后台日志」页
    log_begin = Signal(str, str)   # feature_id, title
    log_line = Signal(str)         # 一行日志

    def __init__(self, parent=None, lazy: bool = False):
        super().__init__(parent)
        self._facade = get_runtime_facade()
        self._service = get_matting_config_service()
        self._build_ui()
        self._wire_sub_routes()
        # 不在构造时做重扫描；仅展示占位，切入菜单时再异步刷新
        if not lazy:
            QTimer.singleShot(50, lambda: self._schedule_refresh(0))

    # ── UI 构建 ──

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        left = QWidget()
        left.setFixedWidth(168)
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(6)

        title = QLabel("配置菜单")
        title.setStyleSheet(
            "color:#a8b0d0;font-size:12px;font-weight:bold;padding:4px 2px;"
        )
        left_lay.addWidget(title)

        self.menu_list = QListWidget()
        self.menu_list.setObjectName("settings_menu")
        self.menu_list.addItem(QListWidgetItem("🛠  开发环境"))
        self.menu_list.addItem(QListWidgetItem("✂  抠图模型配置"))
        self.menu_list.setCurrentRow(0)
        self.menu_list.currentRowChanged.connect(self._on_menu_changed)
        left_lay.addWidget(self.menu_list, 1)
        root.addWidget(left)

        self.dev_route = DevEnvRoute(facade=self._facade)
        self.matting_route = MattingModelRoute(
            facade=self._facade, service=self._service,
        )

        self.content_stack = QStackedWidget()
        self.content_stack.addWidget(self.dev_route)        # 0
        self.content_stack.addWidget(self.matting_route)    # 1
        root.addWidget(self.content_stack, 1)

    def _wire_sub_routes(self):
        # 后台日志统一转发
        self.dev_route.log_begin.connect(self.log_begin)
        self.dev_route.log_line.connect(self.log_line)
        self.matting_route.log_begin.connect(self.log_begin)
        self.matting_route.log_line.connect(self.log_line)

        # 开发环境状态变化 → 模型页门禁重估
        self.dev_route.dev_state_changed.connect(self.matting_route.apply_gate)

        # 模型页 → 开发环境页 / 菜单跳转
        self.matting_route.goto_dev_requested.connect(lambda: self.open_menu(0))
        self.matting_route.install_uv_requested.connect(self._on_install_uv_requested)
        self.matting_route.uv_status_resolved.connect(self.dev_route.apply_uv_status)
        self.matting_route.git_status_resolved.connect(self.dev_route.apply_git_status)
        self.matting_route.vc_recheck_requested.connect(self.dev_route.refresh_vc_status)

    def _on_install_uv_requested(self):
        """模型页请求安装 uv：切到开发环境页并启动安装。"""
        self.open_menu(0)
        self.dev_route.install_uv()

    # ── 菜单 / 对外跳转 ──

    def open_menu(self, row: int):
        """供主窗口/其它模块跳转到指定配置子页。"""
        row = max(0, min(row, self.menu_list.count() - 1))
        if self.menu_list.currentRow() != row:
            self.menu_list.setCurrentRow(row)
        else:
            self._on_menu_changed(row)

    def _on_menu_changed(self, row: int):
        if row < 0:
            return
        self.content_stack.setCurrentIndex(row)
        # 延迟一帧再刷，保证 stacked 切换先完成、不卡顿
        QTimer.singleShot(0, lambda r=row: self._schedule_refresh(r))

    def _schedule_refresh(self, row: int):
        if row == 0:
            self.dev_route.schedule_refresh(force=False)
        elif row == 1:
            # 进入模型页前先确认开发环境是否就绪（门禁在内部处理）
            self.matting_route.on_page_entered()

    # ── 关闭协调（§5.4 规则 5：有界等待后台线程）──

    def request_shutdown(self, wait_ms: int = 3000) -> None:
        self.dev_route.wait_workers(wait_ms)
        self.matting_route.wait_workers(wait_ms)

    def showEvent(self, event):
        super().showEvent(event)
        # 仅首次显示时调度一次后台刷新（切换菜单走 _on_menu_changed）
        if not self.dev_route.dev_loaded and self.menu_list.currentRow() == 0:
            QTimer.singleShot(0, lambda: self._schedule_refresh(0))
