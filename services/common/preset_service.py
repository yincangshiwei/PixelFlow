"""PresetService —— 预设 CRUD 门面（纯逻辑，无 QWidget）。

底层仍用 core.preset_manager.PresetManager；本服务负责：
- 按 feature_id 缓存管理器
- 经 FeatureService.normalize_preset 做兼容迁移
- 外部文件导入的重名决策（返回结果，UI 弹窗由 Route 完成）
- 各功能「上次选中预设」的持久化记忆（runtime/preset_state.json）
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from core.preset_manager import PresetManager
from config import PRESET_STATE_PATH


class PresetConflictAction(Enum):
    RENAME = "rename"
    OVERWRITE = "overwrite"
    CANCEL = "cancel"


@dataclass(frozen=True)
class PresetImportPlan:
    """外部预设导入计划（Route 据此弹窗或直接执行）。"""

    source_path: str
    suggested_name: str
    data: dict
    needs_conflict_resolution: bool
    existing_names: tuple[str, ...]


@dataclass(frozen=True)
class PresetOpResult:
    ok: bool
    message: str = ""
    name: str = ""
    data: Optional[dict] = None


class PresetService:
    """按功能管理预设；normalize 通过注入的 service_resolver 完成。"""

    def __init__(
        self,
        service_resolver: Optional[Callable[[str], Any]] = None,
    ):
        """
        :param service_resolver: feature_id -> FeatureService（需有 normalize_preset /
            default_state）。为 None 时直通原始 dict。
        """
        self._resolver = service_resolver
        self._managers: dict[str, PresetManager] = {}
        # 上次选中预设记忆（feature_id -> preset_name）；惰性加载
        self._state_path: Path = Path(PRESET_STATE_PATH)
        self._last_selected: Optional[dict[str, str]] = None

    def set_service_resolver(self, resolver: Callable[[str], Any] | None) -> None:
        self._resolver = resolver

    def manager_for(self, feature_id: str) -> PresetManager:
        if feature_id not in self._managers:
            self._managers[feature_id] = PresetManager(feature_id)
        return self._managers[feature_id]

    def preset_dir(self, feature_id: str) -> Path:
        return self.manager_for(feature_id).preset_dir

    def list_presets(self, feature_id: str) -> list[str]:
        return self.manager_for(feature_id).list_presets()

    def list_user_presets(self, feature_id: str) -> list[str]:
        return self.manager_for(feature_id).list_user_presets()

    def _normalize(self, feature_id: str, data: dict | None) -> dict:
        svc = self._resolve_service(feature_id)
        if svc is not None and hasattr(svc, "normalize_preset"):
            return dict(svc.normalize_preset(data))
        return dict(data or {})

    def _default_state(self, feature_id: str) -> dict:
        svc = self._resolve_service(feature_id)
        if svc is not None and hasattr(svc, "default_state"):
            return dict(svc.default_state())
        return {}

    def _resolve_service(self, feature_id: str) -> Any | None:
        if self._resolver is None:
            return None
        try:
            return self._resolver(feature_id)
        except Exception:
            return None

    def ensure_default(self, feature_id: str, default_data: dict | None = None) -> None:
        mgr = self.manager_for(feature_id)
        data = default_data if default_data is not None else self._default_state(feature_id)
        mgr.ensure_default(dict(data or {}))

    # ── 上次选中预设记忆 ──

    def last_selected(self, feature_id: str) -> Optional[str]:
        """某功能上次选中的预设名；无记忆或预设已不存在时返回 None。"""
        self._ensure_state_loaded()
        name = (self._last_selected or {}).get(feature_id)
        if not name:
            return None
        if name != "default" and name not in self.list_presets(feature_id):
            return None
        return name

    def remember_selected(self, feature_id: str, name: str) -> None:
        """记录某功能上次选中的预设并持久化。"""
        self._ensure_state_loaded()
        if self._last_selected.get(feature_id) == name:
            return
        self._last_selected[feature_id] = name
        self._save_state()

    def forget_selected(self, feature_id: str) -> None:
        """清除某功能的选中记忆（删除预设后回落 default）。"""
        self._ensure_state_loaded()
        if feature_id in self._last_selected:
            del self._last_selected[feature_id]
            self._save_state()

    def _ensure_state_loaded(self) -> None:
        if self._last_selected is not None:
            return
        state: dict[str, str] = {}
        try:
            if self._state_path.exists():
                raw = json.loads(self._state_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    state = {
                        k: v for k, v in raw.items()
                        if isinstance(k, str) and isinstance(v, str) and v
                    }
        except Exception:
            state = {}
        self._last_selected = state

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps(self._last_selected, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass  # 记忆持久化失败不影响预设主流程

    def load_preset(self, feature_id: str, name: str) -> PresetOpResult:
        mgr = self.manager_for(feature_id)
        raw = mgr.load_preset(name)
        if raw is None:
            return PresetOpResult(ok=False, message=f"预设不存在或已损坏: {name}", name=name)
        data = self._normalize(feature_id, raw)
        return PresetOpResult(ok=True, name=name, data=data)

    def load_default(self, feature_id: str) -> PresetOpResult:
        return self.load_preset(feature_id, "default")

    def save_preset(self, feature_id: str, name: str, data: dict) -> PresetOpResult:
        name = (name or "").strip()
        if not name:
            return PresetOpResult(ok=False, message="预设名称不能为空")
        if name == "default":
            return PresetOpResult(
                ok=False,
                message="不能使用 'default' 作为预设名称，该名称为系统保留",
                name=name,
            )
        normalized = self._normalize(feature_id, data)
        self.manager_for(feature_id).save_preset(name, normalized)
        return PresetOpResult(ok=True, message=f"已保存预设: {name}", name=name, data=normalized)

    def save_default(self, feature_id: str, data: dict) -> PresetOpResult:
        normalized = self._normalize(feature_id, data)
        self.manager_for(feature_id).save_default(normalized)
        return PresetOpResult(ok=True, message="已恢复默认设置", name="default", data=normalized)

    def restore_factory_default(self, feature_id: str) -> PresetOpResult:
        """用出厂默认覆盖 default.json 并返回规范化数据。"""
        defaults = self._default_state(feature_id)
        return self.save_default(feature_id, defaults)

    def delete_preset(self, feature_id: str, name: str) -> PresetOpResult:
        name = (name or "").strip()
        if name == "default":
            return PresetOpResult(ok=False, message="默认预设不能删除", name=name)
        ok = self.manager_for(feature_id).delete_preset(name)
        if not ok:
            return PresetOpResult(ok=False, message=f"删除失败: {name}", name=name)
        return PresetOpResult(ok=True, message=f"已删除预设: {name}", name=name)

    def read_external_file(self, file_path: str | Path) -> PresetOpResult:
        path = Path(file_path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            return PresetOpResult(ok=False, message=f"无法读取预设文件: {e}")
        if not isinstance(data, dict):
            return PresetOpResult(ok=False, message="预设文件内容必须是 JSON 对象")
        return PresetOpResult(ok=True, name=path.stem, data=data)

    def plan_import(
        self, feature_id: str, file_path: str | Path
    ) -> tuple[Optional[PresetImportPlan], Optional[str]]:
        """读取外部文件并生成导入计划；失败时 (None, error_msg)。"""
        result = self.read_external_file(file_path)
        if not result.ok:
            return None, result.message
        name = result.name or Path(file_path).stem
        existing = tuple(self.list_user_presets(feature_id))
        needs = name in existing
        plan = PresetImportPlan(
            source_path=str(file_path),
            suggested_name=name,
            data=dict(result.data or {}),
            needs_conflict_resolution=needs,
            existing_names=existing,
        )
        return plan, None

    def commit_import(
        self,
        feature_id: str,
        plan: PresetImportPlan,
        *,
        final_name: str | None = None,
        action: PresetConflictAction = PresetConflictAction.OVERWRITE,
    ) -> PresetOpResult:
        """执行外部导入。冲突时由调用方先解析 action / final_name。"""
        name = (final_name if final_name is not None else plan.suggested_name).strip()
        if not name:
            return PresetOpResult(ok=False, message="预设名称不能为空")
        if name == "default":
            return PresetOpResult(
                ok=False,
                message="不能使用 'default' 作为预设名称，该名称为系统保留",
                name=name,
            )
        existing = set(self.list_user_presets(feature_id))
        if name in existing and action is PresetConflictAction.CANCEL:
            return PresetOpResult(ok=False, message="已取消", name=name)
        if name in existing and action is PresetConflictAction.RENAME:
            return PresetOpResult(
                ok=False,
                message=f"预设 '{name}' 已存在，请重新选择名称",
                name=name,
            )
        # OVERWRITE 或无冲突
        return self.save_preset(feature_id, name, plan.data)
