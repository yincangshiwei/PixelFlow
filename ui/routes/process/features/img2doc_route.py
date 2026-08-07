"""Img2DocFeatureRoute —— 图片排版导出参数面板（只读写控件）。"""
from __future__ import annotations

import os
import math
import copy
import json
import re
import traceback
import ctypes
from functools import cmp_to_key
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QLabel, QComboBox, QSpinBox, QDoubleSpinBox,
    QPushButton, QFileDialog, QLineEdit, QCheckBox,
    QFrame, QColorDialog, QSizePolicy, QListWidget,
    QListWidgetItem, QStackedWidget, QMessageBox, QTextEdit,
)
from PySide6.QtCore import Qt, Signal, QRectF
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QFont

from core.processors.img2doc_processor import (
    OverlayLayer, TextLayer, ImageLayer, default_img2doc_options,
)
import config


class LayerListItem(QListWidgetItem):
    def __init__(self, layer, index):
        super().__init__()
        self.layer = layer
        self.index = index
        self.update_text()

    def update_text(self):
        label = self.layer.name if self.layer.name else ""
        if self.layer.layer_type == "text":
            self.setText(f"📝 文字 {self.index + 1} | {label}")
        else:
            self.setText(f"🖼️ 图片 {self.index + 1} | {label}")


class PageGridPositionWidget(QWidget):
    CELLS = [
        ("↖", 0.0, 0.0), ("↑", 0.5, 0.0), ("↗", 1.0, 0.0),
        ("←", 0.0, 0.5), ("⊙", 0.5, 0.5), ("→", 1.0, 0.5),
        ("↙", 0.0, 1.0), ("↓", 0.5, 1.0), ("↘", 1.0, 1.0),
    ]

    def __init__(self, x_spin, y_spin, page_size_provider, element_type="text",
                 w_spin=None, h_spin=None, parent=None):
        super().__init__(parent)
        self._x_spin = x_spin
        self._y_spin = y_spin
        self._page_size_provider = page_size_provider
        self._element_type = element_type
        self._w_spin = w_spin
        self._h_spin = h_spin
        self._hovered_cell = -1
        self.setFixedSize(90, 90)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)

    def _page_size(self):
        try:
            return self._page_size_provider()
        except Exception:
            return 33.0, 19.0

    def _cell_at(self, pos):
        cw = self.width() / 3.0
        ch = self.height() / 3.0
        col = int(pos.x() / cw)
        row = int(pos.y() / ch)
        if 0 <= col < 3 and 0 <= row < 3:
            return row * 3 + col
        return -1

    def _apply_position(self, cell_idx):
        if cell_idx < 0:
            return
        _, xr, yr = self.CELLS[cell_idx]
        pw, ph = self._page_size()
        if self._element_type == "image" and self._w_spin and self._h_spin:
            ww, hh = self._w_spin.value(), self._h_spin.value()
            x = (pw - ww) * xr
            y = (ph - hh) * yr
        else:
            x, y = pw * xr, ph * yr
        self._x_spin.setValue(round(max(0.0, x), 2))
        self._y_spin.setValue(round(max(0.0, y), 2))

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        cw, ch = w / 3.0, h / 3.0
        p.fillRect(self.rect(), QColor(22, 22, 40, 180))
        for i, (sym, _, _) in enumerate(self.CELLS):
            r, c = divmod(i, 3)
            rect = QRectF(c * cw + 1, r * ch + 1, cw - 2, ch - 2)
            if i == self._hovered_cell:
                p.setBrush(QBrush(QColor(91, 138, 245, 80)))
                p.setPen(QPen(QColor(91, 138, 245, 180), 1.5))
            else:
                p.setBrush(QBrush(QColor(38, 38, 62, 120)))
                p.setPen(QPen(QColor(100, 110, 170, 60), 1))
            p.drawRoundedRect(rect, 4, 4)
            p.setPen(QColor(200, 205, 230))
            p.setFont(QFont("Segoe UI Symbol", 11))
            p.drawText(rect, Qt.AlignCenter, sym)
        p.end()

    def mouseMoveEvent(self, event):
        idx = self._cell_at(event.position().toPoint())
        if idx != self._hovered_cell:
            self._hovered_cell = idx
            self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            idx = self._cell_at(event.position().toPoint())
            if idx >= 0:
                self._apply_position(idx)

    def leaveEvent(self, event):
        self._hovered_cell = -1
        self.update()


class Img2DocFeatureRoute(QWidget):
    feature_id = "img2doc"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layers = []
        self._list_widget = None
        self._config_stack = None
        self._custom_fonts = {}
        self._current_layer = None
        self._build_widget()

    def build_widget(self, parent=None) -> QWidget:
        if parent is not None and self.parent() is None:
            self.setParent(parent)
        return self

    def _page_size_cm(self):
        return float(self.spin_w.value()), float(self.spin_h.value())

    def _build_widget(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        # ══ 1. 图片压缩（先压缩再排版，可选，默认不选）══
        grp_compress = QGroupBox("图片压缩（排版前预处理）")
        c_lay = QVBoxLayout(grp_compress)

        c_row1 = QHBoxLayout()
        self.chk_compress = QCheckBox("启用压缩（按目标大小，先压缩再排版）")
        self.chk_compress.setChecked(False)
        c_row1.addWidget(self.chk_compress)
        c_row1.addStretch()
        c_lay.addLayout(c_row1)

        self.widget_compress = QWidget()
        c_inner = QHBoxLayout(self.widget_compress)
        c_inner.setContentsMargins(0, 0, 0, 0)
        c_inner.addWidget(QLabel("目标大小(KB):"))
        self.spin_target_kb = QSpinBox()
        self.spin_target_kb.setRange(10, 102400)
        self.spin_target_kb.setValue(500)
        self.spin_target_kb.setSingleStep(50)
        self.spin_target_kb.setToolTip("每张图片压缩后的目标大小上限（KB），使用二分法寻找最优质量")
        c_inner.addWidget(self.spin_target_kb)
        c_inner.addWidget(QLabel("压缩格式:"))
        self.combo_compress_fmt = QComboBox()
        self.combo_compress_fmt.addItems(["JPEG", "WEBP"])
        self.combo_compress_fmt.setToolTip("PNG 不支持有损压缩，建议选 JPEG 或 WEBP")
        self.combo_compress_fmt.setStyleSheet(config.COMBOBOX_STYLE)
        c_inner.addWidget(self.combo_compress_fmt)
        c_inner.addStretch()
        c_lay.addWidget(self.widget_compress)

        def _on_compress_toggled(checked):
            self.widget_compress.setVisible(checked)
        self.chk_compress.toggled.connect(_on_compress_toggled)
        _on_compress_toggled(False)

        root.addWidget(grp_compress)

        # ══ 2. 输出格式与尺寸 ══
        grp_fmt = QGroupBox("输出设置")
        f_lay = QVBoxLayout(grp_fmt)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("导出格式:"))
        self.combo_fmt = QComboBox()
        self.combo_fmt.addItems(["PPTX", "PDF", "DOCX"])
        self.combo_fmt.setStyleSheet(config.COMBOBOX_STYLE)
        row1.addWidget(self.combo_fmt)
        row1.addStretch()
        f_lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("页面宽度(cm):"))
        self.spin_w = QSpinBox()
        self.spin_w.setRange(5, 200)
        self.spin_w.setValue(33)
        row2.addWidget(self.spin_w)
        row2.addWidget(QLabel("高度(cm):"))
        self.spin_h = QSpinBox()
        self.spin_h.setRange(5, 200)
        self.spin_h.setValue(19)
        row2.addWidget(self.spin_h)
        row2.addStretch()
        f_lay.addLayout(row2)
        root.addWidget(grp_fmt)

        def _on_fmt_changed(text):
            if text == "PPTX":
                self.spin_w.setValue(33)
                self.spin_h.setValue(19)
            else:
                self.spin_w.setValue(21)
                self.spin_h.setValue(30)
        self.combo_fmt.currentTextChanged.connect(_on_fmt_changed)

        # ══ 3. 排版布局 ══
        grp_layout = QGroupBox("排版布局")
        l_lay = QVBoxLayout(grp_layout)

        l_row1 = QHBoxLayout()
        l_row1.addWidget(QLabel("每页图片数:"))
        self.spin_count = QSpinBox()
        self.spin_count.setRange(1, 100)
        self.spin_count.setValue(1)
        self.spin_count.setToolTip("输入每页要排列的图片数量，将自动计算最佳网格布局")
        l_row1.addWidget(self.spin_count)
        l_row1.addWidget(QLabel("每行最大列数:"))
        self.spin_max_cols = QSpinBox()
        self.spin_max_cols.setRange(1, 20)
        self.spin_max_cols.setValue(4)
        self.spin_max_cols.setToolTip("每行最多放几张图，超出后自动换行")
        l_row1.addWidget(self.spin_max_cols)
        l_row1.addStretch()
        l_lay.addLayout(l_row1)

        l_row2 = QHBoxLayout()
        self.chk_keep_ratio = QCheckBox("保持图片比例 (不拉伸)")
        self.chk_keep_ratio.setChecked(True)
        l_row2.addWidget(self.chk_keep_ratio)
        self.chk_group_by_folder = QCheckBox("按文件夹分组分页")
        self.chk_group_by_folder.setChecked(False)
        self.chk_group_by_folder.setToolTip("开启后，同一文件夹内的图片连续排版；不同文件夹之间强制从新页开始。关闭时保持原来的连续排版方式")
        l_row2.addWidget(self.chk_group_by_folder)
        l_row2.addWidget(QLabel("行内排列方向:"))
        self.combo_row_align = QComboBox()
        self.combo_row_align.addItem("从左到右", "left")
        self.combo_row_align.addItem("居中对齐", "center")
        self.combo_row_align.addItem("从右到左", "right")
        self.combo_row_align.setToolTip(
            "从左到右：图片紧靠左侧排列\n"
            "居中对齐：图片在行内水平居中\n"
            "从右到左：图片紧靠右侧，顺序仍从左到右"
        )
        self.combo_row_align.setStyleSheet(config.COMBOBOX_STYLE)
        l_row2.addWidget(self.combo_row_align)
        l_row2.addStretch()
        l_lay.addLayout(l_row2)

        root.addWidget(grp_layout)

        # ══ 4. 排序规则 ══
        grp_sort = QGroupBox("排序规则")
        s_lay = QVBoxLayout(grp_sort)

        s_row1 = QHBoxLayout()
        s_row1.addWidget(QLabel("排序模式:"))
        self.combo_sort = QComboBox()
        self.combo_sort.addItem("按文件名默认排序", "default")
        self.combo_sort.addItem("按文件夹按文件名排序", "folder_filename")
        self.combo_sort.addItem("按 Excel 指定列排序", "excel_sort")
        self.combo_sort.setStyleSheet(config.COMBOBOX_STYLE)
        s_row1.addWidget(self.combo_sort)
        s_row1.addStretch()
        s_lay.addLayout(s_row1)

        self.widget_excel = QWidget()
        e_lay = QVBoxLayout(self.widget_excel)
        e_lay.setContentsMargins(0, 0, 0, 0)

        e_row1 = QHBoxLayout()
        self.txt_excel = QLineEdit()
        self.txt_excel.setPlaceholderText("选择 Excel 文件 (.xlsx)")
        self.txt_excel.setReadOnly(True)
        e_row1.addWidget(self.txt_excel)
        self.btn_excel = QPushButton("浏览...")
        self.btn_excel.clicked.connect(self._browse_excel)
        e_row1.addWidget(self.btn_excel)
        e_lay.addLayout(e_row1)

        e_row2 = QHBoxLayout()
        e_row2.addWidget(QLabel("匹配文件名列(字母):"))
        self.txt_col_name = QLineEdit("A")
        self.txt_col_name.setMaximumWidth(40)
        e_row2.addWidget(self.txt_col_name)
        e_row2.addWidget(QLabel("排序列(字母):"))
        self.txt_col_val = QLineEdit("B")
        self.txt_col_val.setMaximumWidth(40)
        e_row2.addWidget(self.txt_col_val)
        e_row2.addWidget(QLabel("数据起始行:"))
        self.spin_start_row = QSpinBox()
        self.spin_start_row.setRange(1, 99999)
        self.spin_start_row.setValue(2)
        self.spin_start_row.setMaximumWidth(60)
        self.spin_start_row.setToolTip("填 2 表示第 1 行是表头，从第 2 行开始读取数据")
        e_row2.addWidget(self.spin_start_row)
        e_row2.addStretch()
        e_lay.addLayout(e_row2)
        s_lay.addWidget(self.widget_excel)

        def _on_sort_changed(idx):
            mode = self.combo_sort.currentData()
            self.widget_excel.setVisible(mode == "excel_sort")

        def _on_group_by_folder_toggled(checked):
            target_mode = "folder_filename" if checked else "default"
            current_mode = self.combo_sort.currentData()
            if checked or current_mode == "folder_filename":
                target_idx = self.combo_sort.findData(target_mode)
                if target_idx >= 0:
                    self.combo_sort.setCurrentIndex(target_idx)

        self.combo_sort.currentIndexChanged.connect(_on_sort_changed)
        self.chk_group_by_folder.toggled.connect(_on_group_by_folder_toggled)
        _on_sort_changed(0)
        root.addWidget(grp_sort)

        # ══ 5. 叠加层系统（左右分栏）══
        grp_overlay = QGroupBox("页面叠加元素（每页均叠加）")
        ov_lay = QHBoxLayout(grp_overlay)
        ov_lay.setSpacing(10)

        # ── 左侧：元素列表 + 图标按钮 ──
        left_panel = QWidget()
        left_panel.setMaximumWidth(280)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)

        self._list_widget = QListWidget()
        self._list_widget.setMinimumHeight(140)
        self._list_widget.currentRowChanged.connect(self._on_layer_selected)
        left_layout.addWidget(self._list_widget)

        # 操作按钮（emoji图标）
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(4)

        def _make_icon_btn(emoji, tooltip, slot):
            btn = QPushButton(emoji)
            btn.setToolTip(tooltip)
            btn.setStyleSheet(config.ICON_BTN_STYLE)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(slot)
            return btn

        btn_add_text = _make_icon_btn("📝", "添加文字", self._add_text_layer)
        btn_layout.addWidget(btn_add_text)

        btn_add_image = _make_icon_btn("🖼", "添加图片", self._add_image_layer)
        btn_layout.addWidget(btn_add_image)

        btn_delete = _make_icon_btn("🗑", "删除选中", self._delete_layer)
        btn_layout.addWidget(btn_delete)

        btn_layout.addSpacing(4)

        btn_rename = _make_icon_btn("✏", "重命名", self._rename_layer)
        btn_layout.addWidget(btn_rename)

        btn_layout.addSpacing(6)

        btn_move_up = _make_icon_btn("⬆", "上移", self._move_layer_up)
        btn_layout.addWidget(btn_move_up)

        btn_move_down = _make_icon_btn("⬇", "下移", self._move_layer_down)
        btn_layout.addWidget(btn_move_down)

        btn_layout.addStretch()
        left_layout.addLayout(btn_layout)

        ov_lay.addWidget(left_panel, stretch=2)

        # ── 右侧：元素配置面板 ──
        grp_config = QGroupBox("元素配置")
        config_root = QVBoxLayout(grp_config)
        config_root.setContentsMargins(8, 8, 8, 8)

        self._config_stack = QStackedWidget()

        # 默认提示页面
        hint_page = QWidget()
        hint_layout = QVBoxLayout(hint_page)
        hint_label = QLabel("请先添加元素，然后在左侧列表中选择一个元素进行配置")
        hint_label.setStyleSheet("color:#666e88;font-size:12px;font-style:italic;")
        hint_label.setAlignment(Qt.AlignCenter)
        hint_layout.addWidget(hint_label)
        self._config_stack.addWidget(hint_page)

        config_root.addWidget(self._config_stack)
        ov_lay.addWidget(grp_config, stretch=5)

        # 坐标说明
        coord_hint = QLabel(
            "坐标说明：X/Y 为距页面左上角的距离（厘米），宽/高为元素尺寸（厘米）。",
            wordWrap=True
        )
        coord_hint.setStyleSheet("color:#666e88;font-size:11px;")
        root.addWidget(grp_overlay)
        root.addWidget(coord_hint)

        root.addStretch()
        return

    def _browse_excel(self):
        f, _ = QFileDialog.getOpenFileName(self, "选择 Excel", str(Path.home() / "Desktop"), "Excel (*.xlsx)")
        if f:
            self.txt_excel.setText(f)

    def _create_text_config_page(self, layer: TextLayer) -> QWidget:
        """创建文字层配置页面"""
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(8)

        # ── 添加方式 ──
        placement_layout = QHBoxLayout()
        placement_layout.addWidget(QLabel("添加方式:"))
        self._combo_placement = QComboBox()
        self._combo_placement.addItem("叠加（不占空间）", "overlay")
        self._combo_placement.addItem("占用空间（参与排版边距）", "reserved")
        self._combo_placement.setCurrentIndex(self._combo_placement.findData(layer.placement))
        self._combo_placement.setToolTip(
            "叠加：直接叠在图片排版区上方，不影响图片排版区域大小\n"
            "占用空间：元素会占据页面边缘区域，图片排版区自动缩小避让"
        )
        self._combo_placement.setStyleSheet(config.COMBOBOX_STYLE)
        placement_layout.addWidget(self._combo_placement)
        placement_layout.addStretch()
        page_layout.addLayout(placement_layout)

        # ── 数据源 ──
        src_layout = QHBoxLayout()
        src_layout.addWidget(QLabel("数据来源:"))
        combo_source = QComboBox()
        combo_source.addItem("固定文本", "fixed")
        combo_source.addItem("Excel列数据", "excel")
        combo_source.addItem("图片文件名称", "filename")
        combo_source.setCurrentIndex(combo_source.findData(layer.source))
        combo_source.setStyleSheet(config.COMBOBOX_STYLE)
        src_layout.addWidget(combo_source)
        src_layout.addStretch()
        page_layout.addLayout(src_layout)

        # 固定文本内容
        self._widget_fixed_text = QWidget()
        fixed_layout = QVBoxLayout(self._widget_fixed_text)
        fixed_layout.setContentsMargins(0, 0, 0, 0)
        fixed_layout.addWidget(QLabel("文本内容:"))
        self._text_content = QLineEdit(layer.text)
        self._text_content.setPlaceholderText("输入要显示的文字")
        fixed_layout.addWidget(self._text_content)
        page_layout.addWidget(self._widget_fixed_text)

        # Excel配置
        self._widget_excel = QWidget()
        excel_layout = QVBoxLayout(self._widget_excel)
        excel_layout.setContentsMargins(0, 0, 0, 0)

        # Excel文件选择
        file_layout = QHBoxLayout()
        file_layout.addWidget(QLabel("Excel文件:"))
        self._excel_file_input = QLineEdit(layer.excel_file)
        self._excel_file_input.setPlaceholderText("选择Excel文件...")
        file_layout.addWidget(self._excel_file_input)
        btn_browse_excel = QPushButton("浏览")
        btn_browse_excel.clicked.connect(self._browse_excel_for_layer)
        file_layout.addWidget(btn_browse_excel)
        excel_layout.addLayout(file_layout)

        # 匹配列和数据列
        excel_col_layout = QHBoxLayout()
        excel_col_layout.addWidget(QLabel("匹配列:"))
        self._excel_match_column = QSpinBox()
        self._excel_match_column.setRange(1, 100)
        self._excel_match_column.setValue(layer.match_column if layer.match_column > 0 else 1)
        self._excel_match_column.setToolTip("图片文件名所在的列号（从1开始），用于匹配对应行")
        excel_col_layout.addWidget(self._excel_match_column)

        excel_col_layout.addWidget(QLabel("数据列:"))
        self._excel_data_column = QSpinBox()
        self._excel_data_column.setRange(1, 100)
        self._excel_data_column.setValue(layer.data_column if layer.data_column > 0 else 2)
        self._excel_data_column.setToolTip("要读取的文本数据列号（从1开始）")
        excel_col_layout.addWidget(self._excel_data_column)
        excel_col_layout.addStretch()
        excel_layout.addLayout(excel_col_layout)

        # 起始行
        excel_row_layout = QHBoxLayout()
        excel_row_layout.addWidget(QLabel("数据起始行:"))
        self._excel_row_start = QSpinBox()
        self._excel_row_start.setRange(1, 10000)
        self._excel_row_start.setValue(layer.excel_row_start)
        self._excel_row_start.setToolTip("数据从第几行开始（跳过表头）")
        excel_row_layout.addWidget(self._excel_row_start)
        excel_row_layout.addStretch()
        excel_layout.addLayout(excel_row_layout)

        # Excel使用说明
        excel_hint = QLabel("💡 匹配列：图片文件名所在列 | 数据列：要叠加的文本内容所在列")
        excel_hint.setStyleSheet("color:#666e88;font-size:11px;font-style:italic;")
        excel_hint.setWordWrap(True)
        excel_layout.addWidget(excel_hint)

        page_layout.addWidget(self._widget_excel)

        # 文件名提示
        self._widget_filename_hint = QLabel("💡 将使用当前页第一张图片的文件名（不含扩展名）作为文本内容")
        self._widget_filename_hint.setStyleSheet("color:#666e88;font-size:11px;font-style:italic;")
        self._widget_filename_hint.setWordWrap(True)
        page_layout.addWidget(self._widget_filename_hint)

        def _on_source_change(idx):
            source = combo_source.itemData(idx)
            self._widget_fixed_text.setVisible(source == 'fixed')
            self._widget_excel.setVisible(source == 'excel')
            self._widget_filename_hint.setVisible(source == 'filename')
            layer.source = source

        combo_source.currentIndexChanged.connect(_on_source_change)
        _on_source_change(combo_source.currentIndex())

        # 字体设置
        font_layout = QHBoxLayout()
        font_layout.addWidget(QLabel("字体:"))
        self._font_family = QComboBox()
        # 常用字体列表（中文字体优先）
        fonts = ['Microsoft YaHei', 'SimHei', 'SimSun', 'KaiTi', 'FangSong', 'Arial', 'Times New Roman']
        self._font_family.addItems(fonts)
        # 恢复自定义字体
        if hasattr(self, '_custom_fonts'):
            for fn in self._custom_fonts:
                if self._font_family.findText(fn) < 0:
                    self._font_family.addItem(fn)
        idx = self._font_family.findText(layer.font_family)
        if idx >= 0:
            self._font_family.setCurrentIndex(idx)
        else:
            idx = self._font_family.findText('Microsoft YaHei')
            if idx >= 0:
                self._font_family.setCurrentIndex(idx)
        
        self._font_family.setStyleSheet(config.COMBOBOX_STYLE)

        # 添加加载字体按钮
        btn_load_font = QPushButton("加载字体")
        btn_load_font.clicked.connect(self._load_font)
        font_layout.addWidget(btn_load_font)
        font_layout.addWidget(self._font_family)

        font_layout.addWidget(QLabel("大小:"))
        self._font_size_pt = QSpinBox()
        self._font_size_pt.setRange(6, 200)
        self._font_size_pt.setValue(layer.font_size_pt)
        font_layout.addWidget(self._font_size_pt)

        self._chk_bold = QCheckBox("加粗")
        self._chk_bold.setChecked(layer.bold)
        font_layout.addWidget(self._chk_bold)

        font_layout.addStretch()
        page_layout.addLayout(font_layout)

        # 颜色选择
        color_layout = QHBoxLayout()
        color_layout.addWidget(QLabel("文字颜色:"))
        self._text_color_btn = QPushButton()
        self._text_color_btn.setFixedSize(36, 26)
        self._text_color_btn.setCursor(Qt.PointingHandCursor)
        self._text_color_btn.clicked.connect(self._pick_text_color)
        color_layout.addWidget(self._text_color_btn)

        self._text_color_lbl = QLabel(layer.color)
        self._text_color_lbl.setStyleSheet("color:#b0b8c8;font-size:12px;background:transparent;")
        color_layout.addWidget(self._text_color_lbl)
        color_layout.addStretch()
        page_layout.addLayout(color_layout)
        self._refresh_text_color_btn(layer.color)

        # ── 位置（cm）──
        pos_layout = QHBoxLayout()

        # 先创建坐标 SpinBox（供后续宫格引用）
        self._text_x_cm = QDoubleSpinBox()
        self._text_x_cm.setRange(0, 200)
        self._text_x_cm.setValue(layer.x_cm)
        self._text_x_cm.setDecimals(2)
        self._text_x_cm.setMaximumWidth(90)
        self._text_x_cm.setToolTip("距页面左边缘的距离（厘米）")

        self._text_y_cm = QDoubleSpinBox()
        self._text_y_cm.setRange(0, 200)
        self._text_y_cm.setValue(layer.y_cm)
        self._text_y_cm.setDecimals(2)
        self._text_y_cm.setMaximumWidth(90)
        self._text_y_cm.setToolTip("距页面上边缘的距离（厘米）")

        pos_layout.addWidget(QLabel("X(cm):"))
        pos_layout.addWidget(self._text_x_cm)
        pos_layout.addWidget(QLabel("Y(cm):"))
        pos_layout.addWidget(self._text_y_cm)
        pos_layout.addSpacing(8)
        grid = PageGridPositionWidget(
            x_spin=self._text_x_cm,
            y_spin=self._text_y_cm,
            page_size_provider=self._page_size_cm,
            element_type='text',
        )
        pos_layout.addWidget(grid)
        pos_layout.addStretch()
        page_layout.addLayout(pos_layout)

        self._current_layer = layer
        self._combo_source = combo_source
        return page

    def _create_image_config_page(self, layer: ImageLayer) -> QWidget:
        """创建图片层配置页面"""
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(8)

        # ── 添加方式 ──
        placement_layout = QHBoxLayout()
        placement_layout.addWidget(QLabel("添加方式:"))
        self._combo_placement = QComboBox()
        self._combo_placement.addItem("叠加（不占空间）", "overlay")
        self._combo_placement.addItem("占用空间（参与排版边距）", "reserved")
        self._combo_placement.setCurrentIndex(self._combo_placement.findData(layer.placement))
        self._combo_placement.setToolTip(
            "叠加：直接叠在图片排版区上方，不影响图片排版区域大小\n"
            "占用空间：元素会占据页面边缘区域，图片排版区自动缩小避让"
        )
        self._combo_placement.setStyleSheet(config.COMBOBOX_STYLE)
        placement_layout.addWidget(self._combo_placement)
        placement_layout.addStretch()
        page_layout.addLayout(placement_layout)

        # ── 图片文件选择 ──
        file_layout = QHBoxLayout()
        file_layout.addWidget(QLabel("图片文件:"))
        self._image_file_input = QLineEdit(layer.path)
        self._image_file_input.setPlaceholderText("选择要叠加的图片（PNG/JPG）...")
        self._image_file_input.setReadOnly(True)
        file_layout.addWidget(self._image_file_input)
        btn_browse = QPushButton("浏览")
        btn_browse.clicked.connect(self._browse_overlay_image)
        file_layout.addWidget(btn_browse)
        page_layout.addLayout(file_layout)

        # 先创建坐标和尺寸 SpinBox（供后续宫格引用）
        self._image_x_cm = QDoubleSpinBox()
        self._image_x_cm.setRange(0, 200)
        self._image_x_cm.setValue(layer.x_cm)
        self._image_x_cm.setDecimals(2)
        self._image_x_cm.setMaximumWidth(90)
        self._image_x_cm.setToolTip("距页面左边缘的距离（厘米）")

        self._image_y_cm = QDoubleSpinBox()
        self._image_y_cm.setRange(0, 200)
        self._image_y_cm.setValue(layer.y_cm)
        self._image_y_cm.setDecimals(2)
        self._image_y_cm.setMaximumWidth(90)
        self._image_y_cm.setToolTip("距页面上边缘的距离（厘米）")

        self._image_w_cm = QDoubleSpinBox()
        self._image_w_cm.setRange(0.1, 200)
        self._image_w_cm.setValue(layer.w_cm)
        self._image_w_cm.setDecimals(2)
        self._image_w_cm.setMaximumWidth(90)
        self._image_w_cm.setToolTip("图片宽度（厘米）")

        self._image_h_cm = QDoubleSpinBox()
        self._image_h_cm.setRange(0.1, 200)
        self._image_h_cm.setValue(layer.h_cm)
        self._image_h_cm.setDecimals(2)
        self._image_h_cm.setMaximumWidth(90)
        self._image_h_cm.setToolTip("图片高度（厘米）")

        # ── 位置（cm）──
        pos_layout = QHBoxLayout()
        pos_layout.addWidget(QLabel("X(cm):"))
        pos_layout.addWidget(self._image_x_cm)
        pos_layout.addWidget(QLabel("Y(cm):"))
        pos_layout.addWidget(self._image_y_cm)
        pos_layout.addSpacing(8)
        grid = PageGridPositionWidget(
            x_spin=self._image_x_cm,
            y_spin=self._image_y_cm,
            page_size_provider=self._page_size_cm,
            element_type='image',
            w_spin=self._image_w_cm,
            h_spin=self._image_h_cm,
        )
        pos_layout.addWidget(grid)
        pos_layout.addStretch()
        page_layout.addLayout(pos_layout)

        # ── 大小（cm）──
        size_layout = QHBoxLayout()
        size_layout.addWidget(QLabel("宽(cm):"))
        size_layout.addWidget(self._image_w_cm)
        size_layout.addWidget(QLabel("高(cm):"))
        size_layout.addWidget(self._image_h_cm)
        size_layout.addWidget(self._image_h_cm)
        size_layout.addStretch()
        page_layout.addLayout(size_layout)

        self._current_layer = layer
        return page

    def _browse_overlay_image(self):
        """浏览选择叠加图片"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择图片文件", str(Path.home() / "Desktop"),
            "Image Files (*.png *.jpg *.jpeg *.webp)"
        )
        if file_path:
            self._image_file_input.setText(file_path)

    def _browse_excel_for_layer(self):
        """浏览选择Excel文件（用于文字层配置）"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择Excel文件", str(Path.home() / "Desktop"), "Excel Files (*.xlsx *.xls)"
        )
        if file_path:
            self._excel_file_input.setText(file_path)

    def _load_font(self):
        """加载外部字体文件"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择字体文件", str(Path.home() / "Desktop"),
            "Font Files (*.ttf *.otf *.ttc)"
        )
        if not file_path:
            return

        try:
            # 验证字体文件是否有效
            from PIL import ImageFont
            test_font = ImageFont.truetype(file_path, 12)

            # 获取字体名称
            font_name = Path(file_path).stem

            # 检查是否已存在
            existing_fonts = [self._font_family.itemText(i) for i in range(self._font_family.count())]
            if font_name in existing_fonts:
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(self, "提示", f"字体 '{font_name}' 已存在")
                return

            # 添加到字体列表
            self._font_family.addItem(font_name)
            self._font_family.setCurrentIndex(self._font_family.count() - 1)

            # 保存字体路径
            if not hasattr(self, '_custom_fonts'):
                self._custom_fonts = {}
            self._custom_fonts[font_name] = file_path

            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(self, "成功", f"字体 '{font_name}' 加载成功！")

        except Exception as e:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "错误", f"字体加载失败: {e}")

    def _pick_text_color(self):
        """选择文本颜色"""
        current_color = self._text_color_lbl.text()
        c = QColorDialog.getColor(QColor(current_color), self, "选择文字颜色")
        if c.isValid():
            color = c.name()
            self._refresh_text_color_btn(color)
            self._text_color_lbl.setText(color.upper())

    def _refresh_text_color_btn(self, color):
        """刷新颜色按钮样式"""
        self._text_color_btn.setStyleSheet(
            f"QPushButton{{background:{color};border:2px solid #5a5a6a;border-radius:6px;min-width:36px;min-height:24px;}}"
            f"QPushButton:hover{{border-color:#5b8af5;}}"
        )

    def _collect_current_layer_options(self):
        """收集当前编辑元素的参数"""
        layer = getattr(self, '_current_layer', None)
        if layer is None:
            return

        # 通用：添加方式
        if hasattr(self, '_combo_placement'):
            layer.placement = self._combo_placement.currentData()

        if layer.layer_type == 'text':
            if hasattr(self, '_combo_source'):
                layer.source = self._combo_source.currentData()
            if layer.source == 'fixed' and hasattr(self, '_text_content'):
                layer.text = self._text_content.text()
            elif layer.source == 'excel':
                if hasattr(self, '_excel_file_input'):
                    layer.excel_file = self._excel_file_input.text()
                if hasattr(self, '_excel_match_column'):
                    layer.match_column = self._excel_match_column.value()
                if hasattr(self, '_excel_data_column'):
                    layer.data_column = self._excel_data_column.value()
                if hasattr(self, '_excel_row_start'):
                    layer.excel_row_start = self._excel_row_start.value()
            if hasattr(self, '_font_family'):
                layer.font_family = self._font_family.currentText()
            if hasattr(self, '_font_size_pt'):
                layer.font_size_pt = self._font_size_pt.value()
            if hasattr(self, '_chk_bold'):
                layer.bold = self._chk_bold.isChecked()
            if hasattr(self, '_text_color_lbl'):
                layer.color = self._text_color_lbl.text()
            if hasattr(self, '_text_x_cm'):
                layer.x_cm = self._text_x_cm.value()
            if hasattr(self, '_text_y_cm'):
                layer.y_cm = self._text_y_cm.value()
        else:
            if hasattr(self, '_image_file_input'):
                layer.path = self._image_file_input.text()
            if hasattr(self, '_image_x_cm'):
                layer.x_cm = self._image_x_cm.value()
            if hasattr(self, '_image_y_cm'):
                layer.y_cm = self._image_y_cm.value()
            if hasattr(self, '_image_w_cm'):
                layer.w_cm = self._image_w_cm.value()
            if hasattr(self, '_image_h_cm'):
                layer.h_cm = self._image_h_cm.value()

    # ── 叠加层 UI 管理 ────────────────────────────────────────────────────────

    def _add_text_layer(self):
        """添加一个文字叠加层"""
        text_count = sum(1 for e in self._layers if e.layer_type == 'text')
        default_y = 17.0 - (text_count * 1.5)  # 依次向上排列，避免重叠
        layer = TextLayer(x_cm=1.0, y_cm=max(0.5, default_y))
        self._layers.append(layer)
        item = LayerListItem(layer, len(self._layers) - 1)
        self._list_widget.addItem(item)
        self._list_widget.setCurrentRow(self._list_widget.count() - 1)

    def _add_image_layer(self):
        """添加一个图片叠加层"""
        img_count = sum(1 for e in self._layers if e.layer_type == 'image')
        default_x = 26.0 - (img_count * 6.0)  # 依次向左排列
        layer = ImageLayer(x_cm=max(0.5, default_x), y_cm=0.5)
        self._layers.append(layer)
        item = LayerListItem(layer, len(self._layers) - 1)
        self._list_widget.addItem(item)
        self._list_widget.setCurrentRow(self._list_widget.count() - 1)

    def _delete_layer(self):
        """删除选中的元素"""
        row = self._list_widget.currentRow()
        if row < 0:
            return

        # 临时断开信号，避免 takeItem 触发 currentRowChanged
        self._list_widget.currentRowChanged.disconnect(self._on_layer_selected)

        self._list_widget.takeItem(row)
        self._layers.pop(row)

        # 更新索引
        for i in range(self._list_widget.count()):
            item = self._list_widget.item(i)
            item.index = i
            item.update_text()

        # 清除配置面板
        self._clear_config_pages()

        # 重新连接信号
        self._list_widget.currentRowChanged.connect(self._on_layer_selected)

        # 如果还有元素，自动选中并显示配置
        if self._list_widget.count() > 0:
            new_row = min(row, self._list_widget.count() - 1)
            # 先强制设为 -1，确保 setCurrentRow 一定能触发 currentRowChanged
            self._list_widget.setCurrentRow(-1)
            self._list_widget.setCurrentRow(new_row)
        else:
            self._config_stack.setCurrentIndex(0)

    def _rename_layer(self):
        """重命名选中元素的名称（| 后面的文字）"""
        row = self._list_widget.currentRow()
        if row < 0:
            return
        from PySide6.QtWidgets import QInputDialog
        current_name = self._layers[row].name
        new_name, ok = QInputDialog.getText(
            self, "重命名元素", "请输入新名称:",
            text=current_name
        )
        if ok:
            self._layers[row].name = new_name.strip()
            self._list_widget.item(row).update_text()

    def _move_layer_up(self):
        """上移选中的元素"""
        row = self._list_widget.currentRow()
        if row <= 0:
            return

        # 临时断开信号
        self._list_widget.currentRowChanged.disconnect(self._on_layer_selected)

        item = self._list_widget.takeItem(row)
        self._list_widget.insertItem(row - 1, item)
        self._layers[row], self._layers[row - 1] = self._layers[row - 1], self._layers[row]

        for i in range(self._list_widget.count()):
            self._list_widget.item(i).index = i
            self._list_widget.item(i).update_text()

        # 重新连接信号
        self._list_widget.currentRowChanged.connect(self._on_layer_selected)

        # 保持选中状态
        self._list_widget.setCurrentRow(row - 1)

    def _move_layer_down(self):
        """下移选中的元素"""
        row = self._list_widget.currentRow()
        if row < 0 or row >= self._list_widget.count() - 1:
            return

        # 临时断开信号
        self._list_widget.currentRowChanged.disconnect(self._on_layer_selected)

        item = self._list_widget.takeItem(row)
        self._list_widget.insertItem(row + 1, item)
        self._layers[row], self._layers[row + 1] = self._layers[row + 1], self._layers[row]

        for i in range(self._list_widget.count()):
            self._list_widget.item(i).index = i
            self._list_widget.item(i).update_text()

        # 重新连接信号
        self._list_widget.currentRowChanged.connect(self._on_layer_selected)

        # 保持选中状态
        self._list_widget.setCurrentRow(row + 1)

    def _on_layer_selected(self, row):
        """选中元素时显示配置面板"""
        if row < 0:
            return

        # 先收集当前编辑元素的参数
        self._collect_current_layer_options()

        layer = self._layers[row]

        # 清除旧配置页面
        self._clear_config_pages()

        # 创建新的配置页面
        try:
            if layer.layer_type == 'text':
                config_page = self._create_text_config_page(layer)
            else:
                config_page = self._create_image_config_page(layer)
        except Exception as e:
            err = QWidget()
            lay = QVBoxLayout(err)
            lbl = QLabel(f"配置面板加载失败: {e}")
            lbl.setWordWrap(True)
            lbl.setStyleSheet("color:#e88;")
            lay.addWidget(lbl)
            self._config_stack.addWidget(err)
            self._config_stack.setCurrentIndex(1)
            return

        self._config_stack.addWidget(config_page)
        self._config_stack.setCurrentIndex(1)

    def _clear_config_pages(self):
        """彻底清除所有配置页面（保留索引0的提示页面）"""
        attrs_to_clear = [
            '_current_layer', '_combo_source', '_combo_placement',
            '_widget_fixed_text', '_widget_excel', '_widget_filename_hint',
            '_text_content', '_excel_file_input', '_excel_match_column',
            '_excel_data_column', '_excel_row_start', '_font_family',
            '_font_size_pt', '_chk_bold', '_text_color_btn', '_text_color_lbl',
            '_text_x_cm', '_text_y_cm',
            '_image_file_input', '_image_x_cm', '_image_y_cm',
            '_image_w_cm', '_image_h_cm'
        ]
        for attr in attrs_to_clear:
            if hasattr(self, attr):
                delattr(self, attr)

        while self._config_stack.count() > 1:
            widget = self._config_stack.widget(1)
            self._config_stack.removeWidget(widget)
            widget.setParent(None)
            widget.deleteLater()

    def collect_raw_state(self) -> dict[str, Any]:
        # 先收集当前正在编辑的元素
        self._collect_current_layer_options()

        # 序列化所有叠加层
        layers_data = []
        for layer in self._layers:
            if layer.layer_type == 'text':
                layers_data.append({
                    'type': 'text',
                    'name': layer.name,
                    'placement': layer.placement,
                    'source': layer.source,
                    'text': layer.text,
                    'excel_file': layer.excel_file,
                    'excel_col': layer.excel_col,
                    'match_column': layer.match_column,
                    'data_column': layer.data_column,
                    'excel_row_start': layer.excel_row_start,
                    'font_family': layer.font_family,
                    'font_size_pt': layer.font_size_pt,
                    'bold': layer.bold,
                    'color': layer.color,
                    'x_cm': layer.x_cm,
                    'y_cm': layer.y_cm,
                })
            else:
                layers_data.append({
                    'type': 'image',
                    'name': layer.name,
                    'placement': layer.placement,
                    'path': layer.path,
                    'x_cm': layer.x_cm,
                    'y_cm': layer.y_cm,
                    'w_cm': layer.w_cm,
                    'h_cm': layer.h_cm,
                })

        # 收集自定义字体信息
        custom_fonts = {}
        if hasattr(self, '_custom_fonts'):
            custom_fonts = self._custom_fonts.copy()

        return {
            "format": self.combo_fmt.currentText(),
            "width_cm": self.spin_w.value(),
            "height_cm": self.spin_h.value(),
            "count_per_page": self.spin_count.value(),
            "max_cols": self.spin_max_cols.value(),
            "keep_ratio": self.chk_keep_ratio.isChecked(),
            "row_align": self.combo_row_align.currentData(),
            "sort_mode": self.combo_sort.currentData(),
            "group_by_folder": self.chk_group_by_folder.isChecked(),
            "excel_path": self.txt_excel.text(),
            "col_name": self.txt_col_name.text().upper(),
            "col_val": self.txt_col_val.text().upper(),
            "start_row": self.spin_start_row.value(),
            # 压缩
            "compress_enabled": self.chk_compress.isChecked(),
            "compress_target_kb": self.spin_target_kb.value(),
            "compress_format": self.combo_compress_fmt.currentText(),
            # 叠加层
            "overlay_layers": layers_data,
            "custom_fonts": custom_fonts,
        }

    def apply_state(self, options: dict | None):
        if self._list_widget is None: return
        fmt = options.get("format", "PPTX")
        idx = self.combo_fmt.findText(fmt)
        if idx >= 0: self.combo_fmt.setCurrentIndex(idx)

        self.spin_w.setValue(options.get("width_cm", 33))
        self.spin_h.setValue(options.get("height_cm", 19))
        self.spin_count.setValue(options.get("count_per_page", 1))
        self.spin_max_cols.setValue(options.get("max_cols", 4))
        self.chk_keep_ratio.setChecked(options.get("keep_ratio", True))
        align_idx = self.combo_row_align.findData(options.get("row_align", "left"))
        if align_idx >= 0:
            self.combo_row_align.setCurrentIndex(align_idx)

        group_by_folder = options.get("group_by_folder", False)
        self.chk_group_by_folder.setChecked(group_by_folder)
        mode = options.get("sort_mode", "folder_filename" if group_by_folder else "default")
        midx = self.combo_sort.findData(mode)
        if midx >= 0: self.combo_sort.setCurrentIndex(midx)

        self.txt_excel.setText(options.get("excel_path", ""))
        self.txt_col_name.setText(options.get("col_name", "A"))
        self.txt_col_val.setText(options.get("col_val", "B"))
        self.spin_start_row.setValue(options.get("start_row", 2))

        # 压缩
        self.chk_compress.setChecked(options.get("compress_enabled", False))
        self.spin_target_kb.setValue(options.get("compress_target_kb", 500))
        ci = self.combo_compress_fmt.findText(options.get("compress_format", "JPEG"))
        if ci >= 0: self.combo_compress_fmt.setCurrentIndex(ci)

        # 叠加层
        # 先收集当前编辑元素的参数
        self._collect_current_layer_options()

        # 清除配置页面，显示提示页面
        self._clear_config_pages()
        self._config_stack.setCurrentIndex(0)

        # 清除现有元素
        self._layers.clear()
        self._list_widget.clear()

        # 加载元素
        layers_data = options.get("overlay_layers", [])
        for data in layers_data:
            if data['type'] == 'text':
                layer = TextLayer(
                    x_cm=data.get('x_cm', 1.0),
                    y_cm=data.get('y_cm', 17.0),
                    source=data.get('source', 'fixed'),
                    text=data.get('text', ''),
                    name=data.get('name', ''),
                    excel_file=data.get('excel_file', ''),
                    excel_col=data.get('excel_col', 'C'),
                    match_column=data.get('match_column', 1),
                    data_column=data.get('data_column', 2),
                    excel_row_start=data.get('excel_row_start', 2),
                    font_family=data.get('font_family', 'Microsoft YaHei'),
                    font_size_pt=data.get('font_size_pt', 12),
                    bold=data.get('bold', False),
                    color=data.get('color', '#000000'),
                    placement=data.get('placement', 'overlay'),
                )
            else:
                layer = ImageLayer(
                    x_cm=data.get('x_cm', 26.0),
                    y_cm=data.get('y_cm', 0.5),
                    path=data.get('path', ''),
                    name=data.get('name', ''),
                    w_cm=data.get('w_cm', 5.0),
                    h_cm=data.get('h_cm', 3.0),
                    placement=data.get('placement', 'overlay'),
                )

            self._layers.append(layer)
            item = LayerListItem(layer, len(self._layers) - 1)
            self._list_widget.addItem(item)

        # 恢复自定义字体
        custom_fonts = options.get('custom_fonts', {})
        if custom_fonts:
            self._custom_fonts = custom_fonts


    def show_validation_errors(self, errors) -> None:
        msgs = list(errors or [])
        if not msgs:
            return
        QMessageBox.warning(self, "参数错误", "\n".join(str(e) for e in msgs))
