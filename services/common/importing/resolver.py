"""HTML 图片引用解析与物化（data URI / http(s) 下载 / 本地路径复制）。

纯逻辑：输入普通 str / Path / bytes，输出落盘后的本地路径。
"""
import base64
import hashlib
import re
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from .constants import IMAGE_EXTS, _IMG_MAGIC
from .refs import _file_uri_to_local_path, _image_ref_dedupe_key
from .temp_store import _paste_temp_dir, _write_image_bytes


def _decode_data_image_uri(uri: str) -> str | None:
    """data:image/...;base64,... → 临时文件路径。"""
    m = re.match(r"data:image/([a-zA-Z0-9.+-]+);base64,(.+)$", uri, flags=re.I | re.S)
    if not m:
        return None
    mime = m.group(1).lower()
    try:
        raw = base64.b64decode(m.group(2), validate=False)
    except Exception:
        return None
    hint = f".{mime.split('+')[0]}"
    return _write_image_bytes(raw, preferred_name="html_data", hint=hint)


def _copy_local_image(path: Path) -> str | None:
    """复制本地图片到粘贴临时目录（避免占用源文件；已是 paste 目录则直接用）。"""
    if not path.is_file():
        return None
    if path.suffix.lower() not in IMAGE_EXTS and path.suffix.lower() not in {'.svg', '.ico', '.avif', '.jfif'}:
        # 无扩展名时仍尝试按内容识别
        try:
            head = path.read_bytes()[:64]
        except OSError:
            return None
        if not any(head.startswith(m[0]) for m in _IMG_MAGIC) and head[:4] not in (b"II*\x00", b"MM\x00*"):
            return None
    paste_dir = _paste_temp_dir()
    if paste_dir is None:
        return None
    try:
        resolved = path.resolve()
        if paste_dir in resolved.parents or resolved.parent == paste_dir:
            return str(resolved)
    except OSError:
        pass
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return _write_image_bytes(data, preferred_name=path.name, hint=path.suffix)


def _download_image_url(url: str, timeout: float = 25.0, referer: str = "") -> str | None:
    """下载远程图片到粘贴临时目录（通用 HTML / 钉钉 CDN 等）。"""
    if not url.lower().startswith(("http://", "https://")):
        return None
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    # 文件名提示
    path_name = Path(unquote(parsed.path)).name
    preferred = path_name if path_name and "." in path_name else "html_net"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    # Referer：优先调用方（页面 SourceURL / HTML 所在站）；否则按主机推断
    if referer:
        headers["Referer"] = referer
    elif "dingtalk.com" in host or "aliyuncs.com" in host or "alidocs" in host:
        headers["Referer"] = "https://alidocs.dingtalk.com/"
    elif host:
        headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return None
    if not data:
        return None
    # 拒绝明显 HTML 错误页
    if ctype.startswith("text/html") or data.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
        return None
    return _write_image_bytes(data, preferred_name=preferred, hint=ctype or preferred)


def _resolve_html_ref(
    ref: str,
    *,
    html_base: Path | None = None,
    page_url: str = "",
) -> tuple[str, str]:
    """
    将引用规范为可 materialize 的形态。
    返回 (kind, value)：kind in data|http|file|skip
    """
    ref = (ref or "").strip()
    if not ref:
        return "skip", ""
    low = ref.lower()
    if low.startswith("data:image/"):
        return "data", ref
    if low.startswith("//"):
        scheme = "https:"
        if page_url.lower().startswith("http://"):
            scheme = "http:"
        return "http", scheme + ref
    if low.startswith(("http://", "https://")):
        return "http", ref
    if low.startswith("file:"):
        local = _file_uri_to_local_path(ref)
        if local:
            return "file", local
        return "skip", ""
    # Windows 绝对路径
    if re.match(r"^[a-zA-Z]:[\\/]", ref) or ref.startswith("\\\\"):
        return "file", ref
    # 相对路径：优先相对 HTML 文件目录，其次相对 page_url
    rel_path = unquote(ref.split("?", 1)[0].split("#", 1)[0])
    if html_base is not None:
        try:
            base_dir = html_base if html_base.is_dir() else html_base.parent
            cand = (base_dir / rel_path).resolve()
            if cand.is_file():
                return "file", str(cand)
        except OSError:
            pass
    if page_url:
        abs_url = urljoin(page_url, ref)
        al = abs_url.lower()
        if al.startswith(("http://", "https://")):
            return "http", abs_url
        if al.startswith("file:"):
            local = _file_uri_to_local_path(abs_url)
            if local:
                return "file", local
    return "skip", ""


def _materialize_image_refs(
    refs: list[str],
    *,
    html_base: Path | None = None,
    page_url: str = "",
) -> list[str]:
    """将 HTML 图片引用落盘：data URI / http(s) / 本地相对或绝对路径（路径+内容去重）。"""
    paths: list[str] = []
    seen_out: set[str] = set()
    seen_ref_keys: set[str] = set()
    seen_content: set[str] = set()
    referer = page_url if page_url.lower().startswith(("http://", "https://")) else ""

    for ref in refs:
        ref_key = _image_ref_dedupe_key(ref)
        if ref_key and ref_key in seen_ref_keys:
            continue
        if ref_key:
            seen_ref_keys.add(ref_key)

        kind, value = _resolve_html_ref(ref, html_base=html_base, page_url=page_url)
        p = None
        if kind == "data":
            p = _decode_data_image_uri(value)
        elif kind == "http":
            # 已通过提取过滤；钉钉等无扩展名签名 URL 仍允许
            p = _download_image_url(value, referer=referer)
        elif kind == "file":
            p = _copy_local_image(Path(value))
        if not p or p in seen_out:
            continue
        # 内容哈希再去重（截断 URL 与完整 URL 下到同一文件时合并）
        try:
            digest = hashlib.sha1(Path(p).read_bytes()).hexdigest()
            if digest in seen_content:
                continue
            seen_content.add(digest)
        except OSError:
            pass
        seen_out.add(p)
        paths.append(p)
    return paths


def _read_html_file_text(path: Path) -> str:
    """读取本地 HTML 文件文本（尝试常见编码）。"""
    data = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
