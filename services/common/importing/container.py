"""容器抽图分发与抽取任务执行（纯逻辑）。"""
import hashlib
from pathlib import Path

from .constants import HTML_EXTS
from .docx_extractor import _extract_images_from_docx
from .html_extractor import _extract_image_refs_from_html
from .pdf_extractor import _extract_images_from_pdf
from .resolver import _materialize_image_refs, _read_html_file_text


def _extract_images_from_container(path: Path) -> list[str]:
    """按扩展名从容器文件提取图片路径列表。"""
    ext = path.suffix.lower()
    if ext in HTML_EXTS:
        try:
            text = _read_html_file_text(path)
        except OSError:
            return []
        refs = _extract_image_refs_from_html(text)
        if not refs:
            return []
        return _materialize_image_refs(refs, html_base=path.parent, page_url=path.as_uri())
    if ext == ".docx":
        return _extract_images_from_docx(path)
    if ext == ".pdf":
        return _extract_images_from_pdf(path)
    return []


def run_extract_jobs(
    container_paths: list[str],
    html_jobs: list[tuple[list[str], str | None, str]],
) -> list[str]:
    """
    执行容器 + 剪贴板 HTML 抽图任务，返回去重后的本地图片路径。

    - container_paths: 本地 HTML/DOCX/PDF 路径列表
    - html_jobs: (refs, html_base_str|None, page_url) 列表
    去重：路径 + 内容 sha1 双层。
    """
    all_paths: list[str] = []
    seen_path: set[str] = set()
    seen_content: set[str] = set()

    def _add_path(p: str):
        if not p or p in seen_path:
            return
        try:
            digest = hashlib.sha1(Path(p).read_bytes()).hexdigest()
            if digest in seen_content:
                return
            seen_content.add(digest)
        except OSError:
            pass
        seen_path.add(p)
        all_paths.append(p)

    for cp in container_paths:
        try:
            for p in _extract_images_from_container(Path(cp)):
                _add_path(p)
        except Exception:
            continue

    for refs, base_s, page_url in html_jobs:
        base = Path(base_s) if base_s else None
        for p in _materialize_image_refs(refs, html_base=base, page_url=page_url or ""):
            _add_path(p)

    return all_paths
