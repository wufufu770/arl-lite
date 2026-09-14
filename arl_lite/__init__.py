"""arl-lite: 灯塔 ARL 降级增强版

2G 内存友好的资产侦察工具,CLI/TUI 形态,集成 AI 增强。
"""

__version__ = "0.7.7"
__author__ = "arl-lite contributors"
__license__ = "MIT"

# Phase 1 核心接口
from .core.base_module import BaseModule, SourceResult, ModuleResult
from .db.storage import Storage
from .core.signal_handler import GracefulShutdown

__all__ = [
    "BaseModule",
    "SourceResult",
    "ModuleResult",
    "Storage",
    "GracefulShutdown",
    "__version__",
]
