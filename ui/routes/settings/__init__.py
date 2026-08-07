"""ui.routes.settings —— 配置中心路由（P5）。

SettingsRoute（配置壳）+ DevEnvRoute（开发环境）+ MattingModelRoute（抠图模型）。
"""
from .settings_route import SettingsRoute
from .dev_env_route import DevEnvRoute
from .matting_model_route import MattingModelRoute

__all__ = ["SettingsRoute", "DevEnvRoute", "MattingModelRoute"]
