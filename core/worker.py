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
                 file_overwrite: bool | None = None,
                 rel_path_map: dict | None = None,
                 file_index_map: dict | None = None,
                 parent=None):
        super().__init__(parent)
        self.file_list = file_list
        self.output_dir = output_dir
        self.processor = processor
        self.options = options
        self.auto_subfolder = auto_subfolder
        # overwrite: 原图路径覆盖模式（batch 元数据等写回源文件）
        self.overwrite = overwrite
        # file_overwrite: 输出目录同名是否直接覆盖（桌面/自定义的「覆盖同名文件」）
        # 未显式传入时与 overwrite 一致，兼容旧调用
        self.file_overwrite = overwrite if file_overwrite is None else bool(file_overwrite)
        self.rel_path_map = rel_path_map or {}
        # 续跑/重试时保持原批次序号（重命名、_image_index）
        self.file_index_map = file_index_map or {}
        self._cancelled = False
        self._current_path: str | None = None
        self.results: list[ProcessResult] = []

    def cancel(self):
        # 只设置取消标记。常驻抠图进程由 run() 所在线程统一关闭，
        # 避免 GUI 线程与正在读写管道的工作线程并发销毁子进程。
        self._cancelled = True

    @property
    def current_path(self) -> str | None:
        return self._current_path

    def run(self):
        try:
            self.results = self._run_impl()
        except Exception as e:
            self.debug.emit("处理线程发生未捕获异常:\n" + traceback.format_exc())
            self.results = [
                ProcessResult(input_path="批量处理", success=False, error=str(e))
            ]
        finally:
            # 必须先释放 AI 抠图常驻子进程，再让 QThread 发出 finished。
            # 完成弹窗和 QThread 引用清理统一由主线程在 finished 后执行。
            # 常驻 worker 仅在本批处理期间保活；结束后 shutdown 释放 GPU/内存。
            try:
                from core.matting.inference import shutdown_matting_workers
                if bool(self.options.get("enable_matting")):
                    self.debug.emit("AI 抠图: 正在卸载模型并释放显存/内存…")
                shutdown_matting_workers()
                if bool(self.options.get("enable_matting")):
                    self.debug.emit("AI 抠图: 模型已卸载")
            except Exception as e:
                try:
                    self.debug.emit(f"AI 抠图: 卸载时异常（可忽略）: {e}")
                except Exception:
                    pass

    def _run_impl(self):
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
            return results

        # 以下为原有的逐张处理逻辑（支持 AI 抠图 micro-batch / 阶段3流水线）
        results = []
        total = len(self.file_list)

        # 输出格式由 GUI 线程在启动前快照，工作线程不得读取 QComboBox 等 UI 控件。
        fmt = str(self.options.get("_output_format", "") or "").lower()
        # fmt 为空串时保留原始格式
        ext_map = {"png": ".png", "jpg": ".jpg", "webp": ".webp", "bmp": ".bmp"}

        # micro-batch：仅当处理器提供 preferred_matting_batch_size / process_many
        batch_size = 1
        use_many = callable(getattr(self.processor, "process_many", None))
        use_pipeline = callable(getattr(self.processor, "matting_many", None))
        matting_on = bool(self.options.get("enable_matting"))
        matting_refine = bool(self.options.get("matting_refine"))
        matting_model = str(self.options.get("matting_model") or "ben2")
        matting_device = ""
        matting_vram = 0.0

        if matting_on:
            if use_many and callable(
                getattr(self.processor, "preferred_matting_batch_size", None)
            ):
                try:
                    batch_size = int(
                        self.processor.preferred_matting_batch_size(self.options) or 1
                    )
                except Exception as e:
                    self.debug.emit(f"解析抠图 batch 失败，回退逐张: {e}")
                    batch_size = 1
            batch_size = max(1, min(batch_size, 3))
            if matting_refine:
                batch_size = 1

            # 读取会话设备信息，写入启动摘要
            try:
                from core.matting.inference import ensure_matting_session_ready

                info = ensure_matting_session_ready(matting_model)
                matting_device = str(info.get("device") or "")
                matting_vram = float(info.get("vram_gb") or 0.0)
                if not matting_refine:
                    batch_size = max(
                        1, min(int(info.get("recommend_batch") or batch_size), 3)
                    )
            except Exception as e:
                self.debug.emit(f"AI 抠图会话预热: {e}")

            path_desc = (
                "全尺寸RGBA+边缘精炼"
                if matting_refine
                else "预缩放1024+仅mask"
            )
            # 配置偏好 vs worker 实际设备
            try:
                from core.matting.model_manager import get_matting_manager
                pref = (
                    get_matting_manager().get_device_preference() or "auto"
                ).lower()
            except Exception:
                pref = "auto"
            pref_labels = {
                "auto": "自动(优先GPU)",
                "cuda": "CUDA(GPU)",
                "cpu": "CPU",
            }
            pref_desc = pref_labels.get(pref, pref)
            act = (matting_device or "").lower()
            if act == "cuda" and matting_vram > 0:
                act_desc = f"CUDA · 显存约 {matting_vram:.1f} GB"
            elif act == "cpu":
                act_desc = "CPU"
            elif act:
                act_desc = act.upper()
            else:
                act_desc = "未知(预热未完成)"
            pipe_desc = "流水线开" if (use_pipeline and total > 1) else "流水线关"
            self.debug.emit(
                "AI 抠图: "
                f"模型={matting_model}  "
                f"推理设备偏好={pref_desc}  实际={act_desc}  "
                f"micro-batch={batch_size}  路径={path_desc}  {pipe_desc}"
                + ("  （OOM 将自动降 batch）" if batch_size > 1 else "")
            )
        else:
            batch_size = 1

        # ── 阶段3：AI 抠图开启且文件数>1 时走三阶段流水线 ──
        if matting_on and use_pipeline and total > 1 and not self._cancelled:
            try:
                from core.matting.pipeline import run_matting_pipeline

                def _set_cur(p):
                    self._current_path = p

                return run_matting_pipeline(
                    file_list=self.file_list,
                    file_index_map=self.file_index_map,
                    options=self.options,
                    processor=self.processor,
                    batch_size=batch_size,
                    out_dir=out_dir,
                    fmt=fmt,
                    ext_map=ext_map,
                    save_fn=self._save_processed_image,
                    cancelled=lambda: self._cancelled,
                    progress_fn=lambda c, t, n: self.progress.emit(c, t, n),
                    debug_fn=lambda s: self.debug.emit(s),
                    image_done_fn=lambda r: self.image_done.emit(r),
                    result_factory=lambda p: ProcessResult(input_path=p),
                    set_current_path_fn=_set_cur,
                    matting_device=matting_device,
                    prefetch_batches=2,
                    post_queue_depth=2,
                )
            except Exception as e:
                self.debug.emit(
                    f"AI 抠图流水线启动失败，回退串行: {e}\n"
                    + traceback.format_exc()
                )

        batch_round = 0  # 已完成的 micro-batch 轮次（仅日志）

        i = 0
        while i < total:
            if self._cancelled:
                break

            # 每轮重新读取建议值（OOM 降级后 cap 会变小）
            prev_bs = batch_size
            if (
                matting_on
                and use_many
                and batch_size >= 1
                and not matting_refine
            ):
                try:
                    from core.matting.inference import recommend_matting_batch_size

                    cur = int(
                        recommend_matting_batch_size(
                            matting_model,
                            refine=False,
                        )
                        or 1
                    )
                    batch_size = max(1, min(cur, 3))
                    if batch_size != prev_bs:
                        self.debug.emit(
                            f"AI 抠图: micro-batch 调整 {prev_bs} → {batch_size}"
                            + ("（可能因 OOM 降级）" if batch_size < prev_bs else "")
                        )
                except Exception:
                    pass

            chunk_end = min(i + batch_size, total)
            # 取消时不要开新 batch
            if self._cancelled:
                break

            if batch_size == 1 or not use_many or (chunk_end - i) == 1:
                # 单张路径
                fpath = self.file_list[i]
                src = Path(fpath)
                self._current_path = fpath
                order = int(self.file_index_map.get(fpath, i + 1) or (i + 1))
                self.progress.emit(i + 1, total, src.name)
                if matting_on:
                    self.debug.emit(
                        f"AI 抠图: [{i + 1}/{total}] {src.name}  "
                        f"batch=1  设备={(matting_device or 'auto').upper()}"
                    )

                result = ProcessResult(input_path=fpath)
                try:
                    img = Image.open(fpath)
                    process_options = dict(self.options)
                    process_options["_image_index"] = order - 1
                    process_options["_current_image_path"] = fpath
                    img, details = self.processor.process(img, process_options)
                    self._save_processed_image(
                        img, details, fpath, src, order, out_dir, fmt, ext_map, result
                    )
                except Exception as e:
                    result.success = False
                    result.error = str(e)
                    self.debug.emit(f"处理失败: {fpath}\n" + traceback.format_exc())

                results.append(result)
                self.image_done.emit(result)
                self._current_path = None
                i += 1
                continue

            # ── micro-batch 路径 ──
            chunk_paths = self.file_list[i:chunk_end]
            chunk_imgs: list = []
            chunk_opts: list[dict] = []
            chunk_meta: list[tuple] = []  # (fpath, src, order, list_index)
            open_errors: dict[int, Exception] = {}
            batch_round += 1
            names_preview = ", ".join(Path(p).name for p in chunk_paths[:4])
            if len(chunk_paths) > 4:
                names_preview += f" 等{len(chunk_paths)}张"
            self.debug.emit(
                f"AI 抠图: batch#{batch_round}  "
                f"文件 {i + 1}-{chunk_end}/{total}  "
                f"size={len(chunk_paths)}  "
                f"设备={(matting_device or 'auto').upper()}  "
                f"[{names_preview}]"
            )

            for j, fpath in enumerate(chunk_paths):
                list_i = i + j
                src = Path(fpath)
                order = int(self.file_index_map.get(fpath, list_i + 1) or (list_i + 1))
                self.progress.emit(list_i + 1, total, src.name)
                try:
                    img = Image.open(fpath)
                    img.load()
                    process_options = dict(self.options)
                    process_options["_image_index"] = order - 1
                    process_options["_current_image_path"] = fpath
                    chunk_imgs.append(img)
                    chunk_opts.append(process_options)
                    chunk_meta.append((fpath, src, order, list_i))
                except Exception as e:
                    open_errors[list_i] = e

            processed_map: dict[int, tuple] = {}  # list_i -> (img, details) | Exception
            if chunk_imgs:
                self._current_path = chunk_meta[0][0]
                try:
                    outs = self.processor.process_many(chunk_imgs, chunk_opts)
                    if len(outs) != len(chunk_meta):
                        raise RuntimeError(
                            f"process_many 返回数量不匹配: {len(outs)} vs {len(chunk_meta)}"
                        )
                    for (fpath, src, order, list_i), (img, details) in zip(
                        chunk_meta, outs
                    ):
                        processed_map[list_i] = (img, details, fpath, src, order)
                    self.debug.emit(
                        f"AI 抠图: batch#{batch_round} 完成  "
                        f"{len(chunk_meta)} 张  micro-batch={len(chunk_meta)}"
                    )
                except Exception as e:
                    # 整批失败：逐张重试，避免一张拖死整批
                    self.debug.emit(
                        f"AI 抠图: batch#{batch_round} 失败，逐张重试 "
                        f"({len(chunk_meta)} 张): {e}\n"
                        + traceback.format_exc()
                    )
                    for (fpath, src, order, list_i), img0, opt0 in zip(
                        chunk_meta, chunk_imgs, chunk_opts
                    ):
                        if self._cancelled:
                            break
                        try:
                            img, details = self.processor.process(img0, opt0)
                            processed_map[list_i] = (img, details, fpath, src, order)
                        except Exception as e2:
                            processed_map[list_i] = e2

            # 按原顺序写出结果（含 open 失败）
            for j, fpath in enumerate(chunk_paths):
                list_i = i + j
                src = Path(fpath)
                order = int(self.file_index_map.get(fpath, list_i + 1) or (list_i + 1))
                result = ProcessResult(input_path=fpath)
                self._current_path = fpath

                if list_i in open_errors:
                    result.success = False
                    result.error = str(open_errors[list_i])
                    self.debug.emit(
                        f"处理失败: {fpath}\n{open_errors[list_i]!r}"
                    )
                elif list_i not in processed_map:
                    result.success = False
                    result.error = "未处理（已取消或内部跳过）"
                else:
                    item = processed_map[list_i]
                    if isinstance(item, Exception):
                        result.success = False
                        result.error = str(item)
                        self.debug.emit(
                            f"处理失败: {fpath}\n{item!r}"
                        )
                    else:
                        img, details, fp, src2, order2 = item
                        try:
                            self._save_processed_image(
                                img,
                                details,
                                fp,
                                src2,
                                order2,
                                out_dir,
                                fmt,
                                ext_map,
                                result,
                            )
                        except Exception as e:
                            result.success = False
                            result.error = str(e)
                            self.debug.emit(
                                f"保存失败: {fpath}\n" + traceback.format_exc()
                            )

                results.append(result)
                self.image_done.emit(result)
                self._current_path = None

            i = chunk_end

        return results

    def _unique_out_path(self, file_out_dir: Path, stem: str, ext: str) -> Path:
        """按是否覆盖生成最终输出路径。

        file_overwrite 开启时直接使用 stem+ext；否则存在同名则追加 _1/_2…
        """
        out_path = file_out_dir / (stem + ext)
        if self.file_overwrite:
            return out_path
        counter = 1
        while out_path.exists():
            out_path = file_out_dir / f"{stem}_{counter}{ext}"
            counter += 1
        return out_path

    def _save_processed_image(
        self,
        img,
        details: dict,
        fpath: str,
        src: Path,
        order: int,
        out_dir: Path,
        fmt: str,
        ext_map: dict,
        result: ProcessResult,
    ) -> None:
        """将 process 结果写入磁盘并填充 ProcessResult。"""
        # 确定实际输出格式
        actual_fmt = fmt if fmt else src.suffix.lstrip(".").lower()
        # 规范化：jpeg → jpg
        if actual_fmt == "jpeg":
            actual_fmt = "jpg"
        ext = ext_map.get(actual_fmt, src.suffix.lower() or ".png")

        # 构建输出文件名（支持重命名）；按相对路径落到对应子目录
        file_out_dir = resolve_file_out_dir(out_dir, fpath, self.rel_path_map)
        stem = _build_stem(src.stem, self.options, order)
        out_path = self._unique_out_path(file_out_dir, stem, ext)

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
                    save_img, final_q, final_size = compress_to_target_size(
                        save_img, target_kb, "JPEG"
                    )
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
                    img, final_q, final_size = compress_to_target_size(
                        img, target_kb, "WEBP"
                    )
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

        # 额外保留抠图透明图：在主输出最终 stem 后追加 _matted（含 _1/_2 情况）
        matting_keep = details.pop("_matting_keep_img", None)
        if matting_keep is not None and self.options.get("keep_matting"):
            try:
                matted_stem = f"{out_path.stem}_matted"
                matted_path = self._unique_out_path(file_out_dir, matted_stem, ".png")
                keep_img = matting_keep.convert("RGBA")
                keep_img.save(str(matted_path), "PNG", **_dpi_kw())
                details["matting_keep_path"] = str(matted_path)
            except Exception as e:
                details["matting_keep_error"] = str(e)

        result.output_path = str(out_path)
        result.success = True
        result.details = details
