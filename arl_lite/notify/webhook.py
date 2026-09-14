"""arl_lite.notify.webhook

Webhook 通知核心实现(纯 stdlib)。

支持的 provider:
- ntfy:  https://ntfy.sh/<topic>  (无认证,简单)
- slack: https://hooks.slack.com/services/...  (Slack incoming webhook)
- generic: 任何接受 POST JSON 的 URL
- local:  只记录日志,不发 HTTP (dry-run)

触发场景:
- critical finding 发现
- 关联分析命中(risk >= 阈值)
- 任务完成
"""
from __future__ import annotations

import json
import logging
import re
import socket

from .. import __version__
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger("arl_lite.notify")

# 严重等级阈值(只 notify 严重事件)
DEFAULT_MIN_SEVERITY = "high"  # high / critical
SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def is_valid_url(url: str) -> bool:
    """检查 URL 是否有效(http/https)"""
    if not isinstance(url, str) or not url:
        return False
    try:
        parsed = urllib.parse.urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


@dataclass
class WebhookConfig:
    """Webhook 配置

    Attributes:
        url: 目标 URL(ntfy.sh / Slack / 自建)
        provider: ntfy / slack / generic / local
        min_severity: 触发通知的最低严重等级
        topic: ntfy topic(可选,也可在 url 里)
        timeout: HTTP 超时秒
        enabled: 总开关
        headers: 自定义 header
    """
    url: str = ""
    provider: str = "generic"  # ntfy | slack | generic | local
    min_severity: str = DEFAULT_MIN_SEVERITY
    topic: str = ""
    timeout: int = 10
    enabled: bool = True
    headers: dict = field(default_factory=dict)

    def __post_init__(self):
        # 规范化
        self.provider = (self.provider or "generic").lower()
        if self.min_severity not in SEVERITY_RANK:
            log.warning(f"webhook: invalid min_severity {self.min_severity!r}, using high")
            self.min_severity = "high"
        if not (1 <= self.timeout <= 60):
            log.warning(f"webhook: invalid timeout {self.timeout}, using 10")
            self.timeout = 10
        if self.provider not in ("ntfy", "slack", "generic", "local"):
            log.warning(f"webhook: unknown provider {self.provider!r}, using generic")
            self.provider = "generic"
        if self.provider != "local" and not is_valid_url(self.url):
            raise ValueError(f"webhook: invalid URL {self.url!r} for provider {self.provider}")
        if not isinstance(self.headers, dict):
            self.headers = {}

    def should_notify(self, severity: str) -> bool:
        """判断给定 severity 是否达到通知阈值"""
        if not self.enabled:
            return False
        sev_rank = SEVERITY_RANK.get(severity, 0)
        min_rank = SEVERITY_RANK.get(self.min_severity, 3)
        return sev_rank >= min_rank


def _header_safe(v: str) -> str:
    """把任意字符串清洗成能放进 HTTP 头的形式(去 CRLF、非 latin-1 替换)"""
    v = re.sub(r"[\r\n]+", " ", str(v))
    return v.encode("latin-1", errors="replace").decode("latin-1")


def _http_post(url: str, data: bytes, headers: dict, timeout: int) -> tuple[int, str]:
    """发送 HTTP POST,返回 (status, body)

    防御:任何网络错返回 (-1, error_msg),不抛异常
    """
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(1024 * 1024).decode("utf-8", errors="replace")
            return resp.status, body
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read(1024 * 1024).decode("utf-8", errors="replace")
        except Exception:
            pass
        return e.code, body
    except (urllib.error.URLError, socket.timeout, ConnectionRefusedError, OSError) as e:
        return -1, f"{type(e).__name__}: {e}"


def notify(
    config: WebhookConfig,
    title: str,
    message: str,
    severity: str = "info",
    tags: list[str] | None = None,
    extra: dict | None = None,
) -> bool:
    """发送通知

    Args:
        config: WebhookConfig
        title: 通知标题
        message: 通知正文
        severity: info/low/medium/high/critical
        tags: 标签列表(用于 ntfy emoji / Slack color)
        extra: 额外 JSON 字段(只对 generic provider)

    Returns:
        True=已通知, False=未触发或失败
    """
    if not config.should_notify(severity):
        log.debug(f"webhook: skip (severity {severity} < {config.min_severity})")
        return False

    tags = tags or []
    extra = extra or {}

    if config.provider == "local":
        log.info(f"[local webhook] {severity.upper()} {title}: {message[:100]} tags={tags}")
        return True

    if config.provider == "ntfy":
        # ntfy 协议:header 携带 title/tags/priority
        headers = {
            # title 可能来自外部页面的 <title>:CRLF 会炸 http.client(且是头注入),
            # 非 latin-1 字符会炸编码,统一清洗
            "Title": _header_safe(title)[:200],
            "Priority": {"info": "1", "low": "2", "medium": "3", "high": "4", "critical": "5"}.get(severity, "3"),
            "Tags": ",".join(_header_safe(tag)[:64] for tag in tags[:5]),
        }
        headers.update(config.headers)
        # 通知渠道常是第三方(ntfy.sh/Slack),大 evidence/消息会打爆配额
        status, body = _http_post(config.url, message[:10000].encode("utf-8"), headers, config.timeout)
        if status < 0 or status >= 400:
            log.warning(f"ntfy notify failed: status={status} body={body[:200]}")
            return False
        log.debug(f"ntfy notify ok: status={status}")
        return True

    if config.provider == "slack":
        # Slack incoming webhook: {"text": "..."} 简化版
        payload = {
            "text": f"*{title}*\n{message}",
        }
        # color 用 attachments
        color_map = {"critical": "danger", "high": "warning", "medium": "good", "low": "#888", "info": "#888"}
        if severity in color_map:
            payload["attachments"] = [{
                "color": color_map[severity],
                "text": message[:2000],
                "fields": [{"title": "Severity", "value": severity, "short": True}],
            }]
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        headers.update(config.headers)
        status, body = _http_post(config.url, data, headers, config.timeout)
        if status < 0 or status >= 400:
            log.warning(f"slack notify failed: status={status} body={body[:200]}")
            return False
        log.debug(f"slack notify ok: status={status}")
        return True

    # generic: 任何 POST JSON
    payload = {
        "title": title,
        "message": message,
        "severity": severity,
        "tags": tags,
        "source": "arl-lite",
    }
    payload.update(extra)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": f"arl-lite/{__version__}"}
    headers.update(config.headers)
    status, body = _http_post(config.url, data, headers, config.timeout)
    if status < 0 or status >= 400:
        log.warning(f"generic notify failed: status={status} body={body[:200]}")
        return False
    log.debug(f"generic notify ok: status={status}")
    return True


def notify_critical_finding(
    config: WebhookConfig,
    finding: dict,
    target: str = "",
) -> bool:
    """critical finding 通知

    Args:
        config: WebhookConfig
        finding: finding dict(包含 title / description / severity / target)
        target: 目标标识
    """
    severity = finding.get("severity", "info")
    title = finding.get("title", "Critical finding")
    description = finding.get("description", "")
    target = target or finding.get("target", "")

    tags = ["rotating_light"]
    if severity == "critical":
        tags.append("skull")
    # evidence 可达 50KB,通知面剔掉(报告里有全文)
    slim = {k: v for k, v in (finding or {}).items() if k != "evidence"}
    return notify(
        config,
        title=f"[ARL] {severity.upper()}: {title[:80]}",
        message=f"Target: {target}\n{description[:500]}",
        severity=severity,
        tags=tags,
        extra={"finding": slim},
    )


def notify_correlation(
    config: WebhookConfig,
    correlation: dict,
    target: str = "",
) -> bool:
    """关联分析命中通知"""
    severity = correlation.get("severity", "info")
    rule_name = correlation.get("rule_name", "?")
    risk = correlation.get("risk", 0)
    headline = correlation.get("headline", "")
    target = target or correlation.get("target", "")

    tags = ["link"]
    if risk >= 9:
        tags.append("fire")
    elif risk >= 7:
        tags.append("warning")
    return notify(
        config,
        title=f"[ARL] Correlation: {rule_name} (risk {risk})",
        message=f"Target: {target}\n{headline}",
        severity=severity,
        tags=tags,
        extra={"correlation": correlation},
    )


def notify_task_done(
    config: WebhookConfig,
    task_id: int,
    target: str,
    found: int,
    duration_seconds: float,
    errors: list[str] | None = None,
) -> bool:
    """任务完成通知"""
    errors = errors or []
    severity = "info" if not errors else "medium"
    return notify(
        config,
        title=f"[ARL] Task #{task_id} done: {target}",
        message=(
            f"Target: {target}\n"
            f"Found: {found} assets\n"
            f"Duration: {duration_seconds:.1f}s\n"
            f"Errors: {len(errors)}"
        ),
        severity=severity,
        tags=["white_check_mark"] if not errors else ["warning"],
        extra={"task_id": task_id, "found": found, "errors": errors},
    )
