"""
抠图推理封装 —— 通过「每模型独立 uv 环境 + 子进程 worker」执行。

主程序（含 PyInstaller 打包）不 import torch/ben2，避免体积膨胀与依赖冲突。

批量场景默认使用常驻 worker：模型只加载一次，stdin/stdout JSON 逐张推理。
单次失败可回退到一次性子进程，保证兼容性。
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


# 本进程内已通过环境校验的模型（批量处理时避免每张图重复 import torch）
_verified_model_envs: set[str] = set()
_verified_lock = threading.Lock()

# 常驻 worker 会话：key -> _PersistentMattingWorker
_sessions: dict[str, "_PersistentMattingWorker"] = {}
_sessions_lock = threading.Lock()

# 常驻进程加载模型的最长时间（首次启动）
_SERVE_READY_TIMEOUT = 300
# 单张推理默认超时
_INFER_TIMEOUT = 600


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


class _PersistentMattingWorker:
    """
    常驻抠图子进程：启动时加载一次模型，之后用 stdin JSON 逐张推理。
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
        with _verified_lock:
            _verified_model_envs.add(self.model_id)

    def infer(
        self,
        input_path: Path,
        output_path: Path,
        *,
        refine: bool = False,
        timeout: float = _INFER_TIMEOUT,
    ) -> dict:
        with self._lock:
            if not self.alive:
                self.start()

            assert self._proc is not None and self._proc.stdin is not None
            req = {
                "cmd": "infer",
                "input": str(input_path),
                "output": str(output_path),
                "refine": bool(refine),
            }
            try:
                self._proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
            except Exception as e:
                self.close()
                raise MattingError(f"向抠图进程发送请求失败: {e}") from e

            try:
                resp = self._read_json_line(timeout)
            except MattingError:
                # 超时或进程挂掉：关掉，下次重建
                self.close()
                raise

            if not resp.get("ok"):
                err = resp.get("error") or "未知错误"
                _mark_import_failure(self.model_id, str(err))
                # 导入类错误让会话失效
                if any(
                    k in str(err).lower()
                    for k in ("modulenotfounderror", "no module named", "importerror")
                ):
                    self.close()
                raise MattingError(f"抠图失败 ({self.model_id}): {err}")

            if not output_path.is_file():
                raise MattingError("抠图子进程未生成输出文件")

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
            weights_dir=weights_dir,
            weights_file=weights_file,
            timeout=int(t),
        )


def remove_background(
    img: Image.Image,
    model_id: str | None = None,
    refine_foreground: bool = False,
    device: str | None = None,
) -> Image.Image:
    """
    对单张图片抠图，返回带 Alpha 的 RGBA 图。
    实现：写临时输入 → 常驻/子进程 worker → 读临时输出。
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

    tmp_dir = Path(tempfile.mkdtemp(prefix="pixelflow_matting_"))
    in_path = tmp_dir / "input.png"
    out_path = tmp_dir / "output.png"
    try:
        # 统一存 PNG，避免 JPEG 有损
        save_img = img
        if save_img.mode not in ("RGB", "RGBA", "L", "LA"):
            save_img = save_img.convert("RGBA")
        save_img.save(in_path, format="PNG")

        _run_worker(
            mid,
            in_path,
            out_path,
            device=dev,
            refine=bool(refine_foreground),
            weights_dir=str(model_dir) if model_dir else None,
            weights_file=str(weight_file) if weight_file else None,
        )

        result = Image.open(out_path)
        result.load()
        if result.mode != "RGBA":
            result = result.convert("RGBA")
        # 复制到内存，便于删临时目录
        result = result.copy()
        return result
    finally:
        try:
            for p in tmp_dir.glob("*"):
                p.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except Exception:
            pass
