"""高清放大（Upscale）核心包 —— 纯逻辑，不 import ui / PySide6。

模块职责：
- ``engine_registry``  放大引擎注册表 + 每引擎独立参数 schema（ParamSpec）
- ``hardware_gate``    显卡/系统门禁（DLSS5 要求 RTX 40 系及以上）
- ``runtime_bundle``   外挂运行时（DLSS5 便携包）定位与完整性校验
- ``upscale_settings`` 引擎选择 / 运行时目录等本地设置（runtime/upscale_settings.json）
- ``dlss5``            DLSS5 引擎实现（多趟放大 / alpha 保留 / 尺寸门禁）
- ``dlss5_session``    DLSS5 原生 worker 的 stdin/stdout 二进制协议

设计约束（与 AI 抠图一致）：
- 主程序与打包 exe **不内嵌**任何 DLSS / ReShade / RenoDX 二进制
- 不引入 numpy / opencv 等新依赖：协议用标准库 struct，像素用 Pillow
"""
