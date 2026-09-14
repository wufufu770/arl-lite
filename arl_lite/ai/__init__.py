"""arl_lite.ai — AI 集成层(5 个边界点,无侵入)

5 个 AI 子命令:
- ai ask — 自然语言查询
- ai report — 报告生成
- ai explain — 关联解释
- ai suggest — 扫描建议
- ai fix — 修复建议

支持 4 个 provider:openai / anthropic / google / ollama
零外部 pip 依赖(纯 stdlib urllib + json + ssl)
无 API key 时自动 fallback 到离线模板(不假装有 AI)
"""
from .client import (
    AIError,
    CompletionResult,
    complete,
    is_configured,
)
from .config import (
    AIConfig,
    CONFIG_PATH,
    PROVIDERS,
    get_config,
    load_config,
    reset_config,
    save_config,
    set_config,
)

__all__ = [
    "AIError",
    "CompletionResult",
    "complete",
    "is_configured",
    "AIConfig",
    "CONFIG_PATH",
    "PROVIDERS",
    "get_config",
    "load_config",
    "reset_config",
    "save_config",
    "set_config",
]
