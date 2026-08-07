"""HTML 图片引用提取（CF_HTML / 钉钉 / srcset / CSS url() 等）。

纯文本输入输出，不依赖 Qt。
"""
import re
from html.parser import HTMLParser

from .constants import _IMAGE_PATH_EXTS
from .refs import _image_ref_dedupe_key, _looks_like_image_url, _normalize_ref_text


class _HtmlImageRefParser(HTMLParser):
    """通用 HTML 图片引用提取：img/srcset/source/meta/background 等。"""

    _SRC_ATTRS = (
        "src", "data-src", "data-original", "data-url", "data-lazy-src",
        "data-actualsrc", "data-lazy", "data-image", "data-img",
        "content",  # og:image 等
    )
    _CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I)

    def __init__(self):
        # convert_charrefs=False：钉钉 data-clipboard-cangjie 内含 &quot; JSON，
        # 若自动转义会提前闭合属性，导致解析出截断 URL / 幽灵标签。
        super().__init__(convert_charrefs=False)
        self.refs: list[str] = []

    def _push(self, value: str | None):
        if not value:
            return
        v = value.strip()
        if not v:
            return
        # data URI 内含逗号，绝不能按 srcset 拆分
        if v.lower().startswith("data:"):
            self.refs.append(v)
            return
        # srcset: "a.jpg 1x, b.jpg 2x"
        if "," in v and re.search(r"\s+\d+(\.\d+)?[wx]\s*(?:,|$)", v, flags=re.I):
            for part in v.split(","):
                token = part.strip().split()[0] if part.strip() else ""
                if token:
                    self.refs.append(token)
            return
        self.refs.append(v)

    def _push_style(self, style: str | None):
        if not style:
            return
        for m in self._CSS_URL_RE.finditer(style):
            self._push(m.group(2))

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        ad = {((k or "").lower()): v for k, v in attrs if k}
        if t in ("img", "image", "source", "input", "embed", "object", "use"):
            for key in self._SRC_ATTRS:
                if key in ad:
                    self._push(ad.get(key))
            if "srcset" in ad:
                self._push(ad.get("srcset"))
            if "data-srcset" in ad:
                self._push(ad.get("data-srcset"))
            if t == "object":
                self._push(ad.get("data"))
            self._push_style(ad.get("style"))
            return
        if t == "meta":
            prop = (ad.get("property") or ad.get("name") or "").lower()
            if prop in ("og:image", "og:image:url", "twitter:image", "twitter:image:src"):
                self._push(ad.get("content"))
            return
        # 任意标签的 style background-image
        if "style" in ad:
            style = ad.get("style") or ""
            if "url(" in style.lower():
                self._push_style(style)
        # 常见背景属性
        for key in ("background", "background-image", "data-background", "data-bg"):
            if key in ad:
                val = ad.get(key) or ""
                if "url(" in val.lower():
                    self._push_style(val)
                else:
                    self._push(val)


def _strip_cf_html_header(raw: str) -> str:
    """去掉 Windows CF_HTML 头部，只保留 HTML 正文。"""
    if not raw:
        return ""
    # StartHTML / StartFragment 偏移（字节，对 utf-8 源通常等同）
    m = re.search(r"StartHTML:(\d+)", raw)
    if m:
        try:
            off = int(m.group(1))
            # 偏移按原始字节计；这里 raw 已是 str，用字符近似（ASCII 头时一致）
            if 0 <= off < len(raw):
                return raw[off:]
        except ValueError:
            pass
    # SourceURL 头也要保留解析用，但正文从 <html 开始
    idx = raw.lower().find("<html")
    if idx >= 0:
        return raw[idx:]
    idx = raw.find("<!--StartFragment")
    if idx >= 0:
        return raw[idx:]
    return raw


def _cf_html_source_url(raw: str) -> str:
    """从 CF_HTML 头读取 SourceURL（作为相对路径/Referer 基准）。"""
    if not raw:
        return ""
    m = re.search(r"SourceURL:([^\r\n]+)", raw)
    if not m:
        return ""
    return (m.group(1) or "").strip()


def _sanitize_html_for_image_extract(html_text: str) -> str:
    """
    抽取前清理会干扰解析的内容：
    - 钉钉 data-clipboard-cangjie 等巨型 JSON 属性（与真实 <img> 重复，且含 &quot;）
    - script/style 块
    """
    if not html_text:
        return ""
    body = html_text
    # 去掉 script / style
    body = re.sub(r"<script\b[^>]*>[\s\S]*?</script>", " ", body, flags=re.I)
    body = re.sub(r"<style\b[^>]*>[\s\S]*?</style>", " ", body, flags=re.I)
    # 去掉易重复/易破坏解析的 JSON 剪贴板属性（钉钉 cangjie 等）
    body = re.sub(
        r"""\s+data-clipboard-cangjie\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""",
        "",
        body,
        flags=re.I,
    )
    body = re.sub(
        r"""\s+data-clipboard-[a-z0-9_-]+\s*=\s*(?:"[^"]*"|'[^']*')""",
        "",
        body,
        flags=re.I,
    )
    return body


def _extract_image_refs_from_html(html_text: str) -> list[str]:
    """
    通用：从任意 HTML 提取图片引用（自动去重）。
    支持 http(s)/data URI/相对路径/file://、img/srcset/CSS url()、钉钉等非常规嵌入。

    注意：
    1) 不要对整段 HTML 先做 html.unescape（会破坏属性边界）
    2) 解析器 convert_charrefs=False，实体在单条 ref 上再 unescape
    3) 去掉钉钉 cangjie 等与 <img> 重复的嵌入 JSON，避免同一图抽两次
    """
    if not html_text:
        return []
    body = _strip_cf_html_header(html_text)
    body = _sanitize_html_for_image_extract(body)

    # key -> 选用的 ref（同 key 保留更完整/更长的那条）
    chosen: dict[str, str] = {}
    order: list[str] = []

    def _add(ref: str):
        ref = _normalize_ref_text(ref)
        if not ref:
            return
        low = ref.lower()
        if low.startswith("data:") and not low.startswith("data:image/"):
            return
        if low.startswith("data:image/") or low.startswith(("http://", "https://", "file:", "//")):
            if low.startswith(("http://", "https://", "//")) and not _looks_like_image_url(ref):
                return
        elif low.startswith("javascript:") or low.startswith("mailto:") or low.startswith("#"):
            return
        else:
            path_only = ref.split("?", 1)[0].split("#", 1)[0].lower()
            if path_only.endswith((".css", ".js", ".mjs", ".map", ".html", ".htm", ".xhtml", ".svgz")):
                if not path_only.endswith(".svg"):
                    return
            if not (
                any(path_only.endswith(ext) for ext in _IMAGE_PATH_EXTS)
                or "/" in ref
                or "\\" in ref
            ):
                return

        key = _image_ref_dedupe_key(ref)
        if not key:
            return
        prev = chosen.get(key)
        if prev is None:
            chosen[key] = ref
            order.append(key)
            return
        # 同资源：优先更长（query 更完整）的 URL；data URI 已按内容哈希合并
        if len(ref) > len(prev):
            chosen[key] = ref

    # 1) 优先用正则抓真实标签上的图片属性（对钉钉等最稳，且已去掉 cangjie）
    for m in re.finditer(
        r"""<\s*img\b[^>]*?\b(?:src|data-src|data-original|data-url|data-lazy-src)\s*=\s*["']([^"']+)["']""",
        body,
        flags=re.I | re.S,
    ):
        _add(m.group(1))
    for m in re.finditer(
        r"""<\s*(?:source|image|embed|object|input)\b[^>]*?\b(?:src|data|data-src)\s*=\s*["']([^"']+)["']""",
        body,
        flags=re.I | re.S,
    ):
        _add(m.group(1))
    for m in re.finditer(
        r"""<\s*meta\b[^>]*?\b(?:property|name)\s*=\s*["'](?:og:image(?::url)?|twitter:image(?::src)?)["'][^>]*?\bcontent\s*=\s*["']([^"']+)["']""",
        body,
        flags=re.I | re.S,
    ):
        _add(m.group(1))
    for m in re.finditer(
        r"""<\s*meta\b[^>]*?\bcontent\s*=\s*["']([^"']+)["'][^>]*?\b(?:property|name)\s*=\s*["'](?:og:image(?::url)?|twitter:image(?::src)?)["']""",
        body,
        flags=re.I | re.S,
    ):
        _add(m.group(1))
    for m in re.finditer(r"""url\(\s*['"]?([^'")\s]+)['"]?\s*\)""", body, flags=re.I):
        _add(m.group(1))
    for m in re.finditer(r"(data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+)", body):
        _add(re.sub(r"\s+", "", m.group(1)))
    for m in re.finditer(
        r"""\bsrcset\s*=\s*["']([^"']+)["']""",
        body,
        flags=re.I,
    ):
        raw_ss = m.group(1)
        if raw_ss.lower().startswith("data:"):
            _add(raw_ss)
        else:
            for part in raw_ss.split(","):
                token = part.strip().split()[0] if part.strip() else ""
                if token:
                    _add(token)

    # 2) 结构化解析补充（懒加载属性、非常规标签）
    parser = _HtmlImageRefParser()
    try:
        parser.feed(body)
        parser.close()
    except Exception:
        parser.refs = []
    for s in parser.refs:
        _add(s)

    # 3) 仍为空时再扫裸 URL（最后手段）
    if not order:
        for m in re.finditer(r"""https?://[^\s"'<>\\]+""", body):
            u = m.group(0).rstrip("\\").rstrip(".,);]")
            if _looks_like_image_url(_normalize_ref_text(u)):
                _add(u)
        for m in re.finditer(r"""//[^\s"'<>\\]+""", body):
            u = m.group(0).rstrip("\\").rstrip(".,);]")
            nu = _normalize_ref_text(u)
            if nu.startswith("//") and _looks_like_image_url("https:" + nu):
                _add(u)

    return [chosen[k] for k in order]
