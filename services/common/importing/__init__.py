"""services.common.importing —— 导入纯逻辑层。

分层约定（见 TECHNICAL.md §1 分层与依赖规则）：
- 纯解析 / 抽取 / 去重 / 临时存储 / 导入计划：本子包（无 Qt 依赖）
- Qt Clipboard / QImage / QUrl 适配：ui/adapters/clipboard_adapter.py
  （唯一允许触碰 QClipboard / QMimeData / QImage 的模块）
"""
from .clipboard import ClipboardPayload, _read_windows_html_clipboard, parse_text_paths
from .constants import (
    DOC_EXTS,
    EXTRACT_EXTS,
    HTML_EXTS,
    IMAGE_EXTS,
    IMPORT_EXTS,
    RAW_EXTS,
    VALID_EXTS,
    _IMAGE_PATH_EXTS,
    _IMG_MAGIC,
    _MIN_EXTRACTED_IMAGE_BYTES,
    image_dialog_filter,
    image_only_dialog_filter,
)
from .container import _extract_images_from_container, run_extract_jobs
from .docx_extractor import _extract_images_from_docx
from .html_extractor import (
    _HtmlImageRefParser,
    _cf_html_source_url,
    _extract_image_refs_from_html,
    _sanitize_html_for_image_extract,
    _strip_cf_html_header,
)
from .import_service import (
    ACTION_EXTRACT_HTML,
    ACTION_IMPORT_PATHS,
    ACTION_SAVE_IMAGE,
    ClipboardAction,
    FileImportService,
    ImportPlan,
)
from .pdf_extractor import _extract_images_from_pdf
from .refs import (
    _file_uri_to_local_path,
    _image_ref_dedupe_key,
    _looks_like_image_url,
    _normalize_ref_text,
)
from .resolver import (
    _copy_local_image,
    _decode_data_image_uri,
    _download_image_url,
    _materialize_image_refs,
    _read_html_file_text,
    _resolve_html_ref,
)
from .scanner import (
    _collect_extract_files,
    _collect_import_groups,
    _normalize_local_path,
    _scan_folder_extract_files,
    _scan_folder_images,
)
from .temp_store import (
    _guess_image_ext,
    _paste_temp_dir,
    _unique_paste_path,
    _write_image_bytes,
)

__all__ = [
    # constants
    "IMAGE_EXTS", "RAW_EXTS", "DOC_EXTS", "HTML_EXTS", "EXTRACT_EXTS", "VALID_EXTS",
    "IMPORT_EXTS", "_IMAGE_PATH_EXTS", "_MIN_EXTRACTED_IMAGE_BYTES", "_IMG_MAGIC",
    "image_dialog_filter", "image_only_dialog_filter",
    # scanner
    "_normalize_local_path", "_scan_folder_images", "_scan_folder_extract_files",
    "_collect_import_groups", "_collect_extract_files",
    # temp store
    "_paste_temp_dir", "_guess_image_ext", "_unique_paste_path", "_write_image_bytes",
    # refs / dedupe
    "_file_uri_to_local_path", "_looks_like_image_url", "_normalize_ref_text",
    "_image_ref_dedupe_key",
    # html extractor
    "_HtmlImageRefParser", "_strip_cf_html_header", "_cf_html_source_url",
    "_sanitize_html_for_image_extract", "_extract_image_refs_from_html",
    # resolver / materialize
    "_decode_data_image_uri", "_copy_local_image", "_download_image_url",
    "_resolve_html_ref", "_materialize_image_refs", "_read_html_file_text",
    # container extractors
    "_extract_images_from_docx", "_extract_images_from_pdf",
    "_extract_images_from_container", "run_extract_jobs",
    # clipboard
    "ClipboardPayload", "parse_text_paths", "_read_windows_html_clipboard",
    # import service
    "FileImportService", "ImportPlan", "ClipboardAction",
    "ACTION_IMPORT_PATHS", "ACTION_SAVE_IMAGE", "ACTION_EXTRACT_HTML",
]
