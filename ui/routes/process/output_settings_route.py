"""OutputSettingsRoute —— 输出设置区路由（P3）。

四种输出路径模式 + 自动文件夹 / 保留结构 / 覆盖同名 / 保留抠图 +
处理范围（全部 / 仅选中）。控件结构与默认值和重构前一致；
对外只暴露 collect_policy() → OutputPolicy 等普通数据接口，
路径语义由 contracts.OutputPolicy / OutputPathService 承载。
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QFileDialog, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QRadioButton, QVBoxLayout,
)

from services.common.output_path_service import OutputPathService
from services.contracts.output_policy import (
    OutputPolicy,
    PathMode,
    default_desktop_path,
)

FEATURE_ID_TRANSPARENT = "transparent_image"


class OutputSettingsRoute(QGroupBox):
    """输出设置区域（UI Controller：只收集状态，不做编排）。"""

    scope_changed = Signal()  # 处理范围切换

    def __init__(
        self,
        scope_counts: Optional[Callable[[], tuple[int, int]]] = None,
        parent=None,
    ):
        """
        :param scope_counts: 返回 (全部文件数, 选中文件数) 的提供器
            （由壳注入文件列表路由的计数，避免路由间直接依赖）
        """
        super().__init__("输出设置", parent)
        self._scope_counts = scope_counts or (lambda: (0, 0))
        self._output_path_service = OutputPathService()
        # 最近一次功能联动状态（路径模式变化时用于重算附加选项可见性）
        self._current_feature_id: str = ""
        self._current_matting_enabled: bool = False
        self._build_widget()
        self._wire_signals()

    # ── UI 构建 ──

    def _build_widget(self) -> None:
        out_lay = QVBoxLayout(self)
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
        for i, rb in enumerate(
            [self.rb_desktop, self.rb_custom, self.rb_overwrite, self.rb_copy]
        ):
            self.path_group.addButton(rb, i)
            path_row.addWidget(rb)
        path_row.addStretch()
        out_lay.addLayout(path_row)

        # 路径输入行（桌面/自定义共用）
        path_input_row = QHBoxLayout()
        self.txt_output_dir = QLineEdit()
        self.txt_output_dir.setText(default_desktop_path())
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

    def _wire_signals(self) -> None:
        self.btn_browse.clicked.connect(self._browse_output)
        self.path_group.idToggled.connect(self._on_path_mode_changed)
        self.scope_group.idToggled.connect(self._on_scope_changed)

    # ── 对外数据接口（供 ActionBarRoute / 编排）──

    def collect_policy(self) -> OutputPolicy:
        """收集当前输出策略快照。"""
        return OutputPolicy(
            path_mode=PathMode(self.path_group.checkedId()),
            root_dir=self.txt_output_dir.text().strip(),
            auto_subfolder=self.chk_auto_folder.isChecked(),
            keep_structure=self.chk_keep_structure.isChecked(),
            file_overwrite=self.chk_overwrite_file.isChecked(),
        )

    def validate_for_start(self) -> Optional[str]:
        """开始前校验，返回错误文案；None 表示通过。"""
        return self._output_path_service.validate_for_start(self.collect_policy())

    def scope_is_selected(self) -> bool:
        return self.rb_scope_selected.isChecked()

    def set_scope_selected(self, selected: bool) -> None:
        if selected:
            self.rb_scope_selected.setChecked(True)
        else:
            self.rb_scope_all.setChecked(True)

    def keep_matting_active(self) -> bool:
        """「保留抠图结果」是否生效（仅可见且勾选时；可见性由功能联动决定）。"""
        return self.chk_keep_matting.isVisible() and self.chk_keep_matting.isChecked()

    # ── 联动刷新 ──

    def refresh_extra_opts(self, feature_id: str, matting_enabled: bool) -> None:
        """按路径模式 / 当前功能，刷新输出区附加选项可见性。"""
        self._current_feature_id = feature_id or ""
        self._current_matting_enabled = bool(matting_enabled)
        mode_id = self.path_group.checkedId()
        show_out_opts = mode_id in (0, 1)
        self.chk_auto_folder.setVisible(show_out_opts)
        self.chk_keep_structure.setVisible(show_out_opts)
        self.chk_overwrite_file.setVisible(show_out_opts)

        show_keep_matting = (
            self._current_feature_id == FEATURE_ID_TRANSPARENT
        ) and self._current_matting_enabled
        self.chk_keep_matting.setVisible(show_keep_matting)

    def update_scope_hint(self) -> None:
        all_n, sel_n = self._scope_counts()
        if self.rb_scope_selected.isChecked():
            self.lbl_scope_hint.setText(
                f"将处理选中的 {sel_n} 个文件" if sel_n else "请先在左侧选中文件"
            )
        else:
            self.lbl_scope_hint.setText(f"将处理全部 {all_n} 个文件")

    # ── 内部交互 ──

    def _on_path_mode_changed(self, btn_id, checked) -> None:
        if not checked:
            return
        # 0=桌面, 1=自定义, 2=覆盖原图, 3=副本原图
        show_input = btn_id in (0, 1)
        show_browse = btn_id == 1
        show_src_hint = btn_id in (2, 3)

        self.txt_output_dir.setVisible(show_input)
        self.btn_browse.setVisible(show_browse)
        self.lbl_src_hint.setVisible(show_src_hint)

        desktop = default_desktop_path()
        if btn_id == 0:
            self.txt_output_dir.setText(desktop)
        elif btn_id == 1:
            if self.txt_output_dir.text() == desktop:
                self.txt_output_dir.setText("")
                self.txt_output_dir.setPlaceholderText("选择或输入自定义输出目录...")

        # 附加选项可见性随路径模式 / 功能联动重算
        self.refresh_extra_opts(
            self._current_feature_id, self._current_matting_enabled
        )

    def _on_scope_changed(self, *_args) -> None:
        self.update_scope_hint()
        self.scope_changed.emit()

    def _browse_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "选择输出目录", default_desktop_path()
        )
        if folder:
            self.txt_output_dir.setText(folder)
