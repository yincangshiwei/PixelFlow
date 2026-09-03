"""高清放大本地设置 —— ``runtime/upscale_settings.json``。

只存「与机器/安装相关」的选择，不存功能参数（功能参数走预设系统
``presets/upscale/*.json``）。与 AI 抠图的 ``runtime/runtime_settings.json`` 同级同风格。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Optional

import config

_SETTINGS_PATH: Path = Path(config.UPSCALE_SETTINGS_PATH)
_lock = threading.RLock()
_cache: Optional[dict[str, Any]] = None

_DEFAULTS: dict[str, Any] = {
    # 当前选用的放大引擎 id
    "engine": "dlss5",
    # DLSS5 运行时目录；空串 = 用默认 <安装目录>/runtime/upscale/dlss5
    "dlss5_dir": "",
    # 跳过显卡架构校验（无法识别 GPU 架构时的应急开关，默认关闭）
    "allow_unverified_gpu": False,
    # 开发调试：不加载 DLSS，用 Pillow LANCZOS 占位跑通「UI→Service→Worker→输出」全链路。
    # 开启时批处理日志会明确标注 backend=placeholder，绝不静默冒充 DLSS 结果。
    "placeholder_backend": False,
}


_BOOL_KEYS = ("allow_unverified_gpu", "placeholder_backend")


def _path() -> Path:
    return _SETTINGS_PATH


def load(*, force: bool = False) -> dict[str, Any]:
    """读取设置（带进程内缓存）；文件缺失/损坏时回默认值。"""
    global _cache
    with _lock:
        if _cache is not None and not force:
            return dict(_cache)
        data = dict(_DEFAULTS)
        p = _path()
        try:
            if p.is_file():
                raw = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for key in _DEFAULTS:
                        if key in raw:
                            data[key] = raw[key]
        except (OSError, ValueError):
            pass
        data["engine"] = str(data.get("engine") or _DEFAULTS["engine"])
        data["dlss5_dir"] = str(data.get("dlss5_dir") or "")
        for key in _BOOL_KEYS:
            data[key] = bool(data.get(key))
        _cache = data
        return dict(data)


def save(patch: dict[str, Any]) -> dict[str, Any]:
    """合并写入设置，返回写入后的完整快照。"""
    with _lock:
        data = load()
        for key, value in dict(patch or {}).items():
            if key not in _DEFAULTS:
                continue
            if key in _BOOL_KEYS:
                data[key] = bool(value)
            else:
                data[key] = str(value or "").strip()
        p = _path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            # 目录不可写时仍保留内存态，避免功能整体不可用
            pass
        global _cache
        _cache = dict(data)
        return dict(data)


def reset_cache() -> None:
    global _cache
    with _lock:
        _cache = None


# ── 便捷读写 ──

def get_engine_id() -> str:
    return str(load().get("engine") or _DEFAULTS["engine"])


def set_engine_id(engine_id: str) -> None:
    save({"engine": engine_id})


def get_dlss5_dir() -> str:
    return str(load().get("dlss5_dir") or "")


def set_dlss5_dir(path: str) -> None:
    save({"dlss5_dir": path})


def get_allow_unverified_gpu() -> bool:
    return bool(load().get("allow_unverified_gpu"))


def set_allow_unverified_gpu(value: bool) -> None:
    save({"allow_unverified_gpu": bool(value)})


def get_placeholder_backend() -> bool:
    return bool(load().get("placeholder_backend"))


def set_placeholder_backend(value: bool) -> None:
    save({"placeholder_backend": bool(value)})


__all__ = [
    "load",
    "save",
    "reset_cache",
    "get_engine_id",
    "set_engine_id",
    "get_dlss5_dir",
    "set_dlss5_dir",
    "get_allow_unverified_gpu",
    "set_allow_unverified_gpu",
    "get_placeholder_backend",
    "set_placeholder_backend",
]
