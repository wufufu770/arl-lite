"""arl_lite.integrations.hackertarget

Hackertarget 被动子域枚举(免费,无需 token)。
- API:https://api.hackertarget.com/hostsearch/?q=example.com
- 返回:纯文本,每行 `sub.example.com,IP`
- 限制:100 条/查询,速率限制(具体不明)

纪律:
- 纯文本解析,逗号分隔
"""
from __future__ import annotations

import urllib.request
import urllib.error
import urllib.parse
import asyncio
import logging
from typing import AsyncIterator

log = logging.getLogger("arl_lite.integrations.hackertarget")

HACKERTARGET_URL = "https://api.hackertarget.com/hostsearch/"
DEFAULT_TIMEOUT = 30


async def query_hackertarget(domain: str, timeout: int = DEFAULT_TIMEOUT) -> AsyncIterator[str]:
    """异步查询 hackertarget"""
    url = HACKERTARGET_URL + "?q=" + urllib.parse.quote(domain)

    def _do_request():
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "arl-lite/0.2"},
        )
        return urllib.request.urlopen(req, timeout=timeout)

    loop = asyncio.get_running_loop()
    try:
        resp = await loop.run_in_executor(None, _do_request)
    except urllib.error.URLError as e:
        if "timeout" in str(e).lower() or "timed out" in str(e).lower():
            raise TimeoutError(f"hackertarget timeout after {timeout}s")
        raise

    try:
        body = await loop.run_in_executor(None, resp.read)
    finally:
        resp.close()

    text = body.decode("utf-8", errors="ignore")

    # 第一行如果是 "API count..." 这种错误信号
    if "error" in text[:100].lower() or "no results" in text[:100].lower():
        # 记录但仍然返回空(不算错)
        log.debug(f"hackertarget: {text[:200]}")
        return

    seen: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or "," not in line:
            continue
        # API 格式是 "subdomain,IP"——IP 一并保留(resolved_ip 之前是死列)
        parts = line.split(",", 1)
        sub = parts[0].strip().lower()
        ip = parts[1].strip() if len(parts) > 1 else ""
        if not sub:
            continue
        if not sub.endswith("." + domain) and sub != domain:
            continue
        if sub.startswith("*"):
            continue
        if sub in seen:
            continue
        seen.add(sub)
        yield sub, (ip or None)


async def collect_subdomains(
    domain: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[list[str], str | None, str | None, dict[str, str | None]]:
    subs: list[str] = []
    ip_map: dict[str, str | None] = {}
    try:
        async for sub, ip in query_hackertarget(domain, timeout=timeout):
            subs.append(sub)
            ip_map[sub] = ip
        return subs, None, None, ip_map
    except TimeoutError as e:
        return [], str(e), "timeout", {}
    except urllib.error.URLError as e:
        return [], f"URLError: {e}", "network", {}
    except Exception as e:
        return [], f"{type(e).__name__}: {e}", "unknown", {}
