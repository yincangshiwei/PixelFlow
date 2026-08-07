"""图片引用（URL / data URI / 本地路径）的规范化、去重键与过滤。"""
import base64
import hashlib
import html as html_lib
import re
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from .constants import _IMAGE_PATH_EXTS


def _file_uri_to_local_path(ref: str) -> str | None:
    """file: URI → 本地文件路径（纯标准库实现，等价 QUrl.toLocalFile 语义）。

    仅当 host 为空或 localhost 时转换（等价 QUrl.isLocalFile 判定）；
    其余（UNC 等）返回 None，由调用方按原逻辑降级处理。
    """
    if not ref:
        return None
    try:
        parsed = urlparse(ref)
    except ValueError:
        return None
    if parsed.scheme.lower() != "file":
        return None
    netloc = (parsed.netloc or "").lower()
    if netloc not in ("", "localhost"):
        return None
    try:
        return url2pathname(parsed.path)
    except (OSError, ValueError):
        return None


def _looks_like_image_url(url: str) -> bool:
    """判断 URL/路径是否像图片资源（过滤页面链接）。"""
    if not url:
        return False
    u = url.strip()
    low = u.lower()
    if low.startswith("data:image/"):
        return True
    if low.startswith("javascript:") or low.startswith("mailto:") or low == "#":
        return False
    # 去 query/hash 看扩展名
    path = urlparse(u).path if "://" in u or u.startswith("//") else u.split("?", 1)[0].split("#", 1)[0]
    path_l = unquote(path).lower()
    if any(path_l.endswith(ext) for ext in _IMAGE_PATH_EXTS):
        return True
    # 常见图床/对象存储特征
    markers = (
        "/img/", "/image/", "/images/", "/static/", "/upload", "/media/",
        "ossaccesskeyid", "x-oss-", "format=jpg", "format=png", "format=webp",
        "imageView", "imageMogr", "x-image",
    )
    return any(m in low for m in markers)


def _normalize_ref_text(ref: str) -> str:
    """清理单个引用：去引号、反转义实体，不破坏 URL 语义。"""
    ref = (ref or "").strip().strip('"').strip("'")
    if not ref:
        return ""
    # 仅对实体做反转义（&amp; &quot;），避免整页 unescape 破坏属性边界
    if "&" in ref:
        ref = html_lib.unescape(ref)
    ref = ref.strip().strip('"').strip("'")
    # 钉钉/HTML 截断残留：末尾孤立 & 或半截参数
    ref = ref.rstrip("\\").rstrip(".,);]")
    while ref.endswith("&") or ref.endswith("?"):
        ref = ref[:-1]
    return ref.strip()


def _image_ref_dedupe_key(ref: str) -> str:
    """
    同一资源的去重键。
    - http(s)：scheme + host + path（忽略签名 query，避免截断/完整 URL 算成两张）
    - data URI：按解码后内容哈希（同图不同空白仍合并）
    - 其它：规范化小写字符串
    """
    ref = _normalize_ref_text(ref)
    if not ref:
        return ""
    low = ref.lower()
    if low.startswith("data:image/"):
        m = re.match(r"data:image/([a-zA-Z0-9.+-]+);base64,(.+)$", ref, flags=re.I | re.S)
        if m:
            try:
                raw = base64.b64decode(re.sub(r"\s+", "", m.group(2)), validate=False)
                return "data:" + hashlib.sha1(raw).hexdigest()
            except Exception:
                return "data:" + hashlib.sha1(ref.encode("utf-8", errors="ignore")).hexdigest()
        return "data:" + hashlib.sha1(ref.encode("utf-8", errors="ignore")).hexdigest()
    if low.startswith("//"):
        ref = "https:" + ref
        low = ref.lower()
    if low.startswith(("http://", "https://")):
        p = urlparse(ref)
        path = unquote(p.path or "").rstrip("/")
        return f"{(p.scheme or 'https').lower()}://{(p.netloc or '').lower()}{path.lower()}"
    if low.startswith("file:"):
        local = _file_uri_to_local_path(ref)
        if local:
            try:
                return "file:" + str(Path(local).resolve()).lower()
            except OSError:
                return "file:" + local.lower()
    # 相对/本地路径：去 query/hash
    path_only = unquote(ref.split("?", 1)[0].split("#", 1)[0]).replace("\\", "/")
    return "path:" + path_only.lower()
