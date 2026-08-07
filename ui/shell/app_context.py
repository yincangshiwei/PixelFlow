"""AppContext —— 只读依赖容器（P5）。

仅在构造时注入依赖，供壳与路由读取；
- 不保存当前页面、当前控件或批处理等可变业务状态；
- 不提供字符串查找（Service Locator），避免隐式依赖。
"""
from __future__ import annotations


class AppContext:
    """组合根注入的只读依赖容器。"""

    def __init__(self, *, orchestrator, import_service, log_manager):
        """
        :param orchestrator: services.common.batch_orchestrator.BatchOrchestrator
        :param import_service: services.common.importing.FileImportService
        :param log_manager: core.log_manager.AppLogManager
        """
        self._orchestrator = orchestrator
        self._import_service = import_service
        self._log_manager = log_manager

    @property
    def orchestrator(self):
        return self._orchestrator

    @property
    def import_service(self):
        return self._import_service

    @property
    def log_manager(self):
        return self._log_manager
