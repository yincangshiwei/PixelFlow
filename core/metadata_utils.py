"""
图片元数据读写 + 格式转换工具
支持 JPG/JPEG/WEBP（EXIF + Windows XP 字段）与 PNG（tEXt/eXIf）的通用属性写入。
Windows 资源管理器中的「标记」对应 Keywords / XPKeywords，多值以 "; " 分隔。

通过文件头 + Pillow 识别真实格式，避免扩展名造假（如 .jpg 实为 MPO）导致跳过转换。
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image
from PIL.PngImagePlugin import PngInfo

# ── EXIF / Windows XP 标签 ──
TAG_IMAGE_DESCRIPTION = 0x010E  # 270  描述
TAG_ARTIST = 0x013B             # 315  作者
TAG_COPYRIGHT = 0x8298          # 33432 版权
TAG_SOFTWARE = 0x0131           # 305  软件
TAG_XP_TITLE = 0x9C9B           # 40091 标题
TAG_XP_COMMENT = 0x9C9C         # 40092 备注
TAG_XP_AUTHOR = 0x9C9D          # 40093 作者
TAG_XP_KEYWORDS = 0x9C9E        # 40094 标记（; 分隔）
TAG_XP_SUBJECT = 0x9C9F         # 40095 主题

FIELD_KEYS = ("title", "description", "author", "copyright", "keywords")

FIELD_LABELS = {
    "title": "标题",
    "description": "描述",
    "author": "作者",
    "copyright": "版权",
    "keywords": "标记",
}

# 输出格式 → 支持的元数据字段（空元组 = 不支持元数据）
FORMAT_META_FIELDS: dict[str, tuple[str, ...]] = {
    "jpg": FIELD_KEYS,
    "jpeg": FIELD_KEYS,
    "png": FIELD_KEYS,
    "webp": FIELD_KEYS,
    "tif": FIELD_KEYS,
    "tiff": FIELD_KEYS,
    "bmp": (),
    "gif": (),
}

# 用户可选的转换目标
CONVERT_TARGETS = ("jpg", "png", "webp", "bmp", "tiff")

# 真实格式规范化映射 → 项目内统一短名
_FORMAT_ALIAS = {
    "JPEG": "jpg",
    "JPG": "jpg",
    "MPO": "mpo",       # 多图对象，基于 JPEG 容器，不能当真正 jpg 跳过
    "PNG": "png",
    "WEBP": "webp",
    "BMP": "bmp",
    "GIF": "gif",
    "TIFF": "tiff",
    "TIF": "tiff",
    "ICO": "ico",
    "PPM": "ppm",
    "PGM": "pgm",
    "PBM": "pbm",
}

META_FORMATS = {"jpg", "png", "webp", "tiff", "tif"}
NO_META_FORMATS = {"bmp", "gif", "ico"}


def encode_xp_string(text: str) -> bytes:
    return (text or "").encode("utf-16le") + b"\x00\x00"


def decode_xp_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-16le").rstrip("\x00")
        except Exception:
            return value.decode("latin-1", errors="replace").rstrip("\x00")
    return str(value).rstrip("\x00")


def normalize_keywords(text: str) -> str:
    if not text:
        return ""
    raw = text.replace("；", ";").replace("，", ";").replace(",", ";")
    parts = [p.strip() for p in raw.split(";") if p.strip()]
    seen = set()
    uniq = []
    for p in parts:
        key = p.casefold()
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return "; ".join(uniq)


def _is_ascii(text: str) -> bool:
    if text is None:
        return True
    try:
        str(text).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def normalize_format_name(fmt: str | None) -> str:
    """将 Pillow/扩展名格式名规范为短名（jpg/png/webp/...）"""
    if not fmt:
        return ""
    s = str(fmt).strip().lstrip(".").upper()
    if s in _FORMAT_ALIAS:
        return _FORMAT_ALIAS[s]
    return s.lower()


def ext_of_format(fmt: str) -> str:
    fmt = normalize_format_name(fmt)
    if fmt == "jpeg":
        return ".jpg"
    if fmt == "tif":
        return ".tiff"
    return f".{fmt}" if fmt else ""


def detect_true_format(path: str | Path) -> str:
    """
    检测图片真实格式（不依赖扩展名）。
    优先 Pillow format；失败时回退文件魔数。
    返回规范化短名，如 jpg / png / mpo / webp；无法识别返回 ""。
    """
    path = Path(path)
    # 1) Pillow
    try:
        with Image.open(str(path)) as img:
            fmt = normalize_format_name(img.format)
            if fmt:
                return fmt
    except Exception:
        pass

    # 2) 魔数
    try:
        with open(path, "rb") as f:
            head = f.read(32)
    except OSError:
        return ""

    if head.startswith(b"\xff\xd8\xff"):
        # JPEG 家族：进一步区分 MPO（APP2 MP 指纹较复杂，扩展检测）
        # 若扩展名为 .mpo 或文件中含 MP 标识，标为 mpo
        try:
            data = path.read_bytes()
            if b"MPF\x00" in data[:65536] or path.suffix.lower() == ".mpo":
                return "mpo"
        except OSError:
            pass
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"BM"):
        return "bmp"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    return ""


def formats_equivalent_for_skip(true_fmt: str, target_fmt: str) -> bool:
    """
    判断真实格式与目标格式是否可视为「同格式」从而跳过重编码。
    mpo ≠ jpg（即使用户扩展名是 .jpg，也必须转换）。
    """
    a = normalize_format_name(true_fmt)
    b = normalize_format_name(target_fmt)
    if not a or not b:
        return False
    # jpeg/jpg 互通
    if a in ("jpg", "jpeg"):
        a = "jpg"
    if b in ("jpg", "jpeg"):
        b = "jpg"
    if a in ("tif", "tiff"):
        a = "tiff"
    if b in ("tif", "tiff"):
        b = "tiff"
    # mpo 永不与 jpg 等价
    if a == "mpo":
        return b == "mpo"
    return a == b


def fields_for_format(fmt: str) -> tuple[str, ...]:
    """某输出格式支持的元数据字段列表。"""
    fmt = normalize_format_name(fmt)
    if fmt in ("jpeg",):
        fmt = "jpg"
    if fmt == "tif":
        fmt = "tiff"
    return FORMAT_META_FIELDS.get(fmt, FIELD_KEYS)


def read_image_metadata(path: str | Path) -> dict[str, Any]:
    """
    读取图片现有元数据，供单图模式回填面板。
    返回:
      {
        "path", "true_format", "ext_format",
        "title", "description", "author", "copyright", "keywords",
        "supported": bool,
      }
    字段值均为 str；读不到则为 ""。
    """
    path = Path(path)
    true_fmt = detect_true_format(path)
    ext_fmt = normalize_format_name(path.suffix)
    result: dict[str, Any] = {
        "path": str(path),
        "true_format": true_fmt or "",
        "ext_format": ext_fmt or "",
        "title": "",
        "description": "",
        "author": "",
        "copyright": "",
        "keywords": "",
        "supported": True,
    }

    out_fmt = true_fmt or ext_fmt
    if out_fmt in NO_META_FORMATS or not out_fmt:
        result["supported"] = False
        return result

    try:
        img = Image.open(str(path))
        img.load()
    except Exception:
        result["supported"] = False
        return result

    try:
        # PNG tEXt
        info = getattr(img, "info", {}) or {}
        if isinstance(info, dict):
            def _info_get(*names: str) -> str:
                for n in names:
                    v = info.get(n)
                    if v is None:
                        continue
                    if isinstance(v, bytes):
                        try:
                            return v.decode("utf-8", errors="replace")
                        except Exception:
                            return decode_xp_string(v)
                    s = str(v).strip()
                    if s:
                        return s
                return ""

            result["title"] = _info_get("Title", "title") or result["title"]
            result["description"] = _info_get("Description", "description", "Comment") or result["description"]
            result["author"] = _info_get("Author", "author") or result["author"]
            result["copyright"] = _info_get("Copyright", "copyright") or result["copyright"]
            result["keywords"] = _info_get("Keywords", "keywords") or result["keywords"]

        # EXIF / XP
        try:
            exif = img.getexif()
        except Exception:
            exif = None

        if exif:
            xp_title = decode_xp_string(exif.get(TAG_XP_TITLE))
            xp_subject = decode_xp_string(exif.get(TAG_XP_SUBJECT))
            xp_comment = decode_xp_string(exif.get(TAG_XP_COMMENT))
            xp_author = decode_xp_string(exif.get(TAG_XP_AUTHOR))
            xp_kw = decode_xp_string(exif.get(TAG_XP_KEYWORDS))
            desc = exif.get(TAG_IMAGE_DESCRIPTION)
            artist = exif.get(TAG_ARTIST)
            copyright_ = exif.get(TAG_COPYRIGHT)

            if not result["title"]:
                result["title"] = xp_title or xp_subject or ""
            if not result["description"]:
                if isinstance(desc, bytes):
                    desc = desc.decode("utf-8", errors="replace")
                result["description"] = (str(desc).strip() if desc else "") or xp_comment
            if not result["author"]:
                if isinstance(artist, bytes):
                    artist = artist.decode("utf-8", errors="replace")
                result["author"] = xp_author or (str(artist).strip() if artist else "")
            if not result["copyright"]:
                if isinstance(copyright_, bytes):
                    copyright_ = copyright_.decode("utf-8", errors="replace")
                result["copyright"] = str(copyright_).strip() if copyright_ else ""
            if not result["keywords"]:
                result["keywords"] = xp_kw or ""

        result["keywords"] = normalize_keywords(result["keywords"])
    finally:
        img.close()

    return result


def load_excel_lookup(
    excel_file: str,
    match_column: int,
    data_column: int,
    row_start: int,
) -> dict[str, str]:
    import openpyxl

    mapping: dict[str, str] = {}
    if not excel_file or not os.path.exists(excel_file):
        return mapping

    wb = openpyxl.load_workbook(excel_file, data_only=True)
    ws = wb.active
    match_letter = openpyxl.utils.get_column_letter(match_column)
    data_letter = openpyxl.utils.get_column_letter(data_column)

    for row in range(row_start, (ws.max_row or 0) + 1):
        match_value = ws[f"{match_letter}{row}"].value
        if match_value is None:
            continue
        match_str = str(match_value).strip()
        if not match_str:
            continue
        stem = Path(match_str).stem if "." in match_str else match_str
        data_value = ws[f"{data_letter}{row}"].value
        mapping[stem.casefold()] = "" if data_value is None else str(data_value).strip()

    wb.close()
    return mapping


def resolve_field_value(
    source: str,
    fixed_value: str,
    image_path: str,
    excel_map: dict[str, str] | None = None,
) -> str:
    source = (source or "fixed").lower()
    if source == "filename":
        return Path(image_path).stem
    if source == "excel":
        stem = Path(image_path).stem.casefold()
        if not excel_map:
            return ""
        return excel_map.get(stem, "")
    return fixed_value or ""


def _build_jpeg_exif(img: Image.Image, fields: dict[str, str], clear_all: bool) -> bytes | None:
    if clear_all:
        return b""

    try:
        exif = img.getexif()
    except Exception:
        exif = Image.Exif()

    if "title" in fields:
        title = fields["title"]
        if title:
            exif[TAG_XP_TITLE] = encode_xp_string(title)
            exif[TAG_XP_SUBJECT] = encode_xp_string(title)
        else:
            exif.pop(TAG_XP_TITLE, None)
            exif.pop(TAG_XP_SUBJECT, None)

    if "description" in fields:
        desc = fields["description"]
        if desc:
            if _is_ascii(desc):
                exif[TAG_IMAGE_DESCRIPTION] = desc
            else:
                exif.pop(TAG_IMAGE_DESCRIPTION, None)
            exif[TAG_XP_COMMENT] = encode_xp_string(desc)
        else:
            exif.pop(TAG_IMAGE_DESCRIPTION, None)
            exif.pop(TAG_XP_COMMENT, None)

    if "author" in fields:
        author = fields["author"]
        if author:
            if _is_ascii(author):
                exif[TAG_ARTIST] = author
            else:
                exif.pop(TAG_ARTIST, None)
            exif[TAG_XP_AUTHOR] = encode_xp_string(author)
        else:
            exif.pop(TAG_ARTIST, None)
            exif.pop(TAG_XP_AUTHOR, None)

    if "copyright" in fields:
        copyright_ = fields["copyright"]
        if copyright_:
            if _is_ascii(copyright_):
                exif[TAG_COPYRIGHT] = copyright_
            else:
                try:
                    copyright_.encode("latin-1")
                    exif[TAG_COPYRIGHT] = copyright_
                except UnicodeEncodeError:
                    exif[TAG_COPYRIGHT] = copyright_.encode("utf-8", errors="replace").decode(
                        "latin-1", errors="replace"
                    )
        else:
            exif.pop(TAG_COPYRIGHT, None)

    if "keywords" in fields:
        kw = normalize_keywords(fields["keywords"])
        if kw:
            exif[TAG_XP_KEYWORDS] = encode_xp_string(kw)
        else:
            exif.pop(TAG_XP_KEYWORDS, None)

    exif[TAG_SOFTWARE] = "PixelFlow"

    try:
        data = exif.tobytes()
        return data if data else b""
    except Exception:
        return b""


def _build_pnginfo(img: Image.Image, fields: dict[str, str], clear_all: bool) -> PngInfo | None:
    meta = PngInfo()
    if clear_all:
        return meta

    existing = {}
    if hasattr(img, "info") and img.info:
        for k, v in img.info.items():
            if isinstance(k, str) and isinstance(v, str):
                existing[k] = v

    key_map = {
        "title": "Title",
        "description": "Description",
        "author": "Author",
        "copyright": "Copyright",
        "keywords": "Keywords",
    }
    for fk, pk in key_map.items():
        if fk in fields:
            existing.pop(pk, None)
            existing.pop(pk.lower(), None)

    for k, v in existing.items():
        try:
            meta.add_text(k, v)
        except Exception:
            pass

    if "title" in fields and fields["title"]:
        meta.add_text("Title", fields["title"])
    if "description" in fields and fields["description"]:
        meta.add_text("Description", fields["description"])
    if "author" in fields and fields["author"]:
        meta.add_text("Author", fields["author"])
    if "copyright" in fields and fields["copyright"]:
        meta.add_text("Copyright", fields["copyright"])
    if "keywords" in fields:
        kw = normalize_keywords(fields["keywords"])
        if kw:
            meta.add_text("Keywords", kw)

    meta.add_text("Software", "PixelFlow")
    return meta


def _prepare_image_for_format(img: Image.Image, out_fmt: str) -> Image.Image:
    """按目标格式做必要的模式转换（如 JPG/BMP 去透明）。"""
    out_fmt = normalize_format_name(out_fmt)
    if out_fmt in ("jpg", "jpeg", "bmp"):
        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            if img.mode in ("RGBA", "LA"):
                background.paste(img, mask=img.split()[-1])
            else:
                background.paste(img)
            return background
        if img.mode != "RGB":
            return img.convert("RGB")
        return img
    if out_fmt == "png":
        if img.mode not in ("RGB", "RGBA", "L", "LA", "P"):
            return img.convert("RGBA")
        return img
    if out_fmt == "webp":
        if img.mode not in ("RGB", "RGBA"):
            return img.convert("RGBA") if "A" in img.getbands() else img.convert("RGB")
        return img
    return img


def _save_with_meta(
    img: Image.Image,
    tmp_path: Path,
    out_fmt: str,
    fields: dict[str, str],
    clear_all: bool,
    details: dict,
) -> None:
    """按目标格式保存，并写入该格式支持的元数据。"""
    out_fmt = normalize_format_name(out_fmt)
    save_img = _prepare_image_for_format(img, out_fmt)
    supported = set(fields_for_format(out_fmt))
    # 仅保留目标格式支持的字段
    use_fields = {k: v for k, v in fields.items() if k in supported}

    if out_fmt in ("jpg", "jpeg"):
        exif_bytes = _build_jpeg_exif(img if not clear_all else save_img, use_fields, clear_all)
        save_kwargs: dict[str, Any] = {"format": "JPEG", "quality": 95}
        if exif_bytes:
            save_kwargs["exif"] = exif_bytes
        try:
            keep_kw: dict[str, Any] = {"format": "JPEG", "quality": "keep"}
            if exif_bytes:
                keep_kw["exif"] = exif_bytes
            save_img.save(str(tmp_path), **keep_kw)
        except Exception:
            save_img.save(str(tmp_path), **save_kwargs)

    elif out_fmt == "webp":
        exif_bytes = _build_jpeg_exif(img, use_fields, clear_all)
        save_kwargs = {"format": "WEBP", "quality": 95}
        if exif_bytes:
            save_kwargs["exif"] = exif_bytes
        try:
            save_img.save(str(tmp_path), **save_kwargs)
        except TypeError:
            save_kwargs.pop("exif", None)
            save_img.save(str(tmp_path), **save_kwargs)
            details["warning"] = "当前环境 WEBP 写入 EXIF 受限，已保存像素"

    elif out_fmt == "png":
        pnginfo = _build_pnginfo(img, use_fields, clear_all)
        save_kwargs = {"format": "PNG", "pnginfo": pnginfo}
        if not clear_all and use_fields:
            exif_bytes = _build_jpeg_exif(img, use_fields, clear_all=False)
            if exif_bytes:
                save_kwargs["exif"] = exif_bytes
        try:
            save_img.save(str(tmp_path), **save_kwargs)
        except Exception:
            save_kwargs.pop("exif", None)
            save_img.save(str(tmp_path), **save_kwargs)

    elif out_fmt in ("tif", "tiff"):
        exif_bytes = _build_jpeg_exif(img, use_fields, clear_all)
        save_kwargs = {"format": "TIFF"}
        if exif_bytes:
            save_kwargs["exif"] = exif_bytes
        save_img.save(str(tmp_path), **save_kwargs)

    elif out_fmt == "bmp":
        save_img.save(str(tmp_path), format="BMP")

    elif out_fmt == "gif":
        save_img.save(str(tmp_path), format="GIF")

    else:
        # 未知目标：尽量按原样
        save_img.save(str(tmp_path))


def write_image_metadata(
    src_path: str,
    dst_path: str,
    fields: dict[str, str],
    clear_all: bool = False,
    target_format: str | None = None,
    enable_convert: bool = False,
) -> dict:
    """
    写入元数据并可选格式转换，保存到 dst_path。

    :param fields: 用户勾选字段 {key: value}，空串表示清空
    :param clear_all: 清除全部元数据
    :param target_format: 目标格式短名（jpg/png/...），enable_convert 时必填
    :param enable_convert: 是否启用格式转换
    :return: details 字典
    """
    src = Path(src_path)
    dst = Path(dst_path)
    true_fmt = detect_true_format(src)
    ext_fmt = normalize_format_name(src.suffix)
    details: dict[str, Any] = {
        "true_format": true_fmt or "unknown",
        "ext_format": ext_fmt or "unknown",
        "format": true_fmt or ext_fmt,
        "fields_written": [],
        "skipped": False,
        "cleared": clear_all,
        "converted": False,
        "convert_skipped": False,
    }

    # 假扩展名提示
    if true_fmt and ext_fmt and not formats_equivalent_for_skip(true_fmt, ext_fmt):
        details["fake_extension"] = True
        details["format_note"] = f"扩展名 .{ext_fmt}，真实格式 {true_fmt}"

    if "keywords" in fields:
        fields = dict(fields)
        fields["keywords"] = normalize_keywords(fields["keywords"])

    # 决定输出格式
    out_fmt = normalize_format_name(target_format) if enable_convert and target_format else ""
    if enable_convert and out_fmt:
        # 同真实格式且扩展名也一致 → 可跳过重编码（仅当不需要改格式时）
        # 但若扩展名与真实格式不符（假格式），即使目标=真实，仍应重写以修正容器/扩展
        same_true = formats_equivalent_for_skip(true_fmt, out_fmt)
        ext_ok = formats_equivalent_for_skip(ext_fmt, out_fmt) if ext_fmt else False
        # mpo → jpg：same_true=False，必转
        # jpg → jpg 且真 jpg：可跳过转换，只写元数据
        # .jpg 实为 mpo，目标 jpg：必转
        need_reencode = (not same_true) or (not ext_ok) or (true_fmt == "mpo")
        if not need_reencode and not clear_all and not fields:
            # 无元数据操作且格式已正确
            if src.resolve() != dst.resolve():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(dst))
            details["skipped"] = True
            details["skip_reason"] = "格式已是目标且无字段变更"
            details["convert_skipped"] = True
            return details
        if need_reencode:
            details["converted"] = True
            details["output_format"] = out_fmt
        else:
            details["convert_skipped"] = True
            details["output_format"] = out_fmt
            # 同格式只改元数据，输出容器仍是 out_fmt
    else:
        # 未启用转换：按真实格式写元数据；无法识别则按扩展名
        out_fmt = true_fmt or ext_fmt
        if out_fmt == "mpo":
            # MPO 当 JPEG 写容易出问题；无转换时尝试按 jpg 容器写回
            out_fmt = "jpg"
            details["warning"] = "检测到 MPO，未开转换时按 JPEG 容器写回；建议启用格式转换"
            details["converted"] = True
        if not out_fmt:
            details["skipped"] = True
            details["skip_reason"] = "无法识别图片格式"
            if src.resolve() != dst.resolve():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(dst))
            return details

    # 无转换、不支持元数据、无字段、不清除 → 复制
    supported_fields = fields_for_format(out_fmt)
    if not enable_convert and out_fmt in NO_META_FORMATS:
        details["skipped"] = True
        details["skip_reason"] = f"{out_fmt} 不支持元数据"
        if src.resolve() != dst.resolve():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
        return details

    if not clear_all and not fields and not details.get("converted"):
        if src.resolve() != dst.resolve():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
        details["skipped"] = True
        details["skip_reason"] = "未启用任何字段且无需转换"
        return details

    # 过滤字段到目标格式支持范围
    use_fields = {k: v for k, v in fields.items() if k in supported_fields}
    if fields and not use_fields and not clear_all and not details.get("converted"):
        details["skipped"] = True
        details["skip_reason"] = f"{out_fmt} 不支持所选元数据字段"
        if src.resolve() != dst.resolve():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
        return details

    img = Image.open(str(src))
    img.load()
    # 多帧 MPO/GIF：取第一帧
    try:
        img.seek(0)
    except EOFError:
        pass

    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(suffix=ext_of_format(out_fmt) or src.suffix, dir=str(dst.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)

    try:
        only_meta_same_fmt = (
            not details.get("converted")
            and not enable_convert
            and formats_equivalent_for_skip(true_fmt, out_fmt)
        )
        # 统一走保存逻辑（同格式元数据写入也重编码写 EXIF，保证一致性）
        _save_with_meta(img, tmp_path, out_fmt, use_fields, clear_all, details)
        os.replace(str(tmp_path), str(dst))
        if only_meta_same_fmt:
            details["format"] = out_fmt
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise
    finally:
        img.close()

    details["format"] = out_fmt
    details["output_format"] = out_fmt

    if clear_all:
        details["fields_written"] = ["(已清除全部元数据)"]
    else:
        details["fields_written"] = [
            f"{FIELD_LABELS.get(k, k)}={v if v else '(清空)'}" for k, v in use_fields.items()
        ]
        dropped = [FIELD_LABELS.get(k, k) for k in fields if k not in use_fields]
        if dropped:
            details["fields_dropped"] = dropped

    return details
