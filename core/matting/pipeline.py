"""
AI 抠图三阶段流水线（阶段3）

  [读图预取] → [模型推理] → [后处理+保存]
       ↑              ↑              ↑
    独立线程        主处理线程      独立线程

- 推理仍串行（常驻 worker / GPU 单会话），与读图、后处理重叠
- 队列深度有限，控制内存峰值（默认预取 2 批、后处理缓冲 2 批）
- 取消时尽快排空并退出；结果按文件顺序 image_done
"""
from __future__ import annotations

import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from core.image_io import load_image


@dataclass
class _LoadedItem:
    list_i: int
    fpath: str
    src: Path
    order: int
    img: Image.Image | None = None
    options: dict = field(default_factory=dict)
    error: BaseException | None = None


@dataclass
class _LoadBatch:
    batch_id: int
    start_i: int  # 含
    end_i: int  # 不含
    items: list[_LoadedItem] = field(default_factory=list)


@dataclass
class _PostBatch:
    batch_id: int
    start_i: int
    end_i: int
    # list_i -> (img, details, fpath, src, order, needs_post) | Exception
    results: dict[int, Any] = field(default_factory=dict)
    open_errors: dict[int, BaseException] = field(default_factory=dict)


def run_matting_pipeline(
    *,
    file_list: list[str],
    file_index_map: dict,
    options: dict,
    processor: Any,
    batch_size: int,
    out_dir: Path,
    fmt: str,
    ext_map: dict,
    save_fn: Callable,
    cancelled: Callable[[], bool],
    progress_fn: Callable[[int, int, str], None],
    debug_fn: Callable[[str], None],
    image_done_fn: Callable[[Any], None],
    result_factory: Callable[[str], Any],
    set_current_path_fn: Callable[[str | None], None] | None = None,
    matting_device: str = "",
    prefetch_batches: int = 2,
    post_queue_depth: int = 2,
) -> list:
    """
    执行抠图流水线，返回按文件顺序的 ProcessResult 列表。

    save_fn(img, details, fpath, src, order, out_dir, fmt, ext_map, result)
    result_factory(fpath) -> ProcessResult
    """
    total = len(file_list)
    if total == 0:
        return []

    batch_size = max(1, int(batch_size))
    prefetch_batches = max(1, min(int(prefetch_batches), 3))
    post_queue_depth = max(1, min(int(post_queue_depth), 3))

    load_q: queue.Queue = queue.Queue(maxsize=prefetch_batches)
    post_q: queue.Queue = queue.Queue(maxsize=post_queue_depth)

    completed: dict[int, Any] = {}
    completed_cv = threading.Condition()
    emitted = [False] * total  # 主线程 emit 标记
    stop_flag = threading.Event()

    def _should_stop() -> bool:
        return bool(cancelled() or stop_flag.is_set())

    def _put_result(list_i: int, res: Any) -> None:
        with completed_cv:
            if list_i not in completed:
                completed[list_i] = res
            completed_cv.notify_all()

    def _loader() -> None:
        batch_id = 0
        i = 0
        try:
            while i < total and not _should_stop():
                end = min(i + batch_size, total)
                batch = _LoadBatch(batch_id=batch_id, start_i=i, end_i=end)
                for list_i in range(i, end):
                    if _should_stop():
                        break
                    fpath = file_list[list_i]
                    src = Path(fpath)
                    order = int(file_index_map.get(fpath, list_i + 1) or (list_i + 1))
                    try:
                        progress_fn(list_i + 1, total, src.name)
                    except Exception:
                        pass
                    item = _LoadedItem(
                        list_i=list_i, fpath=fpath, src=src, order=order
                    )
                    try:
                        img = load_image(fpath)
                        opts = dict(options)
                        opts["_image_index"] = order - 1
                        opts["_current_image_path"] = fpath
                        item.img = img
                        item.options = opts
                    except BaseException as e:
                        item.error = e
                    batch.items.append(item)

                if not batch.items:
                    break

                while not _should_stop():
                    try:
                        load_q.put(batch, timeout=0.25)
                        break
                    except queue.Full:
                        continue
                else:
                    break

                batch_id += 1
                i = end
        except BaseException:
            debug_fn("AI 抠图流水线·读图线程异常:\n" + traceback.format_exc())
            stop_flag.set()
        finally:
            try:
                load_q.put(None)
            except Exception:
                pass

    def _poster() -> None:
        try:
            while True:
                try:
                    item = post_q.get(timeout=0.35)
                except queue.Empty:
                    if stop_flag.is_set() and post_q.empty():
                        break
                    continue

                if item is None:
                    post_q.task_done()
                    break

                batch: _PostBatch = item
                t0 = time.monotonic()
                for list_i in range(batch.start_i, batch.end_i):
                    fpath = file_list[list_i]
                    if set_current_path_fn is not None:
                        try:
                            set_current_path_fn(fpath)
                        except Exception:
                            pass
                    res = result_factory(fpath)

                    if list_i in batch.open_errors:
                        res.success = False
                        res.error = str(batch.open_errors[list_i])
                        debug_fn(
                            f"处理失败: {fpath}\n{batch.open_errors[list_i]!r}"
                        )
                    elif list_i not in batch.results:
                        res.success = False
                        res.error = "已取消" if cancelled() else "未处理"
                    else:
                        payload = batch.results[list_i]
                        if isinstance(payload, BaseException):
                            res.success = False
                            res.error = str(payload)
                            debug_fn(f"处理失败: {fpath}\n{payload!r}")
                        else:
                            img, details, fp, src, order, needs_post = payload
                            try:
                                if needs_post and hasattr(processor, "_post_matting"):
                                    base_details = dict(details)
                                    img, details = processor._post_matting(
                                        img, dict(options), base_details
                                    )
                                save_fn(
                                    img,
                                    details,
                                    fp,
                                    src,
                                    order,
                                    out_dir,
                                    fmt,
                                    ext_map,
                                    res,
                                )
                            except BaseException as e:
                                res.success = False
                                res.error = str(e)
                                debug_fn(
                                    f"保存失败: {fpath}\n" + traceback.format_exc()
                                )

                    _put_result(list_i, res)

                dt = time.monotonic() - t0
                debug_fn(
                    f"AI 抠图流水线: batch#{batch.batch_id} 后处理+保存完成  "
                    f"{batch.end_i - batch.start_i} 张  耗时 {dt:.2f}s"
                )
                post_q.task_done()
        except BaseException:
            debug_fn("AI 抠图流水线·后处理线程异常:\n" + traceback.format_exc())
            stop_flag.set()

    next_emit = 0

    def _emit_ready() -> None:
        """按序把已完成结果发给 UI（在主处理线程调用）。"""
        nonlocal next_emit
        while next_emit < total:
            if emitted[next_emit]:
                next_emit += 1
                continue
            with completed_cv:
                res = completed.get(next_emit)
            if res is None:
                break
            emitted[next_emit] = True
            try:
                image_done_fn(res)
            except Exception:
                pass
            next_emit += 1

    debug_fn(
        f"AI 抠图流水线: 启用  预取队列={prefetch_batches}  "
        f"后处理队列={post_queue_depth}  micro-batch={batch_size}  "
        f"设备={(matting_device or 'auto').upper()}"
    )

    loader_t = threading.Thread(target=_loader, name="matting-load", daemon=True)
    poster_t = threading.Thread(target=_poster, name="matting-post", daemon=True)
    loader_t.start()
    poster_t.start()

    try:
        while True:
            _emit_ready()
            if _should_stop() and not loader_t.is_alive() and load_q.empty():
                break
            try:
                batch = load_q.get(timeout=0.3)
            except queue.Empty:
                if not loader_t.is_alive() and load_q.empty():
                    break
                continue

            if batch is None:
                break

            assert isinstance(batch, _LoadBatch)
            ok_items = [it for it in batch.items if it.error is None and it.img is not None]
            open_errors = {
                it.list_i: it.error
                for it in batch.items
                if it.error is not None
            }
            names = [it.src.name for it in ok_items[:4]]
            preview = ", ".join(names)
            if len(ok_items) > 4:
                preview += f" 等{len(ok_items)}张"
            debug_fn(
                f"AI 抠图流水线: batch#{batch.batch_id} 推理  "
                f"文件 {batch.start_i + 1}-{batch.end_i}/{total}  "
                f"size={len(ok_items)}  [{preview}]"
            )

            post = _PostBatch(
                batch_id=batch.batch_id,
                start_i=batch.start_i,
                end_i=batch.end_i,
                open_errors=open_errors,
            )

            t0 = time.monotonic()
            if ok_items and set_current_path_fn is not None:
                try:
                    set_current_path_fn(ok_items[0].fpath)
                except Exception:
                    pass
            if ok_items and not _should_stop():
                imgs = [it.img for it in ok_items]
                opts_list = [it.options for it in ok_items]
                try:
                    if hasattr(processor, "matting_many"):
                        pairs = processor.matting_many(imgs, opts_list)
                        if len(pairs) != len(ok_items):
                            raise RuntimeError(
                                f"matting_many 数量不匹配: "
                                f"{len(pairs)} vs {len(ok_items)}"
                            )
                        for it, (mim, mdet) in zip(ok_items, pairs):
                            det = dict(mdet)
                            if "original_size" not in det and it.img is not None:
                                det["original_size"] = it.img.size
                            post.results[it.list_i] = (
                                mim,
                                det,
                                it.fpath,
                                it.src,
                                it.order,
                                True,  # needs_post
                            )
                    else:
                        full = processor.process_many(imgs, opts_list)
                        for it, (im, det) in zip(ok_items, full):
                            post.results[it.list_i] = (
                                im,
                                det,
                                it.fpath,
                                it.src,
                                it.order,
                                False,
                            )
                except BaseException as e:
                    debug_fn(
                        f"AI 抠图流水线: batch#{batch.batch_id} 推理失败，逐张重试: {e}\n"
                        + traceback.format_exc()
                    )
                    for it in ok_items:
                        if _should_stop():
                            break
                        try:
                            img, details = processor.process(it.img, it.options)
                            post.results[it.list_i] = (
                                img,
                                details,
                                it.fpath,
                                it.src,
                                it.order,
                                False,  # process 已含后处理
                            )
                        except BaseException as e2:
                            post.results[it.list_i] = e2

            dt = time.monotonic() - t0
            debug_fn(
                f"AI 抠图流水线: batch#{batch.batch_id} 推理完成  "
                f"{len(ok_items)} 张  耗时 {dt:.2f}s"
            )

            # 交给后处理；取消时主线程直接写失败
            if _should_stop():
                for list_i in range(batch.start_i, batch.end_i):
                    res = result_factory(file_list[list_i])
                    res.success = False
                    res.error = "已取消"
                    _put_result(list_i, res)
            else:
                while not _should_stop():
                    try:
                        post_q.put(post, timeout=0.25)
                        break
                    except queue.Full:
                        _emit_ready()
                        continue
                else:
                    for list_i in range(batch.start_i, batch.end_i):
                        res = result_factory(file_list[list_i])
                        res.success = False
                        res.error = "已取消"
                        _put_result(list_i, res)

            _emit_ready()
    except BaseException:
        debug_fn("AI 抠图流水线·推理循环异常:\n" + traceback.format_exc())
        stop_flag.set()
    finally:
        stop_flag.set()
        try:
            post_q.put(None)
        except Exception:
            pass
        loader_t.join(timeout=60)
        poster_t.join(timeout=180)

        # 未完成的标取消/失败
        for list_i in range(total):
            with completed_cv:
                if list_i not in completed:
                    res = result_factory(file_list[list_i])
                    res.success = False
                    res.error = "已取消" if cancelled() else "流水线未完成"
                    completed[list_i] = res
        _emit_ready()

    out = []
    for i in range(total):
        with completed_cv:
            res = completed.get(i)
        if res is None:
            res = result_factory(file_list[i])
            res.success = False
            res.error = "未知错误"
        out.append(res)
    return out
