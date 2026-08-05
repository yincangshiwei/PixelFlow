"""
抠图推理封装 —— 通过「每模型独立 uv 环境 + 子进程 worker」执行。

主程序（含 PyInstaller 打包）不 import torch/ben2，避免体积膨胀与依赖冲突。

批量场景默认使用常驻 worker：模型只加载一次，stdin/stdout JSON 逐张推理。
单次失败可回退到一次性子进程，保证兼容性。

性能（阶段1）：
- BEN2 内部固定 1024×1024 推理；业务侧预缩放至 1024×1024 再送 worker
- 默认只回传单通道 mask，主进程放大后贴回原图像素（RGB 不经 AI 重编码）
- 开启边缘精炼时仍走全尺寸 RGBA（精炼会改边缘前景色，需原图分辨率）

性能（阶段2）：
- GPU 真 tensor micro-batch（infer_batch），按显存自适应 batch=1~3
- CPU / refine 强制 batch=1；OOM 自动降半并记住本会话上限
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image

from core.matting.model_manager import get_matting_manager
from core.matting.model_registry import get_model_info
from core.runtime.env_manager import get_runtime_manager


class MattingError(RuntimeError):
    """抠图相关错误"""

    def __init__(self, message: str = "", *, oom: bool = False):
        super().__init__(message)
        self.oom = bool(oom)


# 本进程内已通过环境校验的模型（批量处理时避免每张图重复 import torch）
_verified_model_envs: set[str] = set()
_verified_lock = threading.Lock()

# 常驻 worker 会话：key -> _PersistentMattingWorker
_sessions: dict[str, "_PersistentMattingWorker"] = {}
_sessions_lock = threading.Lock()

# 本进程内各模型会话的 batch 上限（OOM 后下调，跨文件复用）
_batch_caps: dict[str, int] = {}
_batch_caps_lock = threading.Lock()

# 常驻进程加载模型的最长时间（首次启动）
_SERVE_READY_TIMEOUT = 300
# 单张推理默认超时
_INFER_TIMEOUT = 600
# batch 超时按张数线性放宽（上限）
_INFER_BATCH_TIMEOUT_PER_ITEM = 600

# BEN2 推理固定边长；业务侧缩略图不超过此值即可与模型内部等价
_INFER_MAX_SIDE = 1024
# 消费级 GPU 建议上限（注册表 notes 亦写 batch≤3）
_MAX_BATCH = 3


def _workers_dir() -> Path:
    return Path(__file__).resolve().parent / "workers"


def _resolve_worker_script(model_id: str) -> Path:
    meta = get_model_info(model_id)
    if meta is None or not meta.worker_script:
        raise MattingError(f"模型 {model_id} 未配置 worker 脚本")
    script = _workers_dir() / meta.worker_script
    if not script.is_file():
        raise MattingError(f"找不到 worker 脚本: {script}")
    return script


def resolve_device(preference: str | None = None) -> str:
    """返回传给 worker 的 device 参数（auto/cpu/cuda），不在主进程探测 torch"""
    mgr = get_matting_manager()
    pref = preference or mgr.get_device_preference() or "auto"
    if pref in ("auto", "cuda", "cpu"):
        return pref
    return "auto"


def clear_model_cache():
    """清除环境校验标记并关闭全部常驻 worker"""
    with _verified_lock:
        _verified_model_envs.clear()
    shutdown_matting_workers()


def shutdown_matting_workers():
    """关闭所有常驻抠图子进程（批处理结束 / 取消时调用）"""
    with _sessions_lock:
        workers = list(_sessions.values())
        _sessions.clear()
    for w in workers:
        try:
            w.close()
        except Exception:
            pass


def _probe_error_is_transient(detail: str) -> bool:
    """依赖探测超时/输出异常等瞬时失败，不应在批处理中途误判为环境损坏。"""
    d = (detail or "").lower()
    keys = (
        "探测失败",
        "timeout",
        "timed out",
        "依赖探测输出异常",
        "memory",
        "oom",
        "access is denied",
        "拒绝访问",
        "winerror",
    )
    return any(k in d for k in keys)


def _ensure_model_env_ready(model_id: str) -> Path:
    """
    确认模型隔离环境可用，返回 venv python 路径。

    批量抠图时每张图都会走这里：只在本进程首次使用该模型时做完整依赖
    import 校验并缓存结果；后续仅确认 python.exe 仍在，避免反复加载 torch
    导致超时/内存压力，从而误报「环境未就绪」。
    """
    rt = get_runtime_manager()
    py = rt.model_python(model_id)
    if py is None:
        with _verified_lock:
            _verified_model_envs.discard(model_id)
        raise MattingError(
            f"模型 {model_id} 的隔离环境未创建。\n"
            "请打开「配置 → 抠图模型配置」点击「创建/修复环境」。"
        )

    with _verified_lock:
        if model_id in _verified_model_envs:
            return py

    # 优先用进程内/磁盘缓存，避免每张图 force import
    st = rt.get_model_env_status(model_id, force=False)
    if not st.ready:
        st = rt.get_model_env_status(model_id, force=True)

    if st.ready:
        with _verified_lock:
            _verified_model_envs.add(model_id)
        return py

    # 探测瞬时失败，但 env_meta / 路径表明环境曾就绪 → 放行，由 worker 给出真实错误
    if _probe_error_is_transient(st.detail) and rt.env_exists(model_id):
        quick = rt.get_model_env_status(model_id, quick=True)
        if quick.ready or not quick.missing_packages:
            with _verified_lock:
                _verified_model_envs.add(model_id)
            return py

    miss = ", ".join(st.missing_packages) if st.missing_packages else st.detail
    raise MattingError(
        f"模型 {model_id} 环境未就绪，缺少依赖: {miss}\n"
        "请到「配置 → 抠图模型配置」点击「创建/修复环境」安装完整依赖后再试。"
    )


def _creationflags() -> int:
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def _worker_env() -> dict:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONWARNINGS", "ignore")
    env.setdefault("NO_COLOR", "1")
    return env


def _mark_import_failure(model_id: str, detail: str) -> None:
    detail_l = str(detail).lower()
    if any(
        k in detail_l
        for k in (
            "modulenotfounderror",
            "no module named",
            "importerror",
            "dll load failed",
        )
    ):
        with _verified_lock:
            _verified_model_envs.discard(model_id)
        try:
            get_runtime_manager().invalidate_caches(env=model_id)
        except Exception:
            pass


def _session_key(
    model_id: str,
    device: str,
    weights_dir: str | None,
    weights_file: str | None,
) -> str:
    return "|".join(
        [
            model_id,
            device or "auto",
            weights_dir or "",
            weights_file or "",
        ]
    )


def _error_looks_oom(text: str) -> bool:
    d = (text or "").lower()
    keys = (
        "out of memory",
        "cuda out of memory",
        "oom",
        "cublas_status_alloc_failed",
        "cudnn_status_alloc_failed",
        "hip out of memory",
    )
    return any(k in d for k in keys)


def _batch_cap_key(model_id: str, device: str, weights_dir: str | None, weights_file: str | None) -> str:
    return _session_key(model_id, device, weights_dir, weights_file)


def get_batch_cap(
    model_id: str,
    *,
    device: str = "auto",
    weights_dir: str | None = None,
    weights_file: str | None = None,
) -> int | None:
    """返回本会话因 OOM 记下的 batch 上限；无记录则 None。"""
    key = _batch_cap_key(model_id, device, weights_dir, weights_file)
    with _batch_caps_lock:
        return _batch_caps.get(key)


def _set_batch_cap(
    model_id: str,
    cap: int,
    *,
    device: str,
    weights_dir: str | None,
    weights_file: str | None,
) -> None:
    key = _batch_cap_key(model_id, device, weights_dir, weights_file)
    cap = max(1, min(int(cap), _MAX_BATCH))
    with _batch_caps_lock:
        old = _batch_caps.get(key)
        if old is None or cap < old:
            _batch_caps[key] = cap


def recommend_matting_batch_size(
    model_id: str | None = None,
    *,
    device: str | None = None,
    refine: bool = False,
    vram_gb: float | None = None,
    actual_device: str | None = None,
) -> int:
    """
    根据设备/显存/是否 refine 给出建议 micro-batch。
    CPU 与 refine 恒为 1；GPU 按 VRAM 1~3，并受本会话 OOM 上限约束。
    """
    if refine:
        return 1

    mgr = get_matting_manager()
    mid = model_id or mgr.get_default_model_id()
    dev_pref = resolve_device(device)
    weight_file = mgr.resolve_weight_file(mid)
    model_dir = mgr.resolve_model_dir(mid)
    wdir = str(model_dir) if model_dir else None
    wfile = str(weight_file) if weight_file else None

    # 若常驻会话已启动，优先用其实际 device / vram
    session = None
    try:
        with _sessions_lock:
            session = _sessions.get(_session_key(mid, dev_pref, wdir, wfile))
    except Exception:
        session = None

    act = (actual_device or "").lower()
    if not act and session is not None:
        act = str(getattr(session, "_actual_device", "") or "").lower()
    if not act:
        act = dev_pref

    vg = vram_gb
    if vg is None and session is not None:
        try:
            vg = float(getattr(session, "vram_gb", 0.0) or 0.0)
        except (TypeError, ValueError):
            vg = 0.0

    if act == "cpu":
        base = 1
    elif act == "cuda":
        if vg is None or vg <= 0:
            # 未知显存：保守 2（注册表建议 ≤3）
            base = 2
        elif vg < 4.0:
            base = 1
        elif vg < 8.0:
            base = 2
        else:
            base = 3
    elif session is not None:
        # auto 但已有会话：用 worker ready 上报
        if str(getattr(session, "_actual_device", "")).lower() == "cpu":
            base = 1
        else:
            rb = int(getattr(session, "recommend_batch", 1) or 1)
            base = max(1, min(rb, _MAX_BATCH))
    else:
        # auto 且会话未起：保守 1，避免 CPU 误开大 batch
        base = 1

    cap = get_batch_cap(mid, device=dev_pref, weights_dir=wdir, weights_file=wfile)
    if cap is not None:
        base = min(base, cap)
    return max(1, min(base, _MAX_BATCH))


class _PersistentMattingWorker:
    """
    常驻抠图子进程：启动时加载一次模型，之后用 stdin JSON 逐张/批量推理。
    """

    def __init__(
        self,
        model_id: str,
        *,
        device: str,
        weights_dir: str | None,
        weights_file: str | None,
    ):
        self.model_id = model_id
        self.device = device
        self.weights_dir = weights_dir
        self.weights_file = weights_file
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._stderr_tail: list[str] = []
        self._stderr_thread: threading.Thread | None = None
        self._actual_device = device
        self.vram_gb: float = 0.0
        self.recommend_batch: int = 1

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None
    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                text = (line or "").rstrip()
                if not text:
                    continue
                self._stderr_tail.append(text)
                if len(self._stderr_tail) > 40:
                    self._stderr_tail = self._stderr_tail[-40:]
        except Exception:
            pass

    def _readline_with_timeout(self, timeout: float) -> str:
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise MattingError("抠图常驻进程未启动")

        result_q: queue.Queue = queue.Queue(maxsize=1)

        def _reader():
            try:
                result_q.put(("ok", proc.stdout.readline()))
            except Exception as e:
                result_q.put(("err", e))

        threading.Thread(
            target=_reader, name="matting-stdout", daemon=True
        ).start()
        try:
            kind, payload = result_q.get(timeout=timeout)
        except queue.Empty:
            raise MattingError(f"抠图超时（>{int(timeout)}s）") from None

        if kind == "err":
            raise MattingError(f"读取抠图进程输出失败: {payload}") from payload

        if not payload:
            code = proc.poll()
            tail = "\n".join(self._stderr_tail[-8:])
            raise MattingError(
                f"抠图常驻进程已退出 (code={code})"
                + (f"\n{tail}" if tail else "")
            )
        return payload

    def _read_json_line(self, timeout: float) -> dict:
        deadline = time.monotonic() + max(float(timeout), 1.0)
        last_text = ""
        # 最多跳过若干非 JSON 行（库偶发打印到 stdout）
        for _ in range(32):
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise MattingError(f"抠图超时（>{int(timeout)}s）")
            line = self._readline_with_timeout(remain)
            text = line.strip()
            if not text:
                continue
            last_text = text
            if text.startswith("{") and text.endswith("}"):
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    return data
        raise MattingError(f"抠图进程返回非 JSON: {last_text[:300]}")

    def start(self, ready_timeout: float = _SERVE_READY_TIMEOUT) -> None:
        if self.alive:
            return

        py = _ensure_model_env_ready(self.model_id)
        script = _resolve_worker_script(self.model_id)
        cmd = [
            str(py),
            str(script),
            "--serve",
            "--device", self.device,
        ]
        if self.weights_dir:
            cmd.extend(["--weights-dir", self.weights_dir])
        if self.weights_file:
            cmd.extend(["--weights-file", self.weights_file])

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_worker_env(),
                creationflags=_creationflags(),
                bufsize=1,
            )
        except Exception as e:
            raise MattingError(f"启动抠图常驻进程失败: {e}") from e

        self._stderr_tail = []
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, name=f"matting-stderr-{self.model_id}", daemon=True
        )
        self._stderr_thread.start()

        try:
            ready = self._read_json_line(ready_timeout)
        except Exception:
            self.close()
            raise

        if not ready.get("ok"):
            err = ready.get("error") or "模型加载失败"
            self.close()
            _mark_import_failure(self.model_id, str(err))
            raise MattingError(f"抠图失败 ({self.model_id}): {err}")

        self._actual_device = str(ready.get("device") or self.device)
        try:
            self.vram_gb = float(ready.get("vram_gb") or 0.0)
        except (TypeError, ValueError):
            self.vram_gb = 0.0
        try:
            self.recommend_batch = max(
                1, min(int(ready.get("recommend_batch") or 1), _MAX_BATCH)
            )
        except (TypeError, ValueError):
            self.recommend_batch = 1
        if self._actual_device == "cpu":
            self.recommend_batch = 1
        with _verified_lock:
            _verified_model_envs.add(self.model_id)

    def _send_and_recv(self, req: dict, timeout: float) -> dict:
        assert self._proc is not None and self._proc.stdin is not None
        try:
            self._proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except Exception as e:
            self.close()
            raise MattingError(f"向抠图进程发送请求失败: {e}") from e

        try:
            resp = self._read_json_line(timeout)
        except MattingError:
            self.close()
            raise

        if not resp.get("ok"):
            err = resp.get("error") or "未知错误"
            oom = bool(resp.get("oom")) or _error_looks_oom(str(err))
            _mark_import_failure(self.model_id, str(err))
            if any(
                k in str(err).lower()
                for k in ("modulenotfounderror", "no module named", "importerror")
            ):
                self.close()
            raise MattingError(
                f"抠图失败 ({self.model_id}): {err}", oom=oom
            )
        return resp

    def infer(
        self,
        input_path: Path,
        output_path: Path,
        *,
        refine: bool = False,
        return_mask: bool = False,
        timeout: float = _INFER_TIMEOUT,
    ) -> dict:
        with self._lock:
            if not self.alive:
                self.start()

            resp = self._send_and_recv(
                {
                    "cmd": "infer",
                    "input": str(input_path),
                    "output": str(output_path),
                    "refine": bool(refine),
                    "return_mask": bool(return_mask),
                },
                timeout,
            )

            if not output_path.is_file():
                raise MattingError("抠图子进程未生成输出文件")

            if "device" not in resp and self._actual_device:
                resp = dict(resp)
                resp["device"] = self._actual_device
            return resp

    def infer_batch(
        self,
        items: list[tuple[Path, Path]],
        *,
        return_mask: bool = True,
        timeout: float | None = None,
    ) -> dict:
        """
        真 batch：一次 forward 处理多张（仅 mask 路径）。
        items: [(input_path, output_path), ...]
        """
        if not items:
            raise MattingError("infer_batch items 为空")
        n = len(items)
        t = float(timeout if timeout is not None else max(_INFER_TIMEOUT, n * 180))

        with self._lock:
            if not self.alive:
                self.start()

            payload_items = [
                {"input": str(inp), "output": str(outp)} for inp, outp in items
            ]
            resp = self._send_and_recv(
                {
                    "cmd": "infer_batch",
                    "return_mask": bool(return_mask),
                    "refine": False,
                    "items": payload_items,
                },
                t,
            )

            for _, outp in items:
                if not Path(outp).is_file():
                    raise MattingError(f"抠图子进程未生成输出文件: {outp}")

            if "device" not in resp and self._actual_device:
                resp = dict(resp)
                resp["device"] = self._actual_device
            return resp

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None and proc.stdin:
                try:
                    proc.stdin.write(
                        json.dumps({"cmd": "shutdown"}, ensure_ascii=False) + "\n"
                    )
                    proc.stdin.flush()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
        finally:
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream:
                        stream.close()
                except Exception:
                    pass


def _get_or_create_session(
    model_id: str,
    *,
    device: str,
    weights_dir: str | None,
    weights_file: str | None,
) -> _PersistentMattingWorker:
    key = _session_key(model_id, device, weights_dir, weights_file)
    with _sessions_lock:
        w = _sessions.get(key)
        if w is not None and w.alive:
            return w
        if w is not None:
            try:
                w.close()
            except Exception:
                pass
        w = _PersistentMattingWorker(
            model_id,
            device=device,
            weights_dir=weights_dir,
            weights_file=weights_file,
        )
        _sessions[key] = w
        return w


def _run_worker_oneshot(
    model_id: str,
    input_path: Path,
    output_path: Path,
    *,
    device: str,
    refine: bool,
    return_mask: bool = False,
    weights_dir: str | None,
    weights_file: str | None,
    timeout: int | None = _INFER_TIMEOUT,
) -> dict:
    """兼容回退：每张图独立子进程（不复用模型）。"""
    py = _ensure_model_env_ready(model_id)
    script = _resolve_worker_script(model_id)
    cmd = [
        str(py),
        str(script),
        "--input", str(input_path),
        "--output", str(output_path),
        "--device", device,
    ]
    if refine:
        cmd.append("--refine")
    if return_mask:
        cmd.append("--return-mask")
    if weights_dir:
        cmd.extend(["--weights-dir", weights_dir])
    if weights_file:
        cmd.extend(["--weights-file", weights_file])

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_worker_env(),
            creationflags=_creationflags(),
        )
    except subprocess.TimeoutExpired as e:
        raise MattingError(f"抠图超时（>{timeout}s）") from e
    except Exception as e:
        raise MattingError(f"启动抠图子进程失败: {e}") from e

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    meta_out: dict = {}
    if stdout:
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    meta_out = json.loads(line)
                    break
                except json.JSONDecodeError:
                    pass

    if proc.returncode != 0:
        err = meta_out.get("error") if isinstance(meta_out, dict) else None
        detail = err or stderr or stdout or f"exit={proc.returncode}"
        _mark_import_failure(model_id, str(detail))
        raise MattingError(f"抠图失败 ({model_id}): {detail}")

    if not output_path.is_file():
        raise MattingError("抠图子进程未生成输出文件")

    with _verified_lock:
        _verified_model_envs.add(model_id)

    return meta_out if isinstance(meta_out, dict) else {}


def _run_worker(
    model_id: str,
    input_path: Path,
    output_path: Path,
    *,
    device: str,
    refine: bool,
    return_mask: bool = False,
    weights_dir: str | None,
    weights_file: str | None,
    timeout: int | None = _INFER_TIMEOUT,
) -> dict:
    """优先常驻 worker；启动失败时回退一次性进程。"""
    t = float(timeout or _INFER_TIMEOUT)
    try:
        session = _get_or_create_session(
            model_id,
            device=device,
            weights_dir=weights_dir,
            weights_file=weights_file,
        )
        return session.infer(
            input_path,
            output_path,
            refine=refine,
            return_mask=return_mask,
            timeout=t,
        )
    except MattingError:
        raise
    except Exception:
        # 非预期异常：关掉坏会话，回退 oneshot
        key = _session_key(model_id, device, weights_dir, weights_file)
        with _sessions_lock:
            w = _sessions.pop(key, None)
        if w is not None:
            try:
                w.close()
            except Exception:
                pass
        return _run_worker_oneshot(
            model_id,
            input_path,
            output_path,
            device=device,
            refine=refine,
            return_mask=return_mask,
            weights_dir=weights_dir,
            weights_file=weights_file,
            timeout=int(t),
        )


def _prepare_rgb_for_matting(img: Image.Image) -> Image.Image:
    """与 worker 一致：RGBA 贴白底转 RGB，供缩略图推理。"""
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        return bg
    if img.mode == "LA":
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def _downscale_for_inference(
    img: Image.Image,
    max_side: int = _INFER_MAX_SIDE,
) -> tuple[Image.Image, tuple[int, int], bool]:
    """
    生成与 BEN2 内部等价的推理输入，降低临时文件 I/O。

    BEN2 的 rgb_loader 会把任意尺寸 LANCZOS 到 1024×1024（可变形）。
    此处对「超过 1024 边」的图预先做到同样的 1024×1024，使网络输入与旧路径一致，
    同时避免把 4K/8K 全图写入临时 PNG。

    返回 (推理用图, 原尺寸(w,h), 是否做了预缩放)。
    """
    w, h = img.size
    if w <= 0 or h <= 0:
        raise MattingError(f"无效图片尺寸: {img.size}")
    # 已是模型输入尺寸，或两边都不超过 1024（小图 I/O 可忽略）
    if (w, h) == (max_side, max_side):
        return img, (w, h), False
    if w <= max_side and h <= max_side:
        return img, (w, h), False
    # 与 BEN2 rgb_loader_refiner 完全一致：直接 LANCZOS → 1024×1024
    small = img.resize((max_side, max_side), resample=Image.Resampling.LANCZOS)
    return small, (w, h), True


def _compose_with_mask(
    original: Image.Image,
    mask: Image.Image,
    target_size: tuple[int, int] | None = None,
) -> Image.Image:
    """
    将 mask 贴到原图，得到 RGBA。原图 RGB 像素保持不变。
    """
    ow, oh = target_size if target_size else original.size
    if mask.mode != "L":
        if mask.mode == "RGBA":
            mask = mask.split()[-1]
        elif mask.mode == "LA":
            mask = mask.split()[-1]
        else:
            mask = mask.convert("L")

    if mask.size != (ow, oh):
        # 双线性放大 Alpha，与 BEN2 postprocess 的 bilinear 思路一致
        mask = mask.resize((ow, oh), resample=Image.Resampling.BILINEAR)

    base = original
    if base.size != (ow, oh):
        # 正常路径不会发生；兜底保持尺寸一致
        base = base.resize((ow, oh), resample=Image.Resampling.LANCZOS)

    if base.mode == "RGBA":
        # 保留原图已有 RGB，仅替换 Alpha
        r, g, b, _ = base.split()
        out = Image.merge("RGBA", (r, g, b, mask))
    elif base.mode == "RGB":
        out = base.copy()
        out.putalpha(mask)
    else:
        out = base.convert("RGBA")
        r, g, b, _ = out.split()
        out = Image.merge("RGBA", (r, g, b, mask))
    return out


def remove_background(
    img: Image.Image,
    model_id: str | None = None,
    refine_foreground: bool = False,
    device: str | None = None,
) -> Image.Image:
    """
    对单张图片抠图，返回带 Alpha 的 RGBA 图。

    默认（非 refine）：
      预缩放至 1024×1024（与 BEN2 内部一致）→ worker 只回 mask → 主进程贴回原图像素
    refine：
      全尺寸输入 → worker 回 RGBA（边缘精炼会改前景色，需原图分辨率）
    """
    mgr = get_matting_manager()
    mid = model_id or mgr.get_default_model_id()
    meta = get_model_info(mid)
    if meta is None:
        raise MattingError(f"未知抠图模型: {mid}")

    if not mgr.is_ready(mid):
        raise MattingError(
            f"模型 {meta.name} 权重尚未就绪。请到「配置 → 抠图模型配置」下载或指定本地路径。\n"
            f"模型地址: {meta.page_url}"
        )

    dev = resolve_device(device)
    weight_file = mgr.resolve_weight_file(mid)
    model_dir = mgr.resolve_model_dir(mid)

    refine = bool(refine_foreground)
    # 非 refine 走 mask-only + 预缩放；refine 保持全尺寸 RGBA
    use_mask_path = not refine

    tmp_dir = Path(tempfile.mkdtemp(prefix="pixelflow_matting_"))
    in_path = tmp_dir / "input.png"
    out_path = tmp_dir / ("mask.png" if use_mask_path else "output.png")
    try:
        orig_size = img.size
        if use_mask_path:
            rgb = _prepare_rgb_for_matting(img)
            send_img, orig_size, _scaled = _downscale_for_inference(rgb)
            # 预缩放后 PNG 体积远小于 4K 全图
            send_img.save(in_path, format="PNG", optimize=True)
        else:
            save_img = img
            if save_img.mode not in ("RGB", "RGBA", "L", "LA"):
                save_img = save_img.convert("RGBA")
            save_img.save(in_path, format="PNG")

        meta_out = _run_worker(
            mid,
            in_path,
            out_path,
            device=dev,
            refine=refine,
            return_mask=use_mask_path,
            weights_dir=str(model_dir) if model_dir else None,
            weights_file=str(weight_file) if weight_file else None,
        )

        result = Image.open(out_path)
        result.load()
        result = result.copy()

        if use_mask_path:
            # worker 可能因旧脚本忽略 return_mask 而仍返回 RGBA：兼容提取 alpha
            kind = str(meta_out.get("output_kind") or "").lower()
            if result.mode == "L":
                mask = result
            elif result.mode == "RGBA":
                mask = result.split()[-1]
            elif result.mode == "LA":
                mask = result.split()[-1]
            elif kind == "mask":
                mask = result.convert("L")
            else:
                mask = result.convert("L")

            return _compose_with_mask(img, mask, target_size=orig_size)

        if result.mode != "RGBA":
            result = result.convert("RGBA")
        return result
    finally:
        try:
            for p in tmp_dir.glob("*"):
                p.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except Exception:
            pass


def _load_mask_file(path: Path) -> Image.Image:
    result = Image.open(path)
    result.load()
    result = result.copy()
    if result.mode == "L":
        return result
    if result.mode == "RGBA":
        return result.split()[-1]
    if result.mode == "LA":
        return result.split()[-1]
    return result.convert("L")


def _cleanup_tmp_dir(tmp_dir: Path) -> None:
    try:
        for p in tmp_dir.glob("*"):
            p.unlink(missing_ok=True)
        tmp_dir.rmdir()
    except Exception:
        pass


def _run_mask_batch_once(
    model_id: str,
    pairs: list[tuple[Path, Path]],
    *,
    device: str,
    weights_dir: str | None,
    weights_file: str | None,
) -> dict:
    """向常驻 worker 发 infer_batch；失败时按张回退。"""
    t = max(_INFER_TIMEOUT, len(pairs) * 180)
    try:
        session = _get_or_create_session(
            model_id,
            device=device,
            weights_dir=weights_dir,
            weights_file=weights_file,
        )
        return session.infer_batch(pairs, return_mask=True, timeout=t)
    except MattingError:
        raise
    except Exception as e:
        # 非预期：整批按张 oneshot
        raise MattingError(str(e)) from e


def remove_background_batch(
    images: list[Image.Image],
    model_id: str | None = None,
    refine_foreground: bool = False,
    device: str | None = None,
    batch_size: int | None = None,
) -> list[Image.Image]:
    """
    批量抠图，返回与输入等长的 RGBA 列表。

    - refine=True 或 batch_size=1：逐张 remove_background
    - 否则：预缩放 + 真 tensor micro-batch；OOM 时降半重试并记住上限
    单张失败不会静默吞掉：整批按当前策略抛错或由调用方拆分处理。
    """
    if not images:
        return []

    if refine_foreground or len(images) == 1:
        return [
            remove_background(
                im,
                model_id=model_id,
                refine_foreground=refine_foreground,
                device=device,
            )
            for im in images
        ]

    mgr = get_matting_manager()
    mid = model_id or mgr.get_default_model_id()
    meta = get_model_info(mid)
    if meta is None:
        raise MattingError(f"未知抠图模型: {mid}")
    if not mgr.is_ready(mid):
        raise MattingError(
            f"模型 {meta.name} 权重尚未就绪。请到「配置 → 抠图模型配置」下载或指定本地路径。\n"
            f"模型地址: {meta.page_url}"
        )

    dev = resolve_device(device)
    weight_file = mgr.resolve_weight_file(mid)
    model_dir = mgr.resolve_model_dir(mid)
    wdir = str(model_dir) if model_dir else None
    wfile = str(weight_file) if weight_file else None

    # 预热会话以拿到真实 device / vram
    try:
        session = _get_or_create_session(
            mid, device=dev, weights_dir=wdir, weights_file=wfile
        )
        if not session.alive:
            session.start()
    except MattingError:
        # 会话起不来则逐张（oneshot 回退在 remove_background 内）
        return [
            remove_background(im, model_id=mid, refine_foreground=False, device=device)
            for im in images
        ]

    if batch_size is None:
        bs = recommend_matting_batch_size(
            mid,
            device=dev,
            refine=False,
            vram_gb=session.vram_gb,
            actual_device=session._actual_device,
        )
    else:
        bs = max(1, min(int(batch_size), _MAX_BATCH))
        # 显式 batch 仍受本会话 OOM 上限约束
        cap = get_batch_cap(mid, device=dev, weights_dir=wdir, weights_file=wfile)
        if cap is not None:
            bs = min(bs, cap)

    if str(session._actual_device).lower() == "cpu":
        bs = 1

    results: list[Image.Image | None] = [None] * len(images)
    idx = 0
    n = len(images)

    while idx < n:
        chunk_n = min(bs, n - idx)
        chunk_imgs = images[idx : idx + chunk_n]
        chunk_indices = list(range(idx, idx + chunk_n))

        if chunk_n == 1:
            results[idx] = remove_background(
                chunk_imgs[0],
                model_id=mid,
                refine_foreground=False,
                device=device,
            )
            idx += 1
            continue

        # 准备临时文件
        tmp_dir = Path(tempfile.mkdtemp(prefix="pixelflow_matting_b_"))
        pairs: list[tuple[Path, Path]] = []
        orig_sizes: list[tuple[int, int]] = []
        try:
            for j, im in enumerate(chunk_imgs):
                rgb = _prepare_rgb_for_matting(im)
                send_img, osize, _ = _downscale_for_inference(rgb)
                orig_sizes.append(osize)
                in_p = tmp_dir / f"in_{j:03d}.png"
                out_p = tmp_dir / f"mask_{j:03d}.png"
                send_img.save(in_p, format="PNG", optimize=True)
                pairs.append((in_p, out_p))

            cur_bs = chunk_n
            offset = 0
            while offset < chunk_n:
                sub = pairs[offset : offset + cur_bs]
                sub_imgs = chunk_imgs[offset : offset + cur_bs]
                sub_sizes = orig_sizes[offset : offset + cur_bs]
                sub_idx = chunk_indices[offset : offset + cur_bs]
                try:
                    _run_mask_batch_once(
                        mid,
                        sub,
                        device=dev,
                        weights_dir=wdir,
                        weights_file=wfile,
                    )
                    for k, (im0, sz, (_, out_p)) in enumerate(
                        zip(sub_imgs, sub_sizes, sub)
                    ):
                        mask = _load_mask_file(out_p)
                        results[sub_idx[k]] = _compose_with_mask(
                            im0, mask, target_size=sz
                        )
                    offset += cur_bs
                except MattingError as e:
                    if e.oom and cur_bs > 1:
                        new_cap = max(1, cur_bs // 2)
                        _set_batch_cap(
                            mid,
                            new_cap,
                            device=dev,
                            weights_dir=wdir,
                            weights_file=wfile,
                        )
                        cur_bs = new_cap
                        bs = min(bs, new_cap)
                        # OOM 后会话可能已挂；关掉以便重建
                        try:
                            key = _session_key(mid, dev, wdir, wfile)
                            with _sessions_lock:
                                w = _sessions.pop(key, None)
                            if w is not None:
                                w.close()
                        except Exception:
                            pass
                        continue
                    # 非 OOM 或已是 1：逐张兜底该 sub
                    for k, im0 in enumerate(sub_imgs):
                        results[sub_idx[k]] = remove_background(
                            im0,
                            model_id=mid,
                            refine_foreground=False,
                            device=device,
                        )
                    offset += len(sub_imgs)
        finally:
            _cleanup_tmp_dir(tmp_dir)

        idx += chunk_n

    out_list: list[Image.Image] = []
    for i, r in enumerate(results):
        if r is None:
            # 理论不应发生
            out_list.append(
                remove_background(
                    images[i],
                    model_id=mid,
                    refine_foreground=False,
                    device=device,
                )
            )
        else:
            out_list.append(r)
    return out_list


def ensure_matting_session_ready(
    model_id: str | None = None,
    device: str | None = None,
) -> dict:
    """
    预热常驻 worker，返回 device / vram_gb / recommend_batch。
    供 ProcessWorker 在批处理开始时决定 micro-batch 大小。
    """
    mgr = get_matting_manager()
    mid = model_id or mgr.get_default_model_id()
    dev = resolve_device(device)
    weight_file = mgr.resolve_weight_file(mid)
    model_dir = mgr.resolve_model_dir(mid)
    wdir = str(model_dir) if model_dir else None
    wfile = str(weight_file) if weight_file else None
    session = _get_or_create_session(
        mid, device=dev, weights_dir=wdir, weights_file=wfile
    )
    if not session.alive:
        session.start()
    return {
        "model_id": mid,
        "device": session._actual_device,
        "vram_gb": session.vram_gb,
        "recommend_batch": recommend_matting_batch_size(
            mid,
            device=dev,
            refine=False,
            vram_gb=session.vram_gb,
            actual_device=session._actual_device,
        ),
    }
