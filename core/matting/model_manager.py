"""
抠图模型本地管理：路径、下载、就绪状态、用户配置持久化
"""
from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path
from typing import Callable

import config
from core.matting.model_registry import (
    MATTING_MODELS,
    MattingModelInfo,
    get_model_info,
    list_models,
)


ProgressCallback = Callable[[str, float, str], None]  # model_id, percent(0-100|-1), message


def _models_root() -> Path:
    root = Path(config.MODELS_DIR)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _settings_path() -> Path:
    return _models_root() / "matting_settings.json"


class MattingModelManager:
    """
    管理抠图模型的本地目录与下载状态。
    目录结构：
        models/matting/
            matting_settings.json
            ben2/                  ← 模型本地目录
                model.safetensors
                ...
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._download_threads: dict[str, threading.Thread] = {}
        self._settings = self._load_settings()

    # ── 配置持久化 ──
    def _load_settings(self) -> dict:
        path = _settings_path()
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {
            "default_model": "ben2",
            "device": "auto",          # auto / cuda / cpu
            "models": {},             # model_id -> {custom_path, ...}
        }

    def save_settings(self):
        path = _settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self._settings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_default_model_id(self) -> str:
        mid = self._settings.get("default_model", "ben2")
        return mid if mid in MATTING_MODELS else "ben2"

    def set_default_model_id(self, model_id: str):
        if model_id in MATTING_MODELS:
            self._settings["default_model"] = model_id
            self.save_settings()

    def get_device_preference(self) -> str:
        return self._settings.get("device", "auto")

    def set_device_preference(self, device: str):
        if device in ("auto", "cuda", "cpu"):
            self._settings["device"] = device
            self.save_settings()

    def get_custom_path(self, model_id: str) -> str:
        models = self._settings.setdefault("models", {})
        return str(models.get(model_id, {}).get("custom_path", "") or "")

    def set_custom_path(self, model_id: str, path: str):
        models = self._settings.setdefault("models", {})
        entry = models.setdefault(model_id, {})
        entry["custom_path"] = path.strip()
        self.save_settings()

    # ── 路径 ──
    def default_local_dir(self, model_id: str) -> Path:
        return _models_root() / model_id

    def resolve_model_dir(self, model_id: str) -> Path | None:
        """
        解析实际可用的模型目录：
        1. 用户自定义路径（若有效）
        2. 默认本地目录（若已下载）
        """
        custom = self.get_custom_path(model_id)
        if custom:
            p = Path(custom)
            if p.is_file():
                # 单文件权重：返回其父目录，并在 is_ready 中识别文件
                return p.parent
            if p.is_dir() and self._dir_has_weights(model_id, p):
                return p
        default = self.default_local_dir(model_id)
        if self._dir_has_weights(model_id, default):
            return default
        return None

    def resolve_weight_file(self, model_id: str) -> Path | None:
        """若用户指定了单个权重文件则返回该文件，否则在模型目录中查找。"""
        custom = self.get_custom_path(model_id)
        if custom:
            p = Path(custom)
            if p.is_file() and p.suffix.lower() in (
                ".safetensors", ".pth", ".pt", ".bin", ".ckpt",
            ):
                return p
        model_dir = self.resolve_model_dir(model_id)
        if model_dir is None:
            return None
        meta = get_model_info(model_id)
        if meta is None:
            return None
        candidates = list(meta.weight_files) + list(
            meta.extra.get("alt_weight_files", ())
        )
        for name in candidates:
            fp = model_dir / name
            if fp.is_file():
                return fp
        # 兜底：目录内任意权重
        for pat in ("*.safetensors", "*.pth", "*.pt", "*.bin"):
            found = list(model_dir.glob(pat))
            if found:
                return found[0]
        return None

    def _dir_has_weights(self, model_id: str, directory: Path) -> bool:
        if not directory.is_dir():
            return False
        meta = get_model_info(model_id)
        if meta is None:
            return False
        for name in meta.weight_files:
            if (directory / name).is_file():
                return True
        for name in meta.extra.get("alt_weight_files", ()):
            if (directory / name).is_file():
                return True
        # 任意 safetensors / pth
        for pat in ("*.safetensors", "*.pth", "*.pt"):
            if any(directory.glob(pat)):
                return True
        return False

    def is_ready(self, model_id: str) -> bool:
        if self.resolve_weight_file(model_id) is not None:
            return True
        # from_pretrained 目录模式
        d = self.resolve_model_dir(model_id)
        return d is not None and self._dir_has_weights(model_id, d)

    def status_text(self, model_id: str) -> str:
        if self.is_downloading(model_id):
            return "下载中…"
        if self.is_ready(model_id):
            custom = self.get_custom_path(model_id)
            if custom:
                return "权重就绪（自定义）"
            return "权重就绪（本地）"
        return "权重未下载"

    def is_downloading(self, model_id: str) -> bool:
        t = self._download_threads.get(model_id)
        return t is not None and t.is_alive()

    # ── 依赖检测（查隔离环境，而非主进程）──
    def check_runtime_deps(self, model_id: str | None = None) -> dict:
        """检查指定模型隔离环境中的依赖（默认当前默认模型）"""
        from core.runtime.env_manager import get_runtime_manager

        mid = model_id or self.get_default_model_id()
        meta = get_model_info(mid)
        rt = get_runtime_manager()
        result = {
            "model_id": mid,
            "env_ready": False,
            "env_python": "",
            "env_version": "",
            "missing": [],
            "messages": [],
            # 兼容旧字段名
            "torch": False,
            "torch_version": "",
            "torchvision": False,
            "ben2": False,
            "modelscope": False,
            "safetensors": False,
        }
        py = rt.model_python(mid)
        if py is None:
            result["messages"].append("尚未创建隔离环境，请点击「创建/修复环境」")
            return result

        result["env_python"] = str(py)
        check = list(meta.env_check_packages) if meta else ["torch", "ben2"]
        # status 用包名
        pkg_names = []
        for p in check:
            pkg_names.append("pillow" if p == "PIL" else p)
        st = rt.get_model_env_status(mid, pkg_names)
        result["env_ready"] = st.ready
        result["env_version"] = st.python_version
        result["missing"] = list(st.missing_packages)
        if st.missing_packages:
            result["messages"].append("缺少: " + ", ".join(st.missing_packages))

        # 细分标记
        missing_set = {m.lower() for m in st.missing_packages}
        result["torch"] = "torch" not in missing_set
        result["torchvision"] = "torchvision" not in missing_set
        result["ben2"] = "ben2" not in missing_set
        result["modelscope"] = "modelscope" not in missing_set
        result["safetensors"] = "safetensors" not in missing_set

        if result["torch"] and py:
            try:
                from core.runtime.env_manager import _run
                r = _run(
                    [str(py), "-c", "import torch; print(torch.__version__)"],
                    timeout=30,
                )
                if r.returncode == 0:
                    result["torch_version"] = (r.stdout or "").strip()
            except Exception:
                pass
        return result

    # ── 下载 ──
    def download_async(
        self,
        model_id: str,
        progress: ProgressCallback | None = None,
        finished: Callable[[str, bool, str], None] | None = None,
    ):
        """后台下载模型到默认本地目录。finished(model_id, ok, message)"""
        if self.is_downloading(model_id):
            if progress:
                progress(model_id, -1, "已有下载任务进行中")
            return

        def _run():
            ok = False
            msg = ""
            try:
                self._download_sync(model_id, progress)
                ok = True
                msg = "下载完成"
            except Exception as e:
                ok = False
                msg = str(e)
                if progress:
                    progress(model_id, -1, f"下载失败: {e}")
            finally:
                with self._lock:
                    self._download_threads.pop(model_id, None)
                if finished:
                    finished(model_id, ok, msg)

        t = threading.Thread(target=_run, name=f"matting-dl-{model_id}", daemon=True)
        with self._lock:
            self._download_threads[model_id] = t
        t.start()

    def _download_sync(self, model_id: str, progress: ProgressCallback | None):
        meta = get_model_info(model_id)
        if meta is None:
            raise ValueError(f"未知模型: {model_id}")

        target = self.default_local_dir(model_id)
        target.mkdir(parents=True, exist_ok=True)

        if progress:
            progress(model_id, 0, f"开始下载 {meta.name} …")

        if meta.source == "modelscope":
            self._download_modelscope(meta, target, progress)
        else:
            self._download_huggingface(meta, target, progress)

        if not self._dir_has_weights(model_id, target):
            raise RuntimeError(
                f"下载完成但未找到权重文件（期望: {', '.join(meta.weight_files)}）"
            )
        if progress:
            progress(model_id, 100, "下载完成，模型已就绪")

    def _download_modelscope(
        self,
        meta: MattingModelInfo,
        target: Path,
        progress: ProgressCallback | None,
    ):
        """优先在模型隔离环境中调用 modelscope；否则尝试主进程（开发便利）。"""
        if progress:
            progress(meta.id, 5, f"连接 ModelScope: {meta.repo_id}")

        local_dir = str(target)
        code = (
            "from modelscope.hub.snapshot_download import snapshot_download\n"
            f"snapshot_download(model_id={meta.repo_id!r}, local_dir={local_dir!r})\n"
            "print('OK')\n"
        )

        from core.runtime.env_manager import get_runtime_manager, _run

        rt = get_runtime_manager()
        py = rt.model_python(meta.id)
        if py is not None:
            if progress:
                progress(meta.id, 15, "通过模型隔离环境下载…")
            r = _run([str(py), "-c", code], timeout=None)
            if r.returncode != 0:
                raise RuntimeError(
                    f"隔离环境下载失败:\n{(r.stderr or r.stdout or '')[-1500:]}\n"
                    "请先「创建/修复环境」确保 modelscope 已安装。"
                )
        else:
            # 回退：主进程（若用户碰巧装了）
            try:
                from modelscope.hub.snapshot_download import snapshot_download
            except ImportError as e:
                raise ImportError(
                    "模型隔离环境未创建，且主程序也无 modelscope。\n"
                    "请先到「配置 → 抠图模型配置」点击「创建/修复环境」，再下载权重。"
                ) from e
            if progress:
                progress(meta.id, 15, "通过主进程 modelscope 下载…")
            try:
                snapshot_download(model_id=meta.repo_id, local_dir=local_dir)
            except TypeError:
                path = snapshot_download(meta.repo_id, cache_dir=str(target.parent))
                src = Path(path)
                if src.resolve() != target.resolve() and src.is_dir():
                    for name in meta.weight_files:
                        sp = src / name
                        if sp.is_file():
                            shutil.copy2(sp, target / name)
                    for name in meta.extra.get("alt_weight_files", ()):
                        sp = src / name
                        if sp.is_file() and not (target / name).exists():
                            shutil.copy2(sp, target / name)

        if progress:
            progress(meta.id, 90, f"文件已保存到 {local_dir}")

    def _download_huggingface(
        self,
        meta: MattingModelInfo,
        target: Path,
        progress: ProgressCallback | None,
    ):
        if progress:
            progress(meta.id, 5, f"连接 HuggingFace: {meta.repo_id}")
        local_dir = str(target)
        code = (
            "from huggingface_hub import snapshot_download\n"
            f"snapshot_download(repo_id={meta.repo_id!r}, local_dir={local_dir!r})\n"
            "print('OK')\n"
        )
        from core.runtime.env_manager import get_runtime_manager, _run

        rt = get_runtime_manager()
        py = rt.model_python(meta.id)
        if py is not None:
            r = _run([str(py), "-c", code], timeout=None)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout or "")[-1500:])
        else:
            try:
                from huggingface_hub import snapshot_download
            except ImportError as e:
                raise ImportError(
                    "请先创建模型隔离环境（含 huggingface_hub），再下载权重。"
                ) from e
            snapshot_download(repo_id=meta.repo_id, local_dir=local_dir)
        if progress:
            progress(meta.id, 90, "下载完成")

    def open_model_folder(self, model_id: str) -> Path:
        """确保目录存在并返回路径（供 UI 打开资源管理器）"""
        d = self.default_local_dir(model_id)
        d.mkdir(parents=True, exist_ok=True)
        return d


_manager: MattingModelManager | None = None


def get_matting_manager() -> MattingModelManager:
    global _manager
    if _manager is None:
        _manager = MattingModelManager()
    return _manager
