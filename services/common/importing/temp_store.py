"""导入临时文件存储（粘贴 / 抽图落盘）。

内容寻址文件名：同图多次提取只落一份；命名含 preferred_name 前缀。
跨文件内容去重在 _materialize_image_refs / run_extract_jobs 的 sha1 层完成。
"""
import hashlib
import re
import tempfile
from datetime import datetime
from pathlib import Path

from .constants import _IMG_MAGIC


def _paste_temp_dir() -> Path | None:
    """粘贴临时目录：%TEMP%/PixelFlow_paste"""
    paste_dir = Path(tempfile.gettempdir()) / "PixelFlow_paste"
    try:
        paste_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return paste_dir


def _guess_image_ext(data: bytes, hint: str = "") -> str:
    """根据魔数或 URL/MIME 提示猜测图片扩展名。"""
    if data:
        for magic, ext in _IMG_MAGIC:
            if data.startswith(magic):
                if ext == ".webp":
                    if len(data) >= 12 and data[8:12] == b"WEBP":
                        return ".webp"
                else:
                    return ext
        # TIFF
        if data[:4] in (b"II*\x00", b"MM\x00*"):
            return ".tif"
    hint = (hint or "").lower()
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"):
        if hint.endswith(ext) or ext.strip(".") in hint:
            return ".jpg" if ext == ".jpeg" else (".tif" if ext == ".tiff" else ext)
    return ".png"


def _unique_paste_path(preferred_name: str = "", ext: str = ".png") -> Path | None:
    paste_dir = _paste_temp_dir()
    if paste_dir is None:
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    if preferred_name:
        stem = Path(preferred_name).stem
        stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .") or "paste"
        name = f"{stem}_{stamp}{ext}"
    else:
        name = f"paste_{stamp}{ext}"
    return paste_dir / name


def _write_image_bytes(data: bytes, preferred_name: str = "", hint: str = "") -> str | None:
    """将图片字节写入临时文件，返回绝对路径。同内容复用已有文件。"""
    if not data or len(data) < 24:
        return None
    ext = _guess_image_ext(data, hint)
    paste_dir = _paste_temp_dir()
    if paste_dir is None:
        return None
    # 内容寻址文件名：同图多次提取只落一份
    digest = hashlib.sha1(data).hexdigest()[:16]
    stem = ""
    if preferred_name:
        stem = Path(preferred_name).stem
        stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .")
        if stem:
            stem = stem[:40] + "_"
    out = paste_dir / f"{stem}{digest}{ext}"
    try:
        if out.is_file() and out.stat().st_size == len(data):
            return str(out.resolve())
        out.write_bytes(data)
        return str(out.resolve())
    except OSError:
        # 回退时间戳文件名
        out2 = _unique_paste_path(preferred_name, ext)
        if out2 is None:
            return None
        try:
            out2.write_bytes(data)
            return str(out2.resolve())
        except OSError:
            return None
