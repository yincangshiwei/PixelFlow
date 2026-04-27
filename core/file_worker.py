"""
PixelFlow 文件处理工作线程
用于 BaseFileProcessor 体系（文档转换等非图片处理任务）。
"""
from PySide6.QtCore import QThread, Signal
from pathlib import Path

from core.base_file_processor import BaseFileProcessor, FileProcessResult


class FileProcessWorker(QThread):
    """后台文件处理线程（对应 BaseFileProcessor）"""
    progress = Signal(int, int, str)   # current, total, filename
    file_done = Signal(object)         # FileProcessResult
    all_done = Signal(list)            # list[FileProcessResult]

    def __init__(self, file_list: list[str], output_dir: str,
                 processor: BaseFileProcessor, options: dict,
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
        results = []
        total = len(self.file_list)

        out_dir = Path(self.output_dir)
        if self.auto_subfolder:
            out_dir = out_dir / "PixelFlow_output"
        out_dir.mkdir(parents=True, exist_ok=True)

        for i, fpath in enumerate(self.file_list):
            if self._cancelled:
                break

            src = Path(fpath)
            self.progress.emit(i + 1, total, src.name)

            try:
                result = self.processor.process_file(
                    str(fpath), str(out_dir), self.options, i + 1
                )
            except Exception as e:
                result = FileProcessResult(
                    input_path=str(fpath),
                    success=False,
                    error=str(e)
                )

            results.append(result)
            self.file_done.emit(result)

        self.all_done.emit(results)
