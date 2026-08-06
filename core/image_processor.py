"""
PixelFlow 核心图像处理模块
支持灵活的步骤组合：裁透明边 / 缩放 / 放置到画布
"""
from PIL import Image, ImageFilter
from dataclasses import dataclass, field
from pathlib import Path
import io
import numpy as np


def hex_to_rgba(color):
    """
    支持:
    - '#FFFFFF' / '#RRGGBB'
    - '#RRGGBBAA'（CSS / 本项目存储约定）
    - '#AARRGGBB'（兼容旧版 Qt HexArgb 误存）
    - (255,255,255) / (255,255,255,255)

    8 位色值优先按 RRGGBBAA 解析；若 Alpha 为 0 且 RGB 非全 0，
    同时按 AARRGGBB 解释更合理时（典型：#00FFFFFF 实为透明白），自动兼容。
    """
    if isinstance(color, tuple):
        if len(color) == 3:
            return (*color, 255)
        elif len(color) == 4:
            return color
        else:
            raise ValueError("颜色元组必须是 RGB 或 RGBA")

    if isinstance(color, str):
        raw = color.strip().lstrip('#')
        if len(raw) == 6:
            r = int(raw[0:2], 16)
            g = int(raw[2:4], 16)
            b = int(raw[4:6], 16)
            return (r, g, b, 255)
        elif len(raw) == 8:
            # 主约定：#RRGGBBAA（与 rgba_to_hex / CSS 相反的 Qt HexArgb 区分开）
            r = int(raw[0:2], 16)
            g = int(raw[2:4], 16)
            b = int(raw[4:6], 16)
            a = int(raw[6:8], 16)
            # 兼容旧版颜色选择器写入的 Qt HexArgb（#AARRGGBB）：
            # 旧逻辑仅在 alpha<255 时写 8 位，故引导字节 AA<0xFF；
            # 若按 RRGGBBAA 解得 a==0xFF 且引导字节 <0xFF，则实为 ARGB。
            # 典型残值：选白 + 原生框 Alpha=0 → #00FFFFFF
            aa = int(raw[0:2], 16)
            rr = int(raw[2:4], 16)
            gg = int(raw[4:6], 16)
            bb = int(raw[6:8], 16)
            if a == 255 and aa < 255:
                return (rr, gg, bb, aa)
            return (r, g, b, a)
        elif len(raw) == 4:
            # #RGBA 短写
            r = int(raw[0] * 2, 16)
            g = int(raw[1] * 2, 16)
            b = int(raw[2] * 2, 16)
            a = int(raw[3] * 2, 16)
            return (r, g, b, a)
        elif len(raw) == 3:
            r = int(raw[0] * 2, 16)
            g = int(raw[1] * 2, 16)
            b = int(raw[2] * 2, 16)
            return (r, g, b, 255)

    raise ValueError("不支持的颜色格式")


def rgba_to_hex(color) -> str:
    """RGBA 元组或已有色值 → 统一存储串：不透明 #RRGGBB，否则 #RRGGBBAA。"""
    r, g, b, a = hex_to_rgba(color)
    if a >= 255:
        return f"#{r:02X}{g:02X}{b:02X}"
    return f"#{r:02X}{g:02X}{b:02X}{a:02X}"


def rgba_to_css_hex(color) -> str:
    """供 Qt StyleSheet 使用的 #AARRGGBB（Qt 对 8 位 hex 按 ARGB 解析）。"""
    r, g, b, a = hex_to_rgba(color)
    return f"#{a:02X}{r:02X}{g:02X}{b:02X}"


@dataclass
class ProcessResult:
    """单张图片处理结果"""
    input_path: str = ""
    output_path: str = ""
    original_size: tuple = (0, 0)
    trimmed_size: tuple = None
    trim_bbox: tuple = None
    resized_size: tuple = None
    canvas_size: tuple = None
    paste_position: tuple = None
    success: bool = True
    error: str = ""


@dataclass
class ProcessOptions:
    """处理选项"""
    # 步骤开关
    enable_trim: bool = True
    enable_resize: bool = False
    enable_canvas: bool = False

    # 裁透明边参数
    alpha_threshold: int = 0

    # 缩放参数
    resize_width: int = 800
    resize_height: int = 800
    resize_mode: str = "contain"  # contain / cover / stretch

    # 画布参数
    canvas_width: int = 1500
    canvas_height: int = 1500
    canvas_color: str = "#FFFFFF"

    # 输出
    output_format: str = "png"  # png / webp / jpg


def compress_to_target_size(img: Image.Image, target_kb: int, format_name: str, min_quality: int = 10, max_quality: int = 95) -> tuple[Image.Image, int, int]:
    """
    使用二分法寻找最接近目标大小的 quality 值，保证图片质量最优。
    返回: (处理后的图片, 最终质量, 最终大小KB)
    """
    target_bytes = target_kb * 1024
    
    if format_name.upper() not in ["JPEG", "JPG", "WEBP"]:
        # 对于不支持 quality 压缩的格式，直接返回
        buf = io.BytesIO()
        img.save(buf, format=format_name)
        return img, 100, len(buf.getvalue()) // 1024

    low = min_quality
    high = max_quality
    best_quality = min_quality
    best_size = 0

    # 先检查最低质量是否能满足
    buf = io.BytesIO()
    img.save(buf, format=format_name, quality=min_quality)
    min_size = len(buf.getvalue())
    if min_size > target_bytes:
        # 最低质量也达不到目标大小，直接返回最低质量
        return img, min_quality, min_size // 1024

    # 检查最高质量是否已经满足
    buf = io.BytesIO()
    img.save(buf, format=format_name, quality=max_quality)
    max_size = len(buf.getvalue())
    if max_size <= target_bytes:
        # 最高质量也满足，直接返回最高质量
        return img, max_quality, max_size // 1024

    # 二分查找最佳 quality
    for _ in range(8):  # 8次迭代足够收敛 (2^8 = 256)
        if low > high:
            break
        mid = (low + high) // 2
        buf = io.BytesIO()
        img.save(buf, format=format_name, quality=mid)
        size = len(buf.getvalue())

        if size <= target_bytes:
            best_quality = mid
            best_size = size
            low = mid + 1  # 尝试更高的质量，看是否还能满足
        else:
            high = mid - 1 # 质量太高导致文件太大，需要降低

    return img, best_quality, best_size // 1024

def _bridge_short_gaps(flags: np.ndarray, bridge_gap: int) -> np.ndarray:
    """桥接夹在两段 True 之间、长度不超过 bridge_gap 的 False 空隙。"""
    if bridge_gap <= 0:
        return flags
    flags = flags.copy()
    n = int(flags.size)
    i = 0
    while i < n:
        if flags[i]:
            i += 1
            continue
        j = i
        while j < n and not flags[j]:
            j += 1
        gap = j - i
        if gap <= bridge_gap and i > 0 and j < n and flags[i - 1] and flags[j]:
            flags[i:j] = True
        i = j
    return flags


def _iter_true_runs(flags: np.ndarray) -> list[tuple[int, int]]:
    """返回一维 bool 数组中所有连续 True 区间 [start, end]（含端点）。"""
    runs: list[tuple[int, int]] = []
    n = int(flags.size)
    start: int | None = None
    for i, v in enumerate(flags.tolist()):
        if v and start is None:
            start = i
        elif not v and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, n - 1))
    return runs


def _significant_run_span(
    flags: np.ndarray,
    bridge_gap: int = 0,
    *,
    min_ratio: float = 0.12,
    min_abs: int = 3,
) -> tuple[int, int] | None:
    """
    在一维投影上求「所有显著内容段」的并集区间。

    旧逻辑只保留最长连续段，双物品/多物品中间有透明缝时会裁掉其余物品。
    现改为：桥接主体内部细缝后，保留长度达到最长段一定比例（或绝对下限）
    的所有段，再取并集；边缘断裂半透明噪点通常很短，会被滤掉。
    """
    n = int(flags.size)
    if n == 0 or not bool(flags.any()):
        return None

    bridged = _bridge_short_gaps(flags, bridge_gap)
    runs = _iter_true_runs(bridged)
    if not runs:
        return None

    lengths = [end - start + 1 for start, end in runs]
    longest = max(lengths)
    # 相对最长段过短的视为噪点；绝对下限避免极小图把有效短段滤光
    thr = max(int(min_abs), int(round(longest * float(min_ratio))))
    kept = [run for run, L in zip(runs, lengths) if L >= thr]
    if not kept:
        # 回退：至少保留最长段
        idx = int(np.argmax(np.asarray(lengths)))
        kept = [runs[idx]]

    left = min(s for s, _ in kept)
    right = max(e for _, e in kept)
    return left, right


def trim_transparent(img: Image.Image, alpha_threshold: int = 0):
    """
    裁掉四周透明区域。

    不只做简单的 alpha.getbbox()：AI 抠图后边缘常残留与主体不相连的半透明噪点，
    会把包围盒撑满整张图，导致某一侧已贴边、另一侧仍有大片空白却裁不掉。

    策略：对行列投影收集「所有显著连续内容段」取并集（多物品中间透明缝不会互裁），
    再在并集范围内求精确 bbox，从而忽略四角/对边的断裂短噪点。
    """
    img = img.convert("RGBA")
    alpha = np.asarray(img.getchannel("A"))
    thr = max(0, int(alpha_threshold))
    content = alpha > thr

    if not content.any():
        raise ValueError("图片内容为空：整张图都是透明的")

    h, w = content.shape
    # 短空隙桥接：约 0.15% 边长，最少 1px、最多 8px，避免主体 antialias 细缝拆段
    bridge = max(1, min(8, int(round(min(w, h) * 0.0015))))

    col_run = _significant_run_span(content.any(axis=0), bridge_gap=bridge)
    row_run = _significant_run_span(content.any(axis=1), bridge_gap=bridge)
    if col_run is None or row_run is None:
        raise ValueError("图片内容为空：整张图都是透明的")

    left, right = col_run
    top, bottom = row_run
    # 在主体投影带内再收紧到真实像素（去掉投影带内局部全透明边）
    sub = content[top : bottom + 1, left : right + 1]
    ys, xs = np.where(sub)
    if ys.size == 0:
        raise ValueError("图片内容为空：整张图都是透明的")

    l = left + int(xs.min())
    r = left + int(xs.max()) + 1
    t = top + int(ys.min())
    b = top + int(ys.max()) + 1
    bbox = (l, t, r, b)
    return img.crop(bbox), bbox


def _pick_resample(scale: float) -> Image.Resampling:
    """
    按缩放比选择重采样核（贴近专业软件的 automatic 策略）：
    - 大幅缩小：BOX（区域平均，振铃少）
    - 一般缩小：LANCZOS
    - 放大：BICUBIC（更平滑，避免 LANCZOS 放大过锐振铃）
    """
    if scale < 0.5:
        return Image.Resampling.BOX
    if scale < 1.0:
        return Image.Resampling.LANCZOS
    if scale > 1.0:
        return Image.Resampling.BICUBIC
    return Image.Resampling.NEAREST


def _resize_rgba_premultiplied(
    img: Image.Image,
    size: tuple[int, int],
    resample: Image.Resampling,
) -> Image.Image:
    """
    RGBA 预乘 Alpha 后重采样，再反预乘。

    Pillow 默认对 R/G/B/A 独立滤波，半透明边缘易产生灰边/色晕；
    预乘后再缩放是合成软件的标准做法，轮廓更干净。
    """
    tw, th = int(size[0]), int(size[1])
    if tw < 1 or th < 1:
        raise ValueError("目标尺寸必须大于 0")

    src = img.convert("RGBA") if img.mode != "RGBA" else img
    if src.size == (tw, th):
        return src.copy()

    arr = np.asarray(src, dtype=np.float32)
    rgb = arr[:, :, :3]
    alpha = arr[:, :, 3]
    a = alpha / 255.0
    premul = rgb * a[..., None]

    premul_img = Image.fromarray(np.clip(premul, 0.0, 255.0).astype(np.uint8), mode="RGB")
    alpha_img = Image.fromarray(np.clip(alpha, 0.0, 255.0).astype(np.uint8), mode="L")
    premul_r = np.asarray(premul_img.resize((tw, th), resample), dtype=np.float32)
    alpha_r = np.asarray(alpha_img.resize((tw, th), resample), dtype=np.float32)

    a_r = alpha_r / 255.0
    out = np.zeros((th, tw, 4), dtype=np.float32)
    out[:, :, 3] = alpha_r
    # A≈0 处 RGB 置 0，避免除零噪声
    safe = np.maximum(a_r, 1e-6)
    visible = a_r > 1e-6
    for c in range(3):
        ch = premul_r[:, :, c] / safe
        out[:, :, c] = np.where(visible, ch, 0.0)

    return Image.fromarray(np.clip(out, 0.0, 255.0).astype(np.uint8), mode="RGBA")


def _resize_high_quality(
    img: Image.Image,
    size: tuple[int, int],
    scale: float,
    *,
    detail_restore: str = "normal",
) -> Image.Image:
    """按比例一次缩放到精确像素尺寸；RGBA 走预乘路径。"""
    tw, th = int(size[0]), int(size[1])
    if img.size == (tw, th):
        return img.copy()

    resample = _pick_resample(scale)
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        resized = _resize_rgba_premultiplied(img.convert("RGBA"), (tw, th), resample)
    else:
        resized = img.resize((tw, th), resample)
        if resized.mode not in ("RGB", "L"):
            resized = resized.convert("RGB")

    return _restore_resize_detail(resized, scale, level=detail_restore)


def _restore_resize_detail(
    img: Image.Image,
    scale: float,
    *,
    level: str = "normal",
) -> Image.Image:
    """补偿缩小时重采样带来的轻微软化，不改变 Alpha 通道。"""
    level = (level or "normal").lower()
    if level in ("off", "none", "0") or scale >= 1.0 or min(img.size) < 3:
        return img

    alpha = img.getchannel("A") if img.mode == "RGBA" else None
    color = img.convert("RGB") if alpha is not None else img

    # 缩得越多，半径/强度略增；默认 normal，subtle 更克制
    t = 1.0 - scale
    if level == "subtle":
        radius = min(0.95, max(0.45, 0.4 + t * 0.45))
        percent = min(110, max(55, round(50 + t * 45)))
        threshold = 2
    else:  # normal
        radius = min(1.2, max(0.6, 0.55 + t * 0.65))
        percent = min(135, max(75, round(70 + t * 65)))
        threshold = 3

    color = color.filter(
        ImageFilter.UnsharpMask(radius=radius, percent=percent, threshold=threshold)
    )
    if alpha is not None:
        color.putalpha(alpha)
    return color


def resize_image(img: Image.Image, target_size=(800, 800), mode="contain"):
    """
    高质量缩放图片。
    mode: contain / cover / stretch

    RGBA 使用预乘 Alpha 一次重采样；缩小时对颜色细节做轻量恢复，
    Alpha 通道不参与锐化，避免抠图边缘锯齿或光晕。
    """
    target_w, target_h = (int(target_size[0]), int(target_size[1]))
    if target_w < 1 or target_h < 1:
        raise ValueError("目标尺寸必须大于 0")

    src_w, src_h = img.size

    if mode == "stretch":
        if (src_w, src_h) == (target_w, target_h):
            return img.copy()
        scale = min(target_w / src_w, target_h / src_h)
        return _resize_high_quality(img, (target_w, target_h), scale)

    scale_x = target_w / src_w
    scale_y = target_h / src_h

    if mode == "contain":
        scale = min(scale_x, scale_y)
    elif mode == "cover":
        scale = max(scale_x, scale_y)
    else:
        raise ValueError("mode 只能是 contain / cover / stretch")

    new_w = max(1, round(src_w * scale))
    new_h = max(1, round(src_h * scale))
    if (new_w, new_h) == (src_w, src_h):
        resized = img.copy()
    else:
        resized = _resize_high_quality(img, (new_w, new_h), scale)

    if mode == "cover":
        left = (new_w - target_w) // 2
        top = (new_h - target_h) // 2
        right = left + target_w
        bottom = top + target_h
        resized = resized.crop((left, top, right, bottom))

    return resized


def place_subject_on_canvas(
    asset: Image.Image,
    canvas_size: tuple[int, int],
    *,
    subject_percent: int = 80,
    canvas_color: str = "#FFFFFF",
    detail_restore: str = "normal",
) -> tuple[Image.Image, dict]:
    """
    将全分辨率主体（asset）按智能对象思路一次栅格化到画布。

    - asset：质量基准，本函数不会写回调用方引用
    - 始终按主体占比缩放（可缩小也可放大），保证批量标准化呈现一致
    - 几何用浮点 scale 算到精确显示尺寸后只采样一次
    - 画布/显示小于源时必然丢细节；源本身偏小时放大无法创造真实细节
    """
    asset_rgba = asset.convert("RGBA")
    src_w, src_h = asset_rgba.size
    if src_w < 1 or src_h < 1:
        raise ValueError("主体尺寸无效")

    cw = max(1, int(canvas_size[0]))
    ch = max(1, int(canvas_size[1]))
    percent = max(1, min(100, int(subject_percent)))
    safe_w = max(1.0, cw * percent / 100.0)
    safe_h = max(1.0, ch * percent / 100.0)

    # 始终贴合安全框（标准化构图优先）
    scale = min(safe_w / src_w, safe_h / src_h)

    dst_w = max(1, int(round(src_w * scale)))
    dst_h = max(1, int(round(src_h * scale)))
    # round 可能越出安全框/画布 1px：用同一 scale 再收紧，保持宽高比
    if dst_w > cw or dst_h > ch or dst_w > safe_w or dst_h > safe_h:
        scale = min(cw / src_w, ch / src_h, safe_w / src_w, safe_h / src_h)
        dst_w = max(1, int(round(src_w * scale)))
        dst_h = max(1, int(round(src_h * scale)))
        dst_w = min(dst_w, cw)
        dst_h = min(dst_h, ch)

    # 与实际输出尺寸对齐的有效 scale（供锐化/日志）
    eff_scale = min(dst_w / src_w, dst_h / src_h)

    if (dst_w, dst_h) == (src_w, src_h):
        subject = asset_rgba.copy()
        resample_name = "copy"
    else:
        resample = _pick_resample(eff_scale)
        resample_name = resample.name if hasattr(resample, "name") else str(resample)
        subject = _resize_high_quality(
            asset_rgba,
            (dst_w, dst_h),
            eff_scale,
            detail_restore=detail_restore,
        )

    canvas = Image.new("RGBA", (cw, ch), hex_to_rgba(canvas_color))
    px = (cw - subject.size[0]) // 2
    py = (ch - subject.size[1]) // 2
    canvas.alpha_composite(subject, dest=(px, py))

    info = {
        "asset_size": (src_w, src_h),
        "layout_display_size": subject.size,
        "subject_percent": percent,
        "canvas_size": (cw, ch),
        "paste_pos": (px, py),
        "layout_scale": round(eff_scale, 6),
        "layout_resample": resample_name,
        "layout_detail_restore": detail_restore,
    }
    return canvas, info


def process_single_image(input_path: str, output_path: str, options: ProcessOptions) -> ProcessResult:
    """处理单张图片，根据选项灵活组合步骤"""
    result = ProcessResult(input_path=input_path, output_path=output_path)

    try:
        img = Image.open(input_path).convert("RGBA")
        result.original_size = img.size

        # 步骤1：裁透明边
        if options.enable_trim:
            img, bbox = trim_transparent(img, alpha_threshold=options.alpha_threshold)
            result.trim_bbox = bbox
            result.trimmed_size = img.size

        # 步骤2：缩放
        if options.enable_resize:
            target = (options.resize_width, options.resize_height)
            img = resize_image(img, target_size=target, mode=options.resize_mode)
            result.resized_size = img.size

        # 步骤3：放置到画布
        if options.enable_canvas:
            canvas_size = (options.canvas_width, options.canvas_height)
            canvas_rgba = hex_to_rgba(options.canvas_color)
            canvas = Image.new("RGBA", canvas_size, canvas_rgba)

            img_w, img_h = img.size
            paste_x = (canvas_size[0] - img_w) // 2
            paste_y = (canvas_size[1] - img_h) // 2
            canvas.alpha_composite(img, dest=(paste_x, paste_y))
            img = canvas

            result.canvas_size = canvas_size
            result.paste_position = (paste_x, paste_y)

        # 保存
        out = Path(output_path)
        if options.output_format == "jpg":
            img = img.convert("RGB")
            img.save(str(out), "JPEG", quality=95)
        elif options.output_format == "webp":
            img.save(str(out), "WEBP", quality=95)
        else:
            img.save(str(out), "PNG")

        result.success = True

    except Exception as e:
        result.success = False
        result.error = str(e)

    return result
