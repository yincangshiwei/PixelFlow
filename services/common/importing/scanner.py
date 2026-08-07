"""本地路径扫描与导入分组（纯逻辑，无 Qt 依赖）。"""
import os
from pathlib import Path

from .constants import EXTRACT_EXTS, IMAGE_EXTS


def _normalize_local_path(raw: str | Path) -> Path:
    p = Path(raw)
    try:
        return p.resolve()
    except OSError:
        return Path(os.path.abspath(str(p)))


def _scan_folder_images(folder: str | Path) -> list[str]:
    """递归扫描文件夹中的图片文件，按路径排序。"""
    root = Path(folder)
    return [
        str(f) for f in sorted(root.rglob("*"))
        if f.is_file() and f.suffix.lower() in IMAGE_EXTS
    ]


def _scan_folder_extract_files(folder: str | Path) -> list[Path]:
    """递归扫描文件夹中的抽图容器（HTML/DOCX/PDF）。"""
    root = Path(folder)
    return [
        f for f in sorted(root.rglob("*"))
        if f.is_file() and f.suffix.lower() in EXTRACT_EXTS
    ]


def _collect_import_groups(local_paths: list[str | Path]) -> list[tuple[list[str], str | None]]:
    """
    将本地路径列表整理为可插入列表的图片分组。
    每个分组为 (files, base_dir)：文件夹以自身为 base_dir，单文件 base_dir=None。
    抽图容器（HTML/DOCX/PDF）不进入分组。
    """
    groups: list[tuple[list[str], str | None]] = []
    for raw in local_paths:
        if not raw:
            continue
        p = _normalize_local_path(raw)
        if p.is_file() and p.suffix.lower() in EXTRACT_EXTS:
            continue
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            groups.append(([str(p)], None))
        elif p.is_dir():
            files = _scan_folder_images(p)
            if files:
                groups.append((files, str(p)))
    return groups


def _collect_extract_files(local_paths: list[str | Path]) -> list[Path]:
    """
    收集需要抽图的容器文件。
    - 直接选中的 HTML/DOCX/PDF
    - 文件夹内递归到的 HTML/DOCX/PDF
    """
    result: list[Path] = []
    seen: set[str] = set()

    def _add(p: Path):
        if not p.is_file() or p.suffix.lower() not in EXTRACT_EXTS:
            return
        key = str(p)
        if key not in seen:
            seen.add(key)
            result.append(p)

    for raw in local_paths:
        if not raw:
            continue
        p = _normalize_local_path(raw)
        if p.is_file():
            _add(p)
        elif p.is_dir():
            for f in _scan_folder_extract_files(p):
                _add(f)
    return result
