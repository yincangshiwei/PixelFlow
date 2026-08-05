"""
运行时环境管理器

设计要点：
- 主程序（含打包 exe）保持轻量，不内嵌 torch / ben2
- 每个抠图模型使用独立的 uv 虚拟环境，避免依赖版本冲突
- 推理时通过子进程调用对应环境的 python + worker 脚本
- 可自动检测系统 Python；可自动下载安装 uv（到 runtime/uv/）
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import config


ProgressCb = Callable[[str, float, str], None]  # stage, percent, message


# ── 数据结构 ──

@dataclass
class PythonInfo:
    path: str = ""
    version: str = ""
    major: int = 0
    minor: int = 0
    executable_ok: bool = False
    is_64bit: bool = True
    note: str = ""

    @property
    def version_tuple(self) -> tuple[int, int]:
        return self.major, self.minor

    @property
    def display(self) -> str:
        if not self.path:
            return "未检测到"
        bit = "64-bit" if self.is_64bit else "32-bit"
        return f"{self.version} ({bit})  ·  {self.path}"


@dataclass
class UvInfo:
    path: str = ""
    version: str = ""
    found: bool = False
    source: str = ""  # path / bundled / not_found

    @property
    def display(self) -> str:
        if not self.found:
            return "未安装"
        return f"{self.version or 'ok'}  ·  {self.path}  [{self.source}]"


@dataclass
class ModelEnvStatus:
    model_id: str
    env_dir: str
    python_path: str = ""
    ready: bool = False
    python_version: str = ""
    missing_packages: list[str] = field(default_factory=list)
    detail: str = ""


# ── 工具函数 ──

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    if not text:
        return ""
    return _ANSI_RE.sub("", text)


def _run(
    args: list[str],
    *,
    timeout: int | None = 120,
    cwd: str | Path | None = None,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    # 避免 uv/python 输出被系统代码页/颜色码搞乱
    full_env.setdefault("PYTHONUTF8", "1")
    full_env.setdefault("PYTHONIOENCODING", "utf-8")
    full_env.setdefault("PYTHONWARNINGS", "ignore")
    full_env.setdefault("NO_COLOR", "1")
    full_env.setdefault("TERM", "dumb")
    full_env.setdefault("CLICOLOR", "0")
    full_env.setdefault("CLICOLOR_FORCE", "0")
    # 减少用户 site-packages / conda hook 干扰隔离环境探测
    full_env.setdefault("PYTHONNOUSERSITE", "1")
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
        env=full_env,
        creationflags=creationflags,
    )


def _normalize_check_names(packages: list[str] | None) -> list[str]:
    """把注册表/安装规格统一成用于检测的逻辑名。"""
    out: list[str] = []
    seen: set[str] = set()
    for pkg in packages or []:
        raw = (pkg or "").strip()
        if not raw:
            continue
        if raw.startswith("git+") or "github.com" in raw or raw.endswith(".git"):
            # git+https://.../BEN2.git → ben2
            name = "ben2" if "ben2" in raw.lower() else Path(raw.rstrip("/")).stem
            name = name.replace(".git", "")
        else:
            name = raw.split("[")[0].split("==")[0].split(">=")[0].split("<=")[0]
            name = name.split("@")[0].split(";")[0].strip()
        key = name.lower()
        if key in ("pillow", "pil"):
            name = "pillow"
            key = "pillow"
        if not name or key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _import_name_for(pkg: str) -> str:
    import_map = {
        "pillow": "PIL",
        "pil": "PIL",
        "opencv-python": "cv2",
        "opencv-python-headless": "cv2",
        "cv2": "cv2",
        "pytorch": "torch",
        "ben2": "ben2",
        "huggingface_hub": "huggingface_hub",
        "huggingface-hub": "huggingface_hub",
        "safetensors": "safetensors",
        "modelscope": "modelscope",
        "torchvision": "torchvision",
        "torch": "torch",
        "einops": "einops",
        "timm": "timm",
        "numpy": "numpy",
    }
    return import_map.get(pkg.lower(), pkg.replace("-", "_"))


# 进程内探测缓存，避免 UI 反复起子进程
_python_probe_cache: dict[str, PythonInfo | None] = {}
_uv_probe_cache: dict[str, UvInfo | None] = {}


def _probe_python(exe: str | Path, *, use_cache: bool = True) -> PythonInfo | None:
    exe = str(exe)
    if not exe:
        return None
    try:
        key = str(Path(exe).resolve()).lower()
    except Exception:
        key = exe.lower()
    if use_cache and key in _python_probe_cache:
        return _python_probe_cache[key]
    if not Path(exe).exists():
        _python_probe_cache[key] = None
        return None
    code = (
        "import sys,struct;"
        "print(sys.version.split()[0]);"
        "print(sys.version_info.major);"
        "print(sys.version_info.minor);"
        "print(struct.calcsize('P')*8)"
    )
    info = None
    try:
        r = _run([exe, "-c", code], timeout=8)
        if r.returncode == 0:
            lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
            if len(lines) >= 4:
                major, minor, bits = int(lines[1]), int(lines[2]), int(lines[3])
                info = PythonInfo(
                    path=str(Path(exe).resolve()),
                    version=lines[0],
                    major=major,
                    minor=minor,
                    executable_ok=True,
                    is_64bit=bits >= 64,
                )
    except Exception:
        info = None
    _python_probe_cache[key] = info
    return info


def _probe_uv(exe: str | Path, *, use_cache: bool = True) -> UvInfo | None:
    exe = str(exe)
    if not exe:
        return None
    try:
        key = str(Path(exe).resolve()).lower()
    except Exception:
        key = exe.lower()
    if use_cache and key in _uv_probe_cache:
        return _uv_probe_cache[key]
    if not Path(exe).exists():
        _uv_probe_cache[key] = None
        return None
    info = None
    try:
        r = _run([exe, "--version"], timeout=8)
        if r.returncode == 0:
            ver = (r.stdout or r.stderr or "").strip()
            info = UvInfo(path=str(Path(exe).resolve()), version=ver, found=True)
    except Exception:
        info = None
    _uv_probe_cache[key] = info
    return info


def _settings_path() -> Path:
    p = Path(config.RUNTIME_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p / "runtime_settings.json"


class RuntimeManager:
    """
    管理：
    - 系统可用的基础 Python（用于 uv venv）
    - uv 可执行文件（PATH / 项目内 runtime/uv）
    - 每个模型独立的 venv：runtime/envs/<model_id>/.venv
    """

    # 模型环境最低 Python（与注册表可再收紧）
    DEFAULT_PY_MIN = (3, 10)
    DEFAULT_PY_MAX = (3, 12)  # 含 3.12；3.13 部分 AI 轮子可能未就绪

    def __init__(self):
        self._lock = threading.Lock()
        self._settings = self._load_settings()
        self._busy_lock = threading.Lock()
        self._busy = False
        # UI 友好缓存
        self._py_list_cache: list[PythonInfo] | None = None
        self._uv_cache: UvInfo | None = None
        self._env_status_cache: dict[str, ModelEnvStatus] = {}

    # ── 配置 ──
    def _load_settings(self) -> dict:
        path = _settings_path()
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {
            "python_path": "",
            "uv_path": "",
        }

    def save_settings(self):
        path = _settings_path()
        path.write_text(
            json.dumps(self._settings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_saved_python_path(self) -> str:
        return str(self._settings.get("python_path", "") or "")

    def set_python_path(self, path: str):
        self._settings["python_path"] = path.strip()
        self.save_settings()
        self.invalidate_caches(pythons=True)

    def get_saved_uv_path(self) -> str:
        return str(self._settings.get("uv_path", "") or "")

    def set_uv_path(self, path: str):
        self._settings["uv_path"] = path.strip()
        self.save_settings()
        self.invalidate_caches(uv=True)

    def invalidate_caches(
        self,
        *,
        pythons: bool = False,
        uv: bool = False,
        env: str | bool = False,
        all_: bool = False,
    ):
        """清除探测缓存。env=True 清全部模型；env='ben2' 清指定模型。"""
        if all_ or pythons:
            self._py_list_cache = None
        if all_ or uv:
            self._uv_cache = None
        if all_ or env is True:
            self._env_status_cache.clear()
        elif isinstance(env, str) and env:
            self._env_status_cache.pop(env, None)
            # 也清带 packages 的复合 key
            for k in list(self._env_status_cache.keys()):
                if k.startswith(env + "|"):
                    self._env_status_cache.pop(k, None)

    @property
    def is_busy(self) -> bool:
        return self._busy

    # ── 路径 ──
    def runtime_root(self) -> Path:
        root = Path(config.RUNTIME_DIR)
        root.mkdir(parents=True, exist_ok=True)
        return root

    def envs_root(self) -> Path:
        root = Path(config.RUNTIME_ENVS_DIR)
        root.mkdir(parents=True, exist_ok=True)
        return root

    def model_env_dir(self, model_id: str) -> Path:
        return self.envs_root() / model_id

    def model_venv_dir(self, model_id: str) -> Path:
        return self.model_env_dir(model_id) / ".venv"

    def model_python(self, model_id: str) -> Path | None:
        venv = self.model_venv_dir(model_id)
        if sys.platform == "win32":
            py = venv / "Scripts" / "python.exe"
        else:
            py = venv / "bin" / "python"
        return py if py.is_file() else None

    def bundled_uv_path(self) -> Path:
        name = "uv.exe" if sys.platform == "win32" else "uv"
        return Path(config.RUNTIME_UV_DIR) / name

    # ── Python 发现 ──
    def discover_pythons(self, *, force: bool = False, deep: bool = False) -> list[PythonInfo]:
        """
        扫描本机可用的 Python 解释器（去重，带缓存）。
        deep=False：快速路径（PATH + py -0p + 少量固定目录），供 UI 使用。
        deep=True：额外浅扫常见安装目录（仍避免全盘 rglob）。
        """
        if not force and self._py_list_cache is not None:
            return list(self._py_list_cache)

        candidates: list[str] = []
        saved = self.get_saved_python_path()
        if saved:
            candidates.append(saved)

        # 当前进程（开发模式有用；frozen 时是 exe 本身，通常不是 CPython）
        if not getattr(sys, "frozen", False):
            candidates.append(sys.executable)

        for name in ("python", "python3"):
            p = shutil.which(name)
            if p:
                candidates.append(p)

        if sys.platform == "win32":
            py_launcher = shutil.which("py")
            if py_launcher:
                try:
                    r = _run([py_launcher, "-0p"], timeout=8)
                    for line in (r.stdout or "").splitlines():
                        m = re.search(r"([A-Za-z]:\\[^\s]+\.exe)", line)
                        if m:
                            candidates.append(m.group(1))
                        else:
                            parts = line.strip().split()
                            if parts and parts[-1].lower().endswith(".exe"):
                                candidates.append(parts[-1])
                except Exception:
                    pass

            # 固定浅路径（不 rglob，避免扫到巨大 Anaconda 树卡死 UI）
            local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python"
            if local.is_dir():
                for child in local.iterdir():
                    pe = child / "python.exe"
                    if pe.is_file():
                        candidates.append(str(pe))
            for pe in (
                Path(r"C:\Python312\python.exe"),
                Path(r"C:\Python311\python.exe"),
                Path(r"C:\Python310\python.exe"),
                Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Python312" / "python.exe",
            ):
                if pe.is_file():
                    candidates.append(str(pe))

            if deep:
                # 可选：只扫一层 conda/envs 名称，不递归整棵树
                for root in (
                    Path(r"D:\anaconda3"),
                    Path(r"C:\anaconda3"),
                    Path(os.environ.get("USERPROFILE", "")) / "anaconda3",
                    Path(os.environ.get("USERPROFILE", "")) / "miniconda3",
                ):
                    pe = root / "python.exe"
                    if pe.is_file():
                        candidates.append(str(pe))
                    envs = root / "envs"
                    if envs.is_dir():
                        try:
                            for env_dir in envs.iterdir():
                                epy = env_dir / "python.exe"
                                if epy.is_file():
                                    candidates.append(str(epy))
                        except Exception:
                            pass

        seen: set[str] = set()
        result: list[PythonInfo] = []
        for c in candidates:
            try:
                key = str(Path(c).resolve()).lower()
            except Exception:
                key = c.lower()
            if key in seen:
                continue
            seen.add(key)
            # 跳过本项目 runtime venv
            if "\\runtime\\envs\\" in key or "/runtime/envs/" in key:
                continue
            info = _probe_python(c)
            if info and info.executable_ok:
                if "runtime" in Path(info.path).parts and "envs" in Path(info.path).parts:
                    continue
                result.append(info)

        result.sort(key=lambda x: (x.major, x.minor), reverse=True)
        self._py_list_cache = list(result)
        return list(result)

    def resolve_base_python(
        self,
        min_ver: tuple[int, int] | None = None,
        max_ver: tuple[int, int] | None = None,
    ) -> PythonInfo | None:
        """选取用于创建 uv 环境的基础 Python"""
        min_ver = min_ver or self.DEFAULT_PY_MIN
        max_ver = max_ver or self.DEFAULT_PY_MAX

        saved = self.get_saved_python_path()
        if saved:
            info = _probe_python(saved)
            if info and self._version_ok(info, min_ver, max_ver):
                return info

        for info in self.discover_pythons():
            if self._version_ok(info, min_ver, max_ver):
                return info
        # 放宽：只要 >= min
        for info in self.discover_pythons():
            if (info.major, info.minor) >= min_ver and info.is_64bit:
                info.note = "版本高于推荐上限，部分包可能无预编译轮子"
                return info
        return None

    @staticmethod
    def _version_ok(
        info: PythonInfo,
        min_ver: tuple[int, int],
        max_ver: tuple[int, int],
    ) -> bool:
        if not info.is_64bit:
            return False
        v = (info.major, info.minor)
        return min_ver <= v <= max_ver

    # ── uv 发现 / 安装 ──
    def resolve_uv(self, *, force: bool = False) -> UvInfo:
        if not force and self._uv_cache is not None:
            return self._uv_cache

        saved = self.get_saved_uv_path()
        if saved:
            info = _probe_uv(saved)
            if info:
                info.source = "saved"
                self._uv_cache = info
                return info

        bundled = self.bundled_uv_path()
        info = _probe_uv(bundled)
        if info:
            info.source = "bundled"
            self._uv_cache = info
            return info

        which = shutil.which("uv")
        if which:
            info = _probe_uv(which)
            if info:
                info.source = "path"
                self._uv_cache = info
                return info

        info = UvInfo(found=False, source="not_found")
        self._uv_cache = info
        return info

    def install_uv(self, progress: ProgressCb | None = None) -> UvInfo:
        """
        将 uv 安装到 runtime/uv/（不依赖用户全局环境）。
        Windows: 从 GitHub releases 下载 uv-x86_64-pc-windows-msvc.zip
        其它: 使用官方 install 脚本到 RUNTIME_UV_DIR
        """
        if progress:
            progress("uv", 5, "准备安装 uv…")

        dest_dir = Path(config.RUNTIME_UV_DIR)
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = self.bundled_uv_path()

        if sys.platform == "win32":
            self._install_uv_windows(dest_dir, target, progress)
        else:
            self._install_uv_unix(dest_dir, progress)

        info = _probe_uv(target if target.exists() else shutil.which("uv") or "")
        if not info or not info.found:
            # unix 脚本可能装到 ~/.local/bin
            which = shutil.which("uv")
            if which:
                # 复制到项目目录便于便携
                try:
                    shutil.copy2(which, target)
                    info = _probe_uv(target)
                except Exception:
                    info = _probe_uv(which)
        if not info or not info.found:
            raise RuntimeError(
                "uv 安装失败。请手动从 https://github.com/astral-sh/uv/releases 下载，"
                f"将 uv 放到: {dest_dir}"
            )
        info.source = "bundled"
        self.set_uv_path(info.path)
        if progress:
            progress("uv", 100, f"uv 已就绪: {info.version}")
        return info

    def _install_uv_windows(self, dest_dir: Path, target: Path, progress: ProgressCb | None):
        import tempfile
        import zipfile

        # 固定使用较新的稳定版；若网络失败可回退官方脚本
        urls = [
            "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip",
            "https://ghfast.top/https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip",
        ]
        last_err = None
        for url in urls:
            try:
                if progress:
                    progress("uv", 15, f"下载 uv…\n{url}")
                with tempfile.TemporaryDirectory() as td:
                    zpath = Path(td) / "uv.zip"
                    urllib.request.urlretrieve(url, zpath)
                    if progress:
                        progress("uv", 60, "解压 uv…")
                    with zipfile.ZipFile(zpath, "r") as zf:
                        zf.extractall(td)
                    # 查找 uv.exe
                    found = list(Path(td).rglob("uv.exe"))
                    if not found:
                        raise RuntimeError("压缩包中未找到 uv.exe")
                    shutil.copy2(found[0], target)
                    # 可选 uvw
                    for extra in Path(td).rglob("uvw.exe"):
                        shutil.copy2(extra, dest_dir / "uvw.exe")
                return
            except Exception as e:
                last_err = e
                continue

        # 回退：powershell 官方安装脚本，再复制
        if progress:
            progress("uv", 30, "尝试官方安装脚本…")
        try:
            ps = (
                "irm https://astral.sh/uv/install.ps1 | iex"
            )
            r = _run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                timeout=300,
            )
            if r.returncode != 0:
                raise RuntimeError(r.stderr or r.stdout or "install.ps1 失败")
            # 常见位置
            candidates = [
                Path(os.environ.get("USERPROFILE", "")) / ".local" / "bin" / "uv.exe",
                Path(os.environ.get("USERPROFILE", "")) / ".cargo" / "bin" / "uv.exe",
            ]
            which = shutil.which("uv")
            if which:
                candidates.insert(0, Path(which))
            for c in candidates:
                if c.is_file():
                    shutil.copy2(c, target)
                    return
        except Exception as e:
            last_err = e

        raise RuntimeError(f"无法安装 uv: {last_err}")

    def _install_uv_unix(self, dest_dir: Path, progress: ProgressCb | None):
        if progress:
            progress("uv", 20, "通过官方脚本安装 uv…")
        # UV_INSTALL_DIR 指定安装目录
        env = {"UV_INSTALL_DIR": str(dest_dir)}
        r = _run(
            ["sh", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"],
            timeout=300,
            env=env,
        )
        if r.returncode != 0:
            raise RuntimeError(r.stderr or r.stdout or "uv install.sh 失败")

    # ── 模型环境 ──
    def resolve_required_packages(self, model_id: str, packages: list[str] | None = None) -> list[str]:
        """优先用调用方列表；否则从模型注册表 env_check_packages / env_packages 推导。"""
        if packages:
            return _normalize_check_names(packages)
        try:
            from core.matting.model_registry import get_model_info
            meta = get_model_info(model_id)
        except Exception:
            meta = None
        if meta is None:
            return []
        if meta.env_check_packages:
            return _normalize_check_names(list(meta.env_check_packages))
        return _normalize_check_names(list(meta.env_packages))

    def check_packages_in_python(
        self,
        python_exe: str | Path,
        packages: list[str],
    ) -> tuple[list[str], list[str], str]:
        """
        在指定 python 中检测依赖。
        返回 (missing, present, error_message)。
        使用 JSON 标记行，避免警告/ANSI 污染解析。
        """
        names = _normalize_check_names(packages)
        if not names:
            return [], [], ""
        mods = [_import_name_for(n) for n in names]
        # 子进程只打印一行 PF_DEPS_JSON={...}
        code = r"""
import json, sys
mods = %s
names = %s
miss, ok, errs = [], [], {}
for m, n in zip(mods, names):
    try:
        __import__(m)
        ok.append(n)
    except Exception as e:
        miss.append(n)
        errs[n] = type(e).__name__ + ': ' + str(e)[:160]
sys.stdout.write('PF_DEPS_JSON=' + json.dumps(
    {'missing': miss, 'ok': ok, 'errors': errs}, ensure_ascii=False
) + '\n')
sys.stdout.flush()
""" % (repr(mods), repr(names))
        try:
            r = _run([str(python_exe), "-c", code], timeout=45)
        except Exception as e:
            return names[:], [], f"探测失败: {e}"

        stdout = _strip_ansi(r.stdout or "")
        stderr = _strip_ansi(r.stderr or "")
        payload = None
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("PF_DEPS_JSON="):
                raw = line[len("PF_DEPS_JSON="):]
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    payload = None
                break

        if payload is None:
            # 解析失败时不要把 ANSI/警告当包名
            hint = (stderr or stdout or f"exit={r.returncode}")[-500:]
            hint = _strip_ansi(hint).replace("\x1b", "")
            return names[:], [], f"依赖探测输出异常: {hint}"

        missing = [str(x) for x in (payload.get("missing") or []) if str(x).strip()]
        # 过滤明显不是包名的噪声
        missing = [
            m for m in missing
            if m.isidentifier() or all(c.isalnum() or c in "-_." for c in m)
        ]
        present = [str(x) for x in (payload.get("ok") or []) if str(x).strip()]
        err = ""
        if missing and payload.get("errors"):
            parts = [f"{k}: {v}" for k, v in list(payload["errors"].items())[:4]]
            err = "; ".join(parts)
        return missing, present, err

    def get_model_env_status(
        self,
        model_id: str,
        required_packages: list[str] | None = None,
        *,
        quick: bool = False,
        force: bool = False,
    ) -> ModelEnvStatus:
        """
        quick=True：只判断 venv / meta（不 import），供 UI 轻量展示。
        quick=False：子进程真实 import 校验依赖（带缓存）。
        """
        env_dir = self.model_env_dir(model_id)
        pkgs = self.resolve_required_packages(model_id, required_packages)
        cache_key = f"{model_id}|{'q' if quick else 'f'}|{'+'.join(pkgs)}"
        if not force and cache_key in self._env_status_cache:
            return self._env_status_cache[cache_key]

        py = self.model_python(model_id)
        st = ModelEnvStatus(model_id=model_id, env_dir=str(env_dir))
        if py is None:
            st.detail = "尚未创建隔离环境"
            self._env_status_cache[cache_key] = st
            return st

        st.python_path = str(py)
        meta_path = env_dir / "env_meta.json"
        meta_data: dict = {}
        if meta_path.is_file():
            try:
                meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta_data = {}

        if quick:
            st.ready = False
            st.detail = "已创建（未校验依赖）"
            st.python_version = str(meta_data.get("python_version") or "")
            # quick 只信任 meta.ready==True，且缺失列表为空
            if meta_data.get("ready") is True and not meta_data.get("missing"):
                st.ready = True
                st.detail = "环境就绪（缓存）"
            elif meta_data.get("missing"):
                miss = [str(x) for x in meta_data.get("missing") or []]
                # 过滤历史脏数据（ANSI 等）
                miss = [m for m in miss if m.isidentifier() or all(c.isalnum() or c in "-_." for c in m)]
                if miss:
                    st.missing_packages = miss
                    st.detail = f"缺少: {', '.join(miss)}"
            self._env_status_cache[cache_key] = st
            return st

        info = _probe_python(py)
        if not info:
            st.detail = "环境内 Python 无法运行"
            self._env_status_cache[cache_key] = st
            return st
        st.python_version = info.version

        if not pkgs:
            st.ready = True
            st.detail = "环境就绪"
            self._env_status_cache[cache_key] = st
            return st

        missing, present, err = self.check_packages_in_python(py, pkgs)
        st.missing_packages = missing
        st.ready = len(missing) == 0
        if st.ready:
            st.detail = f"环境就绪（{len(present)} 项依赖）"
        else:
            st.detail = f"缺少: {', '.join(missing)}"
            if err:
                st.detail += f"  [{err[:200]}]"
        self._env_status_cache[cache_key] = st
        return st

    def env_exists(self, model_id: str) -> bool:
        """纯路径判断，零子进程。"""
        return self.model_python(model_id) is not None

    def write_env_meta(
        self,
        model_id: str,
        *,
        base_python: str = "",
        packages: list[str] | None = None,
        status: ModelEnvStatus | None = None,
    ):
        env_dir = self.model_env_dir(model_id)
        env_dir.mkdir(parents=True, exist_ok=True)
        st = status or self.get_model_env_status(model_id, force=True)
        marker = env_dir / "env_meta.json"
        marker.write_text(
            json.dumps(
                {
                    "model_id": model_id,
                    "base_python": base_python,
                    "python_version": st.python_version,
                    "packages": packages or [],
                    "check_packages": self.resolve_required_packages(model_id, packages),
                    "ready": bool(st.ready),
                    "missing": list(st.missing_packages),
                    "detail": st.detail,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self.invalidate_caches(env=model_id)

    def ensure_model_env(
        self,
        model_id: str,
        *,
        python_version: str = "3.12",
        packages: list[str] | None = None,
        progress: ProgressCb | None = None,
        force_recreate: bool = False,
    ) -> ModelEnvStatus:
        """
        使用 uv 为模型创建隔离 venv 并安装依赖。
        packages: 传给 `uv pip install` 的规格列表。
        """
        # 若未显式传入 packages，从注册表取完整依赖列表
        if not packages:
            try:
                from core.matting.model_registry import get_model_info
                meta = get_model_info(model_id)
                if meta and meta.env_packages:
                    packages = list(meta.env_packages)
            except Exception:
                packages = packages or []
        packages = list(packages or [])
        required_check = self.resolve_required_packages(model_id, packages)

        with self._busy_lock:
            if self._busy:
                raise RuntimeError("已有环境任务在进行中，请稍候")
            self._busy = True

        try:
            if progress:
                progress(model_id, 2, "检查 uv…")
            uv = self.resolve_uv()
            if not uv.found:
                if progress:
                    progress(model_id, 5, "未找到 uv，开始自动安装…")
                uv = self.install_uv(progress=progress)

            if progress:
                progress(model_id, 12, "解析基础 Python…")
            parts = python_version.split(".")
            min_v = (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
            base = self.resolve_base_python(min_ver=min_v, max_ver=(min_v[0], min_v[1]))
            if base is None:
                base = self.resolve_base_python(
                    min_ver=(3, 10),
                    max_ver=(3, 12),
                )
            if base is None:
                raise RuntimeError(
                    "未找到可用的 Python 3.10–3.12（64 位）。\n"
                    "请先安装 Python：https://www.python.org/downloads/\n"
                    "安装时勾选 “Add python.exe to PATH”，然后在「开发环境」中重新检测。"
                )
            if not self.get_saved_python_path():
                self.set_python_path(base.path)

            env_dir = self.model_env_dir(model_id)
            venv_dir = self.model_venv_dir(model_id)
            env_dir.mkdir(parents=True, exist_ok=True)

            if force_recreate and venv_dir.exists():
                if progress:
                    progress(model_id, 15, "删除旧环境…")
                shutil.rmtree(venv_dir, ignore_errors=True)

            if not self.model_python(model_id):
                if progress:
                    progress(model_id, 20, f"创建虚拟环境 ({base.version})…")
                r = _run(
                    [uv.path, "venv", "--python", base.path, str(venv_dir)],
                    timeout=180,
                    cwd=env_dir,
                )
                if r.returncode != 0:
                    r = _run(
                        [uv.path, "venv", "--python", python_version, str(venv_dir)],
                        timeout=300,
                        cwd=env_dir,
                    )
                if r.returncode != 0:
                    raise RuntimeError(
                        f"创建 venv 失败:\n{_strip_ansi(r.stderr or r.stdout)}"
                    )

            py = self.model_python(model_id)
            if py is None:
                raise RuntimeError("venv 创建后未找到 python 可执行文件")

            # 始终安装完整依赖（修复环境也会重装/补齐）
            if packages:
                if progress:
                    progress(
                        model_id, 30,
                        f"安装完整依赖（{len(packages)} 项，含 torch/ben2，可能较久）…",
                    )
                # 分两批：先核心推理栈，再其余（便于定位失败点）
                core = [p for p in packages if any(
                    k in p.lower() for k in ("torch", "numpy", "pillow", "opencv")
                )]
                rest = [p for p in packages if p not in core]
                batches = [b for b in (core, rest) if b]
                if not batches:
                    batches = [packages]
                done = 0
                total = max(len(packages), 1)
                for batch in batches:
                    if progress:
                        pct = 30 + int(50 * done / total)
                        progress(model_id, pct, f"uv pip install: {', '.join(batch)[:80]}…")
                    cmd = [
                        uv.path, "pip", "install",
                        "--python", str(py),
                        "--upgrade",
                        *batch,
                    ]
                    r = _run(cmd, timeout=None, cwd=env_dir)
                    if r.returncode != 0:
                        err = _strip_ansi((r.stderr or r.stdout or "")[-2500:])
                        raise RuntimeError(f"依赖安装失败:\n{err}")
                    done += len(batch)

            if progress:
                progress(model_id, 88, "校验依赖是否可导入…")
            self.invalidate_caches(env=model_id)
            st = self.get_model_env_status(
                model_id, required_check or None, force=True
            )

            # 若仍缺依赖，尝试仅补装缺失项一次
            if (not st.ready) and st.missing_packages and packages:
                if progress:
                    progress(
                        model_id, 92,
                        f"补装缺失依赖: {', '.join(st.missing_packages)}",
                    )
                # 把 missing 映射回原始 install 规格
                retry_specs = []
                miss_set = {m.lower() for m in st.missing_packages}
                for p in packages:
                    key = _normalize_check_names([p])
                    if key and key[0].lower() in miss_set:
                        retry_specs.append(p)
                    elif "ben2" in p.lower() and "ben2" in miss_set:
                        retry_specs.append(p)
                if not retry_specs:
                    # 回退：直接用缺失名
                    retry_specs = list(st.missing_packages)
                cmd = [
                    uv.path, "pip", "install",
                    "--python", str(py),
                    "--upgrade",
                    *retry_specs,
                ]
                r = _run(cmd, timeout=None, cwd=env_dir)
                if r.returncode != 0:
                    err = _strip_ansi((r.stderr or r.stdout or "")[-2000:])
                    raise RuntimeError(
                        f"补装依赖失败（缺少 {', '.join(st.missing_packages)}）:\n{err}"
                    )
                self.invalidate_caches(env=model_id)
                st = self.get_model_env_status(
                    model_id, required_check or None, force=True
                )

            self.write_env_meta(
                model_id,
                base_python=base.path,
                packages=packages,
                status=st,
            )
            # 刷新 quick 缓存
            self._env_status_cache[f"{model_id}|q|"] = ModelEnvStatus(
                model_id=model_id,
                env_dir=str(env_dir),
                python_path=st.python_path,
                ready=st.ready,
                python_version=st.python_version,
                missing_packages=list(st.missing_packages),
                detail=st.detail,
            )
            if not st.ready:
                raise RuntimeError(
                    f"环境创建完成但依赖未就绪: {st.detail}\n"
                    "请检查网络后点击「创建/修复环境」重试。"
                )
            if progress:
                progress(model_id, 100, st.detail)
            return st
        finally:
            self._busy = False

    def ensure_model_env_async(
        self,
        model_id: str,
        *,
        python_version: str = "3.12",
        packages: list[str] | None = None,
        progress: ProgressCb | None = None,
        finished: Callable[[bool, str, ModelEnvStatus | None], None] | None = None,
        force_recreate: bool = False,
    ):
        def _run_job():
            ok, msg, st = False, "", None
            try:
                st = self.ensure_model_env(
                    model_id,
                    python_version=python_version,
                    packages=packages,
                    progress=progress,
                    force_recreate=force_recreate,
                )
                ok = st.ready
                msg = st.detail
            except Exception as e:
                ok = False
                msg = str(e)
            if finished:
                finished(ok, msg, st)

        t = threading.Thread(target=_run_job, name=f"env-{model_id}", daemon=True)
        t.start()

    def run_in_model_env(
        self,
        model_id: str,
        args: list[str],
        *,
        timeout: int | None = None,
        cwd: str | Path | None = None,
    ) -> subprocess.CompletedProcess:
        """在模型隔离环境中执行: python <args...>"""
        py = self.model_python(model_id)
        if py is None:
            raise RuntimeError(
                f"模型 {model_id} 的隔离环境未就绪，请先到「配置 → 抠图模型配置」创建环境"
            )
        return _run([str(py), *args], timeout=timeout, cwd=cwd)

    def diagnose_text(self) -> str:
        """生成开发环境诊断文本"""
        lines = []
        lines.append("=== PixelFlow 开发 / AI 运行时 ===")
        lines.append(f"运行时目录: {self.runtime_root()}")
        lines.append(f"模型环境目录: {self.envs_root()}")
        lines.append("")

        lines.append("— uv —")
        uv = self.resolve_uv()
        lines.append(uv.display)
        lines.append("")

        lines.append("— 系统 Python（可用于创建隔离环境）—")
        pys = self.discover_pythons()
        if not pys:
            lines.append("未检测到。请安装 Python 3.10–3.12（64 位）并勾选加入 PATH。")
        else:
            for i, p in enumerate(pys, 1):
                mark = ""
                if self._version_ok(p, self.DEFAULT_PY_MIN, self.DEFAULT_PY_MAX):
                    mark = " [推荐]"
                lines.append(f"{i}. {p.display}{mark}")
        saved = self.get_saved_python_path()
        if saved:
            lines.append(f"已保存选择: {saved}")
        lines.append("")

        lines.append("— 当前应用进程 —")
        lines.append(f"sys.executable: {sys.executable}")
        lines.append(f"frozen: {getattr(sys, 'frozen', False)}")
        lines.append(f"sys.version: {sys.version.split()[0]}")
        lines.append("")
        lines.append(
            "说明: AI 抠图不在主程序进程内加载 torch，"
            "而是为每个模型维护独立 uv 环境，通过子进程调用 worker 脚本，"
            "这样打包 exe 体积不受影响，且不同模型依赖互不冲突。"
        )
        return "\n".join(lines)


_mgr: RuntimeManager | None = None


def get_runtime_manager() -> RuntimeManager:
    global _mgr
    if _mgr is None:
        _mgr = RuntimeManager()
    return _mgr
