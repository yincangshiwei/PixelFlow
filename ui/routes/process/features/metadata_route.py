"""MetadataFeatureRoute —— 元数据编辑参数面板（只读写控件）。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QLabel, QComboBox, QSpinBox, QLineEdit, QPushButton,
    QCheckBox, QFileDialog, QFrame, QMessageBox, QSizePolicy,
)
from PySide6.QtCore import Qt

from core.metadata_utils import (
    FIELD_KEYS, FIELD_LABELS, CONVERT_TARGETS,
    fields_for_format, normalize_format_name, read_image_metadata,
)
import config
from ui.widgets.tag_chip_editor import (
    TagChipEditor, parse_tags, tags_to_value, desktop_path,
)

_TagChipEditor = TagChipEditor
_parse_tags = parse_tags
_tags_to_value = tags_to_value
_desktop = desktop_path


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
        self.chip_editor: TagChipEditor | None = None
        self.wrap: QWidget | None = None


class MetadataFeatureRoute(QWidget):
    """元数据编辑 FeatureRoute：collect_raw_state / apply_state / load_from_image。"""

    feature_id = "metadata_edit"

    def __init__(self, parent=None):
        super().__init__(parent)
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
        self._selected_path: str | None = None
        self._build_widget()

    def build_widget(self, parent=None) -> QWidget:
        if parent is not None and self.parent() is None:
            self.setParent(parent)
        return self

    def _build_widget(self) -> None:
        root = QVBoxLayout(self)
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

        self._grp_convert = QGroupBox("格式转换（默认 JPG）")
        self._grp_convert.setCheckable(True)
        self._grp_convert.setChecked(True)
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
        return

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

            fr.chip_editor = TagChipEditor()
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
            self,
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
        """选中图片变化：记录路径；自动读取开启时回填。"""
        self._selected_path = path
        if not path:
            if self._lbl_loaded is not None and not self._loaded_path:
                self._lbl_loaded.setText("未读取")
            return
        if self._chk_auto_load is None or not self._chk_auto_load.isChecked():
            return
        if self._loaded_path and Path(self._loaded_path).resolve() == Path(path).resolve():
            return
        self.load_from_image(path)

    def _reload_from_current(self):
        """手动读取当前选中图元数据。"""
        path = self._selected_path or self._loaded_path
        if not path:
            QMessageBox.information(self, "提示", "请先在左侧列表选中一张图片")
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

    def collect_raw_state(self) -> dict[str, Any]:
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

    def apply_state(self, options: dict | None):
        options = dict(options or {})
        if not self._fields:
            return

        enable_convert = bool(options.get("enable_convert", True))
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


    def show_validation_errors(self, errors) -> None:
        msgs = list(errors or [])
        if not msgs:
            return
        QMessageBox.warning(self, "参数错误", "\n".join(str(e) for e in msgs))

    def is_auto_load_enabled(self) -> bool:
        return bool(self._chk_auto_load and self._chk_auto_load.isChecked())

    def apply_load_result(self, state: dict | None) -> None:
        """应用 Service.load_selected 返回的回读结果。"""
        if not state:
            return
        note = state.get("_load_note")
        if note is not None and self._lbl_loaded is not None:
            self._lbl_loaded.setText(str(note))
        if not state.get("_load_ok"):
            return
        path = state.get("_load_path")
        fields = state.get("fields") or {}
        for key in FIELD_KEYS:
            fr = self._fields.get(key)
            if fr is None:
                continue
            conf = fields.get(key) or {}
            val = conf.get("value", "") or ""
            idx = fr.combo_source.findData("fixed")
            if idx >= 0:
                fr.combo_source.setCurrentIndex(idx)
            if key == "keywords":
                self._set_tags(fr, val)
            elif fr.edit_value is not None:
                fr.edit_value.setText(val)
            fr.chk.setChecked(bool(str(val).strip()))
            fr.widget_fixed.setVisible(True)
            fr.widget_excel.setVisible(False)
            enabled = fr.chk.isChecked()
            fr.combo_source.setEnabled(enabled)
            fr.widget_fixed.setEnabled(enabled)
            fr.widget_excel.setEnabled(enabled)
        if path:
            self._loaded_path = path
        self._on_format_ui_changed()
