"""ProcessTabRoute —— 图像处理 Tab：功能切换 / 预设栏 / 面板栈。

职责：
- 构建功能下拉、预设栏、参数面板滚动区
- 从 FeatureRegistry 创建面板（各功能 FeatureRoute）
- 预设操作委托 PresetService；日志/弹窗通过信号交给 MainWindow
- 批处理编排由 ActionBarRoute + BatchOrchestrator 负责
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel, QPushButton,
    QComboBox, QStackedWidget, QScrollArea, QMessageBox, QInputDialog,
    QFileDialog,
)

import config
from services.common.preset_service import (
    PresetConflictAction,
    PresetService,
)
from services.contracts.feature_descriptor import FeatureDescriptor, InputKind
from services.features.basic_service import FEATURE_ID as BASIC_ID
from services.features.catalog import build_feature_registry
from services.features.metadata_service import FEATURE_ID as METADATA_ID
from services.features.overlay_service import FEATURE_ID as OVERLAY_ID
from services.features.img2doc_service import FEATURE_ID as IMG2DOC_ID
from services.features.transparent_service import FEATURE_ID as TRANSPARENT_ID
from ui.routes.process.features.basic_route import BasicFeatureRoute
from ui.routes.process.features.metadata_route import MetadataFeatureRoute
from ui.routes.process.features.overlay_route import OverlayFeatureRoute
from ui.routes.process.features.img2doc_route import Img2DocFeatureRoute
from ui.routes.process.features.transparent_route import TransparentFeatureRoute


def _default_route_factories() -> dict[str, callable]:
    return {
        BASIC_ID: BasicFeatureRoute,
        METADATA_ID: MetadataFeatureRoute,
        OVERLAY_ID: OverlayFeatureRoute,
        IMG2DOC_ID: Img2DocFeatureRoute,
        TRANSPARENT_ID: TransparentFeatureRoute,
    }


class ProcessTabRoute(QWidget):
    """图像处理 Tab 路由（控件容器 + 区域 Controller）。"""

    # 协作信号
    log_message = Signal(str)                 # 追加日志
    feature_changed = Signal(str, object)     # (feature_id, processor|None)
    output_opts_need_refresh = Signal()       # 透明图抠图等输出区联动
    panel_activated = Signal(object)          # 当前 processor（可 on_panel_activated）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._registry = None
        self._services: dict[str, Any] = {}
        self._descriptors: list[FeatureDescriptor] = []
        # feature_id → FeatureRoute 面板
        self._panel_by_id: dict[str, QWidget] = {}
        # feature_id → 绑定的 processor 实例（供 Worker）
        self._processor_by_id: dict[str, Any] = {}
        # feature_id → route 实例
        self._route_by_id: dict[str, Any] = {}
        self._current_feature_id: str | None = None
        self._preset_service = PresetService(service_resolver=self._resolve_service)

        self._build_catalog()
        self._build_widget()
        self._wire_signals()
        self._load_default_presets()

    # ── 目录与服务 ──

    def _resolve_service(self, feature_id: str):
        return self._services.get(feature_id)

    def _build_catalog(self) -> None:
        self._registry, self._services = build_feature_registry(
            route_factories=_default_route_factories(),
        )
        self._descriptors = self._registry.all()

        # 创建各功能 processor 实例（Worker 需要长生命周期对象）
        for desc in self._descriptors:
            svc = self._services[desc.id]
            self._processor_by_id[desc.id] = svc.create_processor()

    # ── UI 构建 ──

    def _build_widget(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        proc_group = QGroupBox()
        proc_group.setObjectName("tab_content_group")
        proc_lay = QVBoxLayout(proc_group)

        # 功能选择栏
        menu_row = QHBoxLayout()
        menu_row.setSpacing(6)
        menu_row.addWidget(QLabel("功能:"))
        self.combo_processor = QComboBox()
        self.combo_processor.setStyleSheet(config.COMBOBOX_STYLE)
        for desc in self._descriptors:
            self.combo_processor.addItem(f"{desc.icon}  {desc.name}", desc.id)
        self.combo_processor.setMinimumWidth(180)
        menu_row.addWidget(self.combo_processor)
        self.lbl_proc_desc = QLabel("")
        self.lbl_proc_desc.setStyleSheet("color:#888;font-size:12px;")
        menu_row.addWidget(self.lbl_proc_desc, 1)
        proc_lay.addLayout(menu_row)

        # 预设栏
        preset_row = QHBoxLayout()
        preset_row.setSpacing(6)
        preset_row.addWidget(QLabel("预设:"))
        self.combo_preset = QComboBox()
        self.combo_preset.setStyleSheet(config.COMBOBOX_STYLE)
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
        for b in (
            self.btn_load_preset_file, self.btn_save_preset,
            self.btn_reset_default, self.btn_delete_preset, self.btn_locate_preset,
        ):
            preset_row.addWidget(b)
        preset_row.addStretch()
        proc_lay.addLayout(preset_row)

        # 面板栈
        self.panel_stack = QStackedWidget()
        for desc in self._descriptors:
            panel = self._create_panel_for(desc)
            self._panel_by_id[desc.id] = panel
            self.panel_stack.addWidget(panel)

        self.panel_scroll_area = QScrollArea()
        self.panel_scroll_area.setWidgetResizable(True)
        self.panel_scroll_area.setFrameShape(QScrollArea.NoFrame)
        self.panel_scroll_area.setStyleSheet("background: transparent;")
        self.panel_scroll_area.setWidget(self.panel_stack)
        proc_lay.addWidget(self.panel_scroll_area, 1)

        root.addWidget(proc_group, 1)

    def _create_panel_for(self, desc: FeatureDescriptor) -> QWidget:
        factory = desc.route_factory or _default_route_factories().get(desc.id)
        if factory is None:
            # 兜底：空面板（不应发生）
            w = QWidget()
            QVBoxLayout(w).addWidget(QLabel(f"未注册 Route: {desc.id}"))
            return w
        route = factory()
        self._route_by_id[desc.id] = route
        if hasattr(route, "build_widget"):
            return route.build_widget()
        return route

    def _wire_signals(self) -> None:
        self.combo_processor.currentIndexChanged.connect(self._on_feature_changed)
        self.combo_preset.currentIndexChanged.connect(self._on_preset_selected)
        self.btn_load_preset_file.clicked.connect(self._load_preset_file)
        self.btn_save_preset.clicked.connect(self._save_preset)
        self.btn_reset_default.clicked.connect(self._reset_default)
        self.btn_delete_preset.clicked.connect(self._delete_preset)
        self.btn_locate_preset.clicked.connect(self._locate_preset)

        # 透明图 AI 抠图开关 → 通知输出区
        tr = self._route_by_id.get(TRANSPARENT_ID)
        if tr is not None:
            grp = getattr(tr, "_grp_matting", None) or getattr(tr, "matting_group", None)
            if grp is not None and hasattr(grp, "toggled"):
                grp.toggled.connect(lambda _c: self.output_opts_need_refresh.emit())
            if hasattr(tr, "matting_toggled"):
                tr.matting_toggled.connect(lambda _c: self.output_opts_need_refresh.emit())

        if self._descriptors:
            self._on_feature_changed(0)

    # ── 对外查询（MainWindow / 批处理）──

    @property
    def registry(self):
        return self._registry

    @property
    def current_feature_id(self) -> str | None:
        return self._current_feature_id

    def current_descriptor(self) -> FeatureDescriptor | None:
        if not self._current_feature_id:
            return None
        return self._registry.get(self._current_feature_id)

    def current_service(self):
        if not self._current_feature_id:
            return None
        return self._services.get(self._current_feature_id)

    def current_processor(self):
        """当前功能绑定的 Processor（纯化实例）。"""
        if not self._current_feature_id:
            return None
        return self._processor_by_id.get(self._current_feature_id)

    def current_route(self):
        if not self._current_feature_id:
            return None
        return self._route_by_id.get(self._current_feature_id)

    def processor_for(self, feature_id: str):
        return self._processor_by_id.get(feature_id)

    def route_for(self, feature_id: str):
        return self._route_by_id.get(feature_id)

    def get_processor_by_preset(self, preset_id: str):
        return self._processor_by_id.get(preset_id)

    def is_image_feature(self, feature_id: str | None = None) -> bool:
        fid = feature_id or self._current_feature_id
        desc = self._registry.get(fid) if fid else None
        if desc is None:
            return True
        return desc.input_kind in (InputKind.IMAGE, InputKind.BATCH_MERGED)

    def current_kind(self) -> str:
        """兼容旧 API：'image' | 'file'。"""
        desc = self.current_descriptor()
        if desc is None:
            return "image"
        if desc.input_kind is InputKind.FILE:
            return "file"
        return "image"

    def all_processors(self) -> list:
        return [self._processor_by_id[d.id] for d in self._descriptors]

    def image_processors(self) -> list:
        return [
            self._processor_by_id[d.id]
            for d in self._descriptors
            if d.input_kind in (InputKind.IMAGE, InputKind.BATCH_MERGED)
        ]

    def file_processors(self) -> list:
        return [
            self._processor_by_id[d.id]
            for d in self._descriptors
            if d.input_kind is InputKind.FILE
        ]

    def gather_current_options(self) -> dict:
        """收集当前功能参数（已规范化的普通 dict）。"""
        fid = self._current_feature_id
        if not fid:
            return {}
        svc = self._services.get(fid)
        route = self._route_by_id.get(fid)
        if route is not None and hasattr(route, "collect_raw_state") and svc is not None:
            raw = route.collect_raw_state()
            result = svc.validate_and_normalize(raw, strict=False)
            return dict(result.value or {})
        if svc is not None and hasattr(svc, "validate_and_normalize"):
            return dict(svc.validate_and_normalize({}, strict=False).value or {})
        return {}

    def build_run_options(self, output_policy=None) -> dict:
        """构建批处理用 options（含 _output_format 等跨层字段）。"""
        opts = self.gather_current_options()
        svc = self.current_service()
        if svc is not None and hasattr(svc, "build_run_options"):
            return svc.build_run_options(opts, output_policy)
        return opts

    def apply_state_to_current(self, data: dict | None) -> None:
        fid = self._current_feature_id
        if not fid:
            return
        svc = self._services.get(fid)
        route = self._route_by_id.get(fid)
        if route is not None and hasattr(route, "apply_state"):
            normalized = svc.normalize_preset(data) if svc else dict(data or {})
            route.apply_state(normalized)
            self.output_opts_need_refresh.emit()

    def notify_selected_image(self, path: str | None) -> None:
        """选中图变化：Service.load_selected → Route 应用；或 Route.on_selected_image。"""
        fid = self._current_feature_id
        if not fid:
            return
        route = self._route_by_id.get(fid)
        svc = self._services.get(fid)

        # 元数据：Service 读文件 → Route.apply_load_result（自动读取时）
        if fid == METADATA_ID and route is not None:
            if hasattr(route, "on_selected_image"):
                # 先记录选中路径；自动读取时内部会 load
                # 若 Route 仍自行读文件，保持行为；同时支持 Service 路径
                auto = False
                if hasattr(route, "is_auto_load_enabled"):
                    auto = bool(route.is_auto_load_enabled())
                route.on_selected_image(path)
                # 若 auto 且 route 未自行完成（on_selected_image 已处理），不再重复
                return

        if svc is not None and hasattr(svc, "load_selected"):
            state = svc.load_selected(path)
            if state is not None and route is not None:
                if hasattr(route, "apply_load_result"):
                    route.apply_load_result(state)
                elif hasattr(route, "apply_state"):
                    route.apply_state(state)
                return

        if route is not None and hasattr(route, "on_selected_image"):
            try:
                route.on_selected_image(path)
            except Exception:
                pass
            return

    def activate_feature(
        self, feature_id: str, *, clear_log_feature: bool = True
    ) -> bool:
        """按 feature_id 切换功能（续跑时 clear_log_feature=False）。"""
        for i in range(self.combo_processor.count()):
            if self.combo_processor.itemData(i) == feature_id:
                if self.combo_processor.currentIndex() == i:
                    self._sync_current_refs(feature_id)
                    return True
                if not clear_log_feature:
                    self.combo_processor.blockSignals(True)
                    self.combo_processor.setCurrentIndex(i)
                    self.combo_processor.blockSignals(False)
                    self._apply_feature_ui(feature_id, emit_feature_changed=False)
                    return True
                self.combo_processor.setCurrentIndex(i)
                return True
        return False

    def scroll_to_top(self) -> None:
        self.panel_scroll_area.verticalScrollBar().setValue(0)

    def matting_group_widget(self):
        """透明图抠图 GroupBox（输出区联动用）。"""
        tr = self._route_by_id.get(TRANSPARENT_ID)
        if tr is None:
            return None
        return getattr(tr, "matting_group", None) or getattr(tr, "_grp_matting", None)

    def set_base_image_size(self, w: int, h: int) -> None:
        """同步底图尺寸给叠加 Route / Processor。"""
        route = self._route_by_id.get(OVERLAY_ID)
        if route is not None and hasattr(route, "set_base_image_size"):
            route.set_base_image_size(w, h)
        proc = self._processor_by_id.get(OVERLAY_ID)
        if proc is not None and hasattr(proc, "set_base_image_size"):
            proc.set_base_image_size(w, h)

    # ── 功能切换 ──

    def _on_feature_changed(self, combo_idx: int) -> None:
        feature_id = self.combo_processor.itemData(combo_idx)
        if feature_id is None:
            return
        self._apply_feature_ui(feature_id, emit_feature_changed=True)

    def _sync_current_refs(self, feature_id: str) -> None:
        self._current_feature_id = feature_id

    def _apply_feature_ui(
        self, feature_id: str, *, emit_feature_changed: bool = True
    ) -> None:
        self._current_feature_id = feature_id
        desc = self._registry.get(feature_id)
        panel = self._panel_by_id.get(feature_id)
        if panel is not None:
            self.panel_stack.setCurrentWidget(panel)
        if desc is not None:
            self.lbl_proc_desc.setText(desc.description)

        self.scroll_to_top()
        self._refresh_preset_list()

        route = self._route_by_id.get(feature_id)
        if route is not None and hasattr(route, "on_panel_activated"):
            try:
                route.on_panel_activated()
            except Exception:
                pass

        proc = self._processor_by_id.get(feature_id)
        self.panel_activated.emit(proc)

        if emit_feature_changed:
            self.feature_changed.emit(feature_id, proc)
        self.output_opts_need_refresh.emit()

    # ── 预设 ──

    def _load_default_presets(self) -> None:
        for desc in self._descriptors:
            fid = desc.id
            svc = self._services.get(fid)
            default_data = svc.default_state() if svc else {}
            self._preset_service.ensure_default(fid, default_data)
            result = self._preset_service.load_default(fid)
            if result.ok and result.data is not None:
                self._apply_state_to_feature(fid, result.data)
        self._refresh_preset_list()

    def _apply_state_to_feature(self, feature_id: str, data: dict) -> None:
        svc = self._services.get(feature_id)
        route = self._route_by_id.get(feature_id)
        if route is not None and hasattr(route, "apply_state"):
            normalized = svc.normalize_preset(data) if svc else data
            route.apply_state(normalized)

    def _refresh_preset_list(self) -> None:
        self.combo_preset.blockSignals(True)
        self.combo_preset.clear()
        fid = self._current_feature_id
        if not fid:
            self.combo_preset.blockSignals(False)
            return
        for name in self._preset_service.list_presets(fid):
            display = f"[默认] {name}" if name == "default" else name
            self.combo_preset.addItem(display, name)
        self.combo_preset.blockSignals(False)

    def _select_preset_in_combo(self, name: str) -> None:
        for i in range(self.combo_preset.count()):
            if self.combo_preset.itemData(i) == name:
                self.combo_preset.blockSignals(True)
                self.combo_preset.setCurrentIndex(i)
                self.combo_preset.blockSignals(False)
                break

    def _on_preset_selected(self, _combo_idx: int) -> None:
        fid = self._current_feature_id
        if not fid:
            return
        name = self.combo_preset.currentData()
        if name is None:
            return
        result = self._preset_service.load_preset(fid, name)
        if not result.ok or result.data is None:
            return
        self.apply_state_to_current(result.data)
        display = "默认" if name == "default" else name
        self.log_message.emit(f"已加载预设: {display}")

    def _load_preset_file(self) -> None:
        fid = self._current_feature_id
        if not fid:
            return
        default_dir = str(self._preset_service.preset_dir(fid))
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择预设文件", default_dir, "JSON Files (*.json)"
        )
        if not file_path:
            return
        plan, err = self._preset_service.plan_import(fid, file_path)
        if err or plan is None:
            QMessageBox.warning(self, "错误", err or "导入失败")
            return

        preset_name = plan.suggested_name
        action = PresetConflictAction.OVERWRITE
        if plan.needs_conflict_resolution:
            msg = QMessageBox(self)
            msg.setWindowTitle("预设重名")
            msg.setText(f"预设 '{preset_name}' 已存在，请选择操作：")
            msg.setIcon(QMessageBox.Warning)
            rename_btn = msg.addButton("重命名", QMessageBox.AcceptRole)
            overwrite_btn = msg.addButton("覆盖", QMessageBox.DestructiveRole)
            msg.addButton("取消", QMessageBox.RejectRole)
            msg.exec()
            clicked = msg.clickedButton()
            if clicked == rename_btn:
                new_name, ok = QInputDialog.getText(
                    self, "重命名预设", "请输入新的预设名称:", text=preset_name
                )
                if not ok or not new_name.strip():
                    return
                preset_name = new_name.strip()
                if preset_name == "default":
                    QMessageBox.warning(
                        self, "提示",
                        "不能使用 'default' 作为预设名称，该名称为系统保留",
                    )
                    return
                if preset_name in plan.existing_names:
                    QMessageBox.warning(
                        self, "提示", f"预设 '{preset_name}' 已存在，请重新选择名称"
                    )
                    return
                action = PresetConflictAction.OVERWRITE
            elif clicked == overwrite_btn:
                action = PresetConflictAction.OVERWRITE
            else:
                return

        result = self._preset_service.commit_import(
            fid, plan, final_name=preset_name, action=action
        )
        if not result.ok:
            QMessageBox.warning(self, "错误", result.message)
            return
        self._refresh_preset_list()
        self._select_preset_in_combo(preset_name)
        if result.data is not None:
            self.apply_state_to_current(result.data)
        display = "默认" if preset_name == "default" else preset_name
        self.log_message.emit(f"已从文件加载预设: {display}")
        QMessageBox.information(self, "成功", f"预设 '{display}' 加载成功！")

    def _save_preset(self) -> None:
        fid = self._current_feature_id
        if not fid:
            return
        name, ok = QInputDialog.getText(self, "保存预设", "请输入预设名称:")
        if not ok or not name.strip():
            return
        name = name.strip()
        if name == "default":
            QMessageBox.warning(
                self, "提示", "不能使用 'default' 作为预设名称，该名称为系统保留"
            )
            return
        existing = self._preset_service.list_user_presets(fid)
        if name in existing:
            ret = QMessageBox.question(
                self, "覆盖确认",
                f"预设 '{name}' 已存在，是否覆盖？",
                QMessageBox.Yes | QMessageBox.No,
            )
            if ret != QMessageBox.Yes:
                return
        data = self.gather_current_options()
        result = self._preset_service.save_preset(fid, name, data)
        if not result.ok:
            QMessageBox.warning(self, "提示", result.message)
            return
        self._refresh_preset_list()
        self._select_preset_in_combo(name)
        self.log_message.emit(f"已保存预设: {name}")

    def _reset_default(self) -> None:
        fid = self._current_feature_id
        if not fid:
            return
        result = self._preset_service.restore_factory_default(fid)
        if not result.ok or result.data is None:
            return
        self.apply_state_to_current(result.data)
        self._select_preset_in_combo("default")
        self.log_message.emit("已恢复默认设置")

    def _delete_preset(self) -> None:
        fid = self._current_feature_id
        if not fid:
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
            QMessageBox.Yes | QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        result = self._preset_service.delete_preset(fid, name)
        if result.ok:
            self._refresh_preset_list()
            self.log_message.emit(result.message)

    def _locate_preset(self) -> None:
        fid = self._current_feature_id
        if not fid:
            return
        preset_dir = self._preset_service.preset_dir(fid)
        if not preset_dir.exists():
            preset_dir.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(str(preset_dir))
            elif sys.platform == "darwin":
                subprocess.run(["open", str(preset_dir)])
            else:
                subprocess.run(["xdg-open", str(preset_dir)])
            self.log_message.emit(f"已打开预设目录: {preset_dir}")
        except Exception as e:
            QMessageBox.warning(self, "错误", f"无法打开预设目录: {e}")
