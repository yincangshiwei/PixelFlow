"""FeatureRegistry / catalog（六功能统一契约，无 QWidget 路径）。"""
import unittest

from services.contracts.feature_descriptor import InputKind
from services.features.catalog import (
    EXPECTED_FEATURE_ORDER,
    build_feature_registry,
    reset_default_catalog,
)
from services.features.registry import FeatureRegistry
from services.features.basic_service import BasicService, FEATURE_ID as BASIC_ID
from services.features.metadata_service import MetadataService, FEATURE_ID as METADATA_ID
from services.features.overlay_service import OverlayService, FEATURE_ID as OVERLAY_ID
from services.features.img2doc_service import Img2DocService, FEATURE_ID as IMG2DOC_ID
from services.features.transparent_service import (
    TransparentService,
    FEATURE_ID as TRANSPARENT_ID,
)
from services.features.upscale_service import UpscaleService, FEATURE_ID as UPSCALE_ID


class TestFeatureRegistry(unittest.TestCase):
    def test_register_unique_and_order(self):
        reg = FeatureRegistry()
        from services.contracts.feature_descriptor import FeatureDescriptor
        a = FeatureDescriptor(id="a", name="A")
        b = FeatureDescriptor(id="b", name="B")
        reg.register(a)
        reg.register(b)
        self.assertEqual(reg.ids(), ["a", "b"])
        with self.assertRaises(ValueError):
            reg.register(FeatureDescriptor(id="a", name="dup"))


class TestCatalog(unittest.TestCase):
    def setUp(self):
        reset_default_catalog()

    def tearDown(self):
        reset_default_catalog()

    def test_order_and_ids(self):
        reg, services = build_feature_registry()
        self.assertEqual(reg.ids(), EXPECTED_FEATURE_ORDER)
        self.assertEqual(set(services.keys()), set(EXPECTED_FEATURE_ORDER))

    def test_all_are_new_services(self):
        _reg, services = build_feature_registry()
        self.assertIsInstance(services[BASIC_ID], BasicService)
        self.assertIsInstance(services[METADATA_ID], MetadataService)
        self.assertIsInstance(services[OVERLAY_ID], OverlayService)
        self.assertIsInstance(services[IMG2DOC_ID], Img2DocService)
        self.assertIsInstance(services[TRANSPARENT_ID], TransparentService)
        self.assertIsInstance(services[UPSCALE_ID], UpscaleService)

    def test_batch_merged_resume_flags(self):
        reg, _ = build_feature_registry()
        for desc in reg:
            if desc.id in (IMG2DOC_ID, METADATA_ID):
                self.assertIs(desc.input_kind, InputKind.BATCH_MERGED)
                self.assertFalse(desc.supports_resume)
            else:
                self.assertTrue(desc.supports_resume)

    def test_metadata_selected_load(self):
        reg, _ = build_feature_registry()
        md = reg.require(METADATA_ID)
        self.assertTrue(md.supports_selected_load)
        basic = reg.require(BASIC_ID)
        self.assertFalse(basic.supports_selected_load)

    def test_build_run_options_all(self):
        _reg, services = build_feature_registry()
        for fid, svc in services.items():
            opts = svc.build_run_options(svc.default_state())
            self.assertIn("_output_format", opts, fid)
            self.assertIn("keep_matting", opts, fid)

    def test_no_route_factory_without_injection(self):
        reg, _ = build_feature_registry()
        for desc in reg:
            self.assertIsNone(desc.route_factory, desc.id)

    def test_route_factory_injection(self):
        sentinel = object()

        def factory():
            return sentinel

        reg, _ = build_feature_registry(
            route_factories={BASIC_ID: factory, METADATA_ID: factory},
        )
        self.assertIs(reg.require(BASIC_ID).route_factory, factory)
        self.assertIs(reg.require(METADATA_ID).route_factory, factory)
        self.assertIsNone(reg.require(OVERLAY_ID).route_factory)


if __name__ == "__main__":
    unittest.main()
