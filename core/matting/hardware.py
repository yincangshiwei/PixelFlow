"""
本机硬件检测 —— 用于判断是否适合运行指定抠图模型
"""
from __future__ import annotations

import platform
import sys
from dataclasses import dataclass, field

from core.matting.model_registry import MattingModelInfo, get_model_info


@dataclass
class HardwareInfo:
    """本机硬件摘要"""
    os_name: str = ""
    os_version: str = ""
    python_version: str = ""
    cpu_name: str = ""
    cpu_cores: int = 0
    ram_total_gb: float = 0.0
    ram_available_gb: float = 0.0
    torch_installed: bool = False
    torch_version: str = ""
    cuda_available: bool = False
    cuda_version: str = ""
    gpu_count: int = 0
    gpu_names: list[str] = field(default_factory=list)
    gpu_vram_gb: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def max_vram_gb(self) -> float:
        return max(self.gpu_vram_gb) if self.gpu_vram_gb else 0.0

    @property
    def primary_gpu(self) -> str:
        return self.gpu_names[0] if self.gpu_names else "无"


@dataclass
class ModelCompatibility:
    """某模型与本机的兼容性评估"""
    model_id: str
    model_name: str
    can_run: bool
    level: str              # excellent / good / acceptable / poor / unsupported
    device_recommend: str   # cuda / cpu
    summary: str
    details: list[str] = field(default_factory=list)


def _get_cpu_name() -> str:
    try:
        if sys.platform == "win32":
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            )
            name, _ = winreg.QueryValueEx(key, "ProcessorNameString")
            winreg.CloseKey(key)
            return str(name).strip()
    except Exception:
        pass
    return platform.processor() or "未知"


def _get_ram_gb() -> tuple[float, float]:
    """返回 (总内存GB, 可用内存GB)"""
    try:
        import psutil
        vm = psutil.virtual_memory()
        return vm.total / (1024 ** 3), vm.available / (1024 ** 3)
    except ImportError:
        pass
    # Windows 无 psutil 时用 ctypes
    if sys.platform == "win32":
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            total = stat.ullTotalPhys / (1024 ** 3)
            avail = stat.ullAvailPhys / (1024 ** 3)
            return total, avail
        except Exception:
            pass
    return 0.0, 0.0


def detect_hardware() -> HardwareInfo:
    """检测本机 CPU / 内存 / GPU / CUDA 状态"""
    info = HardwareInfo(
        os_name=platform.system(),
        os_version=platform.version(),
        python_version=sys.version.split()[0],
        cpu_name=_get_cpu_name(),
        cpu_cores=0,
    )
    try:
        import os
        info.cpu_cores = os.cpu_count() or 0
    except Exception:
        pass

    try:
        info.ram_total_gb, info.ram_available_gb = _get_ram_gb()
    except Exception as e:
        info.errors.append(f"内存检测失败: {e}")

    try:
        import torch
        info.torch_installed = True
        info.torch_version = getattr(torch, "__version__", "?")
        info.cuda_available = bool(torch.cuda.is_available())
        if info.cuda_available:
            info.cuda_version = getattr(torch.version, "cuda", "") or ""
            info.gpu_count = torch.cuda.device_count()
            for i in range(info.gpu_count):
                try:
                    name = torch.cuda.get_device_name(i)
                except Exception:
                    name = f"GPU {i}"
                info.gpu_names.append(name)
                vram = 0.0
                try:
                    props = torch.cuda.get_device_properties(i)
                    vram = props.total_memory / (1024 ** 3)
                except Exception:
                    pass
                info.gpu_vram_gb.append(vram)
    except ImportError:
        info.torch_installed = False
        info.errors.append("未安装 PyTorch（torch），无法使用 AI 抠图")
    except Exception as e:
        info.errors.append(f"PyTorch/CUDA 检测异常: {e}")

    return info


def evaluate_model(model_id: str, hw: HardwareInfo | None = None) -> ModelCompatibility:
    """评估本机是否适合运行指定抠图模型"""
    if hw is None:
        hw = detect_hardware()
    meta = get_model_info(model_id)
    if meta is None:
        return ModelCompatibility(
            model_id=model_id,
            model_name=model_id,
            can_run=False,
            level="unsupported",
            device_recommend="cpu",
            summary="未知模型",
            details=["模型未在注册表中定义"],
        )

    details: list[str] = []
    can_run = True
    level = "good"
    device = "cpu"

    # 主进程可以没有 torch：AI 在独立 uv 环境中运行
    if not hw.torch_installed:
        details.append(
            "主程序未安装 torch（正常）。请在「抠图模型配置」创建该模型的隔离环境"
        )

    # 内存
    if hw.ram_total_gb > 0 and hw.ram_total_gb < meta.min_ram_gb:
        can_run = False
        level = "unsupported"
        details.append(
            f"系统内存 {hw.ram_total_gb:.1f} GB 低于最低要求 {meta.min_ram_gb:.0f} GB"
        )
    elif hw.ram_total_gb > 0 and hw.ram_total_gb < meta.recommend_ram_gb:
        details.append(
            f"系统内存 {hw.ram_total_gb:.1f} GB，建议 ≥ {meta.recommend_ram_gb:.0f} GB 以获得更稳体验"
        )
        if level == "good":
            level = "acceptable"

    # GPU / CUDA
    if hw.cuda_available and meta.supports_cuda:
        device = "cuda"
        vram = hw.max_vram_gb
        details.append(f"检测到 GPU: {hw.primary_gpu}（显存约 {vram:.1f} GB）")
        if vram > 0 and vram >= meta.recommend_vram_gb:
            level = "excellent" if level not in ("poor", "unsupported") else level
            details.append(f"显存充足（推荐 ≥ {meta.recommend_vram_gb:.0f} GB），适合 GPU 加速")
        elif vram > 0 and vram >= max(meta.min_vram_gb, 2.0):
            if level in ("good", "excellent"):
                level = "good"
            details.append(
                f"显存 {vram:.1f} GB 可用，建议降低分辨率或关闭边缘精炼"
            )
        elif vram > 0:
            if level in ("good", "excellent"):
                level = "acceptable"
            details.append(f"显存偏低（{vram:.1f} GB），大图可能 OOM，可回退 CPU")
        else:
            details.append("已启用 CUDA，但未能读取显存信息")
    elif meta.supports_cpu:
        device = "cpu"
        if level in ("good", "excellent"):
            level = "acceptable"
        details.append("未检测到可用 CUDA GPU，将使用 CPU 推理（速度较慢）")
        if hw.cpu_cores and hw.cpu_cores < 4:
            level = "poor"
            details.append(f"CPU 核心数较少（{hw.cpu_cores}），批量抠图会很慢")
    else:
        can_run = False
        level = "unsupported"
        details.append("本模型需要 CUDA GPU，当前环境不支持")

    if meta.notes:
        details.append(meta.notes)

    level_text = {
        "excellent": "非常适合",
        "good": "适合运行",
        "acceptable": "可以运行（体验一般）",
        "poor": "勉强可用（不推荐大批量）",
        "unsupported": "不建议 / 无法运行",
    }
    summary = level_text.get(level, level)
    if can_run:
        summary = f"{summary} · 推荐设备: {device.upper()}"
    else:
        summary = f"{summary}"

    return ModelCompatibility(
        model_id=meta.id,
        model_name=meta.name,
        can_run=can_run,
        level=level,
        device_recommend=device,
        summary=summary,
        details=details,
    )
