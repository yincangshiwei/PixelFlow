"""
PixelFlow 核心图像处理模块
支持灵活的步骤组合：裁透明边 / 缩放 / 放置到画布
"""
from PIL import Image
from dataclasses import dataclass, field
from pathlib import Path
import io


def hex_to_rgba(color):
    """
    支持:
    - '#FFFFFF'
    - '#FFFFFFFF'
    - (255,255,255)
    - (255,255,255,255)
    """
    if isinstance(color, tuple):
        if len(color) == 3:
            return (*color, 255)
        elif len(color) == 4:
            return color
        else:
            raise ValueError("颜色元组必须是 RGB 或 RGBA")

    if isinstance(color, str):
        color = color.strip().lstrip('#')
        if len(color) == 6:
            r = int(color[0:2], 16)
            g = int(color[2:4], 16)
            b = int(color[4:6], 16)
            return (r, g, b, 255)
        elif len(color) == 8:
            r = int(color[0:2], 16)
            g = int(color[2:4], 16)
            b = int(color[4:6], 16)
            a = int(color[6:8], 16)
            return (r, g, b, a)

    raise ValueError("不支持的颜色格式")


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

def trim_transparent(img: Image.Image, alpha_threshold: int = 0):
    """裁掉四周透明区域"""
    img = img.convert("RGBA")
    alpha = img.getchannel("A")

    if alpha_threshold > 0:
        mask = alpha.point(lambda p: 255 if p > alpha_threshold else 0)
        bbox = mask.getbbox()
    else:
        bbox = alpha.getbbox()

    if bbox is None:
        raise ValueError("图片内容为空：整张图都是透明的")

    return img.crop(bbox), bbox


def resize_image(img: Image.Image, target_size=(800, 800), mode="contain"):
    """
    缩放图片
    mode: contain / cover / stretch
    """
    target_w, target_h = target_size
    src_w, src_h = img.size

    if mode == "stretch":
        return img.resize((target_w, target_h), Image.LANCZOS)

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

    resized = img.resize((new_w, new_h), Image.LANCZOS)

    if mode == "cover":
        left = (new_w - target_w) // 2
        top = (new_h - target_h) // 2
        right = left + target_w
        bottom = top + target_h
        resized = resized.crop((left, top, right, bottom))

    return resized


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
            canvas.paste(img, (paste_x, paste_y), img)
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
