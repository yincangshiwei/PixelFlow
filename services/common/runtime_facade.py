"""RuntimeFacade —— 开发环境 / 模型隔离环境运行时门面（P5）。

对 ``core.runtime.env_manager.RuntimeManager`` 的薄封装：
- 配置中心「开发环境」「抠图模型」Route 经此读取 / 操作运行时，
  不直接触碰 manager 私有成员；
- 纯委托，不含 UI、不 import PySide6（依赖方向 Route → Service → core）。

行为语义与重构前 ``ui/settings_panel.py`` 直接调用 manager 完全一致。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from core.runtime.env_manager import (
    DEFAULT_GITHUB_PROXY,
    DEFAULT_PIP_INDEX_URL,
    GITHUB_PROXY_PRESETS,
    GIT_DOWNLOAD_URL,
    ModelEnvStatus,
    NVIDIA_DRIVER_URL,
    PIP_INDEX_PRESETS,
    UvInfo,
    GitInfo,
    VC_REDIST_HELP_URL,
    VC_REDIST_X64_URL,
    build_ai_setup_prompt,
    get_runtime_manager,
    match_local_gpu,
    packages_need_git,
    plan_torch_install,
    rewrite_github_url,
)


def evaluate_dev_readiness(uv_found: bool, base_available: bool) -> tuple[bool, str]:
    """开发环境是否可配置抠图模型（纯逻辑，可单测）。

    uv 可按版本号托管下载 CPython 3.12，故「仅有 uv」也视为可配置模型；
    本机 3.10–3.12 为加分项（离线更快），不再与 uv 同时强依赖。
    """
    if not uv_found:
        if not base_available:
            return False, "缺少: 包管理工具 uv，以及可用 Python 3.10–3.12（64 位）"
        return False, "缺少: 包管理工具 uv（创建模型环境必需）"
    if not base_available:
        return True, "开发环境已就绪（uv 可用；将按目标版本托管下载 Python 3.12）"
    return True, "开发环境已就绪"


class RuntimeFacade:
    """开发环境 / 模型环境运行时门面（薄委托）。"""

    def __init__(self, manager=None):
        self._rt = manager if manager is not None else get_runtime_manager()

    @property
    def manager(self):
        return self._rt

    # ── 探测 / 诊断 ──

    def discover_pythons(self, *, force: bool = False):
        return self._rt.discover_pythons(force=force)

    def resolve_uv(self, *, force: bool = False) -> UvInfo:
        return self._rt.resolve_uv(force=force)

    def resolve_git(self, *, force: bool = False) -> GitInfo:
        return self._rt.resolve_git(force=force)

    def resolve_base_python(self, *, min_ver=None, max_ver=None):
        return self._rt.resolve_base_python(min_ver=min_ver, max_ver=max_ver)

    def get_vc_redist(self, *, force: bool = False):
        return self._rt.get_vc_redist(force=force)

    def diagnose_text(self) -> str:
        return self._rt.diagnose_text()

    def invalidate_caches(self, **kwargs) -> None:
        self._rt.invalidate_caches(**kwargs)

    def has_scan_cache(self) -> bool:
        """Python / uv / Git 探测缓存是否齐备（齐备时可零等待直接画 UI）。"""
        rt = self._rt
        return (
            rt._py_list_cache is not None
            and rt._uv_cache is not None
            and rt._git_cache is not None
        )

    def version_ok(self, python_info) -> bool:
        """PythonInfo 是否落在默认允许版本区间（3.10–3.12）。"""
        rt = self._rt
        return rt._version_ok(python_info, rt.DEFAULT_PY_MIN, rt.DEFAULT_PY_MAX)

    # ── 基础解释器 ──

    def get_saved_python_path(self) -> str:
        return self._rt.get_saved_python_path()

    def set_python_path(self, path: str) -> None:
        self._rt.set_python_path(path)

    # ── 镜像 / 代理（仅本应用生效，不改全局配置）──

    def get_pip_index_url(self) -> str:
        return self._rt.get_pip_index_url()

    def set_pip_index_url(self, url: str) -> None:
        self._rt.set_pip_index_url(url)

    def get_github_proxy(self) -> str:
        return self._rt.get_github_proxy()

    def set_github_proxy(self, proxy: str) -> None:
        self._rt.set_github_proxy(proxy)

    # ── uv 安装 ──

    def install_uv(self, progress: Optional[Callable] = None) -> UvInfo:
        return self._rt.install_uv(progress=progress)

    def runtime_root(self) -> Path:
        return self._rt.runtime_root()

    # ── 模型隔离环境 ──

    @property
    def is_busy(self) -> bool:
        return self._rt.is_busy

    def env_exists(self, model_id: str) -> bool:
        return self._rt.env_exists(model_id)

    def model_env_dir(self, model_id: str) -> Path:
        return self._rt.model_env_dir(model_id)

    def get_model_env_status(
        self,
        model_id: str,
        packages: Optional[list] = None,
        *,
        force: bool = False,
        quick: bool = False,
    ) -> ModelEnvStatus:
        return self._rt.get_model_env_status(
            model_id, packages, quick=quick, force=force
        )

    def write_env_meta(self, model_id: str, *, packages=None, status=None) -> None:
        self._rt.write_env_meta(model_id, packages=packages, status=status)

    def ensure_model_env_async(
        self,
        model_id: str,
        *,
        python_version: str = "3.12",
        packages: Optional[list] = None,
        progress: Optional[Callable] = None,
        finished: Optional[Callable] = None,
        force_recreate: bool = False,
    ) -> None:
        self._rt.ensure_model_env_async(
            model_id,
            python_version=python_version,
            packages=packages,
            progress=progress,
            finished=finished,
            force_recreate=force_recreate,
        )

    # ── 开发环境就绪判断（模型页门禁）──

    def check_dev_ready(self) -> tuple[bool, str]:
        """返回 (是否可配置模型, 说明文案)。"""
        uv = self.resolve_uv()
        base = self.resolve_base_python(min_ver=(3, 10), max_ver=(3, 12))
        return evaluate_dev_readiness(bool(uv.found), base is not None)


_default_facade: Optional[RuntimeFacade] = None


def get_runtime_facade() -> RuntimeFacade:
    """进程内单例门面。"""
    global _default_facade
    if _default_facade is None:
        _default_facade = RuntimeFacade()
    return _default_facade


__all__ = [
    "RuntimeFacade",
    "get_runtime_facade",
    "evaluate_dev_readiness",
    # 常量 / 纯函数直通（供配置 Route 使用）
    "DEFAULT_PIP_INDEX_URL",
    "PIP_INDEX_PRESETS",
    "DEFAULT_GITHUB_PROXY",
    "GITHUB_PROXY_PRESETS",
    "GIT_DOWNLOAD_URL",
    "VC_REDIST_X64_URL",
    "VC_REDIST_HELP_URL",
    "NVIDIA_DRIVER_URL",
    "plan_torch_install",
    "build_ai_setup_prompt",
    "match_local_gpu",
    "rewrite_github_url",
    "packages_need_git",
    "UvInfo",
    "GitInfo",
    "ModelEnvStatus",
]
