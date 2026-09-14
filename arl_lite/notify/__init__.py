"""arl_lite.notify

Webhook 通知 — Phase 7

设计:
- 3 种 provider: ntfy / Slack / 通用 URL
- 触发点: critical finding / 关联分析命中 / 任务完成
- 纯 stdlib(urllib),零依赖
- 失败 graceful(网络错不抛)
"""
from .webhook import (
    WebhookConfig,
    notify,
    notify_critical_finding,
    notify_correlation,
    notify_task_done,
    is_valid_url,
)

__all__ = [
    "WebhookConfig",
    "notify",
    "notify_critical_finding",
    "notify_correlation",
    "notify_task_done",
    "is_valid_url",
]
