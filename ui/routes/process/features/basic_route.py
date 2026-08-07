"""BasicFeatureRoute —— 基础处理参数面板（只读写控件，业务在 BasicService）。"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QLabel, QSpinBox, QComboBox, QSlider, QMessageBox,
)

import config
from services.features.basic_service import default_basic_options

_DPI_PRESETS = (72, 96, 150, 300, 600)


class BasicFeatureRoute(QWidget):
    """基础处理 FeatureRoute：collect_raw_state / apply_state。"""

    feature_id = "basic_process"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._grp_compress = None
        self._grp_format = None
        self._grp_dpi = None
        self._grp_rename = None
        self._build_widget()

    def build_widget(self, parent=None) -> QWidget:
        """契约接口：返回自身（已是面板）。"""
        if parent is not None and self.parent() is None:
            self.setParent(parent)
        return self

    def _build_widget(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        # ── 格式转换 ──
        self._grp_format = QGroupBox("格式转换")
        self._grp_format.setCheckable(True)
        self._grp_format.setChecked(False)
        f_lay = QHBoxLayout(self._grp_format)
        f_lay.setSpacing(8)
        f_lay.addWidget(QLabel("输出格式:"))
        self.combo_fmt = QComboBox()
        self.combo_fmt.addItems(["PNG", "JPG", "BMP", "WEBP"])
        self.combo_fmt.setMinimumWidth(100)
        self.combo_fmt.setToolTip("选择目标图片格式")
        self.combo_fmt.setStyleSheet(config.COMBOBOX_STYLE)
        f_lay.addWidget(self.combo_fmt)
        f_lay.addStretch()
        root.addWidget(self._grp_format)

        # ── 图片压缩 ──
        self._grp_compress = QGroupBox("图片压缩")
        self._grp_compress.setCheckable(True)
        self._grp_compress.setChecked(False)
        c_lay = QHBoxLayout(self._grp_compress)
        c_lay.setSpacing(12)

        c_lay.addWidget(QLabel("模式:"))
        self.combo_compress_mode = QComboBox()
        self.combo_compress_mode.addItem("按质量压缩", "quality")
        self.combo_compress_mode.addItem("按目标大小压缩", "size")
        self.combo_compress_mode.setMinimumWidth(130)
        self.combo_compress_mode.setStyleSheet(config.COMBOBOX_STYLE)
        c_lay.addWidget(self.combo_compress_mode)

        self.widget_quality = QWidget()
        q_lay = QHBoxLayout(self.widget_quality)
        q_lay.setContentsMargins(0, 0, 0, 0)
        q_lay.setSpacing(8)
        q_lay.addWidget(QLabel("质量:"))
        self.slider_quality = QSlider(Qt.Horizontal)
        self.slider_quality.setRange(1, 100)
        self.slider_quality.setValue(85)
        self.slider_quality.setFixedWidth(160)
        self.slider_quality.setToolTip("图片质量 1~100，越低文件越小（仅对 JPG / WEBP 有效）")
        q_lay.addWidget(self.slider_quality)
        self.lbl_quality = QLabel("85")
        self.lbl_quality.setFixedWidth(28)
        self.lbl_quality.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        q_lay.addWidget(self.lbl_quality)
        self.slider_quality.valueChanged.connect(
            lambda v: self.lbl_quality.setText(str(v))
        )
        c_lay.addWidget(self.widget_quality)

        self.widget_size = QWidget()
        s_lay = QHBoxLayout(self.widget_size)
        s_lay.setContentsMargins(0, 0, 0, 0)
        s_lay.setSpacing(8)
        s_lay.addWidget(QLabel("目标大小:"))
        self.spin_target_size = QSpinBox()
        self.spin_target_size.setRange(1, 100000)
        self.spin_target_size.setValue(500)
        self.spin_target_size.setToolTip("目标文件大小 (KB)，仅对 JPG / WEBP 格式有效")
        s_lay.addWidget(self.spin_target_size)
        s_lay.addWidget(QLabel("KB"))
        c_lay.addWidget(self.widget_size)

        c_lay.addStretch()
        root.addWidget(self._grp_compress)

        self.combo_compress_mode.currentIndexChanged.connect(
            lambda _: self._sync_compress_format()
        )
        self._grp_compress.toggled.connect(lambda _: self._sync_compress_format())
        self._grp_format.toggled.connect(self._on_format_toggled)
        self._sync_compress_format()

        # ── 修改 DPI ──
        self._grp_dpi = QGroupBox("修改 DPI")
        self._grp_dpi.setCheckable(True)
        self._grp_dpi.setChecked(False)
        self._grp_dpi.setToolTip(
            "仅修改图片的 DPI（打印分辨率）元数据，不改变像素宽高与画面内容。\n"
            "支持 PNG / JPG / WEBP / BMP；与压缩、格式转换可同时使用。"
        )
        d_lay = QHBoxLayout(self._grp_dpi)
        d_lay.setSpacing(8)
        d_lay.addWidget(QLabel("目标 DPI:"))
        self.spin_dpi = QSpinBox()
        self.spin_dpi.setRange(1, 2400)
        self.spin_dpi.setValue(300)
        self.spin_dpi.setMinimumWidth(90)
        self.spin_dpi.setToolTip("目标 DPI 值（水平与垂直使用相同 density）")
        d_lay.addWidget(self.spin_dpi)
        d_lay.addWidget(QLabel("常用:"))
        self.combo_dpi_preset = QComboBox()
        self.combo_dpi_preset.setMinimumWidth(100)
        self.combo_dpi_preset.setStyleSheet(config.COMBOBOX_STYLE)
        self.combo_dpi_preset.setToolTip("选择常用 DPI，自动填入左侧数值")
        for v in _DPI_PRESETS:
            self.combo_dpi_preset.addItem(str(v), v)
        idx_300 = self.combo_dpi_preset.findData(300)
        if idx_300 >= 0:
            self.combo_dpi_preset.setCurrentIndex(idx_300)

        def _on_dpi_preset(_idx):
            val = self.combo_dpi_preset.currentData()
            if val is not None:
                self.spin_dpi.blockSignals(True)
                self.spin_dpi.setValue(int(val))
                self.spin_dpi.blockSignals(False)

        def _on_dpi_spin(val: int):
            idx = self.combo_dpi_preset.findData(val)
            if idx >= 0:
                self.combo_dpi_preset.blockSignals(True)
                self.combo_dpi_preset.setCurrentIndex(idx)
                self.combo_dpi_preset.blockSignals(False)

        self.combo_dpi_preset.currentIndexChanged.connect(_on_dpi_preset)
        self.spin_dpi.valueChanged.connect(_on_dpi_spin)
        d_lay.addWidget(self.combo_dpi_preset)
        d_hint = QLabel("仅改元数据，不缩放像素")
        d_hint.setStyleSheet("color:#666e88;font-size:11px;font-style:italic;")
        d_lay.addWidget(d_hint)
        d_lay.addStretch()
        root.addWidget(self._grp_dpi)

        # ── 批量重命名 ──
        self._grp_rename = QGroupBox("批量重命名")
        self._grp_rename.setCheckable(True)
        self._grp_rename.setChecked(False)
        r_lay = QHBoxLayout(self._grp_rename)
        r_lay.setSpacing(8)
        r_lay.addWidget(QLabel("前缀:"))
        self.combo_prefix_mode = QComboBox()
        self.combo_prefix_mode.addItem("自定义前缀", "custom")
        self.combo_prefix_mode.addItem("保留原文件名", "keep")
        self.combo_prefix_mode.setMinimumWidth(130)
        self.combo_prefix_mode.setToolTip("选择重命名前缀模式")
        self.combo_prefix_mode.setStyleSheet(config.COMBOBOX_STYLE)
        r_lay.addWidget(self.combo_prefix_mode)
        self.combo_fmt_prefix = QComboBox()
        self.combo_fmt_prefix.setEditable(True)
        self.combo_fmt_prefix.setMinimumWidth(110)
        self.combo_fmt_prefix.setPlaceholderText("输入前缀...")
        self.combo_fmt_prefix.setToolTip("自定义前缀文本，模式为「保留原文件名」时此项忽略")
        self.combo_fmt_prefix.setStyleSheet(config.COMBOBOX_STYLE)
        r_lay.addWidget(self.combo_fmt_prefix)
        r_lay.addWidget(QLabel("起始序号:"))
        self.spin_start = QSpinBox()
        self.spin_start.setRange(0, 99999)
        self.spin_start.setValue(1)
        self.spin_start.setToolTip("序号从此值开始，依次递增")
        r_lay.addWidget(self.spin_start)
        r_lay.addWidget(QLabel("位数:"))
        self.spin_digits = QSpinBox()
        self.spin_digits.setRange(1, 8)
        self.spin_digits.setValue(3)
        self.spin_digits.setToolTip("序号补零位数，如 3 位 → 001、002…")
        r_lay.addWidget(self.spin_digits)

        def _on_prefix_mode(_idx):
            self.combo_fmt_prefix.setVisible(
                self.combo_prefix_mode.currentData() == "custom"
            )
        self.combo_prefix_mode.currentIndexChanged.connect(_on_prefix_mode)
        _on_prefix_mode(0)

        r_lay.addStretch()
        root.addWidget(self._grp_rename)

        hint = QLabel("提示：未勾选任何步骤时，文件将原样复制到输出目录")
        hint.setStyleSheet("color:#666e88;font-size:11px;font-style:italic;")
        root.addWidget(hint)
        root.addStretch()

    # ── 控件联动（与历史 BasicProcessor 一致）──

    def _sync_compress_format(self):
        compress_on = self._grp_compress.isChecked()
        if compress_on:
            self._grp_format.blockSignals(True)
            self._grp_format.setChecked(True)
            self._grp_format.blockSignals(False)
            current_fmt = self.combo_fmt.currentText()
            self.combo_fmt.blockSignals(True)
            self.combo_fmt.clear()
            self.combo_fmt.addItems(["JPG", "WEBP"])
            self.combo_fmt.blockSignals(False)
            idx = self.combo_fmt.findText(current_fmt)
            self.combo_fmt.setCurrentIndex(idx if idx >= 0 else 0)
        else:
            current_fmt = self.combo_fmt.currentText()
            self.combo_fmt.blockSignals(True)
            self.combo_fmt.clear()
            self.combo_fmt.addItems(["PNG", "JPG", "BMP", "WEBP"])
            self.combo_fmt.blockSignals(False)
            idx = self.combo_fmt.findText(current_fmt)
            self.combo_fmt.setCurrentIndex(idx if idx >= 0 else 0)

        mode = self.combo_compress_mode.currentData()
        self.widget_quality.setVisible(compress_on and mode == "quality")
        self.widget_size.setVisible(compress_on and mode == "size")

    def _on_format_toggled(self, checked: bool):
        if self._grp_compress.isChecked() and not checked:
            self._grp_format.blockSignals(True)
            self._grp_format.setChecked(True)
            self._grp_format.blockSignals(False)

    # ── FeatureRoute 契约 ──

    def collect_raw_state(self) -> dict[str, Any]:
        return {
            "enable_compress": self._grp_compress.isChecked(),
            "compress_mode": self.combo_compress_mode.currentData(),
            "quality": self.slider_quality.value(),
            "target_size_kb": self.spin_target_size.value(),
            "enable_format": self._grp_format.isChecked(),
            "output_format": self.combo_fmt.currentText().lower(),
            "enable_dpi": self._grp_dpi.isChecked(),
            "dpi": self.spin_dpi.value(),
            "enable_rename": self._grp_rename.isChecked(),
            "prefix_mode": self.combo_prefix_mode.currentData(),
            "prefix": self.combo_fmt_prefix.currentText().strip(),
            "start_index": self.spin_start.value(),
            "digits": self.spin_digits.value(),
        }

    def apply_state(self, state: dict | None) -> None:
        options = dict(state or default_basic_options())
        if self._grp_compress is None:
            return

        self._grp_compress.blockSignals(True)
        self.combo_compress_mode.blockSignals(True)
        self.combo_fmt.blockSignals(True)

        self._grp_compress.setChecked(bool(options.get("enable_compress", False)))
        mode = options.get("compress_mode", "quality")
        idx = self.combo_compress_mode.findData(mode)
        if idx >= 0:
            self.combo_compress_mode.setCurrentIndex(idx)
        self.slider_quality.setValue(int(options.get("quality", 85)))
        self.spin_target_size.setValue(int(options.get("target_size_kb", 500)))

        self._grp_compress.blockSignals(False)
        self.combo_compress_mode.blockSignals(False)
        self.combo_fmt.blockSignals(False)

        self._sync_compress_format()

        fmt = str(options.get("output_format", "png")).upper()
        idx = self.combo_fmt.findText(fmt)
        self.combo_fmt.setCurrentIndex(idx if idx >= 0 else 0)

        self._grp_format.setChecked(bool(options.get("enable_format", False)))

        if self._grp_dpi is not None:
            self._grp_dpi.setChecked(bool(options.get("enable_dpi", False)))
            dpi_val = int(options.get("dpi", 300))
            self.spin_dpi.setValue(dpi_val)
            p_idx = self.combo_dpi_preset.findData(dpi_val)
            if p_idx >= 0:
                self.combo_dpi_preset.setCurrentIndex(p_idx)

        self._grp_rename.setChecked(bool(options.get("enable_rename", False)))
        mode = options.get("prefix_mode", "custom")
        mode_idx = self.combo_prefix_mode.findData(mode)
        if mode_idx >= 0:
            self.combo_prefix_mode.setCurrentIndex(mode_idx)
        self.combo_fmt_prefix.setCurrentText(str(options.get("prefix", "")))
        self.spin_start.setValue(int(options.get("start_index", 1)))
        self.spin_digits.setValue(int(options.get("digits", 3)))

    def show_validation_errors(self, errors) -> None:
        msgs = list(errors or [])
        if not msgs:
            return
        QMessageBox.warning(self, "参数错误", "\n".join(str(e) for e in msgs))
