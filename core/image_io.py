"""统一图像加载入口。

- 常规格式：Pillow ``Image.open``
- 相机 RAW：可选依赖 ``rawpy``（LibRaw），解码为 RGB 后交给后续流水线

约定：
- 业务侧凡「读用户源图」均应走 ``load_image``，避免散落 ``Image.open`` 漏接 RAW
- RAW **不可写回**；输出侧用 ``coerce_writable_format`` 强制为 PNG/JPG 等可写格式
- core 不依赖 services；RAW 扩展名集合以本模块为权威源，importing.constants 再引用
"""
from __future__ import annotations

from pathlib import Path
from typing import Union

from PIL import Image

PathLike = Union[str, Path]

# 常见相机 RAW 扩展名（小写，带点）
RAW_EXTS: frozenset[str] = frozenset({
    ".cr2", ".cr3",           # Canon
    ".nef", ".nrw",           # Nikon
    ".arw", ".srf", ".sr2",   # Sony
    ".dng",                   # Adobe DNG / 通用
    ".orf",                   # Olympus
    ".rw2",                   # Panasonic
    ".pef", ".ptx",           # Pentax
    ".raf",                   # Fujifilm
    ".srw",                   # Samsung
    ".x3f",                   # Sigma
    ".raw", ".rwl",           # 通用 / Leica
    ".3fr", ".fff",           # Hasselblad
    ".mef", ".mos",           # Mamiya / Leaf
    ".iiq",                   # Phase One
    ".kdc", ".dcr",           # Kodak
    ".mrw",                   # Minolta
    ".erf",                   # Epson
})

# 扩展名去掉点后的短名集合（cr2 / nef / …）
RAW_FORMAT_NAMES: frozenset[str] = frozenset(
    e.lstrip(".").lower() for e in RAW_EXTS
) | frozenset({"raw"})

# RAW 源在未指定输出格式时的默认可写格式
DEFAULT_RAW_OUTPUT_FORMAT = "png"

# Pillow 可写、且本项目 worker 主路径认识的短名
_WRITABLE_FORMATS: frozenset[str] = frozenset({
    "png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff", "gif",
})


def is_raw_ext(ext_or_fmt: str | None) -> bool:
    """判断扩展名或格式短名是否为相机 RAW。"""
    if not ext_or_fmt:
        return False
    s = str(ext_or_fmt).strip().lower()
    if not s:
        return False
    if not s.startswith("."):
        if s in RAW_FORMAT_NAMES:
            return True
        s = f".{s}"
    return s in RAW_EXTS


def is_raw_path(path: PathLike) -> bool:
    """按文件扩展名判断是否为 RAW 路径。"""
    try:
        return Path(path).suffix.lower() in RAW_EXTS
    except Exception:
        return False


def coerce_writable_format(
    fmt: str | None,
    src: PathLike | None = None,
    *,
    default: str = DEFAULT_RAW_OUTPUT_FORMAT,
) -> str:
    """将输出格式收敛为可写短名。

    - 已指定可写格式（png/jpg/…）→ 规范化后返回
    - 未指定且源为 RAW，或指定了 RAW 类格式 → ``default``（默认 png）
    - 未指定且源非 RAW → 取源扩展名；仍不可写则 ``default``
    """
    f = str(fmt or "").strip().lower().lstrip(".")
    if f == "jpeg":
        f = "jpg"
    if f == "tif":
        f = "tiff"

    if f and f in _WRITABLE_FORMATS:
        return "jpg" if f == "jpeg" else f

    # 显式给了不可写 / RAW 类，或空串
    src_is_raw = is_raw_path(src) if src is not None else False
    if not f:
        if src is not None:
            src_ext = Path(src).suffix.lower().lstrip(".")
            if src_ext == "jpeg":
                src_ext = "jpg"
            if src_ext == "tif":
                src_ext = "tiff"
            if is_raw_ext(src_ext) or src_is_raw:
                return default if default in _WRITABLE_FORMATS else DEFAULT_RAW_OUTPUT_FORMAT
            if src_ext in _WRITABLE_FORMATS:
                return src_ext
        return default if default in _WRITABLE_FORMATS else DEFAULT_RAW_OUTPUT_FORMAT

    if is_raw_ext(f) or src_is_raw and is_raw_ext(f):
        return default if default in _WRITABLE_FORMATS else DEFAULT_RAW_OUTPUT_FORMAT

    # 未知短名：若源是 RAW 仍强制 default，否则原样（上层可能有自己的分支）
    if src_is_raw:
        return default if default in _WRITABLE_FORMATS else DEFAULT_RAW_OUTPUT_FORMAT
    return f


def _load_with_rawpy(path: Path) -> Image.Image:
    """用 rawpy 解码 RAW → RGB PIL Image。"""
    try:
        import rawpy  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "无法解码 RAW 图像：未安装 rawpy。请在项目虚拟环境中执行：pip install rawpy"
        ) from e

    try:
        with rawpy.imread(str(path)) as raw:
            # use_camera_wb：尽量贴近机内预览；no bright 半自动曝光
            rgb = raw.postprocess(
                use_camera_wb=True,
                no_auto_bright=False,
                output_bps=8,
            )
    except Exception as e:
        raise RuntimeError(f"RAW 解码失败: {path.name}: {e}") from e

    if rgb is None:
        raise RuntimeError(f"RAW 解码结果为空: {path.name}")

    # rawpy 输出 HxWx3 uint8
    img = Image.fromarray(rgb)
    if img.mode != "RGB":
        img = img.convert("RGB")
    # 标记来源，便于上层日志（非标准 PIL 字段，仅内存）
    try:
        img.format = "RAW"
        img.info = dict(img.info or {})
        img.info["raw_source"] = str(path)
        img.info["raw_ext"] = path.suffix.lower()
    except Exception:
        pass
    return img


def load_image(path: PathLike, *, apply_exif_transpose: bool = False) -> Image.Image:
    """加载图片为 ``PIL.Image``（已 ``load()`` 进内存）。

    :param path: 文件路径
    :param apply_exif_transpose: 是否按 EXIF Orientation 转正（预览/部分处理可开）
    :raises FileNotFoundError: 路径不存在
    :raises RuntimeError: RAW 缺依赖或解码失败
    :raises OSError / PIL.UnidentifiedImageError: 常规格式无法识别
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"文件不存在: {p}")

    if is_raw_path(p):
        img = _load_with_rawpy(p)
    else:
        img = Image.open(str(p))
        img.load()

    if apply_exif_transpose:
        try:
            from PIL import ImageOps
            img = ImageOps.exif_transpose(img) or img
        except Exception:
            pass
    return img


def load_image_for_preview(path: PathLike) -> Image.Image:
    """预览 / 缩略图用：RAW 解码 + EXIF 转正。"""
    return load_image(path, apply_exif_transpose=True)
