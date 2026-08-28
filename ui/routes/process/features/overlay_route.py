"""OverlayFeatureRoute —— 图片叠加参数面板（只读写控件）。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from PIL import ImageFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QLabel, QSpinBox, QComboBox, QPushButton, QColorDialog,
    QListWidget, QListWidgetItem, QLineEdit, QCheckBox,
    QFileDialog, QDoubleSpinBox, QTextEdit, QMessageBox,
    QStackedWidget,
)
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QFont

from core.processors.overlay_processor import (
    OverlayElement, TextElement, ImageElement, default_overlay_options,
)
import config


class ElementListItem(QListWidgetItem):
    """元素列表项"""
    def __init__(self, element, index):
        super().__init__()
        self.element = element
        self.index = index
        self.update_text()

    def update_text(self):
        label = self.element.name if self.element.name else ""
        if self.element.element_type == "text":
            self.setText(f"📝 文本 {self.index + 1} | {label}")
        else:
            self.setText(f"🖼️ 图片 {self.index + 1} | {label}")


class GridPositionWidget(QWidget):
    """3x3 宫格定位器：自由模式写入坐标，锚定模式保存动态布局规则。"""

    CELLS = [
        ("↖", 0.0, 0.0, "top_left"), ("↑", 0.5, 0.0, "top_center"),
        ("↗", 1.0, 0.0, "top_right"), ("←", 0.0, 0.5, "center_left"),
        ("⊙", 0.5, 0.5, "center"), ("→", 1.0, 0.5, "center_right"),
        ("↙", 0.0, 1.0, "bottom_left"), ("↓", 0.5, 1.0, "bottom_center"),
        ("↘", 1.0, 1.0, "bottom_right"),
    ]

    def __init__(self, x_spin, y_spin, size_provider, element_type="text",
                 overlay_w_spin=None, overlay_h_spin=None, anchor="center",
                 anchor_changed=None, anchor_mode=None, parent=None):
        super().__init__(parent)
        self._x_spin = x_spin
        self._y_spin = y_spin
        self._size_provider = size_provider  # callable -> (w,h)
        self._element_type = element_type
        self._overlay_w_spin = overlay_w_spin
        self._overlay_h_spin = overlay_h_spin
        self._anchor_changed = anchor_changed
        self._anchor_mode = anchor_mode
        self._hovered_cell = -1
        self._selected_cell = next(
            (i for i, cell in enumerate(self.CELLS) if cell[3] == anchor), 4
        )
        self.setFixedSize(90, 90)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(
            "点击格点自动计算并填充 X/Y 坐标\n"
            "基于左侧列表选中图片的尺寸定位"
        )

    def _get_base_size(self):
        try:
            return self._size_provider()
        except Exception:
            return 1920, 1080


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
        _, x_ratio, y_ratio, anchor = self.CELLS[cell_idx]
        self._selected_cell = cell_idx
        if self._anchor_changed:
            self._anchor_changed(anchor)
        if self._anchor_mode and self._anchor_mode():
            self.update()
            return
        bw, bh = self._get_base_size()
        if self._element_type == "image" and self._overlay_w_spin and self._overlay_h_spin:
            ow = self._overlay_w_spin.value()
            oh = self._overlay_h_spin.value()
            x = int((bw - ow) * x_ratio)
            y = int((bh - oh) * y_ratio)
        else:
            x = int(bw * x_ratio)
            y = int(bh * y_ratio)
        self._x_spin.setValue(max(0, x))
        self._y_spin.setValue(max(0, y))
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        cw, ch = w / 3.0, h / 3.0
        p.fillRect(self.rect(), QColor(22, 22, 40, 180))
        for i, (sym, _, _, _) in enumerate(self.CELLS):
            r, c = divmod(i, 3)
            rect = QRectF(c * cw + 1, r * ch + 1, cw - 2, ch - 2)
            if i == self._selected_cell:
                p.setBrush(QBrush(QColor(91, 138, 245, 130)))
                p.setPen(QPen(QColor(124, 108, 245, 230), 2))
            elif i == self._hovered_cell:
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


class OverlayFeatureRoute(QWidget):
    """图片叠加 FeatureRoute。"""

    feature_id = "image_overlay"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._elements: list = []
        self._list_widget: QListWidget | None = None
        self._config_stack: QStackedWidget | None = None
        self._font_cache: dict = {}
        self._custom_fonts: dict = {}
        self._base_img_width = 1920
        self._base_img_height = 1080
        self._has_base_image = False
        self._current_element = None
        self._build_widget()

    def build_widget(self, parent=None) -> QWidget:
        if parent is not None and self.parent() is None:
            self.setParent(parent)
        return self

    def set_base_image_size(self, w: int, h: int):
        if w > 0 and h > 0:
            self._base_img_width = w
            self._base_img_height = h
            self._has_base_image = True

    def _base_size(self):
        return self._base_img_width, self._base_img_height

    def _build_widget(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        # ── 元素列表 + 配置（左右分栏）──
        grp_elements = QGroupBox("叠加元素")
        elem_layout = QHBoxLayout(grp_elements)
        elem_layout.setSpacing(10)

        # ── 左侧：元素列表 + emoji按钮 ──
        left_panel = QWidget()
        left_panel.setMaximumWidth(280)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)

        self._list_widget = QListWidget()
        self._list_widget.setMinimumHeight(140)
        self._list_widget.currentRowChanged.connect(self._on_element_selected)
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

        btn_add_text = _make_icon_btn("📝", "添加文本", self._add_text_element)
        btn_layout.addWidget(btn_add_text)

        btn_add_image = _make_icon_btn("🖼", "添加图片", self._add_image_element)
        btn_layout.addWidget(btn_add_image)

        btn_delete = _make_icon_btn("🗑", "删除选中", self._delete_element)
        btn_layout.addWidget(btn_delete)

        btn_layout.addSpacing(4)

        btn_rename = _make_icon_btn("✏", "重命名", self._rename_element)
        btn_layout.addWidget(btn_rename)

        btn_layout.addSpacing(6)

        btn_move_up = _make_icon_btn("⬆", "上移", self._move_element_up)
        btn_layout.addWidget(btn_move_up)

        btn_move_down = _make_icon_btn("⬇", "下移", self._move_element_down)
        btn_layout.addWidget(btn_move_down)

        btn_layout.addStretch()
        left_layout.addLayout(btn_layout)

        elem_layout.addWidget(left_panel, stretch=2)

        # ── 右侧：元素配置面板 ──
        grp_config = QGroupBox("元素配置")
        config_root = QVBoxLayout(grp_config)
        config_root.setContentsMargins(8, 8, 8, 8)

        # 使用 QStackedWidget 管理不同的配置面板
        self._config_stack = QStackedWidget()

        # 默认显示提示页面
        hint_page = QWidget()
        hint_layout = QVBoxLayout(hint_page)
        hint_label = QLabel("请先添加元素，然后在左侧列表中选择一个元素进行配置")
        hint_label.setStyleSheet("color:#666e88;font-size:12px;font-style:italic;")
        hint_label.setAlignment(Qt.AlignCenter)
        hint_layout.addWidget(hint_label)
        self._config_stack.addWidget(hint_page)

        config_root.addWidget(self._config_stack)
        elem_layout.addWidget(grp_config, stretch=5)

        root.addWidget(grp_elements)

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

    def _add_text_element(self):
        """添加文本元素"""
        # 根据已有元素数量计算默认位置，避免重叠
        text_count = sum(1 for e in self._elements if e.element_type == 'text')
        default_y = 50 + (text_count * 50)  # 依次向下排列，避免重叠
        element = TextElement(x=50, y=default_y, layout_mode="anchor", anchor="center")
        self._elements.append(element)
        item = ElementListItem(element, len(self._elements) - 1)
        self._list_widget.addItem(item)
        self._list_widget.setCurrentRow(self._list_widget.count() - 1)

    def _add_image_element(self):
        """添加图片元素"""
        # 根据已有元素数量计算默认位置，避免重叠
        img_count = sum(1 for e in self._elements if e.element_type == 'image')
        default_y = 100 + (img_count * 250)  # 依次向下排列
        element = ImageElement(x=100, y=default_y, layout_mode="anchor", anchor="center")
        self._elements.append(element)
        item = ElementListItem(element, len(self._elements) - 1)
        self._list_widget.addItem(item)
        self._list_widget.setCurrentRow(self._list_widget.count() - 1)

    def _delete_element(self):
        """删除选中的元素"""
        row = self._list_widget.currentRow()
        if row < 0:
            return
        self._list_widget.takeItem(row)
        self._elements.pop(row)
        # 更新索引
        for i in range(self._list_widget.count()):
            item = self._list_widget.item(i)
            item.index = i
            item.update_text()

    def _rename_element(self):
        """重命名选中元素的名称（| 后面的文字）"""
        row = self._list_widget.currentRow()
        if row < 0:
            return
        from PySide6.QtWidgets import QInputDialog
        current_name = self._elements[row].name
        new_name, ok = QInputDialog.getText(
            self, "重命名元素", "请输入新名称:",
            text=current_name
        )
        if ok:
            self._elements[row].name = new_name.strip()
            self._list_widget.item(row).update_text()

    def _move_element_up(self):
        """上移选中的元素"""
        row = self._list_widget.currentRow()
        if row <= 0:
            return
        # 交换列表项
        item = self._list_widget.takeItem(row)
        self._list_widget.insertItem(row - 1, item)
        self._list_widget.setCurrentRow(row - 1)
        # 交换元素
        self._elements[row], self._elements[row - 1] = self._elements[row - 1], self._elements[row]
        # 更新索引
        for i in range(self._list_widget.count()):
            self._list_widget.item(i).index = i
            self._list_widget.item(i).update_text()

    def _move_element_down(self):
        """下移选中的元素"""
        row = self._list_widget.currentRow()
        if row < 0 or row >= self._list_widget.count() - 1:
            return
        # 交换列表项
        item = self._list_widget.takeItem(row)
        self._list_widget.insertItem(row + 1, item)
        self._list_widget.setCurrentRow(row + 1)
        # 交换元素
        self._elements[row], self._elements[row + 1] = self._elements[row + 1], self._elements[row]
        # 更新索引
        for i in range(self._list_widget.count()):
            self._list_widget.item(i).index = i
            self._list_widget.item(i).update_text()

    def _on_element_selected(self, row):
        """选中元素时显示配置面板"""
        if row < 0:
            return

        # 先收集当前编辑元素的参数
        self._collect_current_element_options()

        element = self._elements[row]

        # 彻底清除旧配置页面（保留索引0的提示页面）
        self._clear_config_pages()

        # 创建新的配置页面
        try:
            if element.element_type == 'text':
                config_page = self._create_text_config_page(element)
            else:
                config_page = self._create_image_config_page(element)
        except Exception as e:
            # 避免静默失败只留下“请选择元素”提示页
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
        # 清除实例变量引用
        attrs_to_clear = [
            '_current_element', '_combo_source', '_widget_fixed_text', 
            '_widget_excel', '_widget_filename_hint', '_text_content',
            '_excel_file_input', '_excel_match_column', '_excel_data_column',
            '_excel_row_start',
            '_font_family', '_font_size', '_chk_bold',
            '_text_color_btn', '_text_color_lbl', '_text_x', '_text_y',
            '_text_layout_mode', '_text_margin', '_text_offset_x', '_text_offset_y',
            '_text_box_width', '_text_box_width_unit', '_text_box_height', '_text_auto_wrap',
            '_text_h_align', '_text_v_align', '_text_keep_inside',
            '_text_auto_shrink', '_text_min_font_size',
            '_image_file_input', '_image_x', '_image_y',
            '_image_width', '_image_height', '_image_layout_mode',
            '_image_margin', '_image_offset_x', '_image_offset_y',
            '_image_keep_inside', '_image_shrink_to_fit',
            '_image_keep_aspect'
        ]
        for attr in attrs_to_clear:
            if hasattr(self, attr):
                delattr(self, attr)
        
        # 移除并删除所有配置页面（保留索引0）
        while self._config_stack.count() > 1:
            widget = self._config_stack.widget(1)
            self._config_stack.removeWidget(widget)
            widget.setParent(None)  # 断开父子关系
            widget.deleteLater()

    def _create_text_config_page(self, element: TextElement) -> QWidget:
        """创建文本元素配置页面"""
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(8)
        # 数据源选择
        src_layout = QHBoxLayout()
        src_layout.addWidget(QLabel("数据来源:"))
        combo_source = QComboBox()
        combo_source.addItem("固定文本", "fixed")
        combo_source.addItem("Excel列数据", "excel")
        combo_source.addItem("图片文件名称", "filename")
        combo_source.setCurrentIndex(combo_source.findData(element.source))
        combo_source.setStyleSheet(config.COMBOBOX_STYLE)
        src_layout.addWidget(combo_source)
        src_layout.addStretch()
        page_layout.addLayout(src_layout)

        # 固定文本内容
        self._widget_fixed_text = QWidget()
        fixed_layout = QVBoxLayout(self._widget_fixed_text)
        fixed_layout.setContentsMargins(0, 0, 0, 0)
        fixed_layout.addWidget(QLabel("文本内容:"))
        self._text_content = QTextEdit()
        self._text_content.setPlainText(element.content)
        self._text_content.setMaximumHeight(80)
        fixed_layout.addWidget(self._text_content)
        page_layout.addWidget(self._widget_fixed_text)

        # Excel配置
        self._widget_excel = QWidget()
        excel_layout = QVBoxLayout(self._widget_excel)
        excel_layout.setContentsMargins(0, 0, 0, 0)
        
        # Excel文件选择
        file_layout = QHBoxLayout()
        file_layout.addWidget(QLabel("Excel文件:"))
        self._excel_file_input = QLineEdit()
        self._excel_file_input.setText(element.excel_file)
        self._excel_file_input.setPlaceholderText("选择Excel文件...")
        file_layout.addWidget(self._excel_file_input)
        btn_browse_excel = QPushButton("浏览")
        btn_browse_excel.clicked.connect(self._browse_excel)
        file_layout.addWidget(btn_browse_excel)
        excel_layout.addLayout(file_layout)
        
        # 匹配列和数据列
        excel_col_layout = QHBoxLayout()
        excel_col_layout.addWidget(QLabel("匹配列:"))
        self._excel_match_column = QSpinBox()
        self._excel_match_column.setRange(1, 100)
        self._excel_match_column.setValue(element.match_column if element.match_column > 0 else 1)
        self._excel_match_column.setToolTip("图片文件名所在的列号（从1开始），用于匹配对应行")
        excel_col_layout.addWidget(self._excel_match_column)
        
        excel_col_layout.addWidget(QLabel("数据列:"))
        self._excel_data_column = QSpinBox()
        self._excel_data_column.setRange(1, 100)
        self._excel_data_column.setValue(element.data_column if element.data_column > 0 else 2)
        self._excel_data_column.setToolTip("要读取的文本数据列号（从1开始）")
        excel_col_layout.addWidget(self._excel_data_column)
        excel_col_layout.addStretch()
        excel_layout.addLayout(excel_col_layout)
        
        # 起始行
        excel_row_layout = QHBoxLayout()
        excel_row_layout.addWidget(QLabel("数据起始行:"))
        self._excel_row_start = QSpinBox()
        self._excel_row_start.setRange(1, 10000)
        self._excel_row_start.setValue(element.excel_row_start)
        self._excel_row_start.setToolTip("数据从第几行开始（跳过表头）")
        excel_row_layout.addWidget(self._excel_row_start)
        excel_row_layout.addStretch()
        excel_layout.addLayout(excel_row_layout)
        page_layout.addWidget(self._widget_excel)
        
        # Excel使用说明
        excel_hint = QLabel("💡 匹配列：图片文件名所在列 | 数据列：要叠加的文本内容所在列")
        excel_hint.setStyleSheet("color:#666e88;font-size:11px;font-style:italic;")
        excel_hint.setWordWrap(True)
        excel_layout.addWidget(excel_hint)

        # 图片名提示
        self._widget_filename_hint = QLabel("💡 将使用当前处理图片的文件名（不含扩展名）作为文本内容")
        self._widget_filename_hint.setStyleSheet("color:#666e88;font-size:11px;font-style:italic;")
        page_layout.addWidget(self._widget_filename_hint)

        # 根据数据源显示/隐藏对应面板
        def _on_source_change(idx):
            source = combo_source.itemData(idx)
            self._widget_fixed_text.setVisible(source == 'fixed')
            self._widget_excel.setVisible(source == 'excel')
            self._widget_filename_hint.setVisible(source == 'filename')
            element.source = source
        
        combo_source.currentIndexChanged.connect(_on_source_change)
        _on_source_change(combo_source.currentIndex())

        # 字体设置
        font_layout = QHBoxLayout()
        font_layout.addWidget(QLabel("字体:"))
        self._font_family = QComboBox()
        # 常用字体列表（中文字体优先）
        fonts = ['Microsoft YaHei', 'SimHei', 'SimSun', 'KaiTi', 'FangSong', 'Arial', 'Times New Roman']
        self._font_family.addItems(fonts)
        for font_name in self._custom_fonts:
            if self._font_family.findText(font_name) < 0:
                self._font_family.addItem(font_name)
        idx = self._font_family.findText(element.font_family)
        if idx >= 0:
            self._font_family.setCurrentIndex(idx)
        else:
            # 默认使用微软雅黑
            idx = self._font_family.findText('Microsoft YaHei')
            if idx >= 0:
                self._font_family.setCurrentIndex(idx)
        
        # 添加加载字体按钮
        btn_load_font = QPushButton("加载字体")
        btn_load_font.clicked.connect(self._load_font)
        font_layout.addWidget(btn_load_font)
        
        self._font_family.setStyleSheet(config.COMBOBOX_STYLE)
        font_layout.addWidget(self._font_family)
        
        font_layout.addWidget(QLabel("大小:"))
        self._font_size = QSpinBox()
        self._font_size.setRange(8, 200)
        self._font_size.setValue(element.font_size)
        font_layout.addWidget(self._font_size)
        
        self._chk_bold = QCheckBox("加粗")
        self._chk_bold.setChecked(element.bold)
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
        self._text_color_lbl = QLabel(element.color)
        self._text_color_lbl.setStyleSheet("color:#b0b8c8;font-size:12px;background:transparent;")
        color_layout.addWidget(self._text_color_lbl)
        color_layout.addStretch()
        page_layout.addLayout(color_layout)
        self._refresh_text_color_btn(element.color)

        # 文本框布局
        layout_row = QHBoxLayout()
        layout_row.addWidget(QLabel("定位方式:"))
        self._text_layout_mode = QComboBox()
        self._text_layout_mode.addItem("自由坐标", "free")
        self._text_layout_mode.addItem("九宫格锚定", "anchor")
        self._text_layout_mode.setCurrentIndex(
            max(0, self._text_layout_mode.findData(element.layout_mode))
        )
        self._text_layout_mode.setStyleSheet(config.COMBOBOX_STYLE)
        layout_row.addWidget(self._text_layout_mode)
        layout_row.addWidget(QLabel("边距(px):"))
        self._text_margin = QSpinBox()
        self._text_margin.setRange(0, 99999)
        self._text_margin.setValue(element.margin)
        layout_row.addWidget(self._text_margin)
        self._text_keep_inside = QCheckBox("限制在画布内")
        self._text_keep_inside.setChecked(element.keep_inside)
        layout_row.addWidget(self._text_keep_inside)
        layout_row.addStretch()
        page_layout.addLayout(layout_row)

        box_row = QHBoxLayout()
        box_row.addWidget(QLabel("文本框宽度:"))
        self._text_box_width = QSpinBox()
        self._text_box_width.setRange(1, 99999)
        self._text_box_width.setValue(element.box_width)
        box_row.addWidget(self._text_box_width)
        self._text_box_width_unit = QComboBox()
        self._text_box_width_unit.addItem("画布百分比", "percent")
        self._text_box_width_unit.addItem("像素", "px")
        self._text_box_width_unit.setCurrentIndex(
            max(0, self._text_box_width_unit.findData(element.box_width_unit))
        )
        self._text_box_width_unit.setStyleSheet(config.COMBOBOX_STYLE)
        box_row.addWidget(self._text_box_width_unit)
        box_row.addWidget(QLabel("高度(px):"))
        self._text_box_height = QSpinBox()
        self._text_box_height.setRange(0, 99999)
        self._text_box_height.setValue(element.box_height)
        self._text_box_height.setSpecialValueText("自动")
        box_row.addWidget(self._text_box_height)
        self._text_auto_wrap = QCheckBox("自动换行")
        self._text_auto_wrap.setChecked(element.auto_wrap)
        box_row.addWidget(self._text_auto_wrap)
        box_row.addStretch()
        page_layout.addLayout(box_row)

        align_row = QHBoxLayout()
        align_row.addWidget(QLabel("水平对齐:"))
        self._text_h_align = QComboBox()
        for label, value in (("左对齐", "left"), ("居中", "center"), ("右对齐", "right")):
            self._text_h_align.addItem(label, value)
        self._text_h_align.setCurrentIndex(max(0, self._text_h_align.findData(element.h_align)))
        self._text_h_align.setStyleSheet(config.COMBOBOX_STYLE)
        align_row.addWidget(self._text_h_align)
        align_row.addWidget(QLabel("垂直对齐:"))
        self._text_v_align = QComboBox()
        for label, value in (("顶部", "top"), ("居中", "center"), ("底部", "bottom")):
            self._text_v_align.addItem(label, value)
        self._text_v_align.setCurrentIndex(max(0, self._text_v_align.findData(element.v_align)))
        self._text_v_align.setStyleSheet(config.COMBOBOX_STYLE)
        align_row.addWidget(self._text_v_align)
        self._text_auto_shrink = QCheckBox("溢出时缩小字号")
        self._text_auto_shrink.setChecked(element.auto_shrink)
        align_row.addWidget(self._text_auto_shrink)
        align_row.addWidget(QLabel("最小字号:"))
        self._text_min_font_size = QSpinBox()
        self._text_min_font_size.setRange(8, 200)
        self._text_min_font_size.setValue(element.min_font_size)
        align_row.addWidget(self._text_min_font_size)
        align_row.addStretch()
        page_layout.addLayout(align_row)

        pos_layout = QHBoxLayout()
        self._text_x = QSpinBox()
        self._text_x.setRange(0, 99999)
        self._text_x.setValue(element.x)
        self._text_y = QSpinBox()
        self._text_y.setRange(0, 99999)
        self._text_y.setValue(element.y)
        self._text_x_label = QLabel("X坐标(px):")
        self._text_y_label = QLabel("Y坐标(px):")
        self._text_offset_x = QSpinBox()
        self._text_offset_x.setRange(-99999, 99999)
        self._text_offset_x.setValue(element.offset_x)
        self._text_offset_x.setSuffix(" px")
        self._text_offset_x.setToolTip("正值向右，负值向左")
        self._text_offset_y = QSpinBox()
        self._text_offset_y.setRange(-99999, 99999)
        self._text_offset_y.setValue(element.offset_y)
        self._text_offset_y.setSuffix(" px")
        self._text_offset_y.setToolTip("正值向下，负值向上")
        self._text_offset_x_label = QLabel("水平偏移:")
        self._text_offset_y_label = QLabel("垂直偏移:")
        pos_layout.addWidget(self._text_x_label)
        pos_layout.addWidget(self._text_x)
        pos_layout.addWidget(self._text_y_label)
        pos_layout.addWidget(self._text_y)
        pos_layout.addWidget(self._text_offset_x_label)
        pos_layout.addWidget(self._text_offset_x)
        pos_layout.addWidget(self._text_offset_y_label)
        pos_layout.addWidget(self._text_offset_y)
        pos_layout.addSpacing(8)
        grid = GridPositionWidget(
            x_spin=self._text_x,
            y_spin=self._text_y,
            size_provider=self._base_size,
            element_type='text',
            anchor=element.anchor,
            anchor_changed=lambda value: setattr(element, 'anchor', value),
            anchor_mode=lambda: self._text_layout_mode.currentData() == 'anchor',
        )
        pos_layout.addWidget(grid)
        pos_layout.addStretch()
        page_layout.addLayout(pos_layout)

        def _update_text_layout_controls():
            anchored = self._text_layout_mode.currentData() == 'anchor'
            for control in (
                self._text_x_label, self._text_x, self._text_y_label, self._text_y,
            ):
                control.setVisible(not anchored)
            for control in (
                self._text_offset_x_label, self._text_offset_x,
                self._text_offset_y_label, self._text_offset_y,
            ):
                control.setVisible(anchored)
            for control in (
                self._text_margin, self._text_box_width, self._text_box_width_unit,
                self._text_box_height, self._text_auto_wrap, self._text_h_align,
                self._text_v_align, self._text_keep_inside, self._text_auto_shrink,
                self._text_min_font_size,
            ):
                control.setEnabled(anchored)
        self._text_layout_mode.currentIndexChanged.connect(_update_text_layout_controls)
        _update_text_layout_controls()

        # 保存引用用于收集参数
        self._current_element = element
        self._combo_source = combo_source
        
        return page

    def _create_image_config_page(self, element: ImageElement) -> QWidget:
        """创建图片元素配置页面"""
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(8)
        
        # 图片文件选择
        file_layout = QHBoxLayout()
        file_layout.addWidget(QLabel("图片文件:"))
        self._image_file_input = QLineEdit()
        self._image_file_input.setText(element.image_path)
        self._image_file_input.setPlaceholderText("选择要叠加的图片...")
        file_layout.addWidget(self._image_file_input)
        btn_browse = QPushButton("浏览")
        btn_browse.clicked.connect(self._browse_image)
        file_layout.addWidget(btn_browse)
        page_layout.addLayout(file_layout)

        # 先创建坐标和尺寸 SpinBox（供后续宫格引用）
        self._image_x = QSpinBox()
        self._image_x.setRange(0, 99999)
        self._image_x.setValue(element.x)
        self._image_x.setToolTip("相对于底图左上角的X坐标（像素值）")

        self._image_y = QSpinBox()
        self._image_y.setRange(0, 99999)
        self._image_y.setValue(element.y)
        self._image_y.setToolTip("相对于底图左上角的Y坐标（像素值）")

        self._image_width = QSpinBox()
        self._image_width.setRange(1, 99999)
        self._image_width.setValue(element.width)
        self._image_width.setToolTip("叠加图片的宽度（像素值）")

        self._image_height = QSpinBox()
        self._image_height.setRange(1, 99999)
        self._image_height.setValue(element.height)
        self._image_height.setToolTip("叠加图片的高度（像素值）")

        layout_row = QHBoxLayout()
        layout_row.addWidget(QLabel("定位方式:"))
        self._image_layout_mode = QComboBox()
        self._image_layout_mode.addItem("自由坐标", "free")
        self._image_layout_mode.addItem("九宫格锚定", "anchor")
        self._image_layout_mode.setCurrentIndex(
            max(0, self._image_layout_mode.findData(element.layout_mode))
        )
        self._image_layout_mode.setStyleSheet(config.COMBOBOX_STYLE)
        layout_row.addWidget(self._image_layout_mode)
        layout_row.addWidget(QLabel("边距(px):"))
        self._image_margin = QSpinBox()
        self._image_margin.setRange(0, 99999)
        self._image_margin.setValue(element.margin)
        layout_row.addWidget(self._image_margin)
        self._image_keep_inside = QCheckBox("限制在画布内")
        self._image_keep_inside.setChecked(element.keep_inside)
        layout_row.addWidget(self._image_keep_inside)
        layout_row.addStretch()
        page_layout.addLayout(layout_row)

        pos_layout = QHBoxLayout()
        self._image_x_label = QLabel("X坐标(px):")
        self._image_y_label = QLabel("Y坐标(px):")
        self._image_offset_x = QSpinBox()
        self._image_offset_x.setRange(-99999, 99999)
        self._image_offset_x.setValue(element.offset_x)
        self._image_offset_x.setSuffix(" px")
        self._image_offset_x.setToolTip("正值向右，负值向左")
        self._image_offset_y = QSpinBox()
        self._image_offset_y.setRange(-99999, 99999)
        self._image_offset_y.setValue(element.offset_y)
        self._image_offset_y.setSuffix(" px")
        self._image_offset_y.setToolTip("正值向下，负值向上")
        self._image_offset_x_label = QLabel("水平偏移:")
        self._image_offset_y_label = QLabel("垂直偏移:")
        pos_layout.addWidget(self._image_x_label)
        pos_layout.addWidget(self._image_x)
        pos_layout.addWidget(self._image_y_label)
        pos_layout.addWidget(self._image_y)
        pos_layout.addWidget(self._image_offset_x_label)
        pos_layout.addWidget(self._image_offset_x)
        pos_layout.addWidget(self._image_offset_y_label)
        pos_layout.addWidget(self._image_offset_y)
        pos_layout.addSpacing(8)
        grid = GridPositionWidget(
            x_spin=self._image_x,
            y_spin=self._image_y,
            size_provider=self._base_size,
            element_type='image',
            overlay_w_spin=self._image_width,
            overlay_h_spin=self._image_height,
            anchor=element.anchor,
            anchor_changed=lambda value: setattr(element, 'anchor', value),
            anchor_mode=lambda: self._image_layout_mode.currentData() == 'anchor',
        )
        pos_layout.addWidget(grid)
        pos_layout.addStretch()
        page_layout.addLayout(pos_layout)

        size_layout = QHBoxLayout()
        size_layout.addWidget(QLabel("宽度(px):"))
        size_layout.addWidget(self._image_width)
        size_layout.addWidget(QLabel("高度(px):"))
        size_layout.addWidget(self._image_height)
        self._image_keep_aspect = QCheckBox("保持比例")
        self._image_keep_aspect.setChecked(element.keep_aspect)
        size_layout.addWidget(self._image_keep_aspect)
        self._image_shrink_to_fit = QCheckBox("超出画布自动缩小")
        self._image_shrink_to_fit.setChecked(element.shrink_to_fit)
        size_layout.addWidget(self._image_shrink_to_fit)
        size_layout.addStretch()
        page_layout.addLayout(size_layout)

        def _update_image_layout_controls():
            anchored = self._image_layout_mode.currentData() == 'anchor'
            for control in (
                self._image_x_label, self._image_x, self._image_y_label, self._image_y,
            ):
                control.setVisible(not anchored)
            for control in (
                self._image_offset_x_label, self._image_offset_x,
                self._image_offset_y_label, self._image_offset_y,
            ):
                control.setVisible(anchored)
            for control in (
                self._image_margin, self._image_keep_inside,
                self._image_keep_aspect, self._image_shrink_to_fit,
            ):
                control.setEnabled(anchored)
        self._image_layout_mode.currentIndexChanged.connect(_update_image_layout_controls)
        _update_image_layout_controls()

        self._current_element = element
        
        return page

    def _browse_excel(self):
        """浏览选择Excel文件"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择Excel文件", str(Path.home() / "Desktop"), "Excel Files (*.xlsx *.xls)"
        )
        if file_path:
            self._excel_file_input.setText(file_path)

    def _browse_image(self):
        """浏览选择图片文件"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择图片文件", str(Path.home() / "Desktop"),
            "Image Files (*.png *.jpg *.jpeg *.webp *.bmp)"
        )
        if file_path:
            self._image_file_input.setText(file_path)

    def _pick_text_color(self):
        """选择文本颜色"""
        current_color = self._text_color_lbl.text()
        c = QColorDialog.getColor(QColor(current_color), self, "选择文字颜色",
                                  QColorDialog.ShowAlphaChannel)
        if c.isValid():
            color = c.name(QColor.HexArgb) if c.alpha() < 255 else c.name()
            self._refresh_text_color_btn(color)
            self._text_color_lbl.setText(color.upper())

    def _refresh_text_color_btn(self, color):
        """刷新颜色按钮样式"""
        self._text_color_btn.setStyleSheet(
            f"QPushButton{{background:{color};border:2px solid #5a5a6a;border-radius:6px;min-width:36px;min-height:24px;}}"
            f"QPushButton:hover{{border-color:#5b8af5;}}"
        )

    def _collect_current_element_options(self):
        """收集当前编辑元素的参数"""
        element = getattr(self, '_current_element', None)
        if element is None:
            return
        
        if element.element_type == 'text':
            if hasattr(self, '_combo_source'):
                element.source = self._combo_source.currentData()
            if element.source == 'fixed' and hasattr(self, '_text_content'):
                element.content = self._text_content.toPlainText()
            elif element.source == 'excel':
                if hasattr(self, '_excel_file_input'):
                    element.excel_file = self._excel_file_input.text()
                if hasattr(self, '_excel_match_column'):
                    element.match_column = self._excel_match_column.value()
                if hasattr(self, '_excel_data_column'):
                    element.data_column = self._excel_data_column.value()
                if hasattr(self, '_excel_row_start'):
                    element.excel_row_start = self._excel_row_start.value()
            if hasattr(self, '_font_family'):
                element.font_family = self._font_family.currentText()
            if hasattr(self, '_font_size'):
                element.font_size = self._font_size.value()
            if hasattr(self, '_chk_bold'):
                element.bold = self._chk_bold.isChecked()
            if hasattr(self, '_text_color_lbl'):
                element.color = self._text_color_lbl.text()
            if hasattr(self, '_text_x'):
                element.x = self._text_x.value()
            if hasattr(self, '_text_y'):
                element.y = self._text_y.value()
            if hasattr(self, '_text_layout_mode'):
                element.layout_mode = self._text_layout_mode.currentData()
                element.margin = self._text_margin.value()
                element.offset_x = self._text_offset_x.value()
                element.offset_y = self._text_offset_y.value()
                element.box_width = self._text_box_width.value()
                element.box_width_unit = self._text_box_width_unit.currentData()
                element.box_height = self._text_box_height.value()
                element.auto_wrap = self._text_auto_wrap.isChecked()
                element.h_align = self._text_h_align.currentData()
                element.v_align = self._text_v_align.currentData()
                element.keep_inside = self._text_keep_inside.isChecked()
                element.auto_shrink = self._text_auto_shrink.isChecked()
                element.min_font_size = self._text_min_font_size.value()
        else:
            if hasattr(self, '_image_file_input'):
                element.image_path = self._image_file_input.text()
            if hasattr(self, '_image_x'):
                element.x = self._image_x.value()
            if hasattr(self, '_image_y'):
                element.y = self._image_y.value()
            if hasattr(self, '_image_width'):
                element.width = self._image_width.value()
            if hasattr(self, '_image_height'):
                element.height = self._image_height.value()
            if hasattr(self, '_image_layout_mode'):
                element.layout_mode = self._image_layout_mode.currentData()
                element.margin = self._image_margin.value()
                element.offset_x = self._image_offset_x.value()
                element.offset_y = self._image_offset_y.value()
                element.keep_inside = self._image_keep_inside.isChecked()
                element.shrink_to_fit = self._image_shrink_to_fit.isChecked()
                element.keep_aspect = self._image_keep_aspect.isChecked()

    def collect_raw_state(self) -> dict[str, Any]:
        """收集所有参数"""
        # 先收集当前正在编辑的元素
        self._collect_current_element_options()
        
        # 序列化所有元素
        elements_data = []
        for elem in self._elements:
            if elem.element_type == 'text':
                elements_data.append({
                    'type': 'text',
                    'name': elem.name,
                    'source': elem.source,
                    'content': elem.content,
                    'font_size': elem.font_size,
                    'font_family': elem.font_family,
                    'bold': elem.bold,
                    'color': elem.color,
                    'x': elem.x,
                    'y': elem.y,
                    'excel_file': elem.excel_file,
                    'match_column': elem.match_column,
                    'data_column': elem.data_column,
                    'excel_row_start': elem.excel_row_start,
                    'layout_mode': elem.layout_mode,
                    'anchor': elem.anchor,
                    'margin': elem.margin,
                    'offset_x': elem.offset_x,
                    'offset_y': elem.offset_y,
                    'box_width': elem.box_width,
                    'box_width_unit': elem.box_width_unit,
                    'box_height': elem.box_height,
                    'auto_wrap': elem.auto_wrap,
                    'h_align': elem.h_align,
                    'v_align': elem.v_align,
                    'keep_inside': elem.keep_inside,
                    'auto_shrink': elem.auto_shrink,
                    'min_font_size': elem.min_font_size,
                })
            else:
                elements_data.append({
                    'type': 'image',
                    'name': elem.name,
                    'image_path': elem.image_path,
                    'x': elem.x,
                    'y': elem.y,
                    'width': elem.width,
                    'height': elem.height,
                    'layout_mode': elem.layout_mode,
                    'anchor': elem.anchor,
                    'margin': elem.margin,
                    'offset_x': elem.offset_x,
                    'offset_y': elem.offset_y,
                    'keep_inside': elem.keep_inside,
                    'shrink_to_fit': elem.shrink_to_fit,
                    'keep_aspect': elem.keep_aspect,
                })
        
        # 收集自定义字体信息
        custom_fonts = {}
        if hasattr(self, '_custom_fonts'):
            custom_fonts = self._custom_fonts.copy()
        
        return {
            'elements': elements_data,
            'output_format': self.combo_fmt.currentText(),
            'custom_fonts': custom_fonts,
        }

    def apply_state(self, options: dict | None):
        """应用参数到面板"""
        if self._list_widget is None:
            return
        
        # 先收集当前编辑元素的参数
        self._collect_current_element_options()
        
        # 清除配置页面，显示提示页面
        self._clear_config_pages()
        self._config_stack.setCurrentIndex(0)
        
        # 清除现有元素
        self._elements.clear()
        self._list_widget.clear()
        
        # 加载元素
        elements_data = options.get('elements', [])
        for data in elements_data:
            if data['type'] == 'text':
                element = TextElement(
                    x=data.get('x', 50),
                    y=data.get('y', 50),
                    source=data.get('source', 'fixed'),
                    content=data.get('content', ''),
                    name=data.get('name', ''),
                    font_size=data.get('font_size', 24),
                    font_family=data.get('font_family', 'Microsoft YaHei'),
                    bold=data.get('bold', False),
                    color=data.get('color', '#FFFFFF'),
                    excel_file=data.get('excel_file', ''),
                    match_column=data.get('match_column', 1),
                    data_column=data.get('data_column', 2),
                    excel_row_start=data.get('excel_row_start', 2),
                    layout_mode=data.get('layout_mode', 'free'),
                    anchor=data.get('anchor', 'center'),
                    margin=data.get('margin', 0),
                    offset_x=data.get('offset_x', 0),
                    offset_y=data.get('offset_y', 0),
                    box_width=data.get('box_width', 80),
                    box_width_unit=data.get('box_width_unit', 'percent'),
                    box_height=data.get('box_height', 0),
                    auto_wrap=data.get('auto_wrap', True),
                    h_align=data.get('h_align', 'center'),
                    v_align=data.get('v_align', 'center'),
                    keep_inside=data.get('keep_inside', True),
                    auto_shrink=data.get('auto_shrink', True),
                    min_font_size=data.get('min_font_size', 8),
                )
            else:
                element = ImageElement(
                    x=data.get('x', 100),
                    y=data.get('y', 100),
                    image_path=data.get('image_path', ''),
                    name=data.get('name', ''),
                    width=data.get('width', 200),
                    height=data.get('height', 200),
                    layout_mode=data.get('layout_mode', 'free'),
                    anchor=data.get('anchor', 'center'),
                    margin=data.get('margin', 0),
                    offset_x=data.get('offset_x', 0),
                    offset_y=data.get('offset_y', 0),
                    keep_inside=data.get('keep_inside', True),
                    shrink_to_fit=data.get('shrink_to_fit', True),
                    keep_aspect=data.get('keep_aspect', True),
                )
            
            self._elements.append(element)
            item = ElementListItem(element, len(self._elements) - 1)
            self._list_widget.addItem(item)
        
        # 设置输出格式
        fmt = options.get('output_format', 'png')
        idx = ['png', 'webp', 'jpg'].index(fmt) if fmt in ['png', 'webp', 'jpg'] else 0
        self.combo_fmt.setCurrentIndex(idx)
        
        # 字体下拉框在选中文本元素时才创建，此处仅恢复映射。
        self._custom_fonts = dict(options.get('custom_fonts', {}) or {})

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
            test_font = ImageFont.truetype(file_path, 12)
            
            # 获取字体名称
            font_name = Path(file_path).stem
            
            # 检查是否已存在
            existing_fonts = [self._font_family.itemText(i) for i in range(self._font_family.count())]
            if font_name in existing_fonts:
                QMessageBox.warning(self, "提示", f"字体 '{font_name}' 已存在")
                return
            
            # 添加到字体列表
            self._font_family.addItem(font_name)
            self._font_family.setCurrentIndex(self._font_family.count() - 1)
            
            # 复制字体文件到程序字体目录（可选，这里我们直接保存路径）
            # 为了简化，我们直接在字体映射中记录路径
            if not hasattr(self, '_custom_fonts'):
                self._custom_fonts = {}
            self._custom_fonts[font_name] = file_path
            
            QMessageBox.information(self, "成功", f"字体 '{font_name}' 加载成功！")
            
        except Exception as e:
            QMessageBox.warning(self, "错误", f"字体加载失败: {e}")


    def show_validation_errors(self, errors) -> None:
        msgs = list(errors or [])
        if not msgs:
            return
        QMessageBox.warning(self, "参数错误", "\n".join(str(e) for e in msgs))
