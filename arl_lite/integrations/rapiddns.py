"""arl_lite.integrations.rapiddns

RapidDNS 被动子域枚举(无需 token)。
- 数据源:https://rapiddns.io/subdomain/{domain}
- 返回:HTML,每行 <td><a href="...">sub.example.com</a></td>
- 限制:无明确速率限制,但页面会很长(慢)

纪律:
- 用标准库 urllib
- 子域过滤:必须 .endswith(domain)
"""
from __future__ import annotations

import re
import urllib.request
import urllib.error
import urllib.parse
import asyncio
import logging
from typing import AsyncIterator

log = logging.getLogger("arl_lite.integrations.rapiddns")

RAPIDDNS_URL = "https://rapiddns.io/subdomain/{domain}"
DEFAULT_TIMEOUT = 45
# HTML 抓取:表格行 <td>www.example.com</td>(不带 <a>)
# 字符类含 _:DNS 标签合法(_dmarc.example.com 这类会被纯 [a-z0-9-] 丢掉)
SUBDOMAIN_RE = re.compile(
    r'<td[^>]*>\s*([a-z0-9_][a-z0-9_\-\.\*]*\.[a-z]{2,})\s*</td>',
    re.IGNORECASE
)


async def query_rapiddns(domain: str, timeout: int = DEFAULT_TIMEOUT) -> AsyncIterator[str]:
    """异步查询 rapiddns

    Yields:
        子域字符串
    """
    url = RAPIDDNS_URL.format(domain=urllib.parse.quote(domain))

    def _do_request():
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) arl-lite/0.2"},
        )
        return urllib.request.urlopen(req, timeout=timeout)

    loop = asyncio.get_running_loop()
    try:
        resp = await loop.run_in_executor(None, _do_request)
    except urllib.error.URLError as e:
        if "timeout" in str(e).lower() or "timed out" in str(e).lower():
            raise TimeoutError(f"rapiddns timeout after {timeout}s")
        raise

    try:
        # 流式读取避免大页面 OOM
        body_chunks: list[bytes] = []
        while True:
            chunk = await loop.run_in_executor(None, resp.read, 65536)
            if not chunk:
                break
            body_chunks.append(chunk)
        body = b"".join(body_chunks)
    finally:
        resp.close()

    text = body.decode("utf-8", errors="ignore")
    seen: set[str] = set()
    for m in SUBDOMAIN_RE.finditer(text):
        sub = m.group(1).strip().lower()
        if not sub.endswith("." + domain) and sub != domain:
            continue
        if sub.startswith("*"):
            continue
        if sub in seen:
            continue
        seen.add(sub)
        yield sub


async def collect_subdomains(
    domain: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[list[str], str | None, str | None]:
    """三态返回"""
    subs: list[str] = []
    try:
        async for sub in query_rapiddns(domain, timeout=timeout):
            subs.append(sub)
        return subs, None, None
    except TimeoutError as e:
        return [], str(e), "timeout"
    except urllib.error.URLError as e:
        return [], f"URLError: {e}", "network"
    except Exception as e:
        return [], f"{type(e).__name__}: {e}", "unknown"
