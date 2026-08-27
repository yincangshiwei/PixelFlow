"""TransparentFeatureRoute —— 透明图处理参数面板（只读写控件）。"""
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QLabel, QSpinBox, QComboBox, QPushButton, QColorDialog, QCheckBox, QMessageBox,
)
from PySide6.QtCore import Qt, QUrl, Signal, QRectF
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPen, QBrush, QFont

from core.image_processor import hex_to_rgba, rgba_to_hex, rgba_to_css_hex
from core.matting.model_registry import list_models, get_model_info
from core.matting.model_manager import get_matting_manager
from core.processors.transparent_processor import default_transparent_options
import config


class _SubjectPositionGrid(QWidget):
    """与图片叠加一致的 3×3 位置选择器，保存稳定的位置枚举值。"""

    positionChanged = Signal(str)
    CELLS = [
        ("↖", "top_left"), ("↑", "top_center"), ("↗", "top_right"),
        ("←", "middle_left"), ("⊙", "center"), ("→", "middle_right"),
        ("↙", "bottom_left"), ("↓", "bottom_center"), ("↘", "bottom_right"),
    ]

    def __init__(self, value="center", parent=None):
        super().__init__(parent)
        self._hovered_cell = -1
        self._selected_cell = 4
        self.setFixedSize(90, 90)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("选择主体在扣除四边预留后的可用区内的位置")
        self.setCurrentData(value)

    def currentData(self) -> str:
        return self.CELLS[self._selected_cell][1]

    def setCurrentData(self, value: str) -> None:
        idx = next((i for i, (_, data) in enumerate(self.CELLS) if data == value), 4)
        if idx != self._selected_cell:
            self._selected_cell = idx
            self.update()

    def _cell_at(self, pos) -> int:
        col = int(pos.x() / (self.width() / 3.0))
        row = int(pos.y() / (self.height() / 3.0))
        return row * 3 + col if 0 <= col < 3 and 0 <= row < 3 else -1

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        cell_w, cell_h = self.width() / 3.0, self.height() / 3.0
        painter.fillRect(self.rect(), QColor(22, 22, 40, 180))
        for i, (symbol, _) in enumerate(self.CELLS):
            row, col = divmod(i, 3)
            rect = QRectF(col * cell_w + 1, row * cell_h + 1, cell_w - 2, cell_h - 2)
            if i == self._hovered_cell:
                painter.setBrush(QBrush(QColor(91, 138, 245, 110)))
                painter.setPen(QPen(QColor(91, 138, 245, 210), 1.5))
            elif i == self._selected_cell:
                painter.setBrush(QBrush(QColor(91, 138, 245, 150)))
                painter.setPen(QPen(QColor(124, 108, 245, 230), 2))
            else:
                painter.setBrush(QBrush(QColor(38, 38, 62, 120)))
                painter.setPen(QPen(QColor(100, 110, 170, 60), 1))
            painter.drawRoundedRect(rect, 4, 4)
            painter.setPen(QColor(225, 230, 248) if i == self._selected_cell else QColor(200, 205, 230))
            painter.setFont(QFont("Segoe UI Symbol", 11))
            painter.drawText(rect, Qt.AlignCenter, symbol)
        painter.end()

    def mouseMoveEvent(self, event):
        idx = self._cell_at(event.position().toPoint())
        if idx != self._hovered_cell:
            self._hovered_cell = idx
            self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            idx = self._cell_at(event.position().toPoint())
            if idx >= 0 and idx != self._selected_cell:
                self._selected_cell = idx
                self.update()
                self.positionChanged.emit(self.currentData())

    def leaveEvent(self, event):
        self._hovered_cell = -1
        self.update()


class _ColorBlock(QWidget):
    """颜色选择：色块 + 色值标签。存储 #RRGGBB / #RRGGBBAA。"""

    def __init__(self, color="#FFFFFF", parent=None):
        super().__init__(parent)
        self._color = rgba_to_hex(color)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self._btn = QPushButton()
        self._btn.setFixedSize(36, 26)
        self._btn.setCursor(Qt.PointingHandCursor)
        self._btn.setToolTip("点击选择画布颜色（支持透明）")
        self._btn.clicked.connect(self._pick)
        lay.addWidget(self._btn)
        self._lbl = QLabel(self._color)
        self._lbl.setStyleSheet("color:#b0b8c8;font-size:12px;background:transparent;")
        lay.addWidget(self._lbl)
        self._refresh()

    @staticmethod
    def _to_qcolor(color_str: str) -> QColor:
        r, g, b, a = hex_to_rgba(color_str)
        return QColor(r, g, b, a)

    def _pick(self):
        r, g, b, a = hex_to_rgba(self._color)
        init = QColor(r, g, b, 255 if a <= 0 else a)
        dlg = QColorDialog(init, self)
        dlg.setWindowTitle("选择画布颜色")
        dlg.setOption(QColorDialog.ShowAlphaChannel, True)
        dlg.setOption(QColorDialog.DontUseNativeDialog, True)
        dlg.setCustomColor(0, QColor(255, 255, 255, 255).rgba())
        dlg.setCustomColor(1, QColor(0, 0, 0, 255).rgba())
        dlg.setCustomColor(2, QColor(0, 0, 0, 0).rgba())
        if dlg.exec() != QColorDialog.Accepted:
            return
        c = dlg.currentColor()
        if not c.isValid():
            return
        self._color = rgba_to_hex((c.red(), c.green(), c.blue(), c.alpha()))
        self._refresh()

    def _refresh(self):
        r, g, b, a = hex_to_rgba(self._color)
        if a <= 0:
            bg_css = (
                "background-color: #3a3a4a;"
                "background-image: repeating-linear-gradient("
                "45deg, #2a2a3a 0, #2a2a3a 4px, #4a4a5a 4px, #4a4a5a 8px);"
            )
        else:
            bg_css = f"background-color: {rgba_to_css_hex(self._color)};"
        self._btn.setStyleSheet(
            f"QPushButton{{{bg_css}border:2px solid #5a5a6a;border-radius:6px;"
            f"min-width:36px;min-height:24px;}}"
        )
        self._lbl.setText(self._color)

    def set_color(self, color: str):
        self._color = rgba_to_hex(color)
        self._refresh()

    def get_color(self) -> str:
        return self._color


class TransparentFeatureRoute(QWidget):
    """透明图 FeatureRoute。"""

    feature_id = "transparent_image"
    matting_toggled = Signal(bool)
    open_settings_requested = Signal(int)  # 0=dev, 1=matting

    def __init__(self, parent=None):
        super().__init__(parent)
        self._grp_matting = None
        self._grp_trim = None
        self._grp_layout = None
        self._build_widget()

    def build_widget(self, parent=None) -> QWidget:
        if parent is not None and self.parent() is None:
            self.setParent(parent)
        return self

    @property
    def matting_group(self):
        return self._grp_matting

    def _build_widget(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        # ── AI 抠图（流水线最前）──
        self._grp_matting = QGroupBox("AI 抠图")
        self._grp_matting.setCheckable(True)
        self._grp_matting.setChecked(False)
        self._grp_matting.setToolTip(
            "使用本地 AI 模型去除背景（独立 uv 环境 + 子进程）。\n"
            "请先在「配置 → 开发环境 / 抠图模型配置」安装 uv、创建环境并下载权重。\n"
            "开启后会先于裁剪透明边缘执行。"
        )
        m_lay = QVBoxLayout(self._grp_matting)
        m_lay.setSpacing(6)

        m_row = QHBoxLayout()
        m_row.addWidget(QLabel("模型:"))
        self.combo_matting_model = QComboBox()
        self.combo_matting_model.setStyleSheet(config.COMBOBOX_STYLE)
        self._reload_matting_models()
        self.combo_matting_model.setToolTip(
            "每个模型有独立环境与权重；切换后会重新检查就绪状态。"
            "更多模型可在后续版本扩展。"
        )
        m_row.addWidget(self.combo_matting_model)

        self.chk_refine = QCheckBox("边缘精炼")
        self.chk_refine.setChecked(False)
        self.chk_refine.setToolTip(
            "提升发丝/边缘质量，速度更慢（BEN2 refine_foreground）。"
            "当前模型若不支持会自动禁用。"
        )
        m_row.addWidget(self.chk_refine)
        m_row.addStretch()
        m_lay.addLayout(m_row)

        self.lbl_matting_hint = QLabel("")
        self.lbl_matting_hint.setWordWrap(True)
        self.lbl_matting_hint.setStyleSheet("color:#8a90b0;font-size:11px;")
        self.lbl_matting_hint.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.lbl_matting_hint.setOpenExternalLinks(False)
        self.lbl_matting_hint.linkActivated.connect(self._on_matting_link)
        m_lay.addWidget(self.lbl_matting_hint)
        self.combo_matting_model.currentIndexChanged.connect(self._on_matting_model_changed)
        self._grp_matting.toggled.connect(lambda _: self._update_matting_hint())
        self._grp_matting.toggled.connect(self.matting_toggled.emit)
        self._update_matting_hint()
        root.addWidget(self._grp_matting)

        # ── 裁剪透明边缘 ──
        self._grp_trim = QGroupBox("裁剪透明边缘")
        self._grp_trim.setCheckable(True)
        self._grp_trim.setChecked(True)
        t_lay = QHBoxLayout(self._grp_trim)
        t_lay.addWidget(QLabel("Alpha 阈值:"))
        self.spin_alpha = QSpinBox()
        self.spin_alpha.setRange(0, 254)
        self.spin_alpha.setValue(0)
        self.spin_alpha.setToolTip(
            "大于此值视为有效内容（0=仅裁完全透明像素）。\n"
            "会保留图中所有显著内容（含多物品），并自动忽略四角断裂的短半透明噪点。"
        )
        t_lay.addWidget(self.spin_alpha)
        t_lay.addStretch()
        root.addWidget(self._grp_trim)

        # ── 画布与主体布局 ──
        self._grp_layout = QGroupBox("画布与主体布局")
        self._grp_layout.setCheckable(True)
        self._grp_layout.setChecked(False)
        layout_lay = QVBoxLayout(self._grp_layout)
        layout_lay.setSpacing(6)

        canvas_row = QHBoxLayout()
        canvas_row.addWidget(QLabel("画布宽:"))
        self.spin_cw = QSpinBox()
        self.spin_cw.setRange(1, 99999)
        self.spin_cw.setValue(1500)
        self.spin_cw.setSuffix(" px")
        canvas_row.addWidget(self.spin_cw)
        canvas_row.addWidget(QLabel("画布高:"))
        self.spin_ch = QSpinBox()
        self.spin_ch.setRange(1, 99999)
        self.spin_ch.setValue(1500)
        self.spin_ch.setSuffix(" px")
        canvas_row.addWidget(self.spin_ch)
        canvas_row.addWidget(QLabel("背景:"))
        self.color_btn = _ColorBlock("#FFFFFF")
        canvas_row.addWidget(self.color_btn)
        canvas_row.addStretch()
        layout_lay.addLayout(canvas_row)

        reserve_row = QHBoxLayout()
        self._reserve_spins = {}
        for key, label in (
            ("left", "左预留:"), ("right", "右预留:"),
            ("top", "上预留:"), ("bottom", "下预留:"),
        ):
            reserve_row.addWidget(QLabel(label))
            spin = QSpinBox()
            spin.setRange(0, 99)
            spin.setValue(0)
            spin.setSuffix(" %")
            spin.setToolTip("按整张画布对应方向尺寸的百分比预留")
            self._reserve_spins[key] = spin
            reserve_row.addWidget(spin)
        reserve_row.addStretch()
        layout_lay.addLayout(reserve_row)

        subject_row = QHBoxLayout()
        subject_row.addWidget(QLabel("主体大小:"))
        self.spin_subject_percent = QSpinBox()
        self.spin_subject_percent.setRange(1, 100)
        self.spin_subject_percent.setValue(80)
        self.spin_subject_percent.setSuffix(" %")
        self.spin_subject_percent.setToolTip(
            "主体等比完整放入可用区对应比例的安全框；可用区已扣除四边预留。"
        )
        subject_row.addWidget(self.spin_subject_percent)
        subject_row.addWidget(QLabel("主体位置:"))
        self.position_grid = _SubjectPositionGrid("center")
        subject_row.addWidget(self.position_grid, 0, Qt.AlignVCenter)
        subject_row.addWidget(QLabel("细节:"))
        self.combo_detail = QComboBox()
        self.combo_detail.setStyleSheet(config.COMBOBOX_STYLE)
        self.combo_detail.addItem("标准", "normal")
        self.combo_detail.addItem("弱恢复", "subtle")
        self.combo_detail.addItem("关闭", "off")
        self.combo_detail.setCurrentIndex(0)
        self.combo_detail.setToolTip(
            "缩小时对颜色通道做轻度锐化以补偿重采样发软；不影响透明边缘。"
        )
        subject_row.addWidget(self.combo_detail)
        subject_row.addStretch()
        layout_lay.addLayout(subject_row)

        self.lbl_layout_hint = QLabel(
            "先从画布扣除四边预留，再按主体大小缩放，并在剩余可用区内对齐。"
            "左右预留之和、上下预留之和必须分别小于 100%。"
        )
        self.lbl_layout_hint.setWordWrap(True)
        self.lbl_layout_hint.setStyleSheet("color:#8a90b0;font-size:11px;")
        layout_lay.addWidget(self.lbl_layout_hint)
        root.addWidget(self._grp_layout)

        # ── 输出格式 ──
        fmt_row = QHBoxLayout()
        fmt_row.addWidget(QLabel("输出格式:"))
        self.combo_fmt = QComboBox()
        self.combo_fmt.addItems(["png", "webp", "jpg"])
        self.combo_fmt.setStyleSheet(config.COMBOBOX_STYLE)
        fmt_row.addWidget(self.combo_fmt)
        fmt_row.addStretch()
        root.addLayout(fmt_row)

        root.addStretch()
        return

    def _reload_matting_models(self):
        if not hasattr(self, "combo_matting_model"):
            return
        cur = self.combo_matting_model.currentData()
        self.combo_matting_model.blockSignals(True)
        self.combo_matting_model.clear()
        for m in list_models():
            self.combo_matting_model.addItem(m.name, m.id)
        # 默认选中管理器中的默认模型
        prefer = cur or get_matting_manager().get_default_model_id()
        for i in range(self.combo_matting_model.count()):
            if self.combo_matting_model.itemData(i) == prefer:
                self.combo_matting_model.setCurrentIndex(i)
                break
        self.combo_matting_model.blockSignals(False)
        self._sync_refine_for_model(self.combo_matting_model.currentData())

    def on_panel_activated(self):
        """主窗口切回图像处理 Tab 时刷新模型列表与就绪提示。"""
        self._reload_matting_models()
        self._update_matting_hint()

    def _on_matting_model_changed(self, _idx: int = 0):
        """切换模型时同步默认模型，并按该模型独立检查环境/权重。"""
        mid = self.combo_matting_model.currentData()
        if mid:
            get_matting_manager().set_default_model_id(mid)
        self._sync_refine_for_model(mid)
        self._update_matting_hint()

    def _sync_refine_for_model(self, model_id: str | None):
        """按模型是否支持边缘精炼启用/禁用勾选框。"""
        if not hasattr(self, "chk_refine"):
            return
        meta = get_model_info(model_id) if model_id else None
        supports = True
        if meta is not None:
            supports = bool(meta.extra.get("supports_refine", True))
        self.chk_refine.setEnabled(supports)
        if not supports:
            self.chk_refine.setChecked(False)
            self.chk_refine.setToolTip(
                f"{meta.name if meta else '当前模型'} 不提供边缘精炼选项"
            )
        else:
            self.chk_refine.setToolTip(
                "提升发丝/边缘质量，速度更慢（如 BEN2 refine_foreground）"
            )

    def _find_main_window(self):
        w = self
        while w is not None:
            if hasattr(w, "open_settings"):
                return w
            w = w.parentWidget() if hasattr(w, "parentWidget") else None
        return None

    def _on_matting_link(self, link: str):
        """处理提示区中的配置跳转链接。"""
        link = (link or "").strip()
        if link == "pixelflow://settings/dev":
            self.open_settings_requested.emit(0)
            mw = self._find_main_window()
            if mw is not None:
                mw.open_settings(0)
            return
        if link == "pixelflow://settings/matting":
            self.open_settings_requested.emit(1)
            mw = self._find_main_window()
            if mw is not None:
                mw.open_settings(1)
            return
        if link.startswith("http://") or link.startswith("https://"):
            QDesktopServices.openUrl(QUrl(link))

    def _matting_readiness(self, model_id: str) -> dict:
        """
        轻量检查当前模型是否可推理（不启动子进程 import）。
        返回: dev_ok / env_ok / weight_ok / detail 文案组件
        """
        from core.runtime.env_manager import get_runtime_manager

        rt = get_runtime_manager()
        mgr = get_matting_manager()
        uv = rt.resolve_uv()
        base = rt.resolve_base_python()
        dev_ok = bool(uv.found and base is not None)

        env_exists = rt.env_exists(model_id)
        cached = rt.get_model_env_status(model_id, quick=True)
        env_ready = bool(cached.ready)
        env_ok = env_ready or env_exists  # 已创建即可尝试；完整依赖处理时再验
        weight_ok = mgr.is_ready(model_id)
        status = mgr.status_text(model_id)

        if not dev_ok:
            missing = []
            if base is None:
                missing.append("Python")
            if not uv.found:
                missing.append("uv")
            stage = "dev"
            summary = "开发环境未配置（缺少 " + "、".join(missing) + "）"
        elif not env_exists:
            stage = "env"
            summary = "模型隔离环境未创建"
        elif not env_ready:
            stage = "env"
            miss = ", ".join(cached.missing_packages) if cached.missing_packages else ""
            summary = f"模型环境不完整" + (f"（缺 {miss}）" if miss else "")
        elif not weight_ok:
            stage = "weight"
            summary = "模型权重未下载"
        else:
            stage = "ready"
            summary = "已就绪"

        return {
            "dev_ok": dev_ok,
            "env_ok": env_ok and env_exists,
            "env_ready": env_ready,
            "env_exists": env_exists,
            "weight_ok": weight_ok,
            "status": status,
            "stage": stage,
            "summary": summary,
        }

    def _update_matting_hint(self):
        if not hasattr(self, "lbl_matting_hint"):
            return
        mid = self.combo_matting_model.currentData() or "ben2"
        meta = get_model_info(mid)
        if not meta:
            self.lbl_matting_hint.setText("")
            return

        info = self._matting_readiness(mid)
        name = meta.name
        status = info["status"]
        stage = info["stage"]

        # 状态色
        if stage == "ready":
            color = "#6dcea0"
        elif stage == "dev":
            color = "#e0a060"
        else:
            color = "#e0c060"

        # 可点击跳转
        link_dev = '<a href="pixelflow://settings/dev" style="color:#5b8af5;text-decoration:none;">前往开发环境配置</a>'
        link_model = '<a href="pixelflow://settings/matting" style="color:#5b8af5;text-decoration:none;">前往抠图模型配置</a>'

        if stage == "ready":
            body = (
                f"{name} · {status} · 环境就绪"
                f"  · 勾选后处理时将调用该模型独立环境推理"
            )
            extra = ""
        elif stage == "dev":
            body = f"{name} · {info['summary']}"
            extra = f"  — {link_dev}"
        elif stage == "env":
            body = f"{name} · {status} · {info['summary']}"
            extra = f"  — {link_model}"
        else:  # weight
            body = f"{name} · {status} · 环境已创建"
            extra = f"  — 请下载权重：{link_model}"

        # 未勾选 AI 抠图时也显示状态，便于用户提前配置
        html = (
            f'<span style="color:{color};font-size:11px;">{body}</span>'
            f'<span style="color:#8a90b0;font-size:11px;">{extra}</span>'
        )
        self.lbl_matting_hint.setText(html)

    def collect_raw_state(self) -> dict[str, Any]:
        mid = self.combo_matting_model.currentData() or "ben2"
        meta = get_model_info(mid)
        supports_refine = True if meta is None else bool(
            meta.extra.get("supports_refine", True)
        )
        return {
            "enable_matting": self._grp_matting.isChecked(),
            "matting_model": mid,
            "matting_refine": bool(self.chk_refine.isChecked()) and supports_refine,
            # keep_matting 由主窗口输出设置注入（仅 AI 抠图开启时可见）
            "keep_matting": False,
            "enable_trim": self._grp_trim.isChecked(),
            "alpha_threshold": self.spin_alpha.value(),
            "enable_layout": self._grp_layout.isChecked(),
            "canvas_w": self.spin_cw.value(),
            "canvas_h": self.spin_ch.value(),
            "canvas_color": self.color_btn.get_color(),
            "reserve_left_percent": self._reserve_spins["left"].value(),
            "reserve_right_percent": self._reserve_spins["right"].value(),
            "reserve_top_percent": self._reserve_spins["top"].value(),
            "reserve_bottom_percent": self._reserve_spins["bottom"].value(),
            "subject_percent": self.spin_subject_percent.value(),
            "subject_position": self.position_grid.currentData() or "center",
            "detail_restore": self.combo_detail.currentData() or "normal",
            "output_format": self.combo_fmt.currentText(),
        }

    def apply_state(self, options: dict | None):
        options = dict(options or default_transparent_options())
        if self._grp_trim is None:
            return
        self._reload_matting_models()
        self._grp_matting.setChecked(options.get("enable_matting", False))
        mid = options.get("matting_model", "ben2")
        for i in range(self.combo_matting_model.count()):
            if self.combo_matting_model.itemData(i) == mid:
                self.combo_matting_model.setCurrentIndex(i)
                break
        self._sync_refine_for_model(self.combo_matting_model.currentData())
        if self.chk_refine.isEnabled():
            self.chk_refine.setChecked(options.get("matting_refine", False))
        else:
            self.chk_refine.setChecked(False)
        self._update_matting_hint()

        self._grp_trim.setChecked(options.get("enable_trim", True))
        self.spin_alpha.setValue(options.get("alpha_threshold", 0))

        # 兼容旧预设：原来启用画布时迁移到新布局；根据旧目标框估算占比。
        layout_enabled = options.get("enable_layout")
        if layout_enabled is None:
            layout_enabled = bool(options.get("enable_canvas", False))
        canvas_w = int(options.get("canvas_w", 1500))
        canvas_h = int(options.get("canvas_h", 1500))
        subject_percent = options.get("subject_percent")
        if subject_percent is None and options.get("enable_resize"):
            resize_w = max(1, int(options.get("resize_w", 800)))
            resize_h = max(1, int(options.get("resize_h", 800)))
            subject_percent = round(
                min(resize_w / max(1, canvas_w), resize_h / max(1, canvas_h)) * 100
            )
        self._grp_layout.setChecked(bool(layout_enabled))
        self.spin_cw.setValue(canvas_w)
        self.spin_ch.setValue(canvas_h)
        for key in ("left", "right", "top", "bottom"):
            value = int(options.get(f"reserve_{key}_percent", 0) or 0)
            self._reserve_spins[key].setValue(max(0, min(99, value)))
        self.spin_subject_percent.setValue(max(1, min(100, int(subject_percent or 80))))
        position = options.get("subject_position", "center")
        self.position_grid.setCurrentData(position)
        detail = options.get("detail_restore", "normal")
        for i in range(self.combo_detail.count()):
            if self.combo_detail.itemData(i) == detail:
                self.combo_detail.setCurrentIndex(i)
                break
        else:
            self.combo_detail.setCurrentIndex(0)
        color = options.get("canvas_color", "#FFFFFF")
        self.color_btn.set_color(color)
        fmt = options.get("output_format", "png")
        fmt_idx = ["png", "webp", "jpg"].index(fmt) if fmt in ["png", "webp", "jpg"] else 0
        self.combo_fmt.setCurrentIndex(fmt_idx)


    def show_validation_errors(self, errors) -> None:
        msgs = list(errors or [])
        if not msgs:
            return
        QMessageBox.warning(self, "参数错误", "\n".join(str(e) for e in msgs))
