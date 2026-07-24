"""
批量元数据编辑处理器
编辑图片文件元数据（标题/描述/作者/版权/标记），可选格式转换。
数据源：固定值 / Excel 列匹配 / 文件名；支持一键清除全部元数据。
通过真实格式检测处理假扩展名（如 .jpg 实为 MPO）。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from PIL import Image
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QLabel, QComboBox, QSpinBox, QLineEdit, QPushButton,
    QCheckBox, QFileDialog, QFrame, QMessageBox, QSizePolicy,
    QLayout, QApplication,
)
from PySide6.QtCore import Qt, QRect, QPoint, QSize, Signal, QEvent

from core.base_processor import BaseProcessor, ProcessResult, register_processor
from core.metadata_utils import (
    FIELD_KEYS, FIELD_LABELS, CONVERT_TARGETS,
    load_excel_lookup, resolve_field_value,
    write_image_metadata, normalize_keywords,
    fields_for_format, detect_true_format, ext_of_format,
    normalize_format_name, read_image_metadata,
)
import config


def _desktop() -> str:
    return str(Path.home() / "Desktop")


def _parse_tags(text: str) -> list[str]:
    """将分号串解析为标签列表（保序去重，忽略大小写重复）。"""
    if not text:
        return []
    raw = normalize_keywords(text)
    if not raw:
        return []
    return [p.strip() for p in raw.split(";") if p.strip()]


def _tags_to_value(tags: list[str]) -> str:
    return normalize_keywords("; ".join(tags))


class _FlowLayout(QLayout):
    """流式布局：控件按行排列，空间不足自动换行（Qt Flow Layout 示例的 Python 版）"""

    def __init__(self, parent=None, margin: int = 0, spacing: int = 6):
        super().__init__(parent)
        self._items: list = []
        self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)

    def __del__(self):
        try:
            while self.count():
                self.takeAt(0)
        except Exception:
            pass

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        m = self.contentsMargins()
        effective = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x = effective.x()
        y = effective.y()
        line_height = 0
        for item in self._items:
            wid = item.widget()
            hint = item.sizeHint()
            space_x = self.spacing()
            next_x = x + hint.width() + space_x
            if next_x - space_x > effective.right() and line_height > 0:
                x = effective.x()
                y += line_height + space_x
                next_x = x + hint.width() + space_x
                line_height = 0
            # 横向可扩展的控件（如输入框）占满当前行剩余宽度，否则只有 sizeHint 宽、点不进去
            # 注意：PySide6 的 horizontalPolicy() 返回 Policy 枚举，不能与 PolicyFlag 做 &
            width = hint.width()
            if wid is not None:
                hpol = wid.sizePolicy().horizontalPolicy()
                if hpol in (
                    QSizePolicy.Policy.Expanding,
                    QSizePolicy.Policy.MinimumExpanding,
                    QSizePolicy.Policy.Ignored,
                ):
                    width = max(width, effective.right() - x + 1)
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), QSize(width, hint.height())))
            x = x + width + space_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + m.bottom()


class _TagLineEdit(QLineEdit):
    """标签输入框：文本为空时按 Backspace 发信号；失焦时发信号以便自动转成标签"""

    backspace_on_empty = Signal()
    focus_lost = Signal()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Backspace and not self.text():
            self.backspace_on_empty.emit()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        # 离开输入框（点别处 / Tab 切换）时通知编辑器，把未回车文本自动转成标签
        self.focus_lost.emit()


class _TagChip(QFrame):
    """单个标签芯片：文本 + × 删除按钮；按住可拖拽调整顺序（拖拽中呈虚影占位）"""

    _STYLE_NORMAL = (
        "QFrame#tag_chip {"
        "  background: rgba(91, 138, 245, 55);"
        "  border: 1px solid rgba(100, 150, 255, 0.35);"
        "  border-radius: 9px;"
        "}"
        "QFrame#tag_chip QLabel {"
        "  background: transparent; border: none; color: #dfe6ff;"
        "}"
        "QPushButton#tag_chip_del {"
        "  background: transparent; border: none; border-radius: 9px;"
        "  color: #9aa5c8; font-weight: bold; padding: 0;"
        "}"
        "QPushButton#tag_chip_del:hover {"
        "  background: rgba(255, 95, 95, 130); color: #fff;"
        "}"
    )
    # 拖拽中的占位虚影：芯片本体留在布局里标记落点，浮动副本跟随光标
    _STYLE_GHOST = (
        "QFrame#tag_chip {"
        "  background: rgba(91, 138, 245, 16);"
        "  border: 1px dashed rgba(100, 150, 255, 0.45);"
        "  border-radius: 9px;"
        "}"
        "QFrame#tag_chip QLabel {"
        "  background: transparent; border: none; color: rgba(223, 230, 255, 70);"
        "}"
        "QPushButton#tag_chip_del {"
        "  background: transparent; border: none; border-radius: 9px;"
        "  color: rgba(154, 165, 200, 60); font-weight: bold; padding: 0;"
        "}"
    )

    def __init__(self, text: str, on_remove, editor=None, parent=None):
        super().__init__(parent)
        self.text = text
        self._editor = editor
        self._drag_start: QPoint | None = None
        self.setObjectName("tag_chip")
        self.setCursor(Qt.OpenHandCursor)
        self.setToolTip("拖拽可调整顺序")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 1, 3, 1)
        lay.setSpacing(2)
        lay.addWidget(QLabel(text))
        btn = QPushButton("×")
        btn.setObjectName("tag_chip_del")
        btn.setFixedSize(18, 18)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setToolTip("删除该标记")
        btn.clicked.connect(lambda: on_remove(self))
        lay.addWidget(btn)
        self.set_dragging(False)

    def set_dragging(self, on: bool):
        """切换拖拽态：True = 虚影占位（本体被抽离），False = 正常样式。"""
        self.setStyleSheet(self._STYLE_GHOST if on else self._STYLE_NORMAL)

    # ── 拖拽排序（按住芯片移动，编辑器按光标位置实时重排） ──

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_start is not None and self._editor is not None:
            dist = (event.position().toPoint() - self._drag_start).manhattanLength()
            if dist >= QApplication.startDragDistance():
                self.setCursor(Qt.ClosedHandCursor)
                # 坐标换算到容器坐标系后交给编辑器计算插入位置
                self._editor.drag_chip_to(self, self.mapToParent(event.position().toPoint()))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._editor is not None:
            self._editor.end_drag(self)
        self._drag_start = None
        self.setCursor(Qt.OpenHandCursor)
        super().mouseReleaseEvent(event)


class _TagChipEditor(QWidget):
    """标签芯片编辑器：流式一行显示多个标记，× 删除；回车/失焦添加，支持 ; 分隔批量添加"""

    # 浮动芯片样式：拖拽中跟随光标的「被抽离」副本，比正常芯片更亮更实
    _STYLE_FLOATER = (
        "QLabel {"
        "  background: rgba(91, 138, 245, 200);"
        "  border: 1px solid rgba(150, 180, 255, 0.9);"
        "  border-radius: 9px;"
        "  color: #ffffff; font-weight: bold;"
        "  padding: 2px 10px;"
        "}"
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._chips: list[_TagChip] = []
        self._drag_chip: _TagChip | None = None
        self._floater: QLabel | None = None
        self._grab_offset = QPoint(0, 0)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._container = QFrame()
        self._container.setObjectName("tag_chip_box")
        self._container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.MinimumExpanding)
        self._container.setStyleSheet(
            "QFrame#tag_chip_box {"
            "  background-color: rgba(22, 22, 40, 160);"
            "  border: 1px solid rgba(90, 100, 160, 0.25);"
            "  border-radius: 7px;"
            "}"
        )
        self._flow = _FlowLayout(self._container, margin=4, spacing=6)

        self._edit = _TagLineEdit()
        self._edit.setPlaceholderText("输入标记后回车添加（失焦自动确认）；Backspace 删前一个；拖拽调序")
        self._edit.setMinimumWidth(170)
        self._edit.setStyleSheet(
            "QLineEdit {"
            "  background: transparent; border: none;"
            "  color: #e0e4f0; padding: 3px 4px;"
            "}"
        )
        self._edit.returnPressed.connect(self._commit_edit)
        self._edit.focus_lost.connect(self._commit_edit)
        self._edit.backspace_on_empty.connect(self._remove_last)
        self._flow.addWidget(self._edit)

        # 点击容器空白处也能聚焦输入框（与 HTML 标签输入框一致）
        self._container.installEventFilter(self)

        outer.addWidget(self._container)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.MinimumExpanding)

    def eventFilter(self, watched, event):
        if watched is self._container and event.type() == QEvent.Type.MouseButtonPress:
            self._edit.setFocus()
        return super().eventFilter(watched, event)

    # ── 数据接口 ──

    def tags(self) -> list[str]:
        # 读取前冲刷未回车文本，避免开始处理时丢掉输入框里的内容
        self._commit_edit()
        return [c.text for c in self._chips]

    def set_tags(self, tags: list[str]):
        for c in list(self._chips):
            self._remove_chip(c)
        for t in tags:
            self._add_tag(t)

    # ── 内部 ──

    def _exists(self, text: str) -> bool:
        key = text.casefold()
        return any(c.text.casefold() == key for c in self._chips)

    def _add_tag(self, text: str) -> bool:
        text = text.strip()
        if not text or self._exists(text):
            return False
        chip = _TagChip(text, self._remove_chip, editor=self)
        self._chips.append(chip)
        # 芯片始终插在输入框之前
        self._flow.removeWidget(self._edit)
        self._flow.addWidget(chip)
        self._flow.addWidget(self._edit)
        self._relayout()
        return True

    def _remove_chip(self, chip: _TagChip):
        if chip is self._drag_chip:
            self._drag_chip = None
            self._clear_floater()
        if chip in self._chips:
            self._chips.remove(chip)
        self._flow.removeWidget(chip)
        chip.deleteLater()
        self._relayout()

    def _remove_last(self):
        """Backspace：删除光标前（末尾）的标签。"""
        if self._chips:
            self._remove_chip(self._chips[-1])

    # ── 拖拽排序 ──

    def drag_chip_to(self, chip: _TagChip, pos: QPoint):
        """拖拽中：浮动副本跟随光标，虚影占位实时移到插入位置。"""
        if chip not in self._chips:
            return
        if self._drag_chip is not chip:
            self._begin_drag(chip)
        if self._floater is not None:
            # 浮动副本挂在顶层窗口上：坐标从容器映射到窗口，并保持在最上层
            win_pos = self._container.mapTo(self.window(), pos - self._grab_offset)
            self._floater.move(win_pos)
            self._floater.raise_()

        others = [c for c in self._chips if c is not chip]
        idx = len(others)
        for i, c in enumerate(others):
            g = c.geometry()
            same_row = g.top() <= pos.y() <= g.bottom()
            if (same_row and pos.x() < g.center().x()) or pos.y() < g.top():
                idx = i
                break
        new_order = others[:idx] + [chip] + others[idx:]
        if new_order != self._chips:
            self._chips = new_order
            self._refresh_flow()

    def _begin_drag(self, chip: _TagChip):
        """开始拖拽：芯片本体变虚影占位，创建跟随光标的高亮浮动副本。"""
        self._drag_chip = chip
        self._grab_offset = chip._drag_start or QPoint(0, 0)
        chip.set_dragging(True)

        # 父级设为顶层窗口：不被容器边界裁剪，也不会被其他面板/控件遮挡
        win = self.window()
        floater = QLabel(chip.text, win)
        floater.setStyleSheet(self._STYLE_FLOATER)
        # 不拦截鼠标，事件仍由拖拽中的芯片接收
        floater.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        floater.adjustSize()
        floater.move(chip.mapTo(win, QPoint(0, 0)))
        floater.raise_()
        floater.show()
        self._floater = floater

    def end_drag(self, chip: _TagChip | None = None):
        """结束拖拽：销毁浮动副本，芯片恢复正常样式（顺序已实时生效）。"""
        if self._drag_chip is None:
            return
        self._drag_chip.set_dragging(False)
        self._drag_chip = None
        self._clear_floater()

    def _clear_floater(self):
        if self._floater is not None:
            self._floater.deleteLater()
            self._floater = None

    def _refresh_flow(self):
        """按 self._chips 顺序重建流式布局（输入框保持在最后）。"""
        self._flow.removeWidget(self._edit)
        for c in self._chips:
            self._flow.removeWidget(c)
        for c in self._chips:
            self._flow.addWidget(c)
        self._flow.addWidget(self._edit)
        self._flow.invalidate()
        self._flow.activate()  # 立即重排，保证拖拽中下一次比较用的是新几何
        self._container.updateGeometry()
        self.updateGeometry()

    def _commit_edit(self):
        text = self._edit.text()
        if not text.strip():
            return
        for part in re.split(r"[;；,，]", text):
            self._add_tag(part)
        self._edit.clear()
        self._edit.setFocus()

    def _relayout(self):
        self._flow.invalidate()
        self._container.updateGeometry()
        self.updateGeometry()


class _FieldRow:
    """单个元数据字段的 UI 行控件集合"""
    __slots__ = (
        "key", "chk", "combo_source", "edit_value",
        "widget_fixed", "widget_excel",
        "excel_file", "excel_match", "excel_data", "excel_row",
        "chip_editor", "wrap",
    )

    def __init__(self, key: str):
        self.key = key
        self.chk: QCheckBox | None = None
        self.combo_source: QComboBox | None = None
        self.edit_value: QLineEdit | None = None
        self.widget_fixed: QWidget | None = None
        self.widget_excel: QWidget | None = None
        self.excel_file: QLineEdit | None = None
        self.excel_match: QSpinBox | None = None
        self.excel_data: QSpinBox | None = None
        self.excel_row: QSpinBox | None = None
        self.chip_editor: _TagChipEditor | None = None
        self.wrap: QWidget | None = None


@register_processor
class MetadataProcessor(BaseProcessor):
    """批量编辑图片文件元数据 + 可选格式转换"""

    name = "元数据编辑"
    description = "批量修改图片标题、描述、作者、版权、标记，支持格式转换与 Excel 匹配"
    icon = "🏷"
    preset_id = "metadata_edit"

    def __init__(self):
        self._panel: QWidget | None = None
        self._fields: dict[str, _FieldRow] = {}
        self._grp_clear: QGroupBox | None = None
        self._grp_fields: QGroupBox | None = None
        self._grp_convert: QGroupBox | None = None
        self.combo_fmt: QComboBox | None = None
        self._lbl_fmt_hint: QLabel | None = None
        self._lbl_no_meta: QLabel | None = None
        self._lbl_loaded: QLabel | None = None
        self._btn_reload: QPushButton | None = None
        self._chk_auto_load: QCheckBox | None = None
        self._loaded_path: str | None = None

    @property
    def is_batch_processor(self) -> bool:
        return True

    def supports_selected_load(self) -> bool:
        return True

    # ── UI ──

    def create_panel(self, parent=None) -> QWidget:
        self._panel = QWidget(parent)
        root = QVBoxLayout(self._panel)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        # ── 单图回读 ──
        grp_load = QGroupBox("单图回读")
        load_row = QHBoxLayout(grp_load)
        load_row.setSpacing(8)
        self._chk_auto_load = QCheckBox("选中图片时自动读取")
        self._chk_auto_load.setChecked(False)
        self._chk_auto_load.setToolTip(
            "开启后，在左侧点选一张图片会自动读取其标题/描述/作者/版权/标记并填入下方固定值。\n"
            "默认关闭，避免点选图片时覆盖面板中已编辑的内容；\n"
            "处理范围请配合输出区「仅选中」使用。"
        )
        load_row.addWidget(self._chk_auto_load)
        self._btn_reload = QPushButton("读取选中图")
        self._btn_reload.setMinimumWidth(88)
        self._btn_reload.setFixedHeight(28)
        self._btn_reload.setToolTip("手动从当前选中图片读取元数据到面板")
        self._btn_reload.clicked.connect(self._reload_from_current)
        load_row.addWidget(self._btn_reload)
        self._lbl_loaded = QLabel("未读取")
        self._lbl_loaded.setStyleSheet("color:#8a90b0;font-size:11px;")
        self._lbl_loaded.setWordWrap(True)
        load_row.addWidget(self._lbl_loaded, 1)
        root.addWidget(grp_load)

        # ── 格式转换 + 清除全部（并排） ──
        mid_row = QHBoxLayout()
        mid_row.setSpacing(8)

        self._grp_convert = QGroupBox("格式转换（可选）")
        self._grp_convert.setCheckable(True)
        self._grp_convert.setChecked(False)
        self._grp_convert.setToolTip(
            "勾选后强制转换为目标格式。按真实文件头识别格式，"
            "扩展名造假（如 .jpg 实为 MPO）也会正确转换；"
            "真实格式已与目标一致时可跳过重编码，仅写元数据。"
        )
        conv_lay = QHBoxLayout(self._grp_convert)
        conv_lay.setSpacing(8)
        conv_lay.addWidget(QLabel("目标格式:"))
        self.combo_fmt = QComboBox()
        for fmt in CONVERT_TARGETS:
            label = "JPG" if fmt == "jpg" else ("TIFF" if fmt == "tiff" else fmt.upper())
            self.combo_fmt.addItem(label, fmt)
        self.combo_fmt.setMinimumWidth(100)
        self.combo_fmt.setStyleSheet(config.COMBOBOX_STYLE)
        self.combo_fmt.setToolTip("选择输出格式；下方仅显示该格式支持的属性")
        conv_lay.addWidget(self.combo_fmt)

        self._lbl_fmt_hint = QLabel("")
        self._lbl_fmt_hint.setStyleSheet("color:#8a90b0;font-size:11px;")
        self._lbl_fmt_hint.setWordWrap(True)
        conv_lay.addWidget(self._lbl_fmt_hint, 1)
        mid_row.addWidget(self._grp_convert, 3)

        self._grp_convert.toggled.connect(self._on_format_ui_changed)
        self.combo_fmt.currentIndexChanged.connect(self._on_format_ui_changed)

        # ── 清除全部（隐私脱敏）──
        self._grp_clear = QGroupBox("清除全部元数据")
        self._grp_clear.setCheckable(True)
        self._grp_clear.setChecked(False)
        self._grp_clear.setToolTip(
            "开启后将剥离图片中的 EXIF/文本元数据后重新保存，下方字段设置将被忽略"
        )
        clear_lay = QHBoxLayout(self._grp_clear)
        lbl_clear = QLabel("剥离 EXIF / 文本元数据，适合隐私脱敏后分发；开启后忽略下方字段。")
        lbl_clear.setStyleSheet("color:#8a90b0;font-size:11px;")
        lbl_clear.setWordWrap(True)
        clear_lay.addWidget(lbl_clear)
        mid_row.addWidget(self._grp_clear, 2)

        root.addLayout(mid_row)
        self._grp_clear.toggled.connect(self._on_clear_toggled)

        # ── 字段编辑 ──
        self._grp_fields = QGroupBox("编辑字段（勾选启用）")
        fields_lay = QVBoxLayout(self._grp_fields)
        fields_lay.setSpacing(6)

        self._lbl_no_meta = QLabel("当前目标格式不支持元数据属性（如 BMP），仅执行格式转换。")
        self._lbl_no_meta.setWordWrap(True)
        self._lbl_no_meta.setStyleSheet("color:#c0a060;font-size:12px;")
        self._lbl_no_meta.setVisible(False)
        fields_lay.addWidget(self._lbl_no_meta)

        for key in FIELD_KEYS:
            row = self._build_field_row(key)
            self._fields[key] = row
            fields_lay.addWidget(row.wrap)

        root.addWidget(self._grp_fields)

        hint = QLabel(
            "说明：\n"
            "· 输出区可选「全部文件 / 仅选中」；单张修改时选「仅选中」并点选左侧图片\n"
            "· 点「读取选中图」把该图已有元数据填入固定值，便于在原值上修改（可勾选自动读取）\n"
            "·「标记」以标签芯片显示：回车/失焦添加、× 删除、Backspace 删末尾、拖拽调序；写入时用 ; 连接\n"
            "· 未勾选的字段保持原值不变；勾选但内容为空则清空该字段"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666e88;font-size:11px;font-style:italic;")
        root.addWidget(hint)

        root.addStretch()
        self._on_format_ui_changed()
        return self._panel

    def _make_spin(self, lo: int, hi: int, val: int, tip: str) -> QSpinBox:
        sp = QSpinBox()
        sp.setRange(lo, hi)
        sp.setValue(val)
        sp.setToolTip(tip)
        # 保证两位数及以上可见
        sp.setMinimumWidth(72)
        sp.setMaximumWidth(88)
        sp.setFixedHeight(28)
        return sp

    def _build_field_row(self, key: str) -> _FieldRow:
        """
        宽屏横排： [勾选名] [数据源] [值区域…]
        普通字段单行；标记/Excel 值区内部仍可小幅换行但不把名/源挤到上一行。
        """
        fr = _FieldRow(key)
        label = FIELD_LABELS.get(key, key)
        is_keywords = key == "keywords"

        fr.chk = QCheckBox(label)
        fr.chk.setChecked(False)
        fr.chk.setFixedWidth(56)

        fr.combo_source = QComboBox()
        fr.combo_source.addItem("固定值", "fixed")
        fr.combo_source.addItem("Excel列", "excel")
        fr.combo_source.addItem("文件名", "filename")
        fr.combo_source.setFixedWidth(90)
        fr.combo_source.setStyleSheet(config.COMBOBOX_STYLE)

        # ── 固定值区域 ──
        fr.widget_fixed = QWidget()
        fr.widget_fixed.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        if is_keywords:
            fixed_lay = QHBoxLayout(fr.widget_fixed)
            fixed_lay.setContentsMargins(0, 0, 0, 0)
            fixed_lay.setSpacing(0)

            fr.chip_editor = _TagChipEditor()
            fixed_lay.addWidget(fr.chip_editor, 1)

            fr.edit_value = QLineEdit()
            fr.edit_value.setVisible(False)
        else:
            fixed_lay = QHBoxLayout(fr.widget_fixed)
            fixed_lay.setContentsMargins(0, 0, 0, 0)
            fixed_lay.setSpacing(0)
            fr.edit_value = QLineEdit()
            fr.edit_value.setPlaceholderText(f"输入{label}…")
            fr.edit_value.setMinimumWidth(200)
            fr.edit_value.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            fixed_lay.addWidget(fr.edit_value, 1)

        # ── Excel 区域：单行横排 ──
        fr.widget_excel = QWidget()
        fr.widget_excel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        ex_lay = QHBoxLayout(fr.widget_excel)
        ex_lay.setContentsMargins(0, 0, 0, 0)
        ex_lay.setSpacing(6)

        fr.excel_file = QLineEdit()
        fr.excel_file.setPlaceholderText("Excel 路径…")
        fr.excel_file.setMinimumWidth(100)
        fr.excel_file.setMaximumWidth(200)
        fr.excel_file.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        ex_lay.addWidget(fr.excel_file)

        btn_browse = QPushButton("浏览")
        btn_browse.setMinimumWidth(52)
        btn_browse.setFixedHeight(28)
        btn_browse.setToolTip("选择 Excel 文件")
        btn_browse.clicked.connect(lambda _=False, r=fr: self._browse_excel(r))
        ex_lay.addWidget(btn_browse)

        ex_lay.addWidget(QLabel("匹配列"))
        fr.excel_match = self._make_spin(1, 100, 1, "图片文件名所在列（从 1 开始）")
        ex_lay.addWidget(fr.excel_match)
        ex_lay.addWidget(QLabel("数据列"))
        fr.excel_data = self._make_spin(1, 100, 2, f"{label}内容所在列（从 1 开始）")
        ex_lay.addWidget(fr.excel_data)
        ex_lay.addWidget(QLabel("起始行"))
        fr.excel_row = self._make_spin(1, 10000, 2, "数据起始行（跳过表头）")
        ex_lay.addWidget(fr.excel_row)
        ex_lay.addStretch()

        fr.widget_excel.setVisible(False)

        def _on_source(_idx, r=fr):
            src = r.combo_source.currentData()
            r.widget_fixed.setVisible(src == "fixed")
            r.widget_excel.setVisible(src == "excel")
            # 文件名：无值区，右侧留空即可

        fr.combo_source.currentIndexChanged.connect(_on_source)

        def _on_chk(checked, r=fr):
            r.combo_source.setEnabled(checked)
            r.widget_fixed.setEnabled(checked)
            r.widget_excel.setEnabled(checked)

        fr.chk.toggled.connect(_on_chk)
        _on_chk(False)

        # 主行：属性名 | 选项 | 值 —— 全部横排
        box = QFrame()
        box.setObjectName("meta_field_row")
        box.setStyleSheet(
            "QFrame#meta_field_row {"
            "  background: rgba(30, 30, 55, 70);"
            "  border: 1px solid rgba(100, 110, 170, 0.10);"
            "  border-radius: 8px;"
            "}"
        )
        row = QHBoxLayout(box)
        row.setContentsMargins(8, 4, 8, 4)
        row.setSpacing(8)
        row.setAlignment(Qt.AlignTop)
        row.addWidget(fr.chk, 0, Qt.AlignTop)
        row.addWidget(fr.combo_source, 0, Qt.AlignTop)
        row.addWidget(fr.widget_fixed, 1)
        row.addWidget(fr.widget_excel, 1)
        fr.wrap = box
        return fr

    # ── 标记标签操作 ──

    def _set_tags(self, fr: _FieldRow, tags: list[str] | str):
        if not fr.chip_editor:
            return
        if isinstance(tags, str):
            tags = _parse_tags(tags)
        fr.chip_editor.set_tags(tags)

    def _get_fixed_value(self, fr: _FieldRow) -> str:
        if fr.key == "keywords" and fr.chip_editor is not None:
            return _tags_to_value(fr.chip_editor.tags())
        return fr.edit_value.text() if fr.edit_value else ""

    def _browse_excel(self, fr: _FieldRow):
        path, _ = QFileDialog.getOpenFileName(
            self._panel,
            "选择 Excel 文件",
            _desktop(),
            "Excel Files (*.xlsx *.xls)",
        )
        if path:
            fr.excel_file.setText(path)

    def _current_target_format(self) -> str | None:
        if self._grp_convert and self._grp_convert.isChecked() and self.combo_fmt:
            return self.combo_fmt.currentData() or "jpg"
        return None

    def _visible_field_keys(self) -> tuple[str, ...]:
        target = self._current_target_format()
        if target is None:
            return FIELD_KEYS
        return fields_for_format(target)

    def _on_format_ui_changed(self, *_args):
        target = self._current_target_format()
        visible = set(self._visible_field_keys())

        if self._lbl_fmt_hint is not None:
            if target:
                names = [FIELD_LABELS[k] for k in FIELD_KEYS if k in visible]
                if names:
                    self._lbl_fmt_hint.setText(f"可写属性：{' / '.join(names)}")
                else:
                    self._lbl_fmt_hint.setText("该格式不支持元数据，仅转换像素")
            else:
                self._lbl_fmt_hint.setText("未转换时按真实格式写入支持的属性")

        if self._lbl_no_meta is not None:
            self._lbl_no_meta.setVisible(bool(target) and not visible)

        for key, fr in self._fields.items():
            if fr.wrap is not None:
                fr.wrap.setVisible(key in visible)

        clear_on = self._grp_clear.isChecked() if self._grp_clear else False
        if self._grp_fields is not None:
            self._grp_fields.setEnabled(not clear_on)

    def _on_clear_toggled(self, checked: bool):
        if self._grp_fields is not None:
            self._grp_fields.setEnabled(not checked)

    # ── 单图回读 ──

    def on_selected_image(self, path: str | None):
        """主窗口选中图片变化时回调。"""
        if not path:
            if self._lbl_loaded is not None and not self._loaded_path:
                self._lbl_loaded.setText("未读取")
            return
        if self._chk_auto_load is None or not self._chk_auto_load.isChecked():
            return
        # 同一文件重复选中不反复覆盖用户已改内容
        if self._loaded_path and Path(self._loaded_path).resolve() == Path(path).resolve():
            return
        self.load_from_image(path)

    def _reload_from_current(self):
        """手动读取：优先已加载路径，否则由主窗口再推一次选中项。"""
        # 通过 parent 链找 MainWindow 的当前选中
        path = self._loaded_path
        w = self._panel
        while w is not None:
            if hasattr(w, "file_list"):
                item = w.file_list.currentItem()
                if item is not None:
                    path = item.data(Qt.UserRole)
                break
            w = w.parentWidget() if hasattr(w, "parentWidget") else None
        if not path:
            QMessageBox.information(self._panel, "提示", "请先在左侧列表选中一张图片")
            return
        self.load_from_image(path, force=True)

    def load_from_image(self, path: str, force: bool = False):
        """从图片读取元数据并填入固定值字段。"""
        if not path or not os.path.exists(path):
            if self._lbl_loaded is not None:
                self._lbl_loaded.setText("文件不存在")
            return
        if (
            not force
            and self._loaded_path
            and Path(self._loaded_path).resolve() == Path(path).resolve()
        ):
            return

        try:
            meta = read_image_metadata(path)
        except Exception as e:
            if self._lbl_loaded is not None:
                self._lbl_loaded.setText(f"读取失败: {e}")
            return

        name = Path(path).name
        true_fmt = meta.get("true_format") or "?"
        ext_fmt = meta.get("ext_format") or "?"
        note = f"已读取: {name}"
        if true_fmt != ext_fmt:
            note += f"  （扩展名 .{ext_fmt}，真实 {true_fmt}）"
        else:
            note += f"  （{true_fmt}）"
        if not meta.get("supported", True):
            note += "  — 此格式无元数据可写"
        if self._lbl_loaded is not None:
            self._lbl_loaded.setText(note)

        # 填入固定值，并勾选有内容的字段；数据源切到固定值
        for key in FIELD_KEYS:
            fr = self._fields.get(key)
            if fr is None:
                continue
            val = meta.get(key, "") or ""
            # 切到固定值源
            idx = fr.combo_source.findData("fixed")
            if idx >= 0:
                fr.combo_source.setCurrentIndex(idx)
            if key == "keywords":
                self._set_tags(fr, val)
            elif fr.edit_value is not None:
                fr.edit_value.setText(val)
            # 有值则启用，便于直接改；无值不自动勾选以免误清空
            fr.chk.setChecked(bool(str(val).strip()))
            fr.widget_fixed.setVisible(True)
            fr.widget_excel.setVisible(False)
            enabled = fr.chk.isChecked()
            fr.combo_source.setEnabled(enabled)
            fr.widget_fixed.setEnabled(enabled)
            fr.widget_excel.setEnabled(enabled)

        self._loaded_path = path
        self._on_format_ui_changed()

    # ── 参数 ──

    def gather_options(self) -> dict:
        fields = {}
        for key, fr in self._fields.items():
            fields[key] = {
                "enabled": fr.chk.isChecked(),
                "source": fr.combo_source.currentData() or "fixed",
                "value": self._get_fixed_value(fr),
                "excel_file": fr.excel_file.text().strip(),
                "match_column": fr.excel_match.value(),
                "data_column": fr.excel_data.value(),
                "excel_row_start": fr.excel_row.value(),
            }
        enable_convert = bool(self._grp_convert and self._grp_convert.isChecked())
        target_format = ""
        if enable_convert and self.combo_fmt:
            target_format = self.combo_fmt.currentData() or "jpg"
        return {
            "clear_all": self._grp_clear.isChecked() if self._grp_clear else False,
            "enable_convert": enable_convert,
            "target_format": target_format,
            "fields": fields,
        }

    def default_options(self) -> dict:
        fields = {}
        col_hint = {"title": 2, "description": 3, "author": 4, "copyright": 5, "keywords": 6}
        for key in FIELD_KEYS:
            fields[key] = {
                "enabled": False,
                "source": "fixed",
                "value": "",
                "excel_file": "",
                "match_column": 1,
                "data_column": col_hint.get(key, 2),
                "excel_row_start": 2,
            }
        return {
            "clear_all": False,
            "enable_convert": False,
            "target_format": "jpg",
            "fields": fields,
        }

    def apply_options(self, options: dict):
        if not self._fields:
            return

        enable_convert = bool(options.get("enable_convert", False))
        if self._grp_convert is not None:
            self._grp_convert.blockSignals(True)
            self._grp_convert.setChecked(enable_convert)
            self._grp_convert.blockSignals(False)

        target = normalize_format_name(options.get("target_format", "jpg")) or "jpg"
        if self.combo_fmt is not None:
            self.combo_fmt.blockSignals(True)
            idx = self.combo_fmt.findData(target)
            if idx < 0 and target == "jpeg":
                idx = self.combo_fmt.findData("jpg")
            self.combo_fmt.setCurrentIndex(idx if idx >= 0 else 0)
            self.combo_fmt.blockSignals(False)

        clear_all = options.get("clear_all", False)
        if self._grp_clear:
            self._grp_clear.setChecked(clear_all)

        fields = options.get("fields") or {}
        for key, fr in self._fields.items():
            conf = fields.get(key) or {}
            fr.chk.blockSignals(True)
            fr.chk.setChecked(bool(conf.get("enabled", False)))
            fr.chk.blockSignals(False)

            src = conf.get("source", "fixed")
            idx = fr.combo_source.findData(src)
            if idx >= 0:
                fr.combo_source.setCurrentIndex(idx)

            value = conf.get("value", "")
            if key == "keywords":
                self._set_tags(fr, value)
            elif fr.edit_value is not None:
                fr.edit_value.setText(value)

            fr.excel_file.setText(conf.get("excel_file", ""))
            fr.excel_match.setValue(int(conf.get("match_column", 1)))
            fr.excel_data.setValue(int(conf.get("data_column", 2)))
            fr.excel_row.setValue(int(conf.get("excel_row_start", 2)))

            fr.widget_fixed.setVisible(src == "fixed")
            fr.widget_excel.setVisible(src == "excel")
            enabled = fr.chk.isChecked()
            fr.combo_source.setEnabled(enabled)
            fr.widget_fixed.setEnabled(enabled)
            fr.widget_excel.setEnabled(enabled)

        self._on_format_ui_changed()
        if self._grp_fields:
            self._grp_fields.setEnabled(not clear_all)

    def get_output_format(self) -> str:
        return ""

    def process(self, img: Image.Image, options: dict) -> tuple[Image.Image, dict]:
        return img, {}

    # ── 批量处理 ──

    def process_batch(
        self,
        file_list: list[str],
        options: dict,
        output_dir: str,
        progress_callback=None,
    ) -> list[ProcessResult]:
        clear_all = bool(options.get("clear_all", False))
        enable_convert = bool(options.get("enable_convert", False))
        target_format = normalize_format_name(options.get("target_format", "")) or "jpg"
        field_confs = options.get("fields") or {}

        if enable_convert:
            allowed = set(fields_for_format(target_format))
        else:
            allowed = set(FIELD_KEYS)

        excel_maps: dict[str, dict[str, str]] = {}

        def _excel_key(conf: dict) -> str:
            return "|".join([
                conf.get("excel_file", ""),
                str(conf.get("match_column", 1)),
                str(conf.get("data_column", 2)),
                str(conf.get("excel_row_start", 2)),
            ])

        if not clear_all:
            for key in FIELD_KEYS:
                if key not in allowed:
                    continue
                conf = field_confs.get(key) or {}
                if not conf.get("enabled"):
                    continue
                if conf.get("source") != "excel":
                    continue
                ck = _excel_key(conf)
                if ck in excel_maps:
                    continue
                path = conf.get("excel_file", "")
                if not path or not os.path.exists(path):
                    excel_maps[ck] = {}
                    continue
                try:
                    excel_maps[ck] = load_excel_lookup(
                        path,
                        int(conf.get("match_column", 1)),
                        int(conf.get("data_column", 2)),
                        int(conf.get("excel_row_start", 2)),
                    )
                except Exception as e:
                    excel_maps[ck] = {}
                    if progress_callback:
                        progress_callback(0, len(file_list), f"Excel 读取失败: {e}")

        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        allow_overwrite = bool(options.get("_overwrite", False))
        results: list[ProcessResult] = []
        total = len(file_list)

        for i, fpath in enumerate(file_list):
            if progress_callback:
                progress_callback(i + 1, total, Path(fpath).name)

            src = Path(fpath)
            result = ProcessResult(input_path=fpath)
            try:
                true_fmt = detect_true_format(src)

                if enable_convert:
                    out_ext = ext_of_format(target_format)
                else:
                    out_ext = src.suffix if src.suffix else ext_of_format(true_fmt or "png")

                if allow_overwrite:
                    dst = src.with_suffix(out_ext) if enable_convert else src
                else:
                    dst = out_dir / f"{src.stem}{out_ext}"
                    if dst.resolve() == src.resolve() or dst.exists():
                        counter = 1
                        while True:
                            cand = out_dir / f"{src.stem}_{counter}{out_ext}"
                            if not cand.exists() and cand.resolve() != src.resolve():
                                dst = cand
                                break
                            counter += 1

                fields: dict[str, str] = {}
                if not clear_all:
                    for key in FIELD_KEYS:
                        if key not in allowed:
                            continue
                        conf = field_confs.get(key) or {}
                        if not conf.get("enabled"):
                            continue
                        source = conf.get("source", "fixed")
                        excel_map = None
                        if source == "excel":
                            excel_map = excel_maps.get(_excel_key(conf), {})
                        value = resolve_field_value(
                            source,
                            conf.get("value", ""),
                            fpath,
                            excel_map,
                        )
                        if key == "keywords":
                            value = normalize_keywords(value)
                        fields[key] = value

                details = write_image_metadata(
                    str(src),
                    str(dst),
                    fields,
                    clear_all=clear_all,
                    target_format=target_format if enable_convert else None,
                    enable_convert=enable_convert,
                )
                if (
                    allow_overwrite
                    and enable_convert
                    and dst.resolve() != src.resolve()
                    and src.exists()
                ):
                    try:
                        if dst.exists() and details is not None:
                            src.unlink()
                            details["removed_source"] = src.name
                    except OSError:
                        details["warning"] = (
                            (details.get("warning") or "")
                            + f" 已写出 {dst.name}，但未能删除原文件 {src.name}"
                        ).strip()

                result.output_path = str(dst)
                result.success = True
                result.details = details
            except Exception as e:
                result.success = False
                result.error = str(e)

            results.append(result)

        return results
