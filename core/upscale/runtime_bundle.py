"""DLSS5 外挂运行时（便携包）定位与完整性校验。

**重要（许可）**：本模块只做「定位 + 校验 + 读取版本号」，PixelFlow 源码仓库与
安装包**不包含**任何 DLSS / ReShade / RenoDX 二进制。运行时包由维护者上传至
**本项目 Releases**，配置页可一键下载并只提取必需文件到 ``runtime/upscale/dlss5/``。

支持两种目录布局：
- ``upstream``：便携包原始布局 ``<root>/bin/runtime/{host,dlss}/…``
- ``compact`` ：PixelFlow 提取安装后的精简布局 ``<root>/{host,dlss}/…``
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import config
from core.upscale.upscale_settings import get_dlss5_dir

# DLSS5 便携包中「图片高清放大」路径必需的文件（视频/帧生成相关一律不需要）
# (相对 runtime 根的路径, 显示名, 许可)
REQUIRED_FILES: tuple[tuple[str, str, str], ...] = (
    (
        "host/nvngx.dll",
        "DLSS5 渲染入口（D3D12 feeder）",
        "MIT",
    ),
    (
        "host/dxgi.dll",
        "ReShade 6.8.0（完整 add-on 版，重命名为 dxgi.dll）",
        "BSD-3-Clause",
    ),
    (
        "host/renodx-dlss5.addon64",
        "RenoDX DLSS5 add-on",
        "RenoDX 框架 MIT（该 build 无独立再分发授权）",
    ),
    (
        "host/nvngx_dlssnr.dll",
        "NVIDIA DLSSNR 运行时（DLSS 5 神经渲染，feature-18）",
        "NVIDIA RTX SDK License（禁止再分发）",
    ),
    (
        "dlss/nvngx_dlss.dll",
        "NVIDIA DLSS 超分运行时",
        "NVIDIA RTX SDK License（禁止再分发）",
    ),
)

# 已测试文件构建的 SHA256 参考值（仅用于展示比对，**不做硬锁**：
# 二进制随构建更新必然变化，硬锁会让用户无法升级）
KNOWN_SHA256: dict[str, str] = {
    "host/dxgi.dll": "0CEE63F9C9F13F3AC909C5B4903F4DBB4B719A7AB3B4F13B0DEAF83C814B94F7",
    "host/renodx-dlss5.addon64": "D5ADF82EB44B065F4C590AC91FE824BAB07AFEA0EB9F994BDE936710C8593952",
    "host/nvngx_dlssnr.dll": "6EB209E764F39872625DEBD6ABAF45E2BB6322F6F270F781F70C059AE30B3927",
    "dlss/nvngx_dlss.dll": "C85F971CE023C9F3492FC7455F0B01A24BA18EA39636407A846902C4360B0B7E",
}

# 已验证可用的 NVIDIA DLSSNR 版本（读 PE FileVersion 字符串比对）。
# 注意：该 DLL 的 FileVersion 资源字符串是 "310.8.SF.0"（产品版本常写作 310.8.0.0）
EXPECTED_NR_VERSION = "310.8.SF.0"

LAYOUT_UPSTREAM = "upstream"
LAYOUT_COMPACT = "compact"


@dataclass
class BundleFileStatus:
    """单个运行时文件的状态。"""
    rel: str
    label: str
    license: str = ""
    exists: bool = False
    size_mb: float = 0.0
    version: str = ""
    sha256: str = ""            # 仅在显式请求时计算（158MB 文件哈希很慢）
    path: str = ""

    @property
    def known_sha256(self) -> str:
        return KNOWN_SHA256.get(self.rel.replace("\\", "/"), "")

    @property
    def sha256_match(self) -> Optional[bool]:
        if not self.sha256 or not self.known_sha256:
            return None
        return self.sha256.upper() == self.known_sha256.upper()


@dataclass
class BundleStatus:
    """DLSS5 运行时整体状态。"""
    root: Optional[Path] = None
    layout: str = ""                    # upstream / compact / ""
    found: bool = False                 # 目录存在且布局可识别
    complete: bool = False              # 必需文件齐全
    writable: bool = False              # host 目录可写（native worker 每次运行重写 ReShade.ini）
    files: tuple[BundleFileStatus, ...] = ()
    missing: tuple[str, ...] = ()
    nr_version: str = ""
    total_size_mb: float = 0.0
    detail: str = ""
    problems: list[str] = field(default_factory=list)

    # ── 路径访问 ──
    @property
    def runtime_root(self) -> Optional[Path]:
        if self.root is None or not self.layout:
            return None
        if self.layout == LAYOUT_UPSTREAM:
            return self.root / "bin" / "runtime"
        return self.root

    @property
    def host_dir(self) -> Optional[Path]:
        rr = self.runtime_root
        return (rr / "host") if rr is not None else None

    @property
    def worker_path(self) -> Optional[Path]:
        hd = self.host_dir
        return (hd / "nvngx.dll") if hd is not None else None

    @property
    def reshade_log_path(self) -> Optional[Path]:
        hd = self.host_dir
        return (hd / "ReShade.log") if hd is not None else None

    def file_path(self, rel: str) -> Optional[Path]:
        rr = self.runtime_root
        if rr is None:
            return None
        return rr / Path(rel.replace("/", os.sep))

    @property
    def ready(self) -> bool:
        return bool(self.found and self.complete and self.writable)

    @property
    def version_ok(self) -> bool:
        """DLSSNR 版本是否为已验证版本（未知版本只警告，不阻断）。"""
        return self.nr_version == EXPECTED_NR_VERSION


# ── 目录解析 ──

def default_bundle_dir() -> Path:
    """默认运行时目录：``<安装目录>/runtime/upscale/dlss5``。"""
    return Path(config.DLSS5_RUNTIME_DIR)


def resolve_bundle_dir(explicit: Optional[str] = None) -> Path:
    """设置中的自定义目录优先，否则用默认目录。"""
    text = str(explicit if explicit is not None else (get_dlss5_dir() or "")).strip()
    if text:
        return Path(text).expanduser()
    return default_bundle_dir()


def detect_layout(root: Path) -> str:
    """识别目录布局；无法识别返回空串。"""
    if not root or not root.exists():
        return ""
    # 精简布局，或用户直接指到便携包的 bin/runtime（其下同样是 host/ + dlss/）
    if (root / "host" / "nvngx.dll").is_file():
        return LAYOUT_COMPACT
    if (root / "bin" / "runtime" / "host" / "nvngx.dll").is_file():
        return LAYOUT_UPSTREAM
    # 注意：不接受「所有文件平铺在根目录」的旧布局，
    # 半迁移的目录树若被放行，会静默混用不匹配的 NGX / add-on 组件
    return ""


def is_dir_writable(path: Path) -> bool:
    """目录（或其最近的已存在父目录）是否可写。"""
    try:
        target = Path(path)
        if not target.exists():
            # 尚不存在：检查能否创建
            target.mkdir(parents=True, exist_ok=True)
        probe = Path(tempfile.mkdtemp(prefix=".pf_w_", dir=str(target)))
        try:
            (probe / "probe.tmp").write_text("ok", encoding="utf-8")
        finally:
            for child in probe.iterdir():
                try:
                    child.unlink()
                except OSError:
                    pass
            try:
                probe.rmdir()
            except OSError:
                pass
        return True
    except OSError:
        return False
    except Exception:
        return False


# ── PE 版本信息（不加载 DLL，只读版本资源）──

def read_pe_file_version(path: Path) -> str:
    """读取 Windows PE 文件的 FileVersion（如 ``310.8.0.0``）；失败返回空串。"""
    if sys.platform != "win32":
        return ""
    p = str(path)
    try:
        version = ctypes.windll.version
        size = version.GetFileVersionInfoSizeW(ctypes.c_wchar_p(p), None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(ctypes.c_wchar_p(p), 0, size, buf):
            return ""
        # 优先英文（0409）Unicode（04b0），再退回第一个可用的语言块
        keys = ("\\StringFileInfo\\040904b0\\FileVersion",
                "\\StringFileInfo\\040904e4\\FileVersion")
        out = ctypes.c_void_p()
        out_len = ctypes.c_uint()
        for key in keys:
            if version.VerQueryValueW(
                buf, ctypes.c_wchar_p(key),
                ctypes.byref(out), ctypes.byref(out_len),
            ) and out.value and out_len.value > 0:
                text = ctypes.wstring_at(out.value, out_len.value)
                return text.split("\x00")[0].strip()
        # 语言无关：\ 根块里通常也有 FileVersion
        if version.VerQueryValueW(
            buf, ctypes.c_wchar_p("\\"), ctypes.byref(out), ctypes.byref(out_len),
        ) and out.value:
            class _VS_FIXEDFILEINFO(ctypes.Structure):
                _fields_ = [
                    ("dwSignature", ctypes.c_uint32),
                    ("dwStrucVersion", ctypes.c_uint32),
                    ("dwFileVersionMS", ctypes.c_uint32),
                    ("dwFileVersionLS", ctypes.c_uint32),
                    ("dwProductVersionMS", ctypes.c_uint32),
                    ("dwProductVersionLS", ctypes.c_uint32),
                    ("dwFileFlagsMask", ctypes.c_uint32),
                    ("dwFileFlags", ctypes.c_uint32),
                    ("dwFileOS", ctypes.c_uint32),
                    ("dwFileType", ctypes.c_uint32),
                    ("dwFileSubtype", ctypes.c_uint32),
                    ("dwFileDateMS", ctypes.c_uint32),
                    ("dwFileDateLS", ctypes.c_uint32),
                ]

            info = ctypes.cast(out, ctypes.POINTER(_VS_FIXEDFILEINFO)).contents
            if info.dwSignature == 0xFEEF04BD:
                ms, ls = info.dwFileVersionMS, info.dwFileVersionLS
                return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:
        return ""
    return ""


def sha256_of(path: Path, chunk_mb: int = 4) -> str:
    """计算文件 SHA256（大文件分块读，避免 158MB 一次性进内存）。"""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(chunk_mb * 1024 * 1024), b""):
                h.update(block)
        return h.hexdigest().upper()
    except OSError:
        return ""


# ── 校验 ──

def inspect_bundle(
    root: Optional[Path | str] = None,
    *,
    with_hashes: bool = False,
    with_versions: bool = True,
) -> BundleStatus:
    """检查 DLSS5 运行时目录。

    :param with_hashes: 是否计算 SHA256（158MB 文件较慢，仅诊断时开启）
    :param with_versions: 是否读取 PE FileVersion（很快，默认开启）
    """
    st = BundleStatus()
    root_path = Path(root).expanduser() if root else resolve_bundle_dir()
    st.root = root_path

    if not root_path.exists():
        st.detail = f"运行时目录不存在：{root_path}"
        st.problems.append(st.detail)
        return st

    layout = detect_layout(root_path)
    st.layout = layout
    if not layout:
        # 扁平布局（旧版便携包）单独提示
        flat = [
            n for n in ("nvngx.dll", "dxgi.dll", "renodx-dlss5.addon64",
                        "nvngx_dlssnr.dll", "nvngx_dlss.dll")
            if (root_path / n).is_file()
        ]
        if flat:
            st.detail = (
                "检测到旧版扁平布局：请把 host 相关文件（nvngx.dll / dxgi.dll / "
                "renodx-dlss5.addon64 / nvngx_dlssnr.dll）移入 host/ 子目录，"
                "nvngx_dlss.dll 移入 dlss/ 子目录。"
            )
        else:
            st.detail = (
                f"目录中未找到 DLSS5 运行时（缺少 host/nvngx.dll）：{root_path}\n"
                "请用配置页「一键安装 DLSS5 运行时」，或手动解压运行时包后指定目录。"
            )
        st.problems.append(st.detail)
        return st

    st.found = True
    rr = st.runtime_root
    files: list[BundleFileStatus] = []
    missing: list[str] = []
    total = 0.0

    for rel, label, lic in REQUIRED_FILES:
        fs = BundleFileStatus(rel=rel, label=label, license=lic)
        p = Path(rr) / Path(rel.replace("/", os.sep))
        fs.path = str(p)
        if p.is_file():
            fs.exists = True
            try:
                size = p.stat().st_size
                fs.size_mb = round(size / (1024 * 1024), 2)
                total += fs.size_mb
            except OSError:
                pass
            if with_versions and p.suffix.lower() in (".dll", ".exe"):
                fs.version = read_pe_file_version(p)
            if with_hashes:
                fs.sha256 = sha256_of(p)
        else:
            missing.append(rel)
        files.append(fs)

    st.files = tuple(files)
    st.missing = tuple(missing)
    st.total_size_mb = round(total, 2)
    st.complete = not missing
    nr = next((f for f in files if f.rel == "host/nvngx_dlssnr.dll"), None)
    st.nr_version = nr.version if (nr and nr.exists) else ""

    host = st.host_dir
    st.writable = bool(host is not None and is_dir_writable(host))

    # ── 问题汇总 ──
    if missing:
        st.problems.append("缺少必需文件：\n  " + "\n  ".join(missing))
    if not st.writable and st.complete:
        st.problems.append(
            f"host 目录不可写：{host}\n"
            "DLSS5 渲染入口每次运行都要重写 host/ReShade.ini，"
            "请把运行时移到可写目录（不要放在 C:\\Program Files 下），"
            "或以管理员身份运行 PixelFlow。"
        )
    if st.complete and st.nr_version and not st.version_ok:
        st.problems.append(
            f"NVIDIA DLSSNR 版本为 {st.nr_version}，已验证版本是 {EXPECTED_NR_VERSION}。"
            "版本不同可能协议不兼容，若渲染失败请从「配置 → 高清放大引擎」重新安装运行时。"
        )

    if st.ready and not st.problems:
        bits = [f"布局 {st.layout}", f"共 {st.total_size_mb:.0f} MB"]
        if st.nr_version:
            bits.append(f"DLSSNR {st.nr_version}")
        st.detail = "运行时就绪 · " + " · ".join(bits)
    elif st.ready:
        st.detail = "运行时可用（有警告）"
    else:
        st.detail = st.problems[0] if st.problems else "运行时不可用"
    return st


def bundle_summary_lines(st: BundleStatus) -> list[str]:
    """配置页展示用的逐文件清单。"""
    lines: list[str] = []
    for f in st.files:
        mark = "✓" if f.exists else "✗"
        size = f"{f.size_mb:.1f} MB" if f.exists else "缺失"
        ver = f"  v{f.version}" if f.version else ""
        lines.append(f"{mark} {f.rel}  —  {f.label}  [{size}{ver}]")
    if st.root is not None:
        lines.append(f"目录: {st.root}  (布局: {st.layout or '未识别'})")
    return lines


__all__ = [
    "REQUIRED_FILES",
    "KNOWN_SHA256",
    "EXPECTED_NR_VERSION",
    "LAYOUT_UPSTREAM",
    "LAYOUT_COMPACT",
    "BundleFileStatus",
    "BundleStatus",
    "default_bundle_dir",
    "resolve_bundle_dir",
    "detect_layout",
    "is_dir_writable",
    "read_pe_file_version",
    "sha256_of",
    "inspect_bundle",
    "bundle_summary_lines",
]
