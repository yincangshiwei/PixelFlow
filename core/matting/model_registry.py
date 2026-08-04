"""
抠图模型注册表 —— 定义可用模型元信息、硬件建议与隔离环境依赖
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class MattingModelInfo:
    """单个抠图模型的元数据"""
    id: str                          # 内部唯一 ID
    name: str                        # 显示名称
    description: str                 # 简要说明
    source: str                      # 来源平台（modelscope / huggingface）
    repo_id: str                     # 仓库 ID，如 PramaLLC/BEN2
    page_url: str                    # 模型主页
    weight_files: tuple[str, ...]    # 判定「已下载」所需的关键权重文件
    approx_size_mb: int              # 约占用磁盘（MB）
    # 硬件建议
    min_ram_gb: float = 8.0
    recommend_ram_gb: float = 16.0
    min_vram_gb: float = 0.0
    recommend_vram_gb: float = 4.0
    supports_cpu: bool = True
    supports_cuda: bool = True
    notes: str = ""
    # 隔离环境
    python_version: str = "3.12"     # uv venv 目标版本
    # 传给 `uv pip install` 的包规格（顺序有意义时可把 torch 放前）
    env_packages: tuple[str, ...] = ()
    # 校验环境是否就绪时检查的导入/包名
    env_check_packages: tuple[str, ...] = ()
    worker_script: str = ""          # 相对 core/matting/workers/ 的脚本名
    extra: dict = field(default_factory=dict)


# ── 已注册模型（目前仅 BEN2）──
# 说明：依赖装在 runtime/envs/ben2/.venv，不进入主程序/打包体积
MATTING_MODELS: dict[str, MattingModelInfo] = {
    "ben2": MattingModelInfo(
        id="ben2",
        name="BEN2",
        description=(
            "Background Erase Network v2，高精度前景分割/抠图。"
            "擅长发丝、物体边缘与 4K 场景；在独立 uv 环境中运行。"
        ),
        source="modelscope",
        repo_id="PramaLLC/BEN2",
        page_url="https://www.modelscope.cn/models/PramaLLC/BEN2",
        weight_files=("model.safetensors",),
        approx_size_mb=364,
        min_ram_gb=8.0,
        recommend_ram_gb=16.0,
        min_vram_gb=0.0,
        recommend_vram_gb=4.0,
        supports_cpu=True,
        supports_cuda=True,
        notes=(
            "消费级 GPU 建议 batch≤3；开启边缘精炼会更慢但边缘更细。"
            "无 GPU 时可用 CPU，速度较慢。"
            "依赖通过「配置环境」安装到独立 venv，与主程序隔离。"
        ),
        python_version="3.12",
        env_packages=(
            # CPU 默认轮子，兼容性最好；有 NVIDIA 时可在环境页提示改装 CUDA 版
            "torch",
            "torchvision",
            "numpy",
            "einops",
            "timm",
            "Pillow",
            "opencv-python-headless",
            "huggingface_hub",
            "safetensors",
            "modelscope",
            "git+https://github.com/PramaLLC/BEN2.git",
        ),
        env_check_packages=(
            "torch",
            "torchvision",
            "numpy",
            "pillow",
            "cv2",  # opencv-python-headless
            "einops",
            "timm",
            "ben2",
            "safetensors",
            "huggingface_hub",
            "modelscope",
        ),
        worker_script="ben2_worker.py",
        extra={
            "alt_weight_files": ("BEN2_Base.pth", "pytorch_model.bin"),
            "package": "ben2",
            "class_name": "BEN_Base",
        },
    ),
}


def list_models() -> list[MattingModelInfo]:
    return list(MATTING_MODELS.values())


def get_model_info(model_id: str) -> MattingModelInfo | None:
    return MATTING_MODELS.get(model_id)
