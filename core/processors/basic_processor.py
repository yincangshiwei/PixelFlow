"""
基础图片处理器 —— 图片压缩 + 格式转换 + 批量重命名
三个步骤均可独立启用，支持任意组合使用。
"""
from PIL import Image
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QLabel, QSpinBox, QComboBox, QSlider
)
from PySide6.QtCore import Qt

from core.base_processor import BaseProcessor, register_processor
import config


@register_processor
class BasicProcessor(BaseProcessor):
    """基础图片处理：压缩 → 格式转换 → 批量重命名"""

    name = "基础处理"
    description = "图片压缩 / 格式转换 / 批量重命名，可任意组合"
    icon = "⚙"
    preset_id = "basic_process"

    def __init__(self):
        self._panel: QWidget | None = None
        self._grp_compress = None
        self._grp_format = None
        self._grp_rename = None

    def create_panel(self, parent=None) -> QWidget:
        self._panel = QWidget(parent)
        root = QVBoxLayout(self._panel)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        # ── 格式转换 ──
        self._grp_format = QGroupBox("格式转换")
        self._grp_format.setCheckable(True)
        self._grp_format.setChecked(True)
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

        # 压缩模式选择
        c_lay.addWidget(QLabel("模式:"))
        self.combo_compress_mode = QComboBox()
        self.combo_compress_mode.addItem("按质量压缩", "quality")
        self.combo_compress_mode.addItem("按目标大小压缩", "size")
        self.combo_compress_mode.setMinimumWidth(130)
        self.combo_compress_mode.setStyleSheet(config.COMBOBOX_STYLE)
        c_lay.addWidget(self.combo_compress_mode)

        # 按质量压缩面板
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

        # 按目标大小压缩面板
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

        # 信号绑定（两组控件均创建完毕后）
        self.combo_compress_mode.currentIndexChanged.connect(lambda _: self._sync_compress_format())
        self._grp_compress.toggled.connect(lambda _: self._sync_compress_format())
        # size 模式下禁止取消勾选格式转换
        self._grp_format.toggled.connect(self._on_format_toggled)
        self._sync_compress_format()

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

        # 前缀输入框随模式显示/隐藏
        def _on_prefix_mode(idx):
            self.combo_fmt_prefix.setVisible(
                self.combo_prefix_mode.currentData() == "custom"
            )
        self.combo_prefix_mode.currentIndexChanged.connect(_on_prefix_mode)
        _on_prefix_mode(0)

        r_lay.addStretch()
        root.addWidget(self._grp_rename)

        # 预览示例说明
        hint = QLabel("提示：未勾选任何步骤时，文件将原样复制到输出目录")
        hint.setStyleSheet("color:#666e88;font-size:11px;font-style:italic;")
        root.addWidget(hint)

        root.addStretch()
        return self._panel

    def _sync_compress_format(self):
        """启用压缩时：强制开启格式转换，且只允许选 JPG / WEBP"""
        compress_on = self._grp_compress.isChecked()
        is_size_mode = compress_on and self.combo_compress_mode.currentData() == "size"

        if compress_on:
            # 强制勾选格式转换，格式限为 JPG / WEBP
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
            # 恢复全部格式选项
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
        """压缩开启时不允许取消勾选格式转换"""
        if self._grp_compress.isChecked() and not checked:
            self._grp_format.blockSignals(True)
            self._grp_format.setChecked(True)
            self._grp_format.blockSignals(False)

    # ── 参数收集 ──

    def gather_options(self) -> dict:
        return {
            "enable_compress": self._grp_compress.isChecked(),
            "compress_mode": self.combo_compress_mode.currentData(),
            "quality": self.slider_quality.value(),
            "target_size_kb": self.spin_target_size.value(),
            "enable_format": self._grp_format.isChecked(),
            "output_format": self.combo_fmt.currentText().lower(),
            "enable_rename": self._grp_rename.isChecked(),
            "prefix_mode": self.combo_prefix_mode.currentData(),
            "prefix": self.combo_fmt_prefix.currentText().strip(),
            "start_index": self.spin_start.value(),
            "digits": self.spin_digits.value(),
        }

    def get_output_format(self) -> str:
        if self._grp_format and self._grp_format.isChecked():
            return self.combo_fmt.currentText().lower()
        # 格式转换未启用时，由 worker 保留原格式（返回空串作为信号）
        return ""

    def default_options(self) -> dict:
        return {
            "enable_compress": False,
            "compress_mode": "quality",
            "quality": 85,
            "target_size_kb": 500,
            "enable_format": True,
            "output_format": "png",
            "enable_rename": False,
            "prefix_mode": "custom",
            "prefix": "",
            "start_index": 1,
            "digits": 3,
        }

    def apply_options(self, options: dict):
        if self._grp_compress is None:
            return

        # 暂时屏蔽信号，避免中间状态触发联动逻辑
        self._grp_compress.blockSignals(True)
        self.combo_compress_mode.blockSignals(True)
        self.combo_fmt.blockSignals(True)

        self._grp_compress.setChecked(options.get("enable_compress", False))
        mode = options.get("compress_mode", "quality")
        idx = self.combo_compress_mode.findData(mode)
        if idx >= 0:
            self.combo_compress_mode.setCurrentIndex(idx)
        self.slider_quality.setValue(options.get("quality", 85))
        self.spin_target_size.setValue(options.get("target_size_kb", 500))

        self._grp_compress.blockSignals(False)
        self.combo_compress_mode.blockSignals(False)
        self.combo_fmt.blockSignals(False)

        # 触发一次联动，确保格式选项列表正确（压缩开启→仅 JPG/WEBP）
        # _sync_compress_format 会重建 combo_fmt，因此在其后再设置格式值
        self._sync_compress_format()

        fmt = options.get("output_format", "png").upper()
        # 压缩开启时 PNG/BMP 已不在列表中，回退到 JPG
        idx = self.combo_fmt.findText(fmt)
        self.combo_fmt.setCurrentIndex(idx if idx >= 0 else 0)

        self._grp_format.setChecked(options.get("enable_format", True))

        self._grp_rename.setChecked(options.get("enable_rename", False))
        mode = options.get("prefix_mode", "custom")
        mode_idx = self.combo_prefix_mode.findData(mode)
        if mode_idx >= 0:
            self.combo_prefix_mode.setCurrentIndex(mode_idx)
        self.combo_fmt_prefix.setCurrentText(options.get("prefix", ""))
        self.spin_start.setValue(options.get("start_index", 1))
        self.spin_digits.setValue(options.get("digits", 3))

    # ── 核心处理 ──

    def process(self, img: Image.Image, options: dict) -> tuple[Image.Image, dict]:
        """
        图片压缩和格式转换在此处理；重命名逻辑由 BasicProcessWorker 在保存阶段处理。
        此方法仅做图像变换，不涉及文件命名。
        """
        details = {"original_size": img.size, "original_mode": img.mode}

        output_fmt = options.get("output_format", "png") if options.get("enable_format") else None

        # 模式转换：BMP/JPG 不支持透明通道
        if output_fmt in ("jpg", "bmp"):
            if img.mode in ("RGBA", "LA", "P"):
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                if img.mode in ("RGBA", "LA"):
                    background.paste(img, mask=img.split()[-1])
                else:
                    background.paste(img)
                img = background
                details["mode_converted"] = "RGBA→RGB (白底合并)"
            else:
                img = img.convert("RGB")
        elif output_fmt in ("png", "webp", None):
            # 保持 RGBA
            if img.mode != "RGBA":
                img = img.convert("RGBA")

        details["output_format"] = output_fmt or "原格式"
        return img, details
