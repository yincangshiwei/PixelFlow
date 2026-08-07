"""DOCX 内嵌图片提取（ZIP word/media/，纯逻辑）。"""
import hashlib
from pathlib import Path

from .constants import _IMG_MAGIC, _MIN_EXTRACTED_IMAGE_BYTES
from .temp_store import _guess_image_ext, _write_image_bytes


def _extract_images_from_docx(path: Path) -> list[str]:
    """
    从 DOCX 提取内嵌图片到临时目录。
    DOCX 为 ZIP，图片通常在 word/media/。
    """
    import zipfile

    paths: list[str] = []
    seen_hash: set[str] = set()
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = [
                n for n in zf.namelist()
                if n.lower().startswith("word/media/") and not n.endswith("/")
            ]
            names.sort()
            for name in names:
                try:
                    data = zf.read(name)
                except Exception:
                    continue
                if not data or len(data) < _MIN_EXTRACTED_IMAGE_BYTES:
                    continue
                digest = hashlib.sha1(data).hexdigest()
                if digest in seen_hash:
                    continue
                # 仅保留可识别的图片字节
                ext = _guess_image_ext(data, Path(name).suffix)
                if not any(data.startswith(m[0]) for m in _IMG_MAGIC) and data[:4] not in (
                    b"II*\x00", b"MM\x00*",
                ):
                    # EMF/WMF 等矢量占位跳过（Pillow/列表预览通常不可用）
                    low = name.lower()
                    if low.endswith((".emf", ".wmf", ".emz", ".wmz")):
                        continue
                    # 无魔数也尝试写入（少数 jpeg 变体）
                preferred = f"{path.stem}_{Path(name).name}"
                out = _write_image_bytes(data, preferred_name=preferred, hint=ext)
                if out:
                    seen_hash.add(digest)
                    paths.append(out)
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return []
    return paths
