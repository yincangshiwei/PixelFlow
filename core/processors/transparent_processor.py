"""
透明图处理器 —— 裁剪透明边缘 + 调整尺寸 + 放置到画布
"""
from PIL import Image
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QLabel, QSpinBox, QComboBox, QPushButton, QColorDialog
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from core.base_processor import BaseProcessor, register_processor
from core.image_processor import hex_to_rgba, trim_transparent, resize_image
import config


class _ColorBlock(QWidget):
    """颜色选择：色块 + 色值标签"""
    def __init__(self, color="#FFFFFF", parent=None):
        super().__init__(parent)
        self._color = color
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self._btn = QPushButton()
        self._btn.setFixedSize(36, 26)
        self._btn.setCursor(Qt.PointingHandCursor)
        self._btn.clicked.connect(self._pick)
        lay.addWidget(self._btn)
        self._lbl = QLabel(color)
        self._lbl.setStyleSheet("color:#b0b8c8;font-size:12px;background:transparent;")
        lay.addWidget(self._lbl)
        self._refresh()

    def _pick(self):
        c = QColorDialog.getColor(QColor(self._color), self, "选择画布颜色",
                                  QColorDialog.ShowAlphaChannel)
        if c.isValid():
            self._color = c.name(QColor.HexArgb) if c.alpha() < 255 else c.name()
            self._refresh()

    def _refresh(self):
        self._btn.setStyleSheet(
            f"QPushButton{{background:{self._color};border:2px solid #5a5a6a;border-radius:6px;min-width:36px;min-height:24px;}}"
            f"QPushButton:hover{{border-color:#5b8af5;}}"
        )
        self._lbl.setText(self._color.upper())

    def get_color(self) -> str:
        return self._color


@register_processor
class TransparentImageProcessor(BaseProcessor):
    """透明图处理：裁透明边 → 缩放 → 画布居中"""

    name = "透明图处理"
    description = "去除透明边缘 → 调整尺寸 → 放入画布居中"
    icon = "✂"
    preset_id = "transparent_image"

    _MODE_MAP = {"contain": 0, "cover": 1, "stretch": 2}

    def __init__(self):
        self._panel: QWidget | None = None
        self._grp_trim = None
        self._grp_resize = None
        self._grp_canvas = None

    def create_panel(self, parent=None) -> QWidget:
        self._panel = QWidget(parent)
        root = QVBoxLayout(self._panel)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        # ── 裁剪透明边缘 ──
        self._grp_trim = QGroupBox("裁剪透明边缘")
        self._grp_trim.setCheckable(True)
        self._grp_trim.setChecked(True)
        t_lay = QHBoxLayout(self._grp_trim)
        t_lay.addWidget(QLabel("Alpha 阈值:"))
        self.spin_alpha = QSpinBox()
        self.spin_alpha.setRange(0, 254)
        self.spin_alpha.setValue(0)
        self.spin_alpha.setToolTip("大于此值视为有效内容 (0=仅裁完全透明)")
        t_lay.addWidget(self.spin_alpha)
        t_lay.addStretch()
        root.addWidget(self._grp_trim)

        # ── 调整尺寸 ──
        self._grp_resize = QGroupBox("调整尺寸")
        self._grp_resize.setCheckable(True)
        self._grp_resize.setChecked(False)
        r_lay = QHBoxLayout(self._grp_resize)
        r_lay.addWidget(QLabel("宽:"))
        self.spin_rw = QSpinBox()
        self.spin_rw.setRange(1, 99999)
        self.spin_rw.setValue(800)
        r_lay.addWidget(self.spin_rw)
        r_lay.addWidget(QLabel("高:"))
        self.spin_rh = QSpinBox()
        self.spin_rh.setRange(1, 99999)
        self.spin_rh.setValue(800)
        r_lay.addWidget(self.spin_rh)
        r_lay.addWidget(QLabel("模式:"))
        self.combo_mode = QComboBox()
        self.combo_mode.addItem("等比缩放 (完整显示)", "contain")
        self.combo_mode.addItem("等比铺满 (可能裁切)", "cover")
        self.combo_mode.addItem("拉伸填充 (不保持比例)", "stretch")
        self.combo_mode.setStyleSheet(config.COMBOBOX_STYLE)
        r_lay.addWidget(self.combo_mode)
        r_lay.addStretch()
        root.addWidget(self._grp_resize)

        # ── 放置到画布 ──
        self._grp_canvas = QGroupBox("放置到画布")
        self._grp_canvas.setCheckable(True)
        self._grp_canvas.setChecked(False)
        c_lay = QHBoxLayout(self._grp_canvas)
        c_lay.addWidget(QLabel("宽:"))
        self.spin_cw = QSpinBox()
        self.spin_cw.setRange(1, 99999)
        self.spin_cw.setValue(1500)
        c_lay.addWidget(self.spin_cw)
        c_lay.addWidget(QLabel("高:"))
        self.spin_ch = QSpinBox()
        self.spin_ch.setRange(1, 99999)
        self.spin_ch.setValue(1500)
        c_lay.addWidget(self.spin_ch)
        c_lay.addWidget(QLabel("颜色:"))
        self.color_btn = _ColorBlock("#FFFFFF")
        c_lay.addWidget(self.color_btn)
        c_lay.addStretch()
        root.addWidget(self._grp_canvas)

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
        return self._panel

    def gather_options(self) -> dict:
        return {
            "enable_trim": self._grp_trim.isChecked(),
            "alpha_threshold": self.spin_alpha.value(),
            "enable_resize": self._grp_resize.isChecked(),
            "resize_w": self.spin_rw.value(),
            "resize_h": self.spin_rh.value(),
            "resize_mode": self.combo_mode.currentData(),
            "enable_canvas": self._grp_canvas.isChecked(),
            "canvas_w": self.spin_cw.value(),
            "canvas_h": self.spin_ch.value(),
            "canvas_color": self.color_btn.get_color(),
            "output_format": self.combo_fmt.currentText(),
        }

    def get_output_format(self) -> str:
        return self.combo_fmt.currentText()

    def default_options(self) -> dict:
        return {
            "enable_trim": True,
            "alpha_threshold": 0,
            "enable_resize": False,
            "resize_w": 800,
            "resize_h": 800,
            "resize_mode": "contain",
            "enable_canvas": False,
            "canvas_w": 1500,
            "canvas_h": 1500,
            "canvas_color": "#FFFFFF",
            "output_format": "png",
        }

    def apply_options(self, options: dict):
        if self._grp_trim is None:
            return
        self._grp_trim.setChecked(options.get("enable_trim", True))
        self.spin_alpha.setValue(options.get("alpha_threshold", 0))
        self._grp_resize.setChecked(options.get("enable_resize", False))
        self.spin_rw.setValue(options.get("resize_w", 800))
        self.spin_rh.setValue(options.get("resize_h", 800))
        mode = options.get("resize_mode", "contain")
        idx = self._MODE_MAP.get(mode, 0)
        self.combo_mode.setCurrentIndex(idx)
        self._grp_canvas.setChecked(options.get("enable_canvas", False))
        self.spin_cw.setValue(options.get("canvas_w", 1500))
        self.spin_ch.setValue(options.get("canvas_h", 1500))
        color = options.get("canvas_color", "#FFFFFF")
        self.color_btn._color = color
        self.color_btn._refresh()
        fmt = options.get("output_format", "png")
        fmt_idx = ["png", "webp", "jpg"].index(fmt) if fmt in ["png", "webp", "jpg"] else 0
        self.combo_fmt.setCurrentIndex(fmt_idx)

    def process(self, img: Image.Image, options: dict) -> tuple[Image.Image, dict]:
        details = {"original_size": img.size}
        img = img.convert("RGBA")

        if options.get("enable_trim"):
            img, bbox = trim_transparent(img, options.get("alpha_threshold", 0))
            details["trim_bbox"] = bbox
            details["trimmed_size"] = img.size

        if options.get("enable_resize"):
            target = (options["resize_w"], options["resize_h"])
            img = resize_image(img, target_size=target, mode=options.get("resize_mode", "contain"))
            details["resized_size"] = img.size

        if options.get("enable_canvas"):
            cs = (options["canvas_w"], options["canvas_h"])
            canvas_rgba = hex_to_rgba(options.get("canvas_color", "#FFFFFF"))
            canvas = Image.new("RGBA", cs, canvas_rgba)
            px = (cs[0] - img.size[0]) // 2
            py = (cs[1] - img.size[1]) // 2
            canvas.paste(img, (px, py), img)
            img = canvas
            details["canvas_size"] = cs
            details["paste_pos"] = (px, py)

        return img, details
