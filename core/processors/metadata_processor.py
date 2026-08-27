"""
批量元数据编辑处理器（纯处理，无 UI）。

P4：UI 已迁至 ui.routes.process.features.metadata_route.MetadataFeatureRoute；
参数校验/规范化在 services.features.metadata_service.MetadataService。
"""
from __future__ import annotations

import os
from pathlib import Path

from PIL import Image

from core.base_processor import BaseProcessor, ProcessResult
from core.metadata_utils import (
    FIELD_KEYS,
    load_excel_lookup, resolve_field_value,
    write_image_metadata, normalize_keywords,
    fields_for_format, detect_true_format, ext_of_format,
    normalize_format_name,
)


def default_metadata_options() -> dict:
    """出厂默认参数（MetadataService / 预设与此保持一致）。"""
    fields = {}
    col_hint = {"title": 2, "description": 3, "author": 4, "copyright": 5, "keywords": 6}
    for key in FIELD_KEYS:
        fields[key] = {
            "enabled": False,
            "source": "fixed",
            "value": "",
            "excel_file": "",
            "match_column": 1,
            "data_column": col_hint.get(key, 2),
            "excel_row_start": 2,
        }
    return {
        "clear_all": False,
        "enable_convert": True,
        "target_format": "jpg",
        "fields": fields,
    }


class MetadataProcessor(BaseProcessor):
    """批量编辑图片文件元数据 + 可选格式转换（纯处理，无 UI）。"""

    name = "元数据编辑"
    description = "批量修改图片标题、描述、作者、版权、标记，支持格式转换与 Excel 匹配"
    icon = "🏷"
    preset_id = "metadata_edit"

    @property
    def is_batch_processor(self) -> bool:
        return True

    def default_options(self) -> dict:
        return default_metadata_options()

    def process(self, img: Image.Image, options: dict) -> tuple[Image.Image, dict]:
        return img, {}

    def process_batch(
        self,
        file_list: list[str],
        options: dict,
        output_dir: str,
        progress_callback=None,
    ) -> list[ProcessResult]:
        clear_all = bool(options.get("clear_all", False))
        enable_convert = bool(options.get("enable_convert", False))
        target_format = normalize_format_name(options.get("target_format", "")) or "jpg"
        field_confs = options.get("fields") or {}

        if enable_convert:
            allowed = set(fields_for_format(target_format))
        else:
            allowed = set(FIELD_KEYS)

        excel_maps: dict[str, dict[str, str]] = {}

        def _excel_key(conf: dict) -> str:
            return "|".join([
                conf.get("excel_file", ""),
                str(conf.get("match_column", 1)),
                str(conf.get("data_column", 2)),
                str(conf.get("excel_row_start", 2)),
            ])

        if not clear_all:
            for key in FIELD_KEYS:
                if key not in allowed:
                    continue
                conf = field_confs.get(key) or {}
                if not conf.get("enabled"):
                    continue
                if conf.get("source") != "excel":
                    continue
                ck = _excel_key(conf)
                if ck in excel_maps:
                    continue
                path = conf.get("excel_file", "")
                if not path or not os.path.exists(path):
                    excel_maps[ck] = {}
                    continue
                try:
                    excel_maps[ck] = load_excel_lookup(
                        path,
                        int(conf.get("match_column", 1)),
                        int(conf.get("data_column", 2)),
                        int(conf.get("excel_row_start", 2)),
                    )
                except Exception as e:
                    excel_maps[ck] = {}
                    if progress_callback:
                        progress_callback(0, len(file_list), f"Excel 读取失败: {e}")

        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        allow_overwrite = bool(options.get("_overwrite", False))
        rel_path_map = options.get("_rel_path_map") or {}
        results: list[ProcessResult] = []
        total = len(file_list)

        # 延迟导入，避免与 worker 循环依赖在模块加载期出问题
        from core.worker import resolve_file_out_dir

        for i, fpath in enumerate(file_list):
            if progress_callback:
                progress_callback(i + 1, total, Path(fpath).name)

            src = Path(fpath)
            result = ProcessResult(input_path=fpath)
            try:
                true_fmt = detect_true_format(src)

                if enable_convert:
                    out_ext = ext_of_format(target_format)
                else:
                    out_ext = src.suffix if src.suffix else ext_of_format(true_fmt or "png")

                if allow_overwrite:
                    dst = src.with_suffix(out_ext) if enable_convert else src
                else:
                    file_out_dir = resolve_file_out_dir(out_dir, fpath, rel_path_map)
                    dst = file_out_dir / f"{src.stem}{out_ext}"
                    if dst.resolve() == src.resolve() or dst.exists():
                        counter = 1
                        while True:
                            cand = file_out_dir / f"{src.stem}_{counter}{out_ext}"
                            if not cand.exists() and cand.resolve() != src.resolve():
                                dst = cand
                                break
                            counter += 1

                fields: dict[str, str] = {}
                if not clear_all:
                    for key in FIELD_KEYS:
                        if key not in allowed:
                            continue
                        conf = field_confs.get(key) or {}
                        if not conf.get("enabled"):
                            continue
                        source = conf.get("source", "fixed")
                        excel_map = None
                        if source == "excel":
                            excel_map = excel_maps.get(_excel_key(conf), {})
                        value = resolve_field_value(
                            source,
                            conf.get("value", ""),
                            fpath,
                            excel_map,
                        )
                        if key == "keywords":
                            value = normalize_keywords(value)
                        fields[key] = value

                details = write_image_metadata(
                    str(src),
                    str(dst),
                    fields,
                    clear_all=clear_all,
                    target_format=target_format if enable_convert else None,
                    enable_convert=enable_convert,
                )
                if (
                    allow_overwrite
                    and enable_convert
                    and dst.resolve() != src.resolve()
                    and src.exists()
                ):
                    try:
                        if dst.exists() and details is not None:
                            src.unlink()
                            details["removed_source"] = src.name
                    except OSError:
                        details["warning"] = (
                            (details.get("warning") or "")
                            + f" 已写出 {dst.name}，但未能删除原文件 {src.name}"
                        ).strip()

                result.output_path = str(dst)
                result.success = True
                result.details = details
            except Exception as e:
                result.success = False
                result.error = str(e)

            results.append(result)

        return results
