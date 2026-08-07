"""文件导入协调服务：本地路径 / 剪贴板载荷 → 普通导入计划。

不依赖 Qt：窗口/Route 只负责执行 ImportPlan / ClipboardAction，
业务规则（分组、容器判定、粘贴动作链）集中在此。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .clipboard import ClipboardPayload, parse_text_paths
from .constants import HTML_EXTS
from .html_extractor import _cf_html_source_url, _extract_image_refs_from_html
from .refs import _image_ref_dedupe_key, _normalize_ref_text
from .scanner import _collect_extract_files, _collect_import_groups

# 剪贴板动作类型（动作链按顺序尝试，成功即终止）
ACTION_IMPORT_PATHS = "import_paths"   # 执行 ImportPlan（直接入库 + 容器抽图）
ACTION_SAVE_IMAGE = "save_image"       # 剪贴板位图落盘入库（可能失败，继续下一动作）
ACTION_EXTRACT_HTML = "extract_html"   # HTML 引用异步抽图


@dataclass
class ImportPlan:
    """本地路径导入计划（普通数据）。

    - groups: 直接入列表的图片分组 (files, base_dir)；
      base_dir 非 None 表示文件夹导入，用于计算相对路径（保留目录结构）
    - container_paths: 需要异步抽图的 HTML/DOCX/PDF 容器
    - container_kind / busy_tip: 提示文案
    """

    groups: list[tuple[list[str], str | None]] = field(default_factory=list)
    container_paths: list[str] = field(default_factory=list)
    container_kind: str = ""
    busy_tip: str = ""

    @property
    def direct_count(self) -> int:
        return sum(len(files) for files, _base in self.groups)


@dataclass
class ClipboardAction:
    """剪贴板单个可执行动作；多个动作按顺序尝试，成功即终止。"""

    kind: str
    import_plan: ImportPlan | None = None
    html_refs: list[str] = field(default_factory=list)
    html_page_url: str = ""
    busy_tip: str = ""


class FileImportService:
    """导入协调：只产出普通计划，不触碰任何控件。"""

    def plan_local_import(self, local_paths) -> ImportPlan:
        """整理本地路径：图片分组直接入库，容器收集待异步抽图。"""
        paths = list(local_paths or [])
        groups = _collect_import_groups(paths)
        containers = [str(p) for p in _collect_extract_files(paths)]
        kind = ""
        tip = ""
        if containers:
            labels = []
            for p in containers:
                ext = Path(p).suffix.lower()
                if ext in HTML_EXTS:
                    labels.append("HTML")
                elif ext == ".docx":
                    labels.append("DOCX")
                elif ext == ".pdf":
                    labels.append("PDF")
            kind = " / ".join(sorted(set(labels))) or "文档"
            tip = f"正在从 {kind} 提取图片（{len(containers)} 个文件）…"
        return ImportPlan(
            groups=groups,
            container_paths=containers,
            container_kind=kind,
            busy_tip=tip,
        )

    def plan_clipboard(self, payload: ClipboardPayload) -> list[ClipboardAction]:
        """按剪贴板内容产出有序动作链（与原 _paste_from_clipboard 分支语义一致）。

        优先级：本地路径（可导入时直接消费）→ 位图 → HTML 图片引用（消费）→ 纯文本路径。
        位图落盘可能失败，因此其后续动作作为回退保留在链中。
        """
        if payload.local_paths:
            plan = self.plan_local_import(payload.local_paths)
            if plan.direct_count > 0 or plan.container_paths:
                return [ClipboardAction(kind=ACTION_IMPORT_PATHS, import_plan=plan)]

        actions: list[ClipboardAction] = []
        if payload.has_image:
            actions.append(ClipboardAction(kind=ACTION_SAVE_IMAGE))
        if payload.html_text:
            refs = _extract_image_refs_from_html(payload.html_text)
            if refs:
                actions.append(ClipboardAction(
                    kind=ACTION_EXTRACT_HTML,
                    html_refs=refs,
                    html_page_url=_cf_html_source_url(payload.html_text),
                    busy_tip=f"正在从 HTML 提取图片（{len(refs)}）…",
                ))
                return actions
        if payload.text:
            candidates = parse_text_paths(payload.text)
            if candidates:
                actions.append(ClipboardAction(
                    kind=ACTION_IMPORT_PATHS,
                    import_plan=self.plan_local_import(candidates),
                ))
        return actions

    def normalize_html_jobs(
        self,
        html_jobs: list[tuple[list[str], str | None | Path, str]] | None,
    ) -> list[tuple[list[str], str | None, str]]:
        """去重并规范化 HTML 抽图任务（原 _start_container_extract 内逻辑）。"""
        norm_jobs: list[tuple[list[str], str | None, str]] = []
        global_keys: set[str] = set()
        for refs, base, page_url in (html_jobs or []):
            if not refs:
                continue
            uniq_refs: list[str] = []
            for r in refs:
                k = _image_ref_dedupe_key(r)
                if not k or k in global_keys:
                    continue
                global_keys.add(k)
                uniq_refs.append(_normalize_ref_text(r) or r)
            if not uniq_refs:
                continue
            base_s = str(base) if base is not None else None
            norm_jobs.append((uniq_refs, base_s, page_url or ""))
        return norm_jobs
