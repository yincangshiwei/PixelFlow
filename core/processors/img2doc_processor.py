"""
图片转文档处理器 —— 图片排版导出 (PPT / PDF / Word)
支持：
  - 导出为 PPTX / PDF / DOCX
  - 自定义文档尺寸（如 33.87 x 19.05 cm）
  - 单页图片数量（1/2/4 张）及排版方式
  - 排序方式：默认排序 / Excel 指定列排序 / 分组排序
  - 图片压缩（按目标大小，二分法，先压缩再排版）
  - 叠加层系统（每页可叠加文字/图片，支持自定义坐标）
"""
import os
import io
import math
import copy
import json
import re
import traceback
import ctypes
from functools import cmp_to_key
from pathlib import Path
from PIL import Image

from core.base_processor import BaseProcessor, ProcessResult
from core.image_processor import compress_to_target_size


def _import_pptx():
    import pptx
    from pptx.util import Cm, Pt
    return pptx, Cm, Pt

def _import_docx():
    import docx
    from docx.shared import Cm, Pt
    return docx, Cm, Pt

def _import_reportlab():
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import cm
    return canvas, cm

def _import_openpyxl():
    import openpyxl
    return openpyxl


def _col2idx(col_str):
    """将 Excel 列字母转为 0-based 索引，如 A->0, B->1, AA->26"""
    num = 0
    for c in col_str:
        num = num * 26 + (ord(c.upper()) - ord('A')) + 1
    return num - 1


def _natural_parts(text: str):
    return [(1, int(part)) if part.isdigit() else (0, part.casefold()) for part in re.split(r'(\d+)', text)]


def _logical_compare(a: str, b: str):
    try:
        return ctypes.windll.shlwapi.StrCmpLogicalW(str(a), str(b))
    except Exception:
        a_parts = _natural_parts(str(a))
        b_parts = _natural_parts(str(b))
        return (a_parts > b_parts) - (a_parts < b_parts)


def _filename_compare(a: str, b: str):
    return _logical_compare(Path(a).name, Path(b).name)


def _directory_sort_key(dir_path: str):
    return [_natural_parts(part) for part in Path(dir_path).parts]


def _excel_sort_key(value):
    if value is None:
        return (1, 0, "")
    if isinstance(value, (int, float)):
        return (0, 0, value)
    return (0, 1, str(value).strip().casefold())



class OverlayLayer:
    def __init__(self, layer_type, x_cm=0.0, y_cm=0.0, name="", placement="overlay"):
        self.layer_type = layer_type
        self.x_cm = x_cm
        self.y_cm = y_cm
        self.name = name
        self.placement = placement


class TextLayer(OverlayLayer):
    def __init__(
        self, x_cm=1.0, y_cm=17.0, source="fixed", text="",
        excel_file="", excel_col="C", match_column=1, data_column=2,
        excel_row_start=2, font_family="Microsoft YaHei", font_size_pt=12,
        bold=False, color="#000000", name="", placement="overlay",
    ):
        super().__init__("text", x_cm, y_cm, name, placement)
        self.source = source
        self.text = text
        self.excel_file = excel_file
        self.excel_col = excel_col
        self.match_column = match_column
        self.data_column = data_column
        self.excel_row_start = excel_row_start
        self.font_family = font_family
        self.font_size_pt = font_size_pt
        self.bold = bold
        self.color = color


class ImageLayer(OverlayLayer):
    def __init__(
        self, x_cm=26.0, y_cm=0.5, path="", w_cm=5.0, h_cm=3.0,
        name="", placement="overlay",
    ):
        super().__init__("image", x_cm, y_cm, name, placement)
        self.path = path
        self.w_cm = w_cm
        self.h_cm = h_cm


def default_img2doc_options() -> dict:
    return {
        "format": "PPTX",
        "width_cm": 33,
        "height_cm": 19,
        "count_per_page": 1,
        "max_cols": 4,
        "keep_ratio": True,
        "row_align": "left",
        "sort_mode": "default",
        "group_by_folder": False,
        "excel_path": "",
        "col_name": "A",
        "col_val": "B",
        "start_row": 2,
        "compress_enabled": False,
        "compress_target_kb": 500,
        "compress_format": "JPEG",
        "overlay_layers": [],
        "custom_fonts": {},
    }


class Img2DocProcessor(BaseProcessor):
    """图片排版导出（纯处理，无 UI）。"""

    name = "图片排版导出"
    description = "将多张图片按指定布局排版，导出为 PPT / PDF / Word"
    icon = "📑"
    preset_id = "img2doc"

    @property
    def is_batch_processor(self) -> bool:
        return True

    def __init__(self):
        self._custom_fonts: dict = {}
        self._font_cache: dict = {}

    def default_options(self) -> dict:
        return default_img2doc_options()

    def process(self, img, options):
        pass

    def process_batch(self, file_list: list[str], options: dict, output_dir: str, progress_cb=None) -> list[ProcessResult]:
        self._custom_fonts = dict(options.get("custom_fonts") or {})
        if not file_list:
            return []

        sort_mode = options.get("sort_mode", "default")
        group_by_folder = options.get("group_by_folder", False)
        dir_groups = {}
        for fpath in file_list:
            group_key = str(Path(fpath).parent)
            dir_groups.setdefault(group_key, []).append(fpath)

        sorted_group_items = sorted(dir_groups.items(), key=lambda item: _directory_sort_key(item[0]))

        def build_groups(sort_key, force_folder_order=False):
            if group_by_folder or force_folder_order:
                folder_groups = [
                    (Path(group_key).name or group_key, sorted(files, key=sort_key))
                    for group_key, files in sorted_group_items
                ]
                if group_by_folder:
                    return folder_groups
                return [(None, [f for _, files in folder_groups for f in files])]
            return [(None, sorted(file_list, key=sort_key))]

        groups = []

        if sort_mode == "default":
            groups = build_groups(cmp_to_key(_filename_compare))
        elif sort_mode == "folder_filename":
            groups = build_groups(cmp_to_key(_filename_compare), force_folder_order=True)
        else:
            excel_path = options.get("excel_path", "")
            if not os.path.exists(excel_path):
                raise ValueError(f"Excel 文件不存在: {excel_path}")

            openpyxl = _import_openpyxl()
            wb = openpyxl.load_workbook(excel_path, data_only=True, read_only=True)
            try:
                ws = wb.active

                c_name = _col2idx(options.get("col_name", "A"))
                c_val = _col2idx(options.get("col_val", "B"))
                start_row = options.get("start_row", 2)

                mapping = {}
                for row_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
                    if row_idx < start_row:
                        continue
                    if c_name < len(row) and row[c_name] is not None:
                        val = row[c_val] if c_val < len(row) else None
                        mapping[str(row[c_name]).strip()] = val
            finally:
                wb.close()

            def get_sort_val(fpath):
                name = Path(fpath).stem
                return mapping.get(name, mapping.get(Path(fpath).name, None))

            if sort_mode == "excel_sort":
                groups = build_groups(lambda x: (_excel_sort_key(get_sort_val(x)), _natural_parts(Path(x).name)))

        # 2. 读取叠加层 Excel 数据（如果有文字层需要从 Excel 读取）
        overlay_layers = options.get("overlay_layers", [])
        # 构建 excel 叠加层的数据映射
        # overlay_excel_data[layer_index] = {col_letter: [val_row2, val_row3, ...]}
        overlay_excel_data = {}
        excel_layers = [(i, l) for i, l in enumerate(overlay_layers) if l.get("type") == "text" and l.get("source") == "excel"]

        for layer_idx, layer in excel_layers:
            excel_file = layer.get("excel_file", "")
            if not excel_file or not os.path.exists(excel_file):
                # 如果没有配置Excel文件，尝试使用排序Excel
                excel_file = options.get("excel_path", "")

            if excel_file and os.path.exists(excel_file):
                openpyxl = _import_openpyxl()
                wb2 = openpyxl.load_workbook(excel_file, data_only=True, read_only=True)
                try:
                    ws2 = wb2.active

                    match_col = layer.get("match_column", 1)
                    data_col = layer.get("data_column", 2)
                    start_row2 = layer.get("excel_row_start", 2)

                    # 构建文件名到数据列的映射
                    data_map = {}
                    for row_idx, row in enumerate(ws2.iter_rows(values_only=True), start=1):
                        if row_idx < start_row2:
                            continue
                        match_val = row[match_col - 1] if match_col - 1 < len(row) else None
                        data_val = row[data_col - 1] if data_col - 1 < len(row) else None
                        if match_val is not None:
                            match_str = str(match_val).strip()
                            # 去除扩展名
                            if '.' in match_str:
                                match_str = Path(match_str).stem
                            data_map[match_str.lower()] = str(data_val) if data_val is not None else ""
                finally:
                    wb2.close()

                overlay_excel_data[layer_idx] = data_map

        # 3. 图片压缩预处理
        compress_enabled = options.get("compress_enabled", False)
        compress_target_kb = options.get("compress_target_kb", 500)
        compress_fmt = options.get("compress_format", "JPEG")
        # 将压缩后的临时路径映射存起来
        _compressed_cache = {}  # original_path -> compressed_bytes (io.BytesIO)

        if compress_enabled:
            all_files = list(dict.fromkeys(f for _, grp in groups for f in grp))
            for fi, fpath in enumerate(all_files):
                if progress_cb:
                    progress_cb(fi, len(all_files), f"压缩中: {Path(fpath).name}")
                try:
                    with Image.open(fpath) as img:
                        if compress_fmt.upper() in ("JPEG", "JPG"):
                            img_conv = img.convert("RGB")
                            fmt_save = "JPEG"
                        else:
                            img_conv = img.convert("RGBA") if img.mode == "RGBA" else img.convert("RGB")
                            fmt_save = "WEBP"
                        _, quality, _ = compress_to_target_size(img_conv, compress_target_kb, fmt_save)
                        buf = io.BytesIO()
                        img_conv.save(buf, format=fmt_save, quality=quality)
                        buf.seek(0)
                        _compressed_cache[fpath] = buf
                except Exception as e:
                    if progress_cb:
                        progress_cb(fi, len(all_files), f"压缩失败，使用原图: {Path(fpath).name} ({e})")

        fmt = options.get("format", "PPTX")
        results = []
        total_files = len(file_list)
        count_per_page = options.get("count_per_page", 1)
        sorted_files = [f for _, files in groups for f in files]
        page_batches = None
        file_positions = {fpath: idx + 1 for idx, fpath in enumerate(sorted_files)}

        if group_by_folder:
            page_batches = []
            for group_name, files in groups:
                for i in range(0, len(files), count_per_page):
                    batch = files[i:i + count_per_page]
                    page_batches.append((group_name, batch))

        total_pages = len(page_batches) if page_batches is not None else math.ceil(len(sorted_files) / count_per_page)
        if progress_cb:
            group_info = f"，{len(groups)} 个目录组" if group_by_folder else ""
            progress_cb(0, total_files, f"开始导出: 排版导出（共 {total_files} 张图，{total_pages} 页{group_info}）")

        safe_name = "排版导出"
        ext = f".{fmt.lower()}"
        out_path = Path(output_dir) / f"{safe_name}{ext}"

        counter = 1
        while out_path.exists():
            out_path = Path(output_dir) / f"{safe_name}_{counter}{ext}"
            counter += 1

        try:
            def _page_progress_cb(page_idx, page_count, batch_files, group_name=None):
                if progress_cb is None:
                    return
                positions = [file_positions.get(f, 0) for f in batch_files]
                imgs_done = max(positions) if positions else 0
                img_start = min(positions) if positions else imgs_done
                img_end = max(positions) if positions else imgs_done
                names = ", ".join(Path(f).name for f in batch_files)
                prefix = f"{group_name} - " if group_name else ""
                progress_cb(
                    imgs_done, total_files,
                    f"{prefix}第 {page_idx + 1}/{page_count} 页  [{img_start}~{img_end}]  {names}"
                )

            export_kwargs = dict(
                files=sorted_files,
                out_path=str(out_path),
                options=options,
                compressed_cache=_compressed_cache,
                overlay_layers=overlay_layers,
                overlay_excel_data=overlay_excel_data,
                page_progress_cb=_page_progress_cb,
                page_batches=page_batches,
            )
            if fmt == "PPTX":
                self._export_pptx(**export_kwargs)
            elif fmt == "PDF":
                self._export_pdf(**export_kwargs)
            elif fmt == "DOCX":
                self._export_docx(**export_kwargs)

            details = {"files_count": total_files, "pages": total_pages}
            if group_by_folder:
                details["groups"] = len(groups)
            results.append(ProcessResult(
                input_path="排版导出",
                output_path=str(out_path),
                success=True,
                details=details
            ))
        except Exception as e:
            results.append(ProcessResult(
                input_path="排版导出",
                success=False,
                error=f"{e}\n{traceback.format_exc()}"
            ))

        if progress_cb:
            progress_cb(total_files, total_files, "完成")

        # 清理压缩缓存，释放 BytesIO 内存
        for buf in _compressed_cache.values():
            buf.close()
        _compressed_cache.clear()

        return results

    def _calc_overlay_margins(self, w_cm, h_cm, overlay_layers):
        """根据 placement='reserved' 的叠加层计算图片排版区域需要避让的边距 (cm)。

        只有标记为"占用空间"的层才参与边距计算，直接使用 cm 坐标。
        判断规则：
        - 文字层：y_cm < h_cm*0.3 视为顶部，y_cm > h_cm*0.7 视为底部
        - 图片层：按 y_cm + h_cm 和 x_cm + w_cm 判断占用区域
        """
        m_top = 0.5    # 基础边距
        m_bottom = 0.5
        m_left = 0.5
        m_right = 0.5

        for layer in overlay_layers:
            if layer.get("placement", "overlay") != "reserved":
                continue  # 叠加模式不参与边距计算

            x_cm_val = layer.get("x_cm", 0)
            y_cm_val = layer.get("y_cm", 0)

            if layer.get("type") == "text":
                # 文字层：估算占用高度 ≈ font_size_pt / 28.35 cm + 0.3cm 间距
                fs = layer.get("font_size_pt", 12)
                text_h_cm = fs / 28.35 + 0.3

                if y_cm_val < h_cm * 0.3:
                    # 顶部区域
                    m_top = max(m_top, y_cm_val + text_h_cm + 0.2)
                elif y_cm_val > h_cm * 0.7:
                    # 底部区域
                    m_bottom = max(m_bottom, h_cm - y_cm_val + 0.2)

            elif layer.get("type") == "image":
                ow_cm = layer.get("w_cm", 5)
                oh_cm = layer.get("h_cm", 3)
                bottom_edge = y_cm_val + oh_cm
                right_edge = x_cm_val + ow_cm

                if bottom_edge <= h_cm * 0.3:
                    # 顶部区域
                    m_top = max(m_top, bottom_edge + 0.2)
                elif y_cm_val >= h_cm * 0.7:
                    # 底部区域
                    m_bottom = max(m_bottom, h_cm - y_cm_val + 0.2)

                if right_edge >= w_cm * 0.7 and bottom_edge <= h_cm * 0.3:
                    # 右上角
                    m_top = max(m_top, bottom_edge + 0.2)
                elif x_cm_val <= w_cm * 0.3 and bottom_edge <= h_cm * 0.3:
                    # 左上角
                    m_top = max(m_top, bottom_edge + 0.2)

        return m_top, m_bottom, m_left, m_right

    def _calc_layout(self, w_cm, h_cm, img_sizes, options=None):
        """计算图片在页面上的精确坐标和尺寸。

        参数:
            w_cm, h_cm: 页面尺寸 (cm)
            img_sizes: [(img_w_px, img_h_px), ...] 每张图的像素尺寸
            options: 包含 max_cols, row_align, overlay_layers 等

        排版规则：
            - 自动根据叠加层位置计算安全边距，图片排版只在安全区域内
            - 宽度按列均分，高度按每张图实际比例计算
            - 逐行计算行高，整体垂直居中
            - 超高时等比缩小
            - row_align: 'left'(从左到右) / 'center'(居中) / 'right'(从右到左)

        返回: (layouts, rows, cols)
        """
        if options is None:
            options = {}

        overlay_layers = options.get("overlay_layers", [])
        m_top, m_bottom, m_left, m_right = self._calc_overlay_margins(w_cm, h_cm, overlay_layers)

        max_cols = options.get("max_cols", 4)
        row_align = options.get("row_align", "left")
        gap = 0.3

        count = len(img_sizes)
        if count == 0:
            return [], 0, 0

        aw = w_cm - m_left - m_right
        ah = h_cm - m_top - m_bottom
        if aw <= 0 or ah <= 0:
            return [], 0, 0

        cols = min(count, max_cols)
        rows = math.ceil(count / cols)

        cell_w = (aw - gap * (cols - 1)) / cols

        # 按行分组，计算每行行高
        row_groups = []
        row_heights = []
        for r in range(rows):
            start = r * cols
            end = min(start + cols, count)
            row_groups.append((start, end))

            max_h = 0
            for idx in range(start, end):
                iw, ih = img_sizes[idx]
                if iw <= 0:
                    iw = 1
                img_h_cm = cell_w * ih / iw
                if img_h_cm > max_h:
                    max_h = img_h_cm
            row_heights.append(max_h)

        total_h = sum(row_heights) + gap * (rows - 1)

        # 超高等比缩小
        scale = 1.0
        if total_h > ah:
            scale = ah / total_h
            cell_w *= scale
            row_heights = [h * scale for h in row_heights]
            total_h = ah

        # 垂直居中
        v_offset = (ah - total_h) / 2

        layouts = []
        current_y = m_top + v_offset
        for r, (start, end) in enumerate(row_groups):
            row_h = row_heights[r]
            row_count = end - start  # 本行实际图片数（最后行可能不满）

            # 计算本行实际总宽度（用于居中/右对齐）
            row_total_w = row_count * cell_w + (row_count - 1) * gap

            if row_align == "center":
                row_x_start = m_left + (aw - row_total_w) / 2
            elif row_align == "right":
                row_x_start = m_left + (aw - row_total_w)
            else:  # left（默认）
                row_x_start = m_left

            for idx in range(start, end):
                c = idx - start
                iw, ih = img_sizes[idx]
                if iw <= 0:
                    iw = 1
                img_w_cm = cell_w
                img_h_cm = cell_w * ih / iw

                x = row_x_start + c * (cell_w + gap)
                y = current_y + (row_h - img_h_cm) / 2
                layouts.append((x, y, img_w_cm, img_h_cm))

            current_y += row_h + gap

        return layouts, rows, cols

    def _get_fit_size(self, img_w, img_h, box_w, box_h):
        """保持比例缩放，返回 (w, h)"""
        scale = min(box_w / img_w, box_h / img_h)
        return img_w * scale, img_h * scale

    # ── 叠加层辅助 ────────────────────────────────────────────────────────────

    def _get_overlay_text(self, layer: dict, page_idx: int, overlay_excel_data: dict, batch_files: list = None, layer_idx: int = 0) -> str:
        """获取文字层在当前页的文字内容

        Args:
            layer: 叠加层配置字典
            page_idx: 页索引
            overlay_excel_data: Excel数据映射 {layer_idx: {filename_lower: data_value}}
            batch_files: 当前页的文件列表（用于filename数据源和Excel匹配）
            layer_idx: 当前层在overlay_layers中的索引
        """
        source = layer.get("source", "fixed")

        if source == "excel":
            # 使用新的Excel数据结构
            data_map = overlay_excel_data.get(layer_idx, {})
            if batch_files:
                # 使用当前页第一张图片的文件名进行匹配
                file_stem = Path(batch_files[0]).stem.lower()
                return data_map.get(file_stem, "")
            return ""
        elif source == "filename":
            # 使用当前页第一张图片的文件名（不含扩展名）
            if batch_files:
                return Path(batch_files[0]).stem
            return ""
        else:  # fixed
            return layer.get("text", "")

    def _open_image_for_export(self, fpath: str, compressed_cache: dict):
        """打开图片，优先使用压缩缓存。返回独立的 Image 对象（不持有文件句柄）"""
        try:
            if fpath in compressed_cache:
                buf = compressed_cache[fpath]
                buf.seek(0)
                img = Image.open(buf)
                img.load()  # 强制读入内存
                return img
            img = Image.open(fpath)
            img.load()
            return img
        except Exception:
            # 兜底：返回一个 1x1 的占位图
            return Image.new("RGB", (1, 1), (128, 128, 128))

    # ── 导出方法 ──────────────────────────────────────────────────────────────

    def _export_pptx(self, files, out_path, options,
                     compressed_cache=None, overlay_layers=None, overlay_excel_data=None,
                     page_progress_cb=None, page_batches=None):
        compressed_cache = compressed_cache or {}
        overlay_layers = overlay_layers or []
        overlay_excel_data = overlay_excel_data or {}

        pptx, Cm, Pt = _import_pptx()
        from pptx.util import Pt as PtU
        from pptx.dml.color import RGBColor
        prs = pptx.Presentation()

        w_cm = options["width_cm"]
        h_cm = options["height_cm"]
        prs.slide_width = Cm(w_cm)
        prs.slide_height = Cm(h_cm)
        blank_layout = prs.slide_layouts[6]

        count = options["count_per_page"]
        keep_ratio = options["keep_ratio"]
        if page_batches is None:
            page_batches = [(None, files[i:i + count]) for i in range(0, len(files), count)]
        total_pages = len(page_batches)

        page_idx = 0
        for group_name, batch in page_batches:
            slide = prs.slides.add_slide(blank_layout)

            # 读取每张图的实际尺寸
            img_sizes = []
            for f in batch:
                img = self._open_image_for_export(f, compressed_cache)
                img_sizes.append(img.size)
                img.close()

            boxes, _, _ = self._calc_layout(w_cm, h_cm, img_sizes, options)
            for j, f in enumerate(batch):
                bx, by, bw, bh = boxes[j]
                # layout 已按实际比例计算精确尺寸，直接使用
                if f in compressed_cache:
                    compressed_cache[f].seek(0)
                    slide.shapes.add_picture(compressed_cache[f], Cm(bx), Cm(by), Cm(bw), Cm(bh))
                else:
                    slide.shapes.add_picture(f, Cm(bx), Cm(by), Cm(bw), Cm(bh))

            # 叠加层
            for layer_idx, layer in enumerate(overlay_layers):
                x_cm = layer.get("x_cm", 0)
                y_cm = layer.get("y_cm", 0)
                if layer["type"] == "text":
                    text = self._get_overlay_text(layer, page_idx, overlay_excel_data, batch, layer_idx)
                    if not text:
                        continue
                    txBox = slide.shapes.add_textbox(Cm(x_cm), Cm(y_cm), Cm(w_cm * 0.5), Cm(h_cm * 0.1))
                    tf = txBox.text_frame
                    tf.word_wrap = False
                    p = tf.paragraphs[0]
                    run = p.add_run()
                    run.text = text
                    run.font.size = PtU(layer.get("font_size_pt", 12))
                    color_hex = layer.get("color", "#000000").lstrip("#")
                    run.font.color.rgb = RGBColor(
                        int(color_hex[0:2], 16),
                        int(color_hex[2:4], 16),
                        int(color_hex[4:6], 16)
                    )
                elif layer["type"] == "image":
                    img_path = layer.get("path", "")
                    if not img_path or not os.path.exists(img_path):
                        continue
                    ow_cm = layer.get("w_cm", 5)
                    oh_cm = layer.get("h_cm", 3)
                    slide.shapes.add_picture(img_path, Cm(x_cm), Cm(y_cm), Cm(ow_cm), Cm(oh_cm))

            if page_progress_cb:
                page_progress_cb(page_idx, total_pages, batch, group_name)
            page_idx += 1
        prs.save(out_path)

    def _export_pdf(self, files, out_path, options,
                    compressed_cache=None, overlay_layers=None, overlay_excel_data=None,
                    page_progress_cb=None, page_batches=None):
        compressed_cache = compressed_cache or {}
        overlay_layers = overlay_layers or []
        overlay_excel_data = overlay_excel_data or {}

        canvas_mod, cm = _import_reportlab()
        from reportlab.lib.colors import HexColor

        w_cm = options["width_cm"]
        h_cm = options["height_cm"]
        c = canvas_mod.Canvas(out_path, pagesize=(w_cm * cm, h_cm * cm))

        count = options["count_per_page"]
        keep_ratio = options["keep_ratio"]
        if page_batches is None:
            page_batches = [(None, files[i:i + count]) for i in range(0, len(files), count)]
        total_pages = len(page_batches)

        page_idx = 0
        for group_name, batch in page_batches:

            # 读取每张图的实际尺寸
            img_sizes = []
            for f in batch:
                img = self._open_image_for_export(f, compressed_cache)
                img_sizes.append(img.size)
                img.close()

            boxes, _, _ = self._calc_layout(w_cm, h_cm, img_sizes, options)
            for j, f in enumerate(batch):
                bx, by, bw, bh = boxes[j]
                # PDF 坐标原点在左下角，y 需要翻转
                pdf_y = h_cm - by - bh
                if f in compressed_cache:
                    compressed_cache[f].seek(0)
                    from reportlab.lib.utils import ImageReader
                    reader = ImageReader(compressed_cache[f])
                    try:
                        c.drawImage(reader, bx * cm, pdf_y * cm, width=bw * cm, height=bh * cm)
                    finally:
                        image = getattr(reader, "_image", None)
                        if image is not None:
                            image.close()
                else:
                    c.drawImage(f, bx * cm, pdf_y * cm, width=bw * cm, height=bh * cm)

            # 叠加层
            for layer_idx, layer in enumerate(overlay_layers):
                x_cm_val = layer.get("x_cm", 0)
                y_cm_val = layer.get("y_cm", 0)
                if layer["type"] == "text":
                    text = self._get_overlay_text(layer, page_idx, overlay_excel_data, batch, layer_idx)
                    if not text:
                        continue
                    # PDF 坐标原点在左下，y 需要翻转
                    pdf_ty = (h_cm - y_cm_val) * cm
                    color_hex = layer.get("color", "#000000")
                    c.setFillColor(HexColor(color_hex))
                    font_size = layer.get("font_size_pt", 12)
                    c.setFont("Helvetica", font_size)
                    c.drawString(x_cm_val * cm, pdf_ty, text)
                elif layer["type"] == "image":
                    img_path = layer.get("path", "")
                    if not img_path or not os.path.exists(img_path):
                        continue
                    ow_cm = layer.get("w_cm", 5)
                    oh_cm = layer.get("h_cm", 3)
                    pdf_iy = (h_cm - y_cm_val - oh_cm) * cm
                    c.drawImage(img_path, x_cm_val * cm, pdf_iy, width=ow_cm * cm, height=oh_cm * cm, preserveAspectRatio=True, mask='auto')

            c.showPage()
            if page_progress_cb:
                page_progress_cb(page_idx, total_pages, batch, group_name)
            page_idx += 1
        c.save()

    def _export_docx(self, files, out_path, options,
                     compressed_cache=None, overlay_layers=None, overlay_excel_data=None,
                     page_progress_cb=None, page_batches=None):
        """DOCX 导出（注：DOCX 不支持绝对坐标叠加，文字层以页脚/段落形式追加）"""
        compressed_cache = compressed_cache or {}
        overlay_layers = overlay_layers or []
        overlay_excel_data = overlay_excel_data or {}

        docx, Cm, Pt = _import_docx()
        doc = docx.Document()

        section = doc.sections[-1]
        section.page_width = Cm(options["width_cm"])
        section.page_height = Cm(options["height_cm"])
        section.top_margin = Cm(0.5)
        section.bottom_margin = Cm(0.5)
        section.left_margin = Cm(0.5)
        section.right_margin = Cm(0.5)

        count = options["count_per_page"]
        keep_ratio = options["keep_ratio"]
        if page_batches is None:
            page_batches = [(None, files[i:i + count]) for i in range(0, len(files), count)]
        total_pages = len(page_batches)

        page_idx = 0
        for group_name, batch in page_batches:

            # 读取每张图的实际尺寸
            img_sizes = []
            for f in batch:
                img = self._open_image_for_export(f, compressed_cache)
                img_sizes.append(img.size)
                img.close()

            boxes, rows, cols = self._calc_layout(options["width_cm"], options["height_cm"], img_sizes, options)
            if len(batch) == 1:
                p = doc.add_paragraph()
                p.alignment = 1
                r = p.add_run()
                bx, by, bw, bh = boxes[0]
                if batch[0] in compressed_cache:
                    compressed_cache[batch[0]].seek(0)
                    r.add_picture(compressed_cache[batch[0]], width=Cm(bw), height=Cm(bh))
                else:
                    r.add_picture(batch[0], width=Cm(bw), height=Cm(bh))
            else:
                table = doc.add_table(rows=rows, cols=cols)
                for j, f in enumerate(batch):
                    row_idx = j // cols
                    col_idx = j % cols
                    cell = table.cell(row_idx, col_idx)
                    p = cell.paragraphs[0]
                    p.alignment = 1
                    r = p.add_run()
                    bx, by, bw, bh = boxes[j]
                    if f in compressed_cache:
                        compressed_cache[f].seek(0)
                        r.add_picture(compressed_cache[f], width=Cm(bw), height=Cm(bh))
                    else:
                        r.add_picture(f, width=Cm(bw), height=Cm(bh))

            # DOCX 叠加层：以段落形式追加（DOCX 不支持绝对定位，以注释形式说明位置）
            text_overlays = [(i, l) for i, l in enumerate(overlay_layers) if l.get("type") == "text"]
            img_overlays = [l for l in overlay_layers if l.get("type") == "image"]
            for layer_idx, layer in text_overlays:
                text = self._get_overlay_text(layer, page_idx, overlay_excel_data, batch, layer_idx)
                if text:
                    op = doc.add_paragraph(f"[叠加文字 X:{layer.get('x_cm', 0)}cm Y:{layer.get('y_cm', 0)}cm] {text}")
                    op.runs[0].font.size = Pt(layer.get("font_size_pt", 12))
            for layer in img_overlays:
                img_path = layer.get("path", "")
                if img_path and os.path.exists(img_path):
                    op = doc.add_paragraph(f"[叠加图片 X:{layer.get('x_cm', 0)}cm Y:{layer.get('y_cm', 0)}cm]")
                    op.add_run().add_picture(
                        img_path,
                        width=Cm(layer.get("w_cm", 5))
                    )

            if page_idx + 1 < total_pages:
                doc.add_page_break()
            if page_progress_cb:
                page_progress_cb(page_idx, total_pages, batch, group_name)
            page_idx += 1

        doc.save(out_path)

