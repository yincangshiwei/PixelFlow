"""功能级 FeatureRoute（每功能一个）。"""
from .basic_route import BasicFeatureRoute
from .metadata_route import MetadataFeatureRoute
from .overlay_route import OverlayFeatureRoute
from .img2doc_route import Img2DocFeatureRoute
from .transparent_route import TransparentFeatureRoute

__all__ = [
    "BasicFeatureRoute",
    "MetadataFeatureRoute",
    "OverlayFeatureRoute",
    "Img2DocFeatureRoute",
    "TransparentFeatureRoute",
]
