"""高清放大（upscale）功能测试。

覆盖：引擎注册表 / 参数 schema 规范化 / DLSS5 尺寸与多趟规划 /
原生协议参数映射 / 显卡架构门禁 / 运行时布局识别 / Service 契约 / Processor 占位后端 /
运行时一键安装（zip 条目匹配与选择性提取的纯函数部分）。

不依赖 Qt、不依赖 DLSS5 二进制、不调用 nvidia-smi、不访问网络。
"""
from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from PIL import Image

from core.upscale import upscale_settings
from core.upscale.engine_registry import (
    DEFAULT_ENGINE_ID,
    DLSS5_ENGINE,
    DLSS5_MODEL_PRESETS,
    DLSS5_NR_STYLES,
    KIND_BOOL,
    KIND_COMBO,
    KIND_FLOAT,
    ParamSpec,
    UpscaleEngineInfo,
    all_engines_defaults,
    coerce_params,
    get_engine,
    list_engines,
    resolve_engine_id,
)
from core.upscale.hardware_gate import classify_gpu, pick_best_gpu, UpscaleGpu
from core.upscale.runtime_bundle import detect_layout, LAYOUT_COMPACT, LAYOUT_UPSTREAM


class TestEngineRegistry(unittest.TestCase):
    def test_dlss5_registered(self):
        self.assertIn("dlss5", [e.id for e in list_engines()])
        self.assertEqual(DEFAULT_ENGINE_ID, "dlss5")
        self.assertIs(get_engine("dlss5"), DLSS5_ENGINE)
        self.assertIsNone(get_engine("nope"))
        self.assertEqual(resolve_engine_id("nope"), DEFAULT_ENGINE_ID)

    def test_dlss5_gate_metadata(self):
        """用户要求：必须 40 系及以上、仅 Windows。"""
        self.assertEqual(DLSS5_ENGINE.min_gpu_generation, 40)
        self.assertEqual(DLSS5_ENGINE.gpu_vendor, "nvidia")
        self.assertEqual(DLSS5_ENGINE.platforms, frozenset({"win32"}))
        self.assertEqual(DLSS5_ENGINE.runtime_kind, "external_bundle")
        self.assertEqual(DLSS5_ENGINE.max_output_width, 7680)
        self.assertEqual(DLSS5_ENGINE.max_output_height, 4320)
        self.assertEqual(DLSS5_ENGINE.min_input_side, 64)
        self.assertTrue(DLSS5_ENGINE.license_note)

    def test_paramspec_validation(self):
        with self.assertRaises(ValueError):
            ParamSpec(key="", label="x")
        with self.assertRaises(ValueError):
            ParamSpec(key="k", label="x", kind="unknown")
        with self.assertRaises(ValueError):
            ParamSpec(key="k", label="x", kind=KIND_COMBO, choices=())
        with self.assertRaises(ValueError):
            ParamSpec(key="k", label="x", kind=KIND_FLOAT, lo=2.0, hi=1.0)

    def test_engine_rejects_duplicate_keys(self):
        spec = ParamSpec(key="a", label="A", kind=KIND_BOOL, default=False)
        with self.assertRaises(ValueError):
            UpscaleEngineInfo(id="dup", name="dup", params=(spec, spec))

    def test_clamp(self):
        f = DLSS5_ENGINE.param("nr_intensity")
        self.assertEqual(f.clamp(5.0), 2.0)
        self.assertEqual(f.clamp(-1), 0.0)
        self.assertEqual(f.clamp("bad"), f.default)
        c = DLSS5_ENGINE.param("nr_style")
        # 显示文本会被收敛为对应的实际值（UI 回填 / 手写预设兼容）
        self.assertEqual(c.clamp("Cinematic"), 2)
        self.assertEqual(c.clamp("Natural"), 1)
        self.assertEqual(c.clamp("nope"), c.default)
        b = DLSS5_ENGINE.param("automatic_mask")
        self.assertIs(b.clamp(1), True)

    def test_groups_and_rows(self):
        groups = dict(DLSS5_ENGINE.groups(advanced=False))
        self.assertIn("放大倍率", groups)
        self.assertIn("神经渲染 (DLSS 5 NR)", groups)
        self.assertIn("细节与色调", groups)
        self.assertIn("DLSS 模型预设", groups)
        # 高级参数不出现在常规分组
        for _name, specs in DLSS5_ENGINE.groups(advanced=False):
            for s in specs:
                self.assertFalse(s.advanced, s.key)
        adv = dict(DLSS5_ENGINE.groups(advanced=True))
        self.assertIn("高级", adv)
        self.assertIn("warmup_frames", [s.key for s in adv["高级"]])
        # 宽屏布局：放大倍率三个参数同一行
        row0 = DLSS5_ENGINE.rows("放大倍率")[0]
        self.assertEqual({s.key for s in row0},
                         {"scale_mode", "use_multi_pass", "target_scale"})

    def test_defaults_cover_all_params(self):
        defaults = DLSS5_ENGINE.defaults()
        self.assertEqual(set(defaults), {p.key for p in DLSS5_ENGINE.params})
        # 用户确认：NR 参数保持上游默认值 1.0 / Default
        self.assertEqual(defaults["nr_intensity"], 1.0)
        self.assertEqual(defaults["nr_style"], "Default")
        self.assertEqual(defaults["local_tone_strength"], 1.0)
        self.assertEqual(defaults["local_structure_strength"], 1.0)
        self.assertEqual(defaults["skin_structure_strength"], -1.0)
        self.assertEqual(defaults["dlss_model_preset"], "Default")
        self.assertFalse(defaults["automatic_mask"])
        self.assertEqual(defaults["warmup_frames"], 0)
        self.assertIn("dlss5", all_engines_defaults())

    def test_coerce_params_strict(self):
        values, errors = coerce_params(
            DLSS5_ENGINE, {"nr_style": "nope"}, strict=True
        )
        self.assertTrue(errors)
        self.assertEqual(values["nr_style"], "Default")

        values, errors = coerce_params(
            DLSS5_ENGINE, {"nr_intensity": 99}, strict=True
        )
        self.assertTrue(any("NR 强度" in e for e in errors))
        self.assertEqual(values["nr_intensity"], 2.0)

        # 非严格：静默收敛、无错误
        values, errors = coerce_params(
            DLSS5_ENGINE, {"nr_intensity": 99, "nr_style": "nope"}, strict=False
        )
        self.assertEqual(errors, [])
        self.assertEqual(values["nr_intensity"], 2.0)

        # 完整合法输入原样通过
        good = {"scale_mode": "quality", "nr_intensity": 0.5, "target_scale": 3.0}
        values, errors = coerce_params(DLSS5_ENGINE, good, strict=True)
        self.assertEqual(errors, [])
        self.assertEqual(values["scale_mode"], "quality")
        self.assertEqual(values["nr_intensity"], 0.5)


class TestDLSS5Sizing(unittest.TestCase):
    def setUp(self):
        from core.upscale import dlss5
        self.m = dlss5

    def test_nearest_even(self):
        self.assertEqual(self.m.nearest_even(1), 2)
        self.assertEqual(self.m.nearest_even(3), 4)
        self.assertEqual(self.m.nearest_even(1080), 1080)
        self.assertEqual(self.m.nearest_even(1079), 1080)

    def test_output_size(self):
        self.assertEqual(self.m.output_size_for(1920, 1080, "performance"), (3840, 2160))
        self.assertEqual(self.m.output_size_for(1000, 1000, "dlaa"), (1000, 1000))
        self.assertEqual(self.m.output_size_for(1000, 1000, "quality"), (1500, 1500))
        with self.assertRaises(Exception):
            self.m.output_size_for(100, 100, "nope")

    def test_boundary(self):
        self.assertFalse(self.m.exceeds_boundary(7680, 4320))
        self.assertTrue(self.m.exceeds_boundary(7682, 4320))
        self.assertTrue(self.m.exceeds_boundary(4322, 7680))   # 短边超限

    def test_plan_passes(self):
        self.assertEqual(self.m.plan_passes(1.0), (("dlaa",), 1.0))
        self.assertEqual(self.m.plan_passes(2.0)[0], ("performance",))
        self.assertEqual(self.m.plan_passes(3.0)[0], ("ultra_performance",))
        # 4× = 2× 两趟（上游没有 4× 档，这是本项目的增值点）
        modes, achieved = self.m.plan_passes(4.0)
        self.assertEqual(modes, ("performance", "performance"))
        self.assertAlmostEqual(achieved, 4.0)
        modes, achieved = self.m.plan_passes(8.0)
        self.assertEqual(modes, ("performance",) * 3)
        self.assertAlmostEqual(achieved, 8.0)
        self.assertLessEqual(len(self.m.plan_passes(6.0)[0]), self.m.MAX_PASSES)

    def test_plan_passes_descending_order(self):
        """趟次按倍率降序：截断时保住更大倍数，且末趟是画质更好的 2× 档。"""
        modes, achieved = self.m.plan_passes(6.0)
        self.assertEqual(modes, ("ultra_performance", "performance"))
        self.assertAlmostEqual(achieved, 6.0)
        factors = [self.m.DLSS5_SCALE_FACTOR[m] for m in modes]
        self.assertEqual(factors, sorted(factors, reverse=True))

    def test_descending_order_gains_under_cap(self):
        """1080p 请求 6×：降序先跑 3× 得 5760×3240，截断后仍保住 3×。"""
        executed, w, h, truncated, achieved = self.m.resolve_plan(
            1920, 1080, {"use_multi_pass": True, "target_scale": 6.0}
        )
        self.assertEqual(executed, ["ultra_performance"])
        self.assertEqual((w, h), (5760, 3240))
        self.assertTrue(truncated)
        self.assertAlmostEqual(achieved, 3.0)

    def test_resolve_plan_single(self):
        executed, w, h, truncated, achieved = self.m.resolve_plan(
            1920, 1080, {"use_multi_pass": False, "scale_mode": "performance"}
        )
        self.assertEqual(executed, ["performance"])
        self.assertEqual((w, h), (3840, 2160))
        self.assertFalse(truncated)
        self.assertAlmostEqual(achieved, 2.0)

    def test_resolve_plan_multi_1080p_to_8k(self):
        """1080p 4× 恰好落在 7680×4320 上限内，两趟全部执行。"""
        executed, w, h, truncated, achieved = self.m.resolve_plan(
            1920, 1080, {"use_multi_pass": True, "target_scale": 4.0}
        )
        self.assertEqual(executed, ["performance", "performance"])
        self.assertEqual((w, h), (7680, 4320))
        self.assertFalse(truncated)
        self.assertAlmostEqual(achieved, 4.0)

    def test_resolve_plan_truncated_by_cap(self):
        """4K 源请求 4×：第二趟越界，自动截断为一趟并回算实际倍数。"""
        executed, w, h, truncated, achieved = self.m.resolve_plan(
            3840, 2160, {"use_multi_pass": True, "target_scale": 4.0}
        )
        self.assertEqual(executed, ["performance"])
        self.assertEqual((w, h), (7680, 4320))
        self.assertTrue(truncated)
        self.assertAlmostEqual(achieved, 2.0)

    def test_resolve_plan_rejects_small_and_over_cap(self):
        with self.assertRaises(Exception):
            self.m.resolve_plan(32, 32, {"scale_mode": "performance"})
        with self.assertRaises(Exception):
            self.m.resolve_plan(5000, 4000, {"use_multi_pass": False,
                                             "scale_mode": "performance"})

    def test_native_settings_mapping(self):
        s = self.m.native_settings_from_params(
            {
                "nr_style": "Cinematic", "nr_preset": "Preset #2",
                "dlss_model_preset": "L", "automatic_mask": True,
                "nr_intensity": 1.25, "local_tone_strength": 0.8,
                "local_structure_strength": 1.1, "skin_structure_strength": -1.0,
                "warmup_frames": 2,
            },
            "performance",
        )
        self.assertEqual(s.perf_quality, 0)
        self.assertEqual(s.style, 2)
        self.assertEqual(s.preset, 2)
        self.assertEqual(s.dlss_model_preset, 12)
        self.assertEqual(s.auto_mask, 1)
        self.assertAlmostEqual(s.intensity, 1.25)
        self.assertAlmostEqual(s.local_tone, 0.8)
        self.assertAlmostEqual(s.local_structure, 1.1)
        self.assertAlmostEqual(s.skin_structure, -1.0)
        self.assertEqual(s.warmup_frames, 2)
        # DLAA 的 perf_quality 是 5，不是 0
        self.assertEqual(self.m.native_settings_from_params({}, "dlaa").perf_quality, 5)

    def test_describe_plan_text(self):
        text = self.m.describe_plan_text(
            1920, 1080, {"use_multi_pass": True, "target_scale": 4.0}
        )
        self.assertIn("7680×4320", text)
        self.assertIn("2 趟", text)
        self.assertIn("⚠", self.m.describe_plan_text(32, 32, {}))


class TestProtocolConstants(unittest.TestCase):
    """协议常量与上游 version-4 model-preset protocol 对齐。"""

    def test_magics_and_struct_sizes(self):
        import struct
        from core.upscale import dlss5_session as s
        self.assertEqual(s.VIDEO_MAGIC, 0x34563544)
        self.assertEqual(s.SETUP_MAGIC, 0x34505553)
        self.assertEqual(s.FRAME_MAGIC, 0x314D5246)
        self.assertEqual(s.OUT_MAGIC, 0x3154554F)
        self.assertEqual(s.VIDEO_HEADER_SIZE, struct.calcsize("<14I4f"))
        self.assertEqual(s.SETUP_RESPONSE_SIZE, struct.calcsize("<12I"))
        self.assertEqual(s.FRAME_HEADER_SIZE, struct.calcsize("<4Iq"))
        self.assertEqual(s.OUT_HEADER_SIZE, struct.calcsize("<5Iq"))

    def test_header_roundtrip(self):
        import struct
        from core.upscale import dlss5_session as s
        settings = s.NativeSettings(
            perf_quality=0, dlss_model_preset=12, preset=1, style=2,
            auto_mask=1, intensity=1.5, local_tone=0.5,
            local_structure=1.0, skin_structure=-1.0, warmup_frames=0,
        )
        blob = struct.pack(
            s.VIDEO_HEADER_FORMAT,
            s.VIDEO_MAGIC, 1920, 1080, 3840, 2160, 0, 1,
            settings.perf_quality, settings.dlss_model_preset,
            settings.profile, settings.preset, settings.style,
            settings.auto_mask, settings.ui_correction,
            settings.intensity, settings.local_tone,
            settings.local_structure, settings.skin_structure,
        )
        self.assertEqual(len(blob), s.VIDEO_HEADER_SIZE)
        unpacked = struct.unpack(s.VIDEO_HEADER_FORMAT, blob)
        self.assertEqual(unpacked[0], s.VIDEO_MAGIC)
        self.assertEqual(unpacked[1:5], (1920, 1080, 3840, 2160))
        self.assertEqual(unpacked[8], 12)
        self.assertAlmostEqual(unpacked[14], 1.5)

    def test_feature_18_evidence(self):
        from core.upscale.dlss5_session import FEATURE_18_EVIDENCE
        keys = [k for k, _ in FEATURE_18_EVIDENCE]
        self.assertEqual(keys, ["runtime", "created", "evaluated"])


class TestGpuClassification(unittest.TestCase):
    """40 系门禁的架构分类（纯函数，不调用 nvidia-smi）。"""

    def test_by_compute_capability(self):
        cases = {
            (8, 9): ("Ada", 40),          # RTX 40 系
            (12, 0): ("Blackwell", 50),   # RTX 50 系
            (12, 1): ("Blackwell", 50),
            (10, 0): ("Blackwell", 50),
            (8, 6): ("Ampere", 30),       # RTX 30 系 → 阻断
            (8, 0): ("Ampere", 30),
            (7, 5): ("Turing", 20),       # RTX 20 系 → 阻断
            (9, 0): ("Hopper", 0),        # H100 数据中心卡 → 非 RTX
        }
        for (major, minor), (arch, gen) in cases.items():
            got_arch, got_gen, _rtx, method = classify_gpu(
                name="NVIDIA GeForce RTX", compute_major=major, compute_minor=minor
            )
            self.assertEqual((got_arch, got_gen), (arch, gen), f"cc {major}.{minor}")
            self.assertEqual(method, "cc")

    def test_name_fallback(self):
        _a, gen, is_rtx, method = classify_gpu(name="NVIDIA GeForce RTX 4070 Ti Super")
        self.assertEqual(gen, 40)
        self.assertTrue(is_rtx)
        self.assertEqual(method, "name")

        _a, gen, _r, _m = classify_gpu(name="NVIDIA GeForce RTX 3060")
        self.assertEqual(gen, 30)

        _a, gen, _r, _m = classify_gpu(name="NVIDIA GeForce RTX 5090")
        self.assertEqual(gen, 50)

    def test_arch_keyword_fallback(self):
        _a, gen, _r, method = classify_gpu(name="NVIDIA RTX 6000 Ada Generation")
        self.assertEqual(gen, 40)
        self.assertEqual(method, "arch_keyword")

    def test_non_rtx_rejected(self):
        _arch, gen, is_rtx, _m = classify_gpu(
            name="NVIDIA A100-SXM4-40GB", compute_major=8, compute_minor=0
        )
        self.assertFalse(is_rtx)
        self.assertEqual(gen, 30)

    def test_unknown(self):
        arch, gen, is_rtx, method = classify_gpu(name="NVIDIA Mystery Card")
        self.assertEqual(arch, "")
        self.assertEqual(gen, 0)
        self.assertFalse(is_rtx)
        self.assertEqual(method, "none")

    def test_pick_best_gpu_multi_card(self):
        """双卡机器（30 系 + 40 系）必须按世代最高者判定，否则会误拒。"""
        gpus = (
            UpscaleGpu(index=0, name="RTX 3060", generation=30, memory_mb=12288),
            UpscaleGpu(index=1, name="RTX 4080", generation=40, memory_mb=16384),
        )
        best = pick_best_gpu(gpus)
        self.assertEqual(best.generation, 40)
        self.assertEqual(best.name, "RTX 4080")
        self.assertIsNone(pick_best_gpu(()))


class TestRuntimeBundleLayout(unittest.TestCase):
    def test_detect_layout(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual(detect_layout(root), "")

            # 精简布局：<root>/host/nvngx.dll
            (root / "host").mkdir(parents=True)
            (root / "host" / "nvngx.dll").write_bytes(b"x")
            self.assertEqual(detect_layout(root), LAYOUT_COMPACT)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            host = root / "bin" / "runtime" / "host"
            host.mkdir(parents=True)
            (host / "nvngx.dll").write_bytes(b"x")
            self.assertEqual(detect_layout(root), LAYOUT_UPSTREAM)

    def test_inspect_missing_dir(self):
        from core.upscale.runtime_bundle import inspect_bundle
        st = inspect_bundle(Path(tempfile.gettempdir()) / "__pf_no_such_dlss5__")
        self.assertFalse(st.found)
        self.assertFalse(st.ready)
        self.assertTrue(st.problems)

    def test_inspect_flat_layout_rejected(self):
        """旧扁平布局必须被明确拒绝（与上游 validate_runtime_files 一致）。"""
        from core.upscale.runtime_bundle import inspect_bundle
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ("nvngx.dll", "dxgi.dll", "nvngx_dlssnr.dll"):
                (root / name).write_bytes(b"x")
            st = inspect_bundle(root)
            self.assertFalse(st.found)
            self.assertIn("扁平布局", st.detail)

    def test_inspect_incomplete(self):
        from core.upscale.runtime_bundle import REQUIRED_FILES, inspect_bundle
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # 只放前两个文件
            for rel, _label, _lic in REQUIRED_FILES[:2]:
                p = root / Path(rel)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"x")
            st = inspect_bundle(root)
            self.assertTrue(st.found)
            self.assertEqual(st.layout, LAYOUT_COMPACT)
            self.assertFalse(st.complete)
            self.assertEqual(len(st.missing), len(REQUIRED_FILES) - 2)
            self.assertFalse(st.ready)

    def test_writable_check(self):
        from core.upscale.runtime_bundle import is_dir_writable
        with tempfile.TemporaryDirectory() as td:
            self.assertTrue(is_dir_writable(Path(td)))
            self.assertTrue(is_dir_writable(Path(td) / "sub" / "deep"))


class TestUpscaleService(unittest.TestCase):
    def setUp(self):
        from services.features.upscale_service import UpscaleService
        self.svc = UpscaleService()

    def test_descriptor(self):
        from services.contracts.feature_descriptor import InputKind
        d = self.svc.descriptor
        self.assertEqual(d.id, "upscale")
        self.assertEqual(d.preset_schema_version, 1)
        self.assertIs(d.input_kind, InputKind.IMAGE)
        self.assertTrue(d.supports_resume)
        self.assertFalse(d.supports_selected_load)
        self.assertIsNone(self.svc.load_selected("x.png"))

    def test_default_state_namespaced(self):
        state = self.svc.default_state()
        self.assertEqual(state["engine"], "dlss5")
        self.assertIn("dlss5", state["engines"])
        self.assertEqual(state["output_format"], "png")
        self.assertEqual(state["quality"], 95)
        self.assertEqual(
            set(state["engines"]["dlss5"]), {p.key for p in DLSS5_ENGINE.params}
        )

    def test_validate_strict_rejects_bad(self):
        r = self.svc.validate_and_normalize(
            {"engine": "dlss5", "engines": {"dlss5": {"nr_style": "nope"}}}, strict=True
        )
        self.assertFalse(r.ok)
        self.assertTrue(r.errors)

    def test_validate_normalizes_and_clamps(self):
        r = self.svc.validate_and_normalize(
            {
                "engine": "dlss5",
                "engines": {"dlss5": {"nr_intensity": 99, "scale_mode": "quality"}},
                "output_format": "JPEG",
                "quality": 500,
            },
            strict=False,
        )
        self.assertTrue(r.ok)
        self.assertEqual(r.value["engines"]["dlss5"]["nr_intensity"], 2.0)
        self.assertEqual(r.value["engines"]["dlss5"]["scale_mode"], "quality")
        self.assertEqual(r.value["output_format"], "jpeg")
        self.assertEqual(r.value["quality"], 100)

    def test_preset_keeps_other_engine_namespace(self):
        """切换引擎不丢参数：预设按引擎命名空间存放。"""
        state = self.svc.default_state()
        state["engines"]["dlss5"]["nr_intensity"] = 0.42
        normalized = self.svc.normalize_preset(state)
        self.assertAlmostEqual(normalized["engines"]["dlss5"]["nr_intensity"], 0.42)

    def test_flat_preset_compat(self):
        """手写/旧预设把参数平铺在顶层时也能读进来。"""
        normalized = self.svc.normalize_preset(
            {"engine": "dlss5", "nr_intensity": 0.3, "scale_mode": "dlaa"}
        )
        self.assertAlmostEqual(normalized["engines"]["dlss5"]["nr_intensity"], 0.3)
        self.assertEqual(normalized["engines"]["dlss5"]["scale_mode"], "dlaa")

    def test_build_run_options_png(self):
        opts = self.svc.build_run_options(self.svc.default_state())
        self.assertEqual(opts["_output_format"], "png")
        self.assertFalse(opts["keep_matting"])
        self.assertFalse(opts["enable_compress"])

    def test_build_run_options_jpg_uses_worker_quality_channel(self):
        """JPG/WEBP 复用 Worker 既有保存质量通道。"""
        state = self.svc.default_state()
        state["output_format"] = "jpg"
        state["quality"] = 80
        opts = self.svc.build_run_options(state)
        self.assertEqual(opts["_output_format"], "jpg")
        self.assertTrue(opts["enable_compress"])
        self.assertEqual(opts["compress_mode"], "quality")
        self.assertEqual(opts["quality"], 80)

    def test_multi_pass_requires_target_above_one(self):
        r = self.svc.validate_and_normalize(
            {"engine": "dlss5",
             "engines": {"dlss5": {"use_multi_pass": True, "target_scale": 0.5}}},
            strict=True,
        )
        self.assertFalse(r.ok)

    def test_format_start_log(self):
        state = self.svc.default_state()
        state["engines"]["dlss5"]["use_multi_pass"] = True
        state["engines"]["dlss5"]["target_scale"] = 4.0
        text = self.svc.format_start_log(state)
        self.assertIn("DLSS 5", text)
        self.assertIn("多趟放大", text)
        self.assertIn("4", text)


class TestUpscaleProcessor(unittest.TestCase):
    def setUp(self):
        from core.processors.upscale_processor import UpscaleProcessor
        self.proc = UpscaleProcessor()

    def test_metadata(self):
        self.assertEqual(self.proc.preset_id, "upscale")
        self.assertEqual(self.proc.name, "高清放大")
        self.assertEqual(self.proc.icon, "🔍")
        self.assertFalse(self.proc.is_batch_processor)

    def test_no_ui_api(self):
        for attr in ("create_panel", "gather_options", "apply_options",
                     "get_output_format", "on_selected_image"):
            self.assertFalse(hasattr(self.proc, attr), attr)

    def test_default_options_match_service(self):
        from services.features.upscale_service import UpscaleService
        self.assertEqual(self.proc.default_options(), UpscaleService().default_state())

    def test_unknown_engine_raises(self):
        with self.assertRaises(ValueError):
            self.proc.process(Image.new("RGB", (64, 64)), {"engine": "nope"})

    def test_placeholder_backend_end_to_end(self):
        """占位后端：不加载 DLSS，验证 process() 契约与 Alpha 保留。"""
        opts = self.proc.default_options()
        opts["engines"]["dlss5"]["use_multi_pass"] = False
        opts["engines"]["dlss5"]["scale_mode"] = "performance"

        # DLSS 要求输入宽高都 ≥64，故用 80×64
        img = Image.new("RGBA", (80, 64), (200, 30, 30, 128))
        with mock.patch.object(upscale_settings, "get_placeholder_backend", return_value=True):
            out, details = self.proc.process(img, opts)

        self.assertEqual(out.size, (160, 128))
        self.assertEqual(details["backend"], "placeholder(LANCZOS)")
        self.assertEqual(details["engine"], "dlss5")
        self.assertEqual(details["output_size"], "160×128")
        self.assertIn("warning", details)
        # Alpha 通道保留
        self.assertEqual(out.mode, "RGBA")
        self.assertEqual(out.split()[-1].getextrema(), (128, 128))

    def test_placeholder_jpg_flattens_alpha(self):
        opts = self.proc.default_options()
        opts["output_format"] = "jpg"
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        with mock.patch.object(upscale_settings, "get_placeholder_backend", return_value=True):
            out, details = self.proc.process(img, opts)
        self.assertEqual(out.mode, "RGB")
        self.assertEqual(details["mode_converted"], "RGBA→RGB (白底合并)")

    def test_native_backend_blocked_without_runtime(self):
        """未就绪时必须给出可读原因，绝不静默降级成普通缩放。"""
        from core.upscale.dlss5 import DLSS5Error
        opts = self.proc.default_options()
        img = Image.new("RGB", (64, 64), (10, 20, 30))
        with mock.patch.object(upscale_settings, "get_placeholder_backend", return_value=False):
            with mock.patch(
                "core.upscale.dlss5.check_engine"
            ) as m_check, mock.patch(
                "core.upscale.dlss5.inspect_bundle"
            ) as m_bundle:
                from core.upscale.hardware_gate import EngineAvailability
                m_check.return_value = EngineAvailability(
                    engine_id="dlss5", usable=False, level="blocked",
                    summary="显卡为 RTX 30 系", reasons=("显卡为 RTX 30 系，需要 40 系及以上",),
                )
                m_bundle.return_value = None
                with self.assertRaises(DLSS5Error) as ctx:
                    self.proc.process(img, opts)
        self.assertIn("40 系", str(ctx.exception))


class TestSessionPoolHelpers(unittest.TestCase):
    def test_settings_key_stable(self):
        from core.upscale.dlss5 import _settings_key
        from core.upscale.dlss5_session import NativeSettings
        a = NativeSettings(intensity=1.0)
        b = NativeSettings(intensity=1.0)
        c = NativeSettings(intensity=1.5)
        self.assertEqual(_settings_key(a), _settings_key(b))
        self.assertNotEqual(_settings_key(a), _settings_key(c))

    def test_shutdown_is_safe_when_empty(self):
        from core.upscale.dlss5 import (
            clear_cancel, get_cancel_event, request_cancel,
            shutdown_upscale_sessions,
        )
        shutdown_upscale_sessions()
        clear_cancel()
        self.assertFalse(get_cancel_event().is_set())
        request_cancel()
        self.assertTrue(get_cancel_event().is_set())
        clear_cancel()
        self.assertFalse(get_cancel_event().is_set())

    def test_model_preset_table_matches_native_runtime(self):
        self.assertEqual(dict(DLSS5_MODEL_PRESETS),
                         {"Default": 0, "J": 10, "K": 11, "L": 12, "M": 13})
        self.assertEqual(dict(DLSS5_NR_STYLES),
                         {"Default": 0, "Natural": 1, "Cinematic": 2})


class TestBundleInstallerPureFunctions(unittest.TestCase):
    """一键安装器的纯函数部分（离线，不访问网络）。"""

    def _required_rels(self):
        from core.upscale.runtime_bundle import REQUIRED_FILES
        return [rel for rel, _label, _lic in REQUIRED_FILES]

    def test_match_entries_upstream_layout_with_prefix(self):
        """便携包原始结构：条目可带任意顶层目录前缀，含 bin/runtime/ 段。"""
        from core.upscale.bundle_installer import match_entries
        rels = self._required_rels()
        names = [
            "DLSS5.Runtime.v5.0/readme.txt",
            "DLSS5.Runtime.v5.0/bin/runtime/host/nvngx.dll",
            "DLSS5.Runtime.v5.0/bin/runtime/host/dxgi.dll",
            "DLSS5.Runtime.v5.0/bin/runtime/host/renodx-dlss5.addon64",
            "DLSS5.Runtime.v5.0/bin/runtime/host/nvngx_dlssnr.dll",
            "DLSS5.Runtime.v5.0/bin/runtime/dlss/nvngx_dlss.dll",
            "DLSS5.Runtime.v5.0/bin/ffmpeg/bin/ffmpeg.exe",
            "DLSS5.Runtime.v5.0/bin/python-3.13-embed/",
        ]
        matched = match_entries(names)
        self.assertEqual(sorted(matched.keys()), sorted(rels))
        self.assertEqual(
            matched["host/nvngx.dll"],
            "DLSS5.Runtime.v5.0/bin/runtime/host/nvngx.dll",
        )
        self.assertEqual(
            matched["dlss/nvngx_dlss.dll"],
            "DLSS5.Runtime.v5.0/bin/runtime/dlss/nvngx_dlss.dll",
        )
        # ffmpeg / 内置 Python 不在匹配结果里
        self.assertNotIn("bin/ffmpeg/bin/ffmpeg.exe", matched.values())

    def test_match_entries_compact_layout(self):
        """自制 zip：只有 host/ + dlss/，直接以 /rel 结尾。"""
        from core.upscale.bundle_installer import match_entries
        rels = self._required_rels()
        names = ["readme.txt"] + rels
        matched = match_entries(names)
        self.assertEqual(sorted(matched.keys()), sorted(rels))
        self.assertEqual(matched["host/nvngx.dll"], "host/nvngx.dll")

    def test_match_entries_backslash_and_dirs_ignored(self):
        from core.upscale.bundle_installer import match_entries
        names = [
            "pkg\\bin\\runtime\\host\\nvngx.dll",       # 反斜杠
            "pkg/bin/runtime/docs/",                     # 目录条目
            "",
            "pkg/bin/runtime/host/nvngx.dll.bak",        # 部分匹配不算
        ]
        matched = match_entries(names)
        self.assertEqual(matched, {"host/nvngx.dll": "pkg\\bin\\runtime\\host\\nvngx.dll"})

    def test_match_entries_missing(self):
        from core.upscale.bundle_installer import match_entries
        matched = match_entries(["only/readme.txt"])
        self.assertEqual(matched, {})

    def test_pick_asset_rules(self):
        from core.upscale.bundle_installer import _pick_asset
        # 含 dlss 的 zip 优先
        assets = [
            {"name": "screenshots.zip", "state": "uploaded", "size": 1},
            {"name": "DLSS5.Runtime.v5.0.zip", "state": "uploaded", "size": 459},
        ]
        self.assertEqual(_pick_asset(assets)["name"], "DLSS5.Runtime.v5.0.zip")
        # 唯一 zip
        self.assertEqual(
            _pick_asset([{"name": "runtime.zip", "state": "uploaded"}])["name"],
            "runtime.zip",
        )
        # 无 zip / 未上传完成 / 多个不含 dlss 的 zip
        self.assertIsNone(_pick_asset([{"name": "a.exe", "state": "uploaded"}]))
        self.assertIsNone(_pick_asset(
            [{"name": "x.zip", "state": "starter"}]      # GitHub 上传中状态
        ))
        self.assertIsNone(_pick_asset([
            {"name": "a.zip", "state": "uploaded"},
            {"name": "b.zip", "state": "uploaded"},
        ]))

    def test_find_release_asset_uses_fixed_direct_url(self):
        """config 固定直链存在时：零网络请求直接返回（离线可测）。"""
        from core.upscale.bundle_installer import find_release_asset
        asset = find_release_asset()
        self.assertEqual(asset.name, "DLSS5.Runtime.v5.0.zip")
        self.assertEqual(
            asset.url,
            "https://github.com/yincangshiwei/PixelFlow/releases/download/"
            "DISS5/DLSS5.Runtime.v5.0.zip",
        )
        self.assertEqual(asset.release_tag, "DISS5")

    def _make_runtime_zip(self, td: str, prefix: str = "") -> Path:
        rels = self._required_rels()
        zpath = Path(td) / "runtime.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            for rel in rels:
                arcname = f"{prefix}bin/runtime/{rel}" if prefix else rel
                zf.writestr(arcname, f"content-of-{rel}")
            zf.writestr(f"{prefix}bin/ffmpeg/bin/ffmpeg.exe", "fake-ffmpeg")
            zf.writestr("readme.txt", "hello")
        return zpath

    def test_extract_required_upstream_zip(self):
        """从带前缀的便携包 zip 提取：只落 5 个文件，compact 布局，无多余目录。"""
        from core.upscale.bundle_installer import extract_required
        with tempfile.TemporaryDirectory() as td:
            zpath = self._make_runtime_zip(td, prefix="DLSS5.Runtime.v5.0/")
            dest = Path(td) / "out"
            root, done = extract_required(zpath, dest)
            self.assertEqual(root, dest)
            self.assertEqual(
                sorted(done),
                sorted(self._required_rels()),
            )
            self.assertEqual(
                (dest / "host" / "nvngx.dll").read_text(encoding="utf-8"),
                "content-of-host/nvngx.dll",
            )
            # ffmpeg 与 readme 不被提取
            self.assertFalse((dest / "bin").exists())
            self.assertFalse((dest / "readme.txt").exists())
            # 目录树里只有 host/ 与 dlss/ 两组
            top = sorted(p.name for p in dest.iterdir())
            self.assertEqual(top, ["dlss", "host"])

    def test_extract_required_compact_zip(self):
        from core.upscale.bundle_installer import extract_required
        with tempfile.TemporaryDirectory() as td:
            zpath = self._make_runtime_zip(td)   # 无前缀，顶层即 host/ + dlss/
            dest = Path(td) / "out"
            _root, done = extract_required(zpath, dest)
            self.assertEqual(len(done), 5)
            self.assertTrue((dest / "dlss" / "nvngx_dlss.dll").is_file())

    def test_extract_required_missing_raises(self):
        from core.upscale.bundle_installer import BundleInstallError, extract_required
        with tempfile.TemporaryDirectory() as td:
            zpath = Path(td) / "bad.zip"
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("host/nvngx.dll", "x")
                zf.writestr("readme.txt", "y")
            with self.assertRaises(BundleInstallError) as ctx:
                extract_required(zpath, Path(td) / "out")
            self.assertIn("缺少必需文件", str(ctx.exception))

    def test_extract_reports_progress(self):
        from core.upscale.bundle_installer import extract_required
        events: list = []
        with tempfile.TemporaryDirectory() as td:
            zpath = self._make_runtime_zip(td)
            extract_required(
                zpath, Path(td) / "out",
                progress=lambda s, p, m: events.append((s, p, m)),
            )
        self.assertTrue(events)
        stages = {e[0] for e in events}
        self.assertIn("extract", stages)
        self.assertTrue(all(60 <= e[1] <= 90 for e in events))
        self.assertTrue(events[-1][1] >= events[0][1])   # 进度单调不减

    def test_extract_result_passes_bundle_inspection(self):
        """提取结果应能被 inspect_bundle 识别为完整可用的 compact 布局。"""
        from core.upscale.bundle_installer import extract_required
        from core.upscale.runtime_bundle import inspect_bundle
        with tempfile.TemporaryDirectory() as td:
            zpath = self._make_runtime_zip(td)
            dest = Path(td) / "out"
            extract_required(zpath, dest)
            st = inspect_bundle(dest)
            self.assertTrue(st.found)
            self.assertTrue(st.complete)
            self.assertEqual(st.layout, "compact")
            self.assertEqual(len(st.files), 5)


if __name__ == "__main__":
    unittest.main()
