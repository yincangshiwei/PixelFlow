"""批处理日志格式化（纯逻辑，无 Qt）。

把既有 MainWindow._on_image_done / _on_file_done / _format_duration /
_format_matting_start_log 的展示逻辑抽为纯函数，供 ActionBarRoute 使用，
文案与重构前逐字一致。
"""
from __future__ import annotations

from pathlib import Path


def format_duration(seconds: float) -> str:
    """将秒数格式化为可读时长，如 12.3 秒 / 1 分 05.2 秒 / 1 小时 02 分 03.1 秒。"""
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    s_whole, ms = divmod(total_ms, 1000)
    h, rem = divmod(s_whole, 3600)
    m, s = divmod(rem, 60)
    frac = f"{s}.{ms:03d}".rstrip("0").rstrip(".")
    if h > 0:
        return f"{h} 小时 {m:02d} 分 {frac} 秒（共 {seconds:.3f} 秒）"
    if m > 0:
        return f"{m} 分 {frac} 秒（共 {seconds:.3f} 秒）"
    return f"{frac} 秒"


def format_image_result_line(result) -> str:
    """单张图片处理结果日志行（成功含详情，失败含首行错误）。"""
    name = Path(result.input_path).name
    if result.success:
        d = result.details or {}
        # 批量合并处理器（如排版导出）的结果
        if result.input_path.startswith("分组:"):
            group_label = result.input_path  # e.g. "分组: 排版导出"
            files_count = d.get("files_count", "?")
            pages = d.get("pages", "?")
            out_name = Path(result.output_path).name if result.output_path else ""
            return f"✓ {group_label}  共 {files_count} 张图 → {pages} 页  →  {out_name}"
        parts = [f"✓ {name}"]
        if "matting_model" in d:
            refine = "+精炼" if d.get("matting_refine") else ""
            batch_n = d.get("matting_batch")
            dev = str(d.get("matting_device") or "").upper()
            path = d.get("matting_path") or ""
            matting_bits = [f"抠图→{d['matting_model']}{refine}"]
            if batch_n:
                matting_bits.append(f"batch={batch_n}")
            if dev:
                matting_bits.append(dev)
            if path == "mask_prescale":
                matting_bits.append("mask")
            elif path == "refine_rgba":
                matting_bits.append("精炼全尺寸")
            if d.get("matting_pipeline"):
                matting_bits.append("流水线")
            parts.append(" · ".join(matting_bits))
        if "trimmed_size" in d:
            parts.append(f"裁剪→{d['trimmed_size'][0]}×{d['trimmed_size'][1]}")
        if d.get("matting_keep_path"):
            parts.append(f"抠图副本→{Path(d['matting_keep_path']).name}")
        elif d.get("matting_keep_error"):
            parts.append(f"抠图副本失败:{d['matting_keep_error']}")
        if "layout_display_size" in d:
            size = d["layout_display_size"]
            parts.append(
                f"主体→{size[0]}×{size[1]}（画布占比 {d.get('subject_percent', '?')}%）"
            )
        elif "resized_size" in d:
            parts.append(f"缩放→{d['resized_size'][0]}×{d['resized_size'][1]}")
        if "canvas_size" in d:
            parts.append(f"画布→{d['canvas_size'][0]}×{d['canvas_size'][1]}")
        if "compress_info" in d:
            parts.append(f"压缩→{d['compress_info']}")
        if "dpi" in d:
            parts.append(f"DPI→{d['dpi']}")
        if "output_format" in d:
            parts.append(f"格式→{d['output_format'].upper()}")
        if d.get("fake_extension") or d.get("format_note"):
            parts.append(f"格式识别→{d.get('format_note') or d.get('true_format', '?')}")
        if d.get("converted"):
            parts.append(f"转换→{(d.get('output_format') or d.get('format', '?')).upper()}")
        elif d.get("convert_skipped"):
            parts.append("转换→已是目标格式(跳过重编码)")
        if d.get("skipped"):
            parts.append(f"跳过→{d.get('skip_reason', '不支持')}")
        elif d.get("cleared"):
            parts.append("元数据→已清除")
        elif d.get("fields_written"):
            parts.append("元数据→" + ", ".join(d["fields_written"]))
        if d.get("fields_dropped"):
            parts.append("忽略字段→" + "/".join(d["fields_dropped"]))
        if d.get("warning"):
            parts.append(f"⚠{d['warning']}")
        return "  ".join(parts)
    error = str(result.error)
    return f"✗ {name}  错误: {error.splitlines()[0] if error else ''}"


def format_file_result_line(result) -> str:
    """单个文件处理结果日志行（BaseFileProcessor 结果）。"""
    name = Path(result.input_path).name
    if result.success:
        d = result.details or {}
        parts = [f"✓ {name}"]
        if "paragraphs" in d:
            parts.append(f"段落数: {d['paragraphs']}")
        if "tables" in d:
            parts.append(f"表格数: {d['tables']}")
        if "pages" in d:
            parts.append(f"页数: {d['pages']}")
        if "chars" in d:
            parts.append(f"字符数: {d['chars']}")
        return "  ".join(parts)
    error = str(result.error)
    return f"✗ {name}  错误: {error.splitlines()[0] if error else ''}"


def format_multiline_error_debug(result) -> str | None:
    """失败结果为多行错误时，追加完整错误信息（仅写后台日志文件）。"""
    error = str(result.error)
    if "\n" in error:
        name = Path(result.input_path).name
        return f"{name} 完整错误信息:\n{error}"
    return None


def format_matting_start_log(options: dict) -> str:
    """点击开始时写 AI 抠图摘要（含配置里的推理设备偏好）。"""
    mid = options.get("matting_model", "ben2")
    refine = bool(options.get("matting_refine", False))
    try:
        from core.matting.model_manager import get_matting_manager
        pref = (
            get_matting_manager().get_device_preference() or "auto"
        ).lower()
    except Exception:
        pref = "auto"
    labels = {
        "auto": "自动(优先GPU)",
        "cuda": "CUDA(GPU)",
        "cpu": "CPU",
    }
    pref_desc = labels.get(pref, pref)
    return (
        f"AI 抠图: 已开启  模型={mid}  "
        f"推理设备={pref_desc}  "
        f"边缘精炼={'开' if refine else '关'}  "
        f"（实际运行设备与 batch 见处理线程日志）"
    )
