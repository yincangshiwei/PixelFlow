"""
PixelFlow 通用工作线程
基于 BaseProcessor 插件架构的批量处理
"""
import traceback

from PySide6.QtCore import QThread, Signal
from PIL import Image
from pathlib import Path

from core.base_processor import BaseProcessor, ProcessResult
from core.image_processor import compress_to_target_size


def _build_stem(original_stem: str, options: dict, index: int) -> str:
    """根据重命名选项构建输出文件主名（不含扩展名）"""
    if not options.get("enable_rename"):
        return original_stem

    digits = options.get("digits", 3)
    start = options.get("start_index", 1)
    seq = str(start + index - 1).zfill(digits)

    mode = options.get("prefix_mode", "custom")
    if mode == "keep":
        return f"{original_stem}_{seq}"
    else:
        prefix = options.get("prefix", "").strip()
        if prefix:
            return f"{prefix}_{seq}"
        return seq


def resolve_file_out_dir(base_out_dir: Path, fpath: str, rel_path_map: dict | None) -> Path:
    """
    根据相对路径映射，在输出根目录下解析单文件的目标子目录。
    rel_path 形如 "sub/a.png" 时，返回 base_out_dir/sub，并确保目录存在。
    """
    rel = None
    if rel_path_map:
        rel = rel_path_map.get(fpath)
    if not rel:
        base_out_dir.mkdir(parents=True, exist_ok=True)
        return base_out_dir
    rel_p = Path(str(rel).replace("\\", "/"))
    # 防御：去掉盘符/绝对路径/向上穿越
    parts = [p for p in rel_p.parts if p not in ("/", "\\", ".", "..") and ":" not in p]
    if len(parts) > 1:
        target = base_out_dir.joinpath(*parts[:-1])
    else:
        target = base_out_dir
    target.mkdir(parents=True, exist_ok=True)
    return target


class ProcessWorker(QThread):
    """后台处理线程"""
    progress = Signal(int, int, str)   # current, total, filename
    image_done = Signal(object)        # ProcessResult
    all_done = Signal(list)            # list[ProcessResult]
    debug = Signal(str)                # 详细调试/异常信息

    def __init__(self, file_list: list[str], output_dir: str,
                 processor: BaseProcessor, options: dict,
                 auto_subfolder: bool = True, overwrite: bool = False,
                 rel_path_map: dict | None = None,
                 parent=None):
        super().__init__(parent)
        self.file_list = file_list
        self.output_dir = output_dir
        self.processor = processor
        self.options = options
        self.auto_subfolder = auto_subfolder
        self.overwrite = overwrite
        self.rel_path_map = rel_path_map or {}
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        out_dir = Path(self.output_dir)
        if self.auto_subfolder:
            out_dir = out_dir / "PixelFlow_output"
        out_dir.mkdir(parents=True, exist_ok=True)

        if getattr(self.processor, "is_batch_processor", False):
            # 批量合并处理（如图片转PPT/PDF/Word）
            def _progress_cb(current, total, msg):
                if not self._cancelled:
                    self.progress.emit(current, total, msg)

            try:
                self.debug.emit(f"开始批量合并处理: {self.processor.name}，文件数: {len(self.file_list)}，输出目录: {out_dir}")
                batch_options = dict(self.options)
                batch_options["_overwrite"] = self.overwrite
                # 元数据等逐文件写出的 batch 处理器可据此重建子目录
                if self.rel_path_map:
                    batch_options["_rel_path_map"] = self.rel_path_map
                results = self.processor.process_batch(self.file_list, batch_options, str(out_dir), _progress_cb)
            except Exception as e:
                # 发生严重异常时返回单个失败结果
                self.debug.emit("批量处理发生未捕获异常:\n" + traceback.format_exc())
                res = ProcessResult(input_path="批量处理", success=False, error=str(e))
                results = [res]
            
            for res in results:
                self.image_done.emit(res)
            self.all_done.emit(results)
            return

        # 以下为原有的逐张处理逻辑
        results = []
        total = len(self.file_list)

        fmt = self.processor.get_output_format()
        # fmt 为空串时保留原始格式
        ext_map = {"png": ".png", "jpg": ".jpg", "webp": ".webp", "bmp": ".bmp"}

        for i, fpath in enumerate(self.file_list):
            if self._cancelled:
                break

            src = Path(fpath)
            self.progress.emit(i + 1, total, src.name)

            result = ProcessResult(input_path=fpath)
            try:
                img = Image.open(fpath)
                # 为处理器提供额外的上下文信息（图片索引和路径）
                process_options = dict(self.options)
                process_options['_image_index'] = i
                process_options['_current_image_path'] = fpath
                img, details = self.processor.process(img, process_options)

                # 确定实际输出格式
                actual_fmt = fmt if fmt else src.suffix.lstrip(".").lower()
                # 规范化：jpeg → jpg
                if actual_fmt == "jpeg":
                    actual_fmt = "jpg"
                ext = ext_map.get(actual_fmt, src.suffix.lower() or ".png")

                # 构建输出文件名（支持重命名）；按相对路径落到对应子目录
                file_out_dir = resolve_file_out_dir(out_dir, fpath, self.rel_path_map)
                stem = _build_stem(src.stem, self.options, i + 1)
                out_path = file_out_dir / (stem + ext)
                counter = 1
                while out_path.exists():
                    out_path = file_out_dir / f"{stem}_{counter}{ext}"
                    counter += 1

                # 保存参数：DPI 仅写入 density 元数据，不缩放像素；
                # 未开压缩时 JPG/WEBP 仍用 quality=95（与既有最优质量策略一致）
                dpi_tuple = None
                if self.options.get("enable_dpi"):
                    dpi_val = int(self.options.get("dpi", 300))
                    dpi_tuple = (dpi_val, dpi_val)
                    details["dpi"] = dpi_val

                def _dpi_kw():
                    return {"dpi": dpi_tuple} if dpi_tuple else {}

                if actual_fmt == "jpg":
                    save_img = img.convert("RGB") if img.mode in ("RGBA", "LA", "P") else img
                    if self.options.get("enable_compress"):
                        if self.options.get("compress_mode") == "size":
                            target_kb = self.options.get("target_size_kb", 500)
                            save_img, final_q, final_size = compress_to_target_size(save_img, target_kb, "JPEG")
                            details["compress_info"] = f"质量:{final_q}, 大小:{final_size}KB"
                            save_img.save(str(out_path), "JPEG", quality=final_q, **_dpi_kw())
                        else:
                            quality = self.options.get("quality", 85)
                            save_img.save(str(out_path), "JPEG", quality=quality, **_dpi_kw())
                    else:
                        save_img.save(str(out_path), "JPEG", quality=95, **_dpi_kw())

                elif actual_fmt == "webp":
                    if self.options.get("enable_compress"):
                        if self.options.get("compress_mode") == "size":
                            target_kb = self.options.get("target_size_kb", 500)
                            img, final_q, final_size = compress_to_target_size(img, target_kb, "WEBP")
                            details["compress_info"] = f"质量:{final_q}, 大小:{final_size}KB"
                            img.save(str(out_path), "WEBP", quality=final_q, **_dpi_kw())
                        else:
                            quality = self.options.get("quality", 85)
                            img.save(str(out_path), "WEBP", quality=quality, **_dpi_kw())
                    else:
                        img.save(str(out_path), "WEBP", quality=95, **_dpi_kw())

                elif actual_fmt == "bmp":
                    save_img = img.convert("RGB") if img.mode in ("RGBA", "LA", "P") else img
                    # BMP：Pillow 将 dpi 写入文件头 XPelsPerMeter / YPelsPerMeter
                    save_img.save(str(out_path), "BMP", **_dpi_kw())
                else:
                    # PNG：dpi 写入 pHYs 块，像素数据仍为 PNG 无损编码
                    img.save(str(out_path), "PNG", **_dpi_kw())

                result.output_path = str(out_path)
                result.success = True
                result.details = details
            except Exception as e:
                result.success = False
                result.error = str(e)
                self.debug.emit(f"处理失败: {fpath}\n" + traceback.format_exc())

            results.append(result)
            self.image_done.emit(result)

        self.all_done.emit(results)
