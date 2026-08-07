"""PDF 内嵌图片提取（pypdf XObject 遍历，非整页渲染，纯逻辑）。"""
import hashlib
from pathlib import Path

from .constants import _MIN_EXTRACTED_IMAGE_BYTES
from .temp_store import _guess_image_ext, _write_image_bytes


def _extract_images_from_pdf(path: Path) -> list[str]:
    """
    从 PDF 提取内嵌图片（使用已有依赖 pypdf）。
    仅提取嵌入位图，不做整页渲染。
    """
    try:
        from pypdf import PdfReader
        from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject
    except ImportError:
        return []

    paths: list[str] = []
    seen_hash: set[str] = set()
    try:
        reader = PdfReader(str(path), strict=False)
    except Exception:
        return []

    def _walk_xobjects(xobj, page_idx: int, counter: list[int]):
        if xobj is None:
            return
        try:
            if isinstance(xobj, IndirectObject):
                xobj = xobj.get_object()
        except Exception:
            return
        if not isinstance(xobj, DictionaryObject):
            return
        subtype = xobj.get("/Subtype")
        if subtype == "/Image":
            counter[0] += 1
            try:
                data = xobj.get_data()
            except Exception:
                return
            if not data or len(data) < _MIN_EXTRACTED_IMAGE_BYTES:
                return
            digest = hashlib.sha1(data).hexdigest()
            if digest in seen_hash:
                return
            # 过滤过小的图标（宽或高 < 8）
            try:
                w = int(xobj.get("/Width") or 0)
                h = int(xobj.get("/Height") or 0)
                if w and h and (w < 8 or h < 8):
                    return
            except Exception:
                pass
            filt = xobj.get("/Filter")
            hint = ".bin"
            # 根据 Filter 猜测扩展名
            filters = []
            if isinstance(filt, ArrayObject):
                filters = [str(f) for f in filt]
            elif filt is not None:
                filters = [str(filt)]
            fl = " ".join(filters).lower()
            if "dct" in fl or "jpeg" in fl:
                hint = ".jpg"
            elif "jpx" in fl:
                hint = ".jp2"
            elif "flate" in fl or "lzw" in fl or not filters:
                # 尝试按魔数；Flate 原始像素常需解码，pypdf get_data 已解流
                hint = _guess_image_ext(data, ".png")
            elif "ccitt" in fl:
                hint = ".tiff"
            # 仅保留常见可预览格式
            if not (
                data.startswith(b"\x89PNG")
                or data.startswith(b"\xff\xd8\xff")
                or data.startswith(b"GIF8")
                or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
                or data.startswith(b"BM")
                or data[:4] in (b"II*\x00", b"MM\x00*")
            ):
                # 原始像素流：用 Pillow 按 ColorSpace/Bits 尝试重建
                try:
                    from PIL import Image
                    import io

                    width = int(xobj.get("/Width") or 0)
                    height = int(xobj.get("/Height") or 0)
                    if width < 8 or height < 8:
                        return
                    bpc = int(xobj.get("/BitsPerComponent") or 8)
                    cs = xobj.get("/ColorSpace")
                    cs_name = str(cs) if cs is not None else "/DeviceRGB"
                    if isinstance(cs, ArrayObject) and len(cs) > 0:
                        cs_name = str(cs[0])
                    mode = None
                    if "RGB" in cs_name:
                        mode = "RGB"
                    elif "Gray" in cs_name or "Grey" in cs_name:
                        mode = "L"
                    elif "CMYK" in cs_name:
                        mode = "CMYK"
                    if mode is None or bpc != 8:
                        return
                    expected = width * height * (1 if mode == "L" else (4 if mode == "CMYK" else 3))
                    if len(data) < expected:
                        return
                    img = Image.frombytes(mode, (width, height), data[:expected])
                    if mode == "CMYK":
                        img = img.convert("RGB")
                    buf = io.BytesIO()
                    img.save(buf, format="PNG")
                    data = buf.getvalue()
                    hint = ".png"
                except Exception:
                    return
            preferred = f"{path.stem}_p{page_idx + 1}_{counter[0]}"
            out = _write_image_bytes(data, preferred_name=preferred, hint=hint)
            if out:
                seen_hash.add(digest)
                paths.append(out)
            return

        # Form XObject：继续向下找图片
        if subtype == "/Form":
            try:
                resources = xobj.get("/Resources")
                if isinstance(resources, IndirectObject):
                    resources = resources.get_object()
                if isinstance(resources, DictionaryObject):
                    inner = resources.get("/XObject")
                    if isinstance(inner, IndirectObject):
                        inner = inner.get_object()
                    if isinstance(inner, DictionaryObject):
                        for _k, v in inner.items():
                            _walk_xobjects(v, page_idx, counter)
            except Exception:
                return

    for page_idx, page in enumerate(reader.pages):
        counter = [0]
        try:
            resources = page.get("/Resources")
            if isinstance(resources, IndirectObject):
                resources = resources.get_object()
            if not isinstance(resources, DictionaryObject):
                continue
            xobjects = resources.get("/XObject")
            if isinstance(xobjects, IndirectObject):
                xobjects = xobjects.get_object()
            if not isinstance(xobjects, DictionaryObject):
                continue
            for _name, obj in xobjects.items():
                _walk_xobjects(obj, page_idx, counter)
        except Exception:
            continue

    return paths
