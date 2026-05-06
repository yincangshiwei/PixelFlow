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


class ProcessWorker(QThread):
    """后台处理线程"""
    progress = Signal(int, int, str)   # current, total, filename
    image_done = Signal(object)        # ProcessResult
    all_done = Signal(list)            # list[ProcessResult]
    debug = Signal(str)                # 详细调试/异常信息

    def __init__(self, file_list: list[str], output_dir: str,
                 processor: BaseProcessor, options: dict,
                 auto_subfolder: bool = True, parent=None):
        super().__init__(parent)
        self.file_list = file_list
        self.output_dir = output_dir
        self.processor = processor
        self.options = options
        self.auto_subfolder = auto_subfolder
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
                results = self.processor.process_batch(self.file_list, self.options, str(out_dir), _progress_cb)
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

                # 构建输出文件名（支持重命名）
                stem = _build_stem(src.stem, self.options, i + 1)
                out_path = out_dir / (stem + ext)
                counter = 1
                while out_path.exists():
                    out_path = out_dir / f"{stem}_{counter}{ext}"
                    counter += 1

                # 保存
                if actual_fmt == "jpg":
                    save_img = img.convert("RGB") if img.mode in ("RGBA", "LA", "P") else img
                    if self.options.get("enable_compress"):
                        if self.options.get("compress_mode") == "size":
                            target_kb = self.options.get("target_size_kb", 500)
                            save_img, final_q, final_size = compress_to_target_size(save_img, target_kb, "JPEG")
                            details["compress_info"] = f"质量:{final_q}, 大小:{final_size}KB"
                            save_img.save(str(out_path), "JPEG", quality=final_q)
                        else:
                            quality = self.options.get("quality", 85)
                            save_img.save(str(out_path), "JPEG", quality=quality)
                    else:
                        save_img.save(str(out_path), "JPEG", quality=95)

                elif actual_fmt == "webp":
                    if self.options.get("enable_compress"):
                        if self.options.get("compress_mode") == "size":
                            target_kb = self.options.get("target_size_kb", 500)
                            img, final_q, final_size = compress_to_target_size(img, target_kb, "WEBP")
                            details["compress_info"] = f"质量:{final_q}, 大小:{final_size}KB"
                            img.save(str(out_path), "WEBP", quality=final_q)
                        else:
                            quality = self.options.get("quality", 85)
                            img.save(str(out_path), "WEBP", quality=quality)
                    else:
                        img.save(str(out_path), "WEBP", quality=95)

                elif actual_fmt == "bmp":
                    save_img = img.convert("RGB") if img.mode in ("RGBA", "LA", "P") else img
                    save_img.save(str(out_path), "BMP")
                else:
                    # PNG
                    img.save(str(out_path), "PNG")

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
