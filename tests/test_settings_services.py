"""services 薄封装单测（P5）：runtime_facade / matting_config_service。

- 纯逻辑函数：开发环境就绪评估、显存/内存数值格式化
- 门面委托语义：不直接触碰 manager 私有成员，行为与直调一致
"""
import unittest
from pathlib import Path
from types import SimpleNamespace

from services.common.matting_config_service import (
    MattingConfigService,
    format_gb,
)
from services.common.runtime_facade import (
    RuntimeFacade,
    evaluate_dev_readiness,
)


class EvaluateDevReadinessTests(unittest.TestCase):
    def test_both_ready(self):
        ok, detail = evaluate_dev_readiness(True, True)
        self.assertTrue(ok)
        self.assertEqual(detail, "开发环境已就绪")

    def test_only_uv(self):
        """仅 uv 可用：可托管下载 3.12，视为可配置模型"""
        ok, detail = evaluate_dev_readiness(True, False)
        self.assertTrue(ok)
        self.assertIn("托管下载", detail)

    def test_only_base_python(self):
        """有 Python 但缺 uv：不可配置模型"""
        ok, detail = evaluate_dev_readiness(False, True)
        self.assertFalse(ok)
        self.assertIn("uv", detail)

    def test_neither(self):
        ok, detail = evaluate_dev_readiness(False, False)
        self.assertFalse(ok)
        self.assertIn("uv", detail)
        self.assertIn("3.10", detail)


class FormatGbTests(unittest.TestCase):
    def test_integer_value(self):
        self.assertEqual(format_gb(8.0), "8")

    def test_fraction_value(self):
        self.assertEqual(format_gb(4.5), "4.5")

    def test_invalid(self):
        self.assertEqual(format_gb(None), "?")
        self.assertEqual(format_gb("abc"), "?")


class _FakeRuntimeManager:
    """记录调用的假 RuntimeManager。"""

    DEFAULT_PY_MIN = (3, 10)
    DEFAULT_PY_MAX = (3, 12)

    def __init__(self):
        self.calls = []
        self._py_list_cache = object()
        self._uv_cache = object()
        self._git_cache = None

    def discover_pythons(self, force=False):
        self.calls.append(("discover_pythons", force))
        return ["py1"]

    def resolve_uv(self, force=False):
        self.calls.append(("resolve_uv", force))
        return SimpleNamespace(found=True, display="uv 0.9")

    def resolve_base_python(self, min_ver=None, max_ver=None):
        self.calls.append(("resolve_base_python", min_ver, max_ver))
        return SimpleNamespace(display="python 3.12")

    def _version_ok(self, p, min_ver, max_ver):
        self.calls.append(("_version_ok", p))
        return True

    def get_pip_index_url(self):
        return "https://pypi.tuna.tsinghua.edu.cn/simple"

    def set_pip_index_url(self, url):
        self.calls.append(("set_pip_index_url", url))

    def get_model_env_status(self, model_id, packages=None, quick=False, force=False):
        self.calls.append(("get_model_env_status", model_id, quick, force))
        return SimpleNamespace(ready=True)


class RuntimeFacadeTests(unittest.TestCase):
    def test_delegates_and_keeps_kwargs(self):
        fake = _FakeRuntimeManager()
        facade = RuntimeFacade(manager=fake)

        self.assertEqual(facade.discover_pythons(force=True), ["py1"])
        self.assertIn(("discover_pythons", True), fake.calls)

        uv = facade.resolve_uv(force=True)
        self.assertTrue(uv.found)
        self.assertIn(("resolve_uv", True), fake.calls)

        # 版本区间判断委托 manager._version_ok（facade 封装私有访问）
        self.assertTrue(facade.version_ok(SimpleNamespace()))

        # 镜像源委托
        self.assertEqual(
            facade.get_pip_index_url(), "https://pypi.tuna.tsinghua.edu.cn/simple"
        )
        facade.set_pip_index_url("")
        self.assertIn(("set_pip_index_url", ""), fake.calls)

    def test_has_scan_cache(self):
        fake = _FakeRuntimeManager()
        facade = RuntimeFacade(manager=fake)
        # git 缓存缺失 → False
        self.assertFalse(facade.has_scan_cache())
        fake._git_cache = object()
        self.assertTrue(facade.has_scan_cache())

    def test_check_dev_ready(self):
        fake = _FakeRuntimeManager()
        facade = RuntimeFacade(manager=fake)
        ok, detail = facade.check_dev_ready()
        self.assertTrue(ok)
        self.assertEqual(detail, "开发环境已就绪")

    def test_model_env_status_kwargs_passthrough(self):
        fake = _FakeRuntimeManager()
        facade = RuntimeFacade(manager=fake)
        facade.get_model_env_status("ben2", quick=True)
        self.assertIn(
            ("get_model_env_status", "ben2", True, False), fake.calls
        )
        facade.get_model_env_status("ben2", force=True)
        self.assertIn(
            ("get_model_env_status", "ben2", False, True), fake.calls
        )


class _FakeMattingManager:
    def __init__(self):
        self.calls = []
        self._default = "ben2"
        self._device = "auto"

    def get_default_model_id(self):
        return self._default

    def set_default_model_id(self, mid):
        self.calls.append(("set_default_model_id", mid))
        self._default = mid

    def get_device_preference(self):
        return self._device

    def set_device_preference(self, dev):
        self.calls.append(("set_device_preference", dev))
        self._device = dev

    def is_ready(self, mid):
        return mid == "ben2"

    def status_text(self, mid):
        return "已就绪" if mid == "ben2" else "未下载"

    def default_local_dir(self, mid):
        return Path("models") / "matting" / mid

    def get_custom_path(self, mid):
        return ""

    def set_custom_path(self, mid, path):
        self.calls.append(("set_custom_path", mid, path))

    def download_async(self, mid, progress=None, finished=None):
        self.calls.append(("download_async", mid, bool(progress), bool(finished)))


class MattingConfigServiceTests(unittest.TestCase):
    def test_delegation(self):
        fake = _FakeMattingManager()
        svc = MattingConfigService(manager=fake)

        self.assertEqual(svc.get_default_model_id(), "ben2")
        svc.set_default_model_id("rmbg2")
        self.assertEqual(fake._default, "rmbg2")

        svc.set_device_preference("cuda")
        self.assertEqual(svc.get_device_preference(), "cuda")

        self.assertTrue(svc.is_ready("ben2"))
        self.assertFalse(svc.is_ready("rmbg2"))
        self.assertEqual(svc.status_text("rmbg2"), "未下载")
        self.assertEqual(
            svc.default_local_dir("ben2"), Path("models") / "matting" / "ben2"
        )

    def test_download_async_kwargs(self):
        fake = _FakeMattingManager()
        svc = MattingConfigService(manager=fake)
        svc.download_async("ben2", progress=lambda *a: None, finished=lambda *a: None)
        self.assertIn(("download_async", "ben2", True, True), fake.calls)

    def test_registry_helpers(self):
        svc = MattingConfigService(manager=_FakeMattingManager())
        models = svc.list_models()
        self.assertTrue(any(getattr(m, "id", None) == "ben2" for m in models))
        info = svc.get_model_info("ben2")
        self.assertEqual(info.id, "ben2")


if __name__ == "__main__":
    unittest.main()
