"""ImportCollection —— 与 QListWidget 解耦的导入集合（纯逻辑，无 Qt）。

现状（重构前）列表状态散落在 QListWidgetItem 的 data role 中；P1 起：
- 路径 / 相对路径 / 显示名 / 任务状态全部保存在本集合（ImportEntry 为真相）
- 控件只是集合的投影（ui/routes/file_list/file_list_route.py）
- 排序语义与旧实现保持一致：插入后由控件 sortItems 决定顺序，
  投影层把排序结果通过 reorder() 回写集合，保证处理顺序与重构前一致
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from core.batch_session import STATUS_PENDING
from services.contracts.import_entry import ImportEntry, ImportSource


def compute_display_name(path: str, base_dir: str | None) -> str:
    """列表显示名（与重构前 _insert_files 完全一致）：

    - 有 base_dir：相对导入根目录的路径（分隔符 "/"）；无法求相对路径时仅文件名
    - 无 base_dir：父目录名/文件名（便于区分单独添加的同名文件）
    """
    if base_dir:
        try:
            return str(Path(path).relative_to(base_dir)).replace("\\", "/")
        except ValueError:
            return Path(path).name
    return f"{Path(path).parent.name}/{Path(path).name}"


@dataclass
class EntryState:
    """集合内单个条目的可变状态（条目本体不可变，状态围绕它维护）。"""

    entry: ImportEntry
    display_name: str
    job_status: str = STATUS_PENDING
    job_error: str = ""


class ImportCollection:
    """导入条目集合：增删、去重、排序同步、按范围生成输入快照。

    可在无 QApplication / QWidget 的环境中使用（纯标准库 + contracts）。
    """

    def __init__(self):
        self._order: list[str] = []                    # entry_id 顺序
        self._by_id: dict[str, EntryState] = {}
        self._path_to_id: dict[str, str] = {}          # 去重键：绝对路径

    # ─── 基本容器接口 ───
    def __len__(self) -> int:
        return len(self._order)

    def __iter__(self) -> Iterator[ImportEntry]:
        for eid in self._order:
            yield self._by_id[eid].entry

    def __contains__(self, path: object) -> bool:
        return isinstance(path, str) and path in self._path_to_id

    @property
    def entries(self) -> list[ImportEntry]:
        return [self._by_id[eid].entry for eid in self._order]

    def paths(self) -> list[str]:
        return [self._by_id[eid].entry.path for eid in self._order]

    # ─── 查询 ───
    def has_path(self, path: str) -> bool:
        return path in self._path_to_id

    def has_id(self, entry_id: str) -> bool:
        return entry_id in self._by_id

    def entry_by_path(self, path: str) -> ImportEntry | None:
        eid = self._path_to_id.get(path)
        return self._by_id[eid].entry if eid is not None else None

    def entry_by_id(self, entry_id: str) -> ImportEntry | None:
        st = self._by_id.get(entry_id)
        return st.entry if st is not None else None

    def state_by_path(self, path: str) -> EntryState | None:
        eid = self._path_to_id.get(path)
        return self._by_id.get(eid) if eid is not None else None

    def state_by_id(self, entry_id: str) -> EntryState | None:
        return self._by_id.get(entry_id)

    def display_name_of(self, path: str) -> str:
        st = self.state_by_path(path)
        return st.display_name if st is not None else Path(path).name

    # ─── 增加 ───
    def add(
        self,
        path: str,
        *,
        relative_path: str | None = None,
        source: ImportSource = ImportSource.LOCAL_FILE,
        content_hash: str = "",
        display_name: str | None = None,
    ) -> ImportEntry | None:
        """添加单个条目；路径已存在时返回 None（去重语义与旧列表一致）。"""
        if not path or path in self._path_to_id:
            return None
        entry = ImportEntry(
            path=path,
            relative_path=relative_path,
            source=source,
            content_hash=content_hash,
        )
        self._by_id[entry.entry_id] = EntryState(
            entry=entry,
            display_name=display_name or Path(path).name,
        )
        self._path_to_id[path] = entry.entry_id
        self._order.append(entry.entry_id)
        return entry

    def add_files(
        self,
        files: Iterable[str],
        base_dir: str | None = None,
        source: ImportSource = ImportSource.LOCAL_FILE,
    ) -> list[ImportEntry]:
        """批量添加（与旧 _insert_files 相同的路径去重与显示名规则）。

        base_dir 存在时记录相对路径（供「保留目录结构」输出使用）。
        返回实际新增的条目（重复路径被跳过）。
        """
        added: list[ImportEntry] = []
        for f in files:
            if not f or f in self._path_to_id:
                continue
            rel_path = None
            if base_dir:
                try:
                    rel_path = str(Path(f).relative_to(base_dir)).replace("\\", "/")
                except ValueError:
                    rel_path = None
            entry = self.add(
                f,
                relative_path=rel_path,
                source=source,
                display_name=compute_display_name(f, base_dir),
            )
            if entry is not None:
                added.append(entry)
        return added

    # ─── 删除 ───
    def remove_paths(self, paths: Iterable[str]) -> list[str]:
        """按路径移除条目，返回实际移除的路径列表。"""
        removed: list[str] = []
        for p in paths:
            eid = self._path_to_id.pop(p, None)
            if eid is None:
                continue
            self._by_id.pop(eid, None)
            try:
                self._order.remove(eid)
            except ValueError:
                pass
            removed.append(p)
        return removed

    def clear(self):
        self._order.clear()
        self._by_id.clear()
        self._path_to_id.clear()

    # ─── 顺序 ───
    def reorder(self, entry_ids: Iterable[str]):
        """按给定 entry_id 顺序重排集合（投影层 sortItems 后回写）。

        未知 id 忽略；缺失 id 保持原相对顺序追加在末尾（防御性兜底）。
        """
        ids = [eid for eid in entry_ids if eid in self._by_id]
        known = set(ids)
        tail = [eid for eid in self._order if eid not in known]
        self._order = ids + tail

    # ─── 输入快照（处理范围）───
    def snapshot(self, paths: Iterable[str] | None = None) -> list[tuple[str, str | None]]:
        """生成输入快照 [(完整路径, 相对路径|None), ...]，保持集合顺序。

        paths 为 None 时取全部；否则取子集（用于「仅选中」范围）。
        """
        if paths is None:
            return [
                (st.entry.path, st.entry.relative_path)
                for st in (self._by_id[eid] for eid in self._order)
            ]
        wanted = set(paths)
        return [
            (st.entry.path, st.entry.relative_path)
            for st in (self._by_id[eid] for eid in self._order)
            if st.entry.path in wanted
        ]

    # ─── 任务状态（批处理投影）───
    def set_job_status(self, path: str, status: str, error: str = "") -> bool:
        st = self.state_by_path(path)
        if st is None:
            return False
        st.job_status = status
        st.job_error = error or ""
        return True

    def reset_job_status(self):
        for eid in self._order:
            st = self._by_id[eid]
            st.job_status = STATUS_PENDING
            st.job_error = ""

    def job_status(self, path: str) -> str:
        st = self.state_by_path(path)
        return st.job_status if st is not None else STATUS_PENDING
