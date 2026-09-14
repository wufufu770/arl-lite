"""arl_lite.integrations.virustotal

VirusTotal 被动子域枚举(需要 API key,免费 4 req/min)。
- API:https://www.virustotal.com/api/v3/domains/{domain}/subdomains
- Header:x-apikey: <KEY>
- 返回:JSON,字段 data[].id 即子域
- 限制:4 req/min,5000 req/month(免费)

纪律:
- API key 缺 → ok=False, error_type=auth
- 429 → error_type=rate_limit(等 60s 后重试)
- 子域过滤:必须以 .domain 结尾
"""
from __future__ import annotations

from . import valid_hostname

import json
import urllib.request
import urllib.error
import asyncio
import logging
import os
from typing import AsyncIterator

log = logging.getLogger("arl_lite.integrations.virustotal")

VT_URL = "https://www.virustotal.com/api/v3/domains/{domain}/subdomains"
DEFAULT_TIMEOUT = 30
DEFAULT_LIMIT = 100  # 每次最多 100 条


async def query_virustotal(
    domain: str,
    api_key: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    limit: int = DEFAULT_LIMIT,
) -> AsyncIterator[str]:
    """异步查询 VirusTotal

    Args:
        domain: 目标域名
        api_key: VT API key(优先用入参,否则从 VT_API_KEY 环境变量取)
    """
    api_key = api_key or os.environ.get("VT_API_KEY")
    if not api_key:
        raise PermissionError("virustotal: API key required (set VT_API_KEY env or pass api_key=)")

    url = VT_URL.format(domain=domain) + f"?limit={limit}"

    def _do_request():
        req = urllib.request.Request(
            url,
            headers={
                "x-apikey": api_key,
                "User-Agent": "arl-lite/0.2",
            },
        )
        return urllib.request.urlopen(req, timeout=timeout)

    loop = asyncio.get_running_loop()
    try:
        resp = await loop.run_in_executor(None, _do_request)
    except urllib.error.HTTPError as e:
        if e.code == 401 or e.code == 403:
            raise PermissionError(f"virustotal: auth failed ({e.code})")
        if e.code == 429:
            raise PermissionError("virustotal: rate limit (4 req/min)")
        if "timeout" in str(e).lower() or "timed out" in str(e).lower():
            raise TimeoutError(f"virustotal timeout after {timeout}s")
        raise
    except urllib.error.URLError as e:
        if "timeout" in str(e).lower() or "timed out" in str(e).lower():
            raise TimeoutError(f"virustotal timeout after {timeout}s")
        raise

    try:
        body = await loop.run_in_executor(None, resp.read)
    finally:
        resp.close()

    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise ValueError(f"virustotal returned invalid JSON: {e}")

    records = data.get("data") or []
    seen: set[str] = set()
    for rec in records:
        # 单条畸形记录(非 dict / id 非 str)只跳过该条,不清空整源
        if not isinstance(rec, dict):
            continue
        rid = rec.get("id")
        if not isinstance(rid, str):
            continue
        sub = rid.strip().lower()
        if not valid_hostname(sub) or not sub.endswith("." + domain) and sub != domain:
            continue
        if sub in seen:
            continue
        seen.add(sub)
        yield sub


async def collect_subdomains(
    domain: str,
    api_key: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[list[str], str | None, str | None]:
    subs: list[str] = []
    try:
        async for sub in query_virustotal(domain, api_key=api_key, timeout=timeout):
            subs.append(sub)
        return subs, None, None
    except PermissionError as e:
        err_str = str(e).lower()
        if "rate limit" in err_str:
            return [], str(e), "rate_limit"
        if "auth" in err_str or "api key" in err_str:
            return [], str(e), "auth"
        return [], str(e), "auth"
    except TimeoutError as e:
        return [], str(e), "timeout"
    except urllib.error.URLError as e:
        return [], f"URLError: {e}", "network"
    except json.JSONDecodeError as e:
        return [], f"JSON parse error: {e}", "parse"
    except ValueError as e:
        # 集成层对响应结构/JSON 的主动 raise → parse(不是 unknown)
        return [], str(e), "parse"
    except PermissionError as e:
        return [], str(e), "auth"
    except Exception as e:
        return [], f"{type(e).__name__}: {e}", "unknown"
