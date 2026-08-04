"""
抠图推理封装 —— 通过「每模型独立 uv 环境 + 子进程 worker」执行。

主程序（含 PyInstaller 打包）不 import torch/ben2，避免体积膨胀与依赖冲突。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from PIL import Image

import config
from core.matting.model_manager import get_matting_manager
from core.matting.model_registry import get_model_info
from core.runtime.env_manager import get_runtime_manager


class MattingError(RuntimeError):
    """抠图相关错误"""


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
    """兼容旧接口：子进程模式无主进程模型缓存"""
    return


def _run_worker(
    model_id: str,
    input_path: Path,
    output_path: Path,
    *,
    device: str,
    refine: bool,
    weights_dir: str | None,
    weights_file: str | None,
    timeout: int | None = 600,
) -> dict:
    rt = get_runtime_manager()
    py = rt.model_python(model_id)
    if py is None:
        raise MattingError(
            f"模型 {model_id} 的隔离环境未创建。\n"
            "请打开「配置 → 抠图模型配置」点击「创建/修复环境」。"
        )

    # 真实 import 校验（force，避免脏缓存/ANSI 误报）
    st = rt.get_model_env_status(model_id, force=True)
    if not st.ready:
        miss = ", ".join(st.missing_packages) if st.missing_packages else st.detail
        raise MattingError(
            f"模型 {model_id} 环境未就绪，缺少依赖: {miss}\n"
            "请到「配置 → 抠图模型配置」点击「创建/修复环境」安装完整依赖后再试。"
        )

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

    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as e:
        raise MattingError(f"抠图超时（>{timeout}s）") from e
    except Exception as e:
        raise MattingError(f"启动抠图子进程失败: {e}") from e

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    meta_out = {}
    if stdout:
        # 取最后一行 JSON
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
        raise MattingError(f"抠图失败 ({model_id}): {detail}")

    if not output_path.is_file():
        raise MattingError("抠图子进程未生成输出文件")

    return meta_out if isinstance(meta_out, dict) else {}


def remove_background(
    img: Image.Image,
    model_id: str | None = None,
    refine_foreground: bool = False,
    device: str | None = None,
) -> Image.Image:
    """
    对单张图片抠图，返回带 Alpha 的 RGBA 图。
    实现：写临时输入 → 子进程 worker → 读临时输出。
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
