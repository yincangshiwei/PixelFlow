"""
PixelFlow 日志落盘管理
按功能维护实时日志与每日历史日志。
"""
from __future__ import annotations

import re
import traceback
from datetime import datetime
from pathlib import Path

from config import LOGS_DIR


_SAFE_NAME_RE = re.compile(r"[^\w\-.\u4e00-\u9fff]+", re.UNICODE)


def safe_log_name(value: str) -> str:
    name = _SAFE_NAME_RE.sub("_", value.strip())
    name = name.strip("._")
    return name or "unknown"


class FeatureLogWriter:
    def __init__(self, feature_id: str):
        self.feature_id = safe_log_name(feature_id)
        self.feature_dir = LOGS_DIR / self.feature_id
        self.history_dir = self.feature_dir / "history"
        self.current_path = self.feature_dir / "current.log"
        self._ensure_dirs()

    @property
    def history_path(self) -> Path:
        return self.history_dir / f"{datetime.now():%Y-%m-%d}.log"

    def reset_current(self):
        self._ensure_dirs()
        self.current_path.write_text("", encoding="utf-8")

    def clear_current(self):
        self.reset_current()

    def write(self, text: str):
        self._ensure_dirs()
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n"
        for path in (self.current_path, self.history_path):
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)

    def write_exception(self, title: str, exc: BaseException | None = None):
        if exc is None:
            detail = traceback.format_exc()
        else:
            detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self.write(f"{title}\n{detail.rstrip()}")

    def _ensure_dirs(self):
        self.history_dir.mkdir(parents=True, exist_ok=True)


class AppLogManager:
    def __init__(self):
        self._current_writer: FeatureLogWriter | None = None

    def switch_feature(self, feature_id: str, clear_current: bool = True) -> FeatureLogWriter:
        self._current_writer = FeatureLogWriter(feature_id)
        if clear_current:
            self._current_writer.reset_current()
        return self._current_writer

    def current_writer(self) -> FeatureLogWriter:
        if self._current_writer is None:
            return self.switch_feature("app", clear_current=False)
        return self._current_writer

    def write(self, text: str):
        self.current_writer().write(text)

    def clear_current(self):
        self.current_writer().clear_current()
