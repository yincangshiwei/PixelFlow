"""元数据标记芯片编辑器（UI 控件，供 MetadataFeatureRoute 使用）。"""
from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QFrame, QSizePolicy, QLayout, QApplication,
)
from PySide6.QtCore import Qt, QRect, QPoint, QSize, Signal, QEvent

from core.metadata_utils import normalize_keywords


def desktop_path() -> str:
    return str(Path.home() / "Desktop")


def parse_tags(text: str) -> list[str]:
    """将分号串解析为标签列表（保序去重，忽略大小写重复）。"""
    if not text:
        return []
    raw = normalize_keywords(text)
    if not raw:
        return []
    return [p.strip() for p in raw.split(";") if p.strip()]


def tags_to_value(tags: list[str]) -> str:
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



# 对外别名
TagChipEditor = _TagChipEditor
