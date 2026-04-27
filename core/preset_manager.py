"""
PixelFlow 预设管理器
按功能模块管理预设，每个功能一个文件夹，每个文件夹内有 default.json + 用户预设。
预设目录结构：
    <app_dir>/presets/
        <processor_id>/
            default.json
            我的预设.json
            ...
"""
import json
from pathlib import Path

from config import PRESETS_DIR


def _get_presets_root() -> Path:
    """预设根目录"""
    return PRESETS_DIR


class PresetManager:
    """管理某个处理器的预设文件"""

    def __init__(self, processor_id: str):
        self._id = processor_id
        self._dir = _get_presets_root() / processor_id
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def preset_dir(self) -> Path:
        return self._dir

    # ── 默认预设 ──

    def has_default(self) -> bool:
        return (self._dir / "default.json").exists()

    def load_default(self) -> dict | None:
        return self._read("default")

    def save_default(self, data: dict):
        self._write("default", data)

    def ensure_default(self, default_data: dict):
        """如果 default.json 不存在则创建"""
        if not self.has_default():
            self.save_default(default_data)

    # ── 用户预设 ──

    def list_presets(self) -> list[str]:
        """列出所有预设名（不含 .json 后缀，包含 default）"""
        names = []
        for f in sorted(self._dir.glob("*.json")):
            names.append(f.stem)
        return names

    def list_user_presets(self) -> list[str]:
        """列出用户预设名（不含 default）"""
        return [n for n in self.list_presets() if n != "default"]

    def load_preset(self, name: str) -> dict | None:
        return self._read(name)

    def save_preset(self, name: str, data: dict):
        self._write(name, data)

    def delete_preset(self, name: str) -> bool:
        """删除预设（不允许删除 default）"""
        if name == "default":
            return False
        p = self._dir / f"{name}.json"
        if p.exists():
            p.unlink()
            return True
        return False

    def rename_preset(self, old_name: str, new_name: str) -> bool:
        if old_name == "default" or new_name == "default":
            return False
        old_path = self._dir / f"{old_name}.json"
        new_path = self._dir / f"{new_name}.json"
        if old_path.exists() and not new_path.exists():
            old_path.rename(new_path)
            return True
        return False

    # ── 内部 ──

    def _read(self, name: str) -> dict | None:
        p = self._dir / f"{name}.json"
        if not p.exists():
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def _write(self, name: str, data: dict):
        p = self._dir / f"{name}.json"
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
