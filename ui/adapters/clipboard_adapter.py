"""Qt 剪贴板适配器 —— 唯一触碰 QClipboard / QMimeData / QImage 的模块。

职责：把 Qt 剪贴板对象转换为 services.common.importing.ClipboardPayload
（普通数据），并提供位图落盘、QUrl→本地路径转换。不写业务规则。
"""
from PySide6.QtCore import QUrl
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QApplication

from services.common.importing import (
    ClipboardPayload,
    _read_windows_html_clipboard,
    _unique_paste_path,
)


def urls_to_local_paths(urls) -> list[str]:
    """从 QUrl 列表提取本地文件/文件夹路径。"""
    paths: list[str] = []
    for url in urls:
        if isinstance(url, QUrl):
            if not url.isLocalFile():
                continue
            local = url.toLocalFile()
        else:
            local = str(url)
        if local:
            paths.append(local)
    return paths


def save_clipboard_image(image) -> str | None:
    """将剪贴板中的位图保存为临时 PNG，返回路径；失败返回 None。"""
    if image is None:
        return None
    qimg = None
    if isinstance(image, QImage):
        qimg = image
    elif isinstance(image, QPixmap):
        qimg = image.toImage()
    else:
        try:
            qimg = QImage(image)
        except Exception:
            return None
    if qimg is None or qimg.isNull():
        return None
    out = _unique_paste_path("", ".png")
    if out is None:
        return None
    if qimg.save(str(out), "PNG"):
        return str(out.resolve())
    return None


def clipboard_html_text(mime) -> str:
    """优先 Qt text/html，否则 Windows CF_HTML 回退。"""
    html = ""
    if mime is not None and mime.hasHtml():
        html = mime.html() or ""
    if not html and mime is not None:
        # 部分环境以自定义格式暴露
        for fmt in ("text/html", "HTML Format", "application/x-qt-windows-mime;value=\"HTML Format\""):
            if mime.hasFormat(fmt):
                try:
                    ba = mime.data(fmt)
                    raw = bytes(ba)
                    if raw:
                        html = raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
                        break
                except Exception:
                    pass
    if not html or ("<img" not in html.lower() and "data:image/" not in html.lower()):
        win_html = _read_windows_html_clipboard()
        if win_html and (len(win_html) > len(html or "")):
            html = win_html
    return html or ""


def _clipboard_image_object(clipboard):
    """取剪贴板位图对象（QImage/QPixmap），无则 None。"""
    mime = clipboard.mimeData() if clipboard is not None else None
    image = None
    if mime is not None and mime.hasImage():
        image = mime.imageData()
    if image is None and clipboard is not None:
        pix = clipboard.pixmap()
        if pix is not None and not pix.isNull():
            image = pix
    return image


def read_clipboard_payload(clipboard=None) -> ClipboardPayload:
    """读取剪贴板并转换为普通载荷。

    位图不在此处落盘：仅记录 has_image，待动作链确认走 save_image 时
    再调 grab_clipboard_image_path，避免路径导入成功时产生孤儿临时文件。
    """
    if clipboard is None:
        clipboard = QApplication.clipboard()
    payload = ClipboardPayload()
    if clipboard is None:
        return payload
    mime = clipboard.mimeData()
    if mime is None:
        return payload
    if mime.hasUrls():
        payload.local_paths = urls_to_local_paths(mime.urls())
    payload.has_image = _clipboard_image_object(clipboard) is not None
    payload.html_text = clipboard_html_text(mime)
    if mime.hasText():
        payload.text = (mime.text() or "").strip()
    return payload


def grab_clipboard_image_path(clipboard=None) -> str | None:
    """将剪贴板位图落盘为临时 PNG 并返回路径（save_image 动作确认时调用）。"""
    if clipboard is None:
        clipboard = QApplication.clipboard()
    image = _clipboard_image_object(clipboard)
    if image is None:
        return None
    return save_clipboard_image(image)
