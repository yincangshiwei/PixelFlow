"""UpscaleFeatureRoute —— 高清放大参数面板。

**面板由引擎注册表的 ParamSpec 动态生成**：切换「放大引擎」下拉即切换整套参数，
本文件不为任何具体引擎写死控件。新增引擎只需在
``core/upscale/engine_registry.py`` 注册，UI 无需改动。

布局遵循项目宽屏优先规则：同分组内按 ``ParamSpec.row`` 横排，单行不超过 4 组
「标签 + 控件」，行末 addStretch 推齐。
"""
from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QGroupBox, QHBoxLayout, QLabel,
    QPushButton, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

import config
from core.upscale.engine_registry import (
    DEFAULT_OUTPUT_FORMAT,
    KIND_BOOL,
    KIND_COMBO,
    KIND_FLOAT,
    KIND_INT,
    OUTPUT_FORMATS,
    ParamSpec,
    UpscaleEngineInfo,
    list_engines,
)
from core.upscale.hardware_gate import (
    LEVEL_BLOCKED,
    LEVEL_READY,
    LEVEL_WARN,
    EngineAvailability,
    check_engine,
    clear_cache as clear_gate_cache,
    engine_choice_label,
)

# 配置中心「高清放大引擎」子页序号（0=开发环境 1=抠图模型 2=高清放大引擎）
SETTINGS_MENU_ROW = 2

_LOSSY_FORMATS = frozenset({"jpg", "jpeg", "webp"})

_BANNER_STYLE = {
    LEVEL_READY: "color:#8fe3a8;font-size:12px;padding:6px 8px;"
                 "background-color:rgba(40,90,60,90);border-radius:7px;",
    LEVEL_WARN: "color:#f0d48a;font-size:12px;padding:6px 8px;"
                "background-color:rgba(110,90,30,90);border-radius:7px;",
    LEVEL_BLOCKED: "color:#f0a0a0;font-size:12px;padding:6px 8px;"
                   "background-color:rgba(110,40,45,110);border-radius:7px;",
}


class _GateWorker(QThread):
    """后台执行硬件/运行时门禁检测（nvidia-smi + 文件校验），避免卡住界面。"""

    done = Signal(dict)

    def __init__(self, engine_ids: list[str], force: bool = False, parent=None):
        super().__init__(parent)
        self._ids = list(engine_ids)
        self._force = bool(force)

    def run(self):
        out: dict[str, Any] = {}
        for eid in self._ids:
            try:
                out[eid] = check_engine(eid, force=self._force)
            except Exception as e:      # noqa: BLE001 - 检测失败不应影响面板
                out[eid] = e
        self.done.emit(out)


class UpscaleFeatureRoute(QWidget):
    """高清放大 FeatureRoute：collect_raw_state / apply_state。"""

    feature_id = "upscale"
    open_settings_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._engines: list[UpscaleEngineInfo] = list_engines()
        # engine_id → {param_key: 输入控件}
        self._inputs: dict[str, dict[str, QWidget]] = {}
        # engine_id → 该引擎页面容器
        self._pages: dict[str, QWidget] = {}
        self._availability: dict[str, EngineAvailability] = {}
        self._gate_worker: Optional[_GateWorker] = None
        self._gate_checked = False
        self._base_w = 0
        self._base_h = 0
        self._build_widget()
        self._refresh_engine_combo_labels()

    def build_widget(self, parent=None) -> QWidget:
        """契约接口：返回自身（已是面板）。"""
        if parent is not None and self.parent() is None:
            self.setParent(parent)
        return self

    # ══════════════════════════════════════════════════════════
    #  构建
    # ══════════════════════════════════════════════════════════

    def _build_widget(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        # ── 状态横幅 ──
        self.lbl_banner = QLabel("正在检测硬件与运行时…")
        self.lbl_banner.setWordWrap(True)
        self.lbl_banner.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.lbl_banner.setStyleSheet(_BANNER_STYLE[LEVEL_WARN])
        self.lbl_banner.linkActivated.connect(self._on_link)
        root.addWidget(self.lbl_banner)

        # ── 引擎选择行 ──
        eng_row = QHBoxLayout()
        eng_row.setSpacing(8)
        eng_row.addWidget(QLabel("放大引擎:"))
        self.combo_engine = QComboBox()
        self.combo_engine.setStyleSheet(config.COMBOBOX_STYLE)
        self.combo_engine.setMinimumWidth(260)
        self.combo_engine.setToolTip(
            "不同放大引擎有各自独立的参数；切换引擎会整体切换下方参数面板，"
            "且各引擎的参数分别保存在预设中，互不覆盖。"
        )
        for engine in self._engines:
            self.combo_engine.addItem(f"{engine.icon} {engine.name}".strip(), engine.id)
        self.combo_engine.currentIndexChanged.connect(self._on_engine_changed)
        eng_row.addWidget(self.combo_engine)

        self.btn_recheck = QPushButton("重新检测")
        self.btn_recheck.setToolTip("重新检测显卡架构与 DLSS5 运行时完整性")
        self.btn_recheck.clicked.connect(self.refresh_availability)
        eng_row.addWidget(self.btn_recheck)

        self.btn_config = QPushButton("引擎配置…")
        self.btn_config.setToolTip("打开「配置 → 高清放大引擎」下载/指定运行时")
        self.btn_config.clicked.connect(
            lambda: self._goto_settings(SETTINGS_MENU_ROW)
        )
        eng_row.addWidget(self.btn_config)
        eng_row.addStretch()
        root.addLayout(eng_row)

        # ── 各引擎参数页 ──
        self.stack_params = QStackedWidget()
        for engine in self._engines:
            page = self._build_engine_page(engine)
            self._pages[engine.id] = page
            self.stack_params.addWidget(page)
        root.addWidget(self.stack_params)

        # ── 输出行 ──
        out_row = QHBoxLayout()
        out_row.setSpacing(8)
        out_row.addWidget(QLabel("输出格式:"))
        self.combo_output = QComboBox()
        self.combo_output.setStyleSheet(config.COMBOBOX_STYLE)
        self.combo_output.setMinimumWidth(150)
        for text, value in OUTPUT_FORMATS:
            self.combo_output.addItem(text, value)
        self.combo_output.setToolTip(
            "放大结果建议用 PNG 无损保存；选 JPG/WEBP 时可设置质量"
        )
        self.combo_output.currentIndexChanged.connect(self._on_output_changed)
        out_row.addWidget(self.combo_output)

        self.lbl_quality = QLabel("质量:")
        out_row.addWidget(self.lbl_quality)
        self.spin_quality = QSpinBox()
        self.spin_quality.setRange(1, 100)
        self.spin_quality.setValue(95)
        self.spin_quality.setFixedWidth(80)
        self.spin_quality.setToolTip("JPG / WEBP 输出质量 1~100（PNG 无损，此项忽略）")
        out_row.addWidget(self.spin_quality)

        self.btn_plan = QPushButton("预计输出")
        self.btn_plan.setToolTip("按当前选中图片尺寸与参数，计算预计输出尺寸与放大趟数")
        self.btn_plan.clicked.connect(self._refresh_plan_hint)
        out_row.addWidget(self.btn_plan)
        out_row.addStretch()
        root.addLayout(out_row)

        self.lbl_plan = QLabel("")
        self.lbl_plan.setWordWrap(True)
        self.lbl_plan.setStyleSheet("color:#a8b0d0;font-size:11px;")
        root.addWidget(self.lbl_plan)

        self._wire_live_hints()
        self._on_output_changed(0)
        root.addStretch()

    def _build_engine_page(self, engine: UpscaleEngineInfo) -> QWidget:
        """按 ParamSpec schema 动态生成一个引擎的参数页。"""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        inputs: dict[str, QWidget] = {}
        self._inputs[engine.id] = inputs

        for group_name, _specs in engine.groups(advanced=False):
            lay.addWidget(self._build_group(engine, group_name, inputs, advanced=False))

        # 高级选项（默认折叠）
        advanced_groups = engine.groups(advanced=True)
        if advanced_groups:
            self_btn = QPushButton("显示高级选项 ▾")
            self_btn.setCheckable(True)
            self_btn.setToolTip("展开该引擎的高级/实验性参数")
            lay.addWidget(self_btn, 0, Qt.AlignLeft)

            container = QWidget()
            c_lay = QVBoxLayout(container)
            c_lay.setContentsMargins(0, 0, 0, 0)
            c_lay.setSpacing(8)
            for group_name, _specs in advanced_groups:
                c_lay.addWidget(
                    self._build_group(engine, group_name, inputs, advanced=True)
                )
            container.setVisible(False)
            lay.addWidget(container)

            def _toggle(checked: bool, _c=container, _b=self_btn):
                _c.setVisible(checked)
                _b.setText("隐藏高级选项 ▴" if checked else "显示高级选项 ▾")

            self_btn.toggled.connect(_toggle)

        if engine.notes:
            note = QLabel(engine.notes)
            note.setWordWrap(True)
            note.setStyleSheet(
                "color:#8890b0;font-size:11px;font-style:italic;"
                "padding:4px 6px;background-color:rgba(38,38,62,90);border-radius:6px;"
            )
            lay.addWidget(note)
        return page

    def _build_group(
        self,
        engine: UpscaleEngineInfo,
        group_name: str,
        inputs: dict[str, QWidget],
        *,
        advanced: bool,
    ) -> QGroupBox:
        box = QGroupBox(group_name)
        box_lay = QVBoxLayout(box)
        box_lay.setSpacing(6)
        for specs in engine.rows(group_name, advanced=advanced):
            row = QHBoxLayout()
            row.setSpacing(8)
            for spec in specs:
                row.addWidget(QLabel(f"{spec.label}:"))
                widget = self._make_input(spec)
                inputs[spec.key] = widget
                row.addWidget(widget)
            row.addStretch()
            box_lay.addLayout(row)
        return box

    def _make_input(self, spec: ParamSpec) -> QWidget:
        """按 ParamSpec.kind 创建输入控件。"""
        if spec.kind == KIND_BOOL:
            w = QCheckBox()
            w.setChecked(bool(spec.default))
        elif spec.kind == KIND_INT:
            w = QSpinBox()
            w.setRange(int(spec.lo), int(spec.hi))
            w.setSingleStep(int(spec.step) or 1)
            w.setValue(int(spec.clamp(spec.default)))
            if spec.suffix:
                w.setSuffix(spec.suffix)
            if spec.min_width:
                w.setMinimumWidth(spec.min_width)
        elif spec.kind == KIND_FLOAT:
            w = QDoubleSpinBox()
            w.setRange(float(spec.lo), float(spec.hi))
            w.setSingleStep(float(spec.step) or 0.05)
            w.setDecimals(int(spec.decimals))
            w.setValue(float(spec.clamp(spec.default)))
            if spec.suffix:
                w.setSuffix(spec.suffix)
            if spec.min_width:
                w.setMinimumWidth(spec.min_width)
        else:  # combo
            w = QComboBox()
            w.setStyleSheet(config.COMBOBOX_STYLE)
            for text, value in spec.choices:
                w.addItem(text, value)
            idx = w.findData(spec.clamp(spec.default))
            w.setCurrentIndex(idx if idx >= 0 else 0)
            if spec.min_width:
                w.setMinimumWidth(spec.min_width)
            if spec.editable:
                w.setEditable(True)
        if spec.tooltip:
            w.setToolTip(spec.tooltip)
        return w

    def _wire_live_hints(self) -> None:
        """全部控件建好后再连线，避免构造期 setValue 触发回调访问未建好的控件。"""
        for engine in self._engines:
            inputs = self._inputs.get(engine.id, {})
            for spec in engine.params:
                widget = inputs.get(spec.key)
                if widget is None:
                    continue
                if spec.kind == KIND_BOOL:
                    widget.toggled.connect(lambda _c: self._refresh_plan_hint())
                elif spec.kind in (KIND_INT, KIND_FLOAT):
                    widget.valueChanged.connect(lambda _v: self._refresh_plan_hint())
                else:
                    widget.currentIndexChanged.connect(lambda _i: self._refresh_plan_hint())

    # ══════════════════════════════════════════════════════════
    #  取值 / 赋值
    # ══════════════════════════════════════════════════════════

    def _read_widget(self, spec: ParamSpec, widget: QWidget) -> Any:
        if spec.kind == KIND_BOOL:
            return bool(widget.isChecked())
        if spec.kind == KIND_INT:
            return int(widget.value())
        if spec.kind == KIND_FLOAT:
            return round(float(widget.value()), int(spec.decimals))
        data = widget.currentData()
        if spec.editable:
            text = widget.currentText().strip()
            # 可编辑下拉：优先匹配已有项，否则保留原始文本
            idx = widget.findText(text)
            return widget.itemData(idx) if idx >= 0 else text
        return data

    def _write_widget(self, spec: ParamSpec, widget: QWidget, value: Any) -> None:
        value = spec.clamp(value)
        widget.blockSignals(True)
        try:
            if spec.kind == KIND_BOOL:
                widget.setChecked(bool(value))
            elif spec.kind == KIND_INT:
                widget.setValue(int(value))
            elif spec.kind == KIND_FLOAT:
                widget.setValue(float(value))
            else:
                idx = widget.findData(value)
                if idx < 0 and spec.editable:
                    widget.setCurrentText(str(value))
                else:
                    widget.setCurrentIndex(idx if idx >= 0 else 0)
        finally:
            widget.blockSignals(False)

    def _collect_engine(self, engine: UpscaleEngineInfo) -> dict[str, Any]:
        inputs = self._inputs.get(engine.id, {})
        return {
            spec.key: self._read_widget(spec, inputs[spec.key])
            for spec in engine.params if spec.key in inputs
        }

    def _apply_engine(self, engine: UpscaleEngineInfo, values: dict | None) -> None:
        inputs = self._inputs.get(engine.id, {})
        data = dict(values or {})
        for spec in engine.params:
            widget = inputs.get(spec.key)
            if widget is None:
                continue
            self._write_widget(spec, widget, data.get(spec.key, spec.default))

    # ══════════════════════════════════════════════════════════
    #  门禁 / 状态
    # ══════════════════════════════════════════════════════════

    def current_engine_id(self) -> str:
        return str(self.combo_engine.currentData() or (self._engines[0].id if self._engines else ""))

    def refresh_availability(self, *, force: bool = True) -> None:
        """后台重新检测各引擎可用性。"""
        if force:
            clear_gate_cache()
        if self._gate_worker is not None and self._gate_worker.isRunning():
            return
        self.btn_recheck.setEnabled(False)
        self.lbl_banner.setText("正在检测硬件与运行时…")
        self.lbl_banner.setStyleSheet(_BANNER_STYLE[LEVEL_WARN])
        self._gate_worker = _GateWorker([e.id for e in self._engines], force=force)
        self._gate_worker.done.connect(self._on_gate_done)
        self._gate_worker.start()

    def _on_gate_done(self, result: dict) -> None:
        self.btn_recheck.setEnabled(True)
        self._gate_checked = True
        for eid, value in dict(result or {}).items():
            if isinstance(value, EngineAvailability):
                self._availability[eid] = value
        self._refresh_engine_combo_labels()
        self._refresh_banner()
        self._refresh_plan_hint()

    def _refresh_engine_combo_labels(self) -> None:
        """把可用性写进下拉项文本与 tooltip。

        不禁用条目：即使引擎不可用也要让用户能选中它、看到红色横幅里的具体原因，
        否则只有一个引擎时下拉会变成空白。真正的拦截在处理前的 preflight。
        """
        current = self.current_engine_id()
        self.combo_engine.blockSignals(True)
        try:
            for i in range(self.combo_engine.count()):
                eid = self.combo_engine.itemData(i)
                engine = next((e for e in self._engines if e.id == eid), None)
                if engine is None:
                    continue
                av = self._availability.get(str(eid))
                self.combo_engine.setItemText(i, engine_choice_label(engine, av))
                tip = engine.description
                if av is not None and not av.usable:
                    tip = (tip + "\n\n" if tip else "") + "\n".join(av.reasons)
                self.combo_engine.setItemData(i, tip, Qt.ToolTipRole)
        finally:
            self.combo_engine.blockSignals(False)
        idx = self.combo_engine.findData(current)
        if idx >= 0:
            self.combo_engine.setCurrentIndex(idx)

    def _refresh_banner(self) -> None:
        eid = self.current_engine_id()
        engine = next((e for e in self._engines if e.id == eid), None)
        av = self._availability.get(eid)
        if engine is None:
            return
        if av is None:
            self.lbl_banner.setText("尚未检测。点「重新检测」确认显卡与运行时是否满足要求。")
            self.lbl_banner.setStyleSheet(_BANNER_STYLE[LEVEL_WARN])
            return

        if av.level == LEVEL_BLOCKED:
            text = f"<b>{engine.name} 当前不可用</b><br>" + "<br>".join(
                _escape(r) for r in av.reasons
            )
            text += (
                '<br><a href="pixelflow://settings/upscale">前往「配置 → 高清放大引擎」处理 →</a>'
            )
            self.lbl_banner.setText(text)
            self.lbl_banner.setStyleSheet(_BANNER_STYLE[LEVEL_BLOCKED])
        elif av.level == LEVEL_WARN:
            text = f"<b>{engine.name} 可用，但有注意事项</b><br>" + "<br>".join(
                _escape(w) for w in av.warnings
            )
            text += (
                '<br><a href="pixelflow://settings/upscale">查看引擎配置 →</a>'
            )
            self.lbl_banner.setText(text)
            self.lbl_banner.setStyleSheet(_BANNER_STYLE[LEVEL_WARN])
        else:
            bits = [_escape(d) for d in av.details]
            self.lbl_banner.setText(
                f"<b>{engine.name} 就绪</b>" + ("<br>" + "<br>".join(bits) if bits else "")
            )
            self.lbl_banner.setStyleSheet(_BANNER_STYLE[LEVEL_READY])

    def _on_link(self, link: str) -> None:
        link = (link or "").strip()
        if link == "pixelflow://settings/upscale":
            self._goto_settings(SETTINGS_MENU_ROW)
        elif link.startswith("http://") or link.startswith("https://"):
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl(link))

    def _find_main_window(self):
        w = self
        while w is not None:
            if hasattr(w, "open_settings"):
                return w
            w = w.parentWidget() if hasattr(w, "parentWidget") else None
        return None

    def _goto_settings(self, row: int) -> None:
        self.open_settings_requested.emit(int(row))
        mw = self._find_main_window()
        if mw is not None:
            mw.open_settings(int(row))

    # ══════════════════════════════════════════════════════════
    #  联动
    # ══════════════════════════════════════════════════════════

    def _on_engine_changed(self, index: int) -> None:
        eid = self.combo_engine.itemData(index)
        page = self._pages.get(str(eid))
        if page is not None:
            self.stack_params.setCurrentWidget(page)
        self._refresh_banner()
        self._refresh_plan_hint()

    def _on_output_changed(self, _index: int) -> None:
        fmt = str(self.combo_output.currentData() or DEFAULT_OUTPUT_FORMAT).lower()
        lossy = fmt in _LOSSY_FORMATS
        self.lbl_quality.setVisible(lossy)
        self.spin_quality.setVisible(lossy)

    def set_base_image_size(self, w: int, h: int) -> None:
        """主窗口同步当前选中图片尺寸，用于实时显示预计输出。"""
        if w > 0 and h > 0:
            self._base_w = int(w)
            self._base_h = int(h)
            self._refresh_plan_hint()

    def _refresh_plan_hint(self) -> None:
        if getattr(self, "lbl_plan", None) is None:
            return
        if self._base_w <= 0 or self._base_h <= 0:
            self.lbl_plan.setText("选中左侧图片后可查看预计输出尺寸与放大趟数。")
            return
        eid = self.current_engine_id()
        engine = next((e for e in self._engines if e.id == eid), None)
        if engine is None or eid != "dlss5":
            self.lbl_plan.setText("")
            return
        try:
            from core.upscale.dlss5 import describe_plan_text
            params = self._collect_engine(engine)
            self.lbl_plan.setText(describe_plan_text(self._base_w, self._base_h, params))
        except Exception as e:      # noqa: BLE001 - 提示区不应抛错
            self.lbl_plan.setText(f"⚠ {e}")

    def showEvent(self, event):
        super().showEvent(event)
        # 首次切到本面板时才做检测，避免拖慢启动
        if not self._gate_checked:
            self.refresh_availability(force=False)

    # ══════════════════════════════════════════════════════════
    #  FeatureRoute 契约
    # ══════════════════════════════════════════════════════════

    def collect_raw_state(self) -> dict[str, Any]:
        return {
            "engine": self.current_engine_id(),
            "engines": {e.id: self._collect_engine(e) for e in self._engines},
            "output_format": str(self.combo_output.currentData() or DEFAULT_OUTPUT_FORMAT),
            "quality": int(self.spin_quality.value()),
        }

    def apply_state(self, state: dict | None) -> None:
        from services.features.upscale_service import active_engine_id, active_params
        data = dict(state or {})
        eid = active_engine_id(data)

        idx = self.combo_engine.findData(eid)
        if idx >= 0:
            self.combo_engine.blockSignals(True)
            self.combo_engine.setCurrentIndex(idx)
            self.combo_engine.blockSignals(False)
        page = self._pages.get(eid)
        if page is not None:
            self.stack_params.setCurrentWidget(page)

        engines = data.get("engines")
        for engine in self._engines:
            values = None
            if isinstance(engines, dict) and isinstance(engines.get(engine.id), dict):
                values = engines[engine.id]
            elif engine.id == eid:
                values = active_params(data)
            self._apply_engine(engine, values)

        fmt = str(data.get("output_format", DEFAULT_OUTPUT_FORMAT) or DEFAULT_OUTPUT_FORMAT).lower()
        f_idx = self.combo_output.findData(fmt)
        self.combo_output.setCurrentIndex(f_idx if f_idx >= 0 else 0)
        try:
            self.spin_quality.setValue(max(1, min(100, int(data.get("quality", 95)))))
        except (TypeError, ValueError):
            self.spin_quality.setValue(95)
        self._on_output_changed(0)
        self._refresh_banner()
        self._refresh_plan_hint()

    def show_validation_errors(self, errors) -> None:
        from PySide6.QtWidgets import QMessageBox
        msgs = list(errors or [])
        if not msgs:
            return
        QMessageBox.warning(self, "参数错误", "\n".join(str(e) for e in msgs))


def _escape(text: str) -> str:
    """富文本转义（横幅用 QLabel rich text）。"""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br>")
    )
