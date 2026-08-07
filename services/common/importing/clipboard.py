"""剪贴板载荷 DTO 与纯解析。

Qt 对象（QClipboard/QMimeData/QImage）的读取在 ui/adapters/clipboard_adapter.py
完成；本模块只接收/产出普通 Python 数据。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from .refs import _file_uri_to_local_path


@dataclass
class ClipboardPayload:
    """Qt Adapter 读取的剪贴板普通快照（不含任何 Qt 对象）。

    - local_paths: mime urls 解析出的本地文件/文件夹路径
    - has_image:   是否存在位图（截图 / 浏览器复制等）。位图落盘延迟到
                   动作链确认走「save_image」时执行，避免路径导入成功时
                   产生孤儿临时文件
    - html_text:   text/html 或 Windows CF_HTML 原文
    - text:        纯文本内容
    """

    local_paths: list[str] = field(default_factory=list)
    has_image: bool = False
    html_text: str = ""
    text: str = ""


def parse_text_paths(text: str) -> list[str]:
    """从纯文本解析本地路径候选（每行一条，支持 file: URI 与引号包裹）。"""
    candidates: list[str] = []
    if not text:
        return candidates
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.strip().strip('"').strip("'")
        if not line:
            continue
        if line.lower().startswith("file:"):
            local = _file_uri_to_local_path(line)
            if local:
                candidates.append(local)
            continue
        p = Path(line)
        if p.exists():
            candidates.append(str(p))
    return candidates


def _read_windows_html_clipboard() -> str:
    """
    读取 Windows CF_HTML（钉钉等可能只放 HTML Format，Qt 有时取不到完整内容）。
    失败返回空字符串。仅使用标准库 ctypes。
    """
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
        user32.EnumClipboardFormats.restype = wintypes.UINT
        user32.GetClipboardFormatNameW.argtypes = [wintypes.UINT, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClipboardFormatNameW.restype = ctypes.c_int
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalSize.restype = ctypes.c_size_t
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL

        if not user32.OpenClipboard(None):
            return ""
        try:
            fmt = 0
            html_fmt = 0
            while True:
                fmt = user32.EnumClipboardFormats(fmt)
                if fmt == 0:
                    break
                buf = ctypes.create_unicode_buffer(512)
                n = user32.GetClipboardFormatNameW(fmt, buf, 512)
                if n and buf.value == "HTML Format":
                    html_fmt = fmt
                    break
            if not html_fmt:
                return ""
            handle = user32.GetClipboardData(html_fmt)
            if not handle:
                return ""
            size = int(kernel32.GlobalSize(handle) or 0)
            if size <= 0 or size > 20 * 1024 * 1024:
                return ""
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                data = ctypes.string_at(ptr, size)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

        # CF_HTML 通常为 UTF-8，末尾可能带 \0
        text = data.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
        return text
    except Exception:
        return ""
