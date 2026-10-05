"""arl_lite.integrations.quake

360 Quake 被动子域枚举(需要 API key,免费 3000 条/月)。
- API:https://quake.360.net/api/v3/search/quake_service
- Header: X-QuakeToken: <KEY>
- Body:JSON,query: domain:"example.com"
- 限制:免费额度 3000 条/月,3 qps

纪律:
- API key 缺 → ok=False, error_type=auth
- 401/403 → auth
- 429 → rate_limit
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

log = logging.getLogger("arl_lite.integrations.quake")

QUAKE_URL = "https://quake.360.net/api/v3/search/quake_service"
DEFAULT_TIMEOUT = 30
DEFAULT_SIZE = 100  # 每页


async def query_quake(
    domain: str,
    api_key: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    size: int = DEFAULT_SIZE,
) -> AsyncIterator[str]:
    """异步查询 360 Quake"""
    api_key = api_key or os.environ.get("QUAKE_TOKEN")
    if not api_key:
        raise PermissionError("quake: API key required (set QUAKE_TOKEN env or pass api_key=)")

    body = json.dumps({
        "query": f'domain:"{domain}"',
        "start": 0,
        "size": size,
    }).encode()

    def _do_request():
        req = urllib.request.Request(
            QUAKE_URL,
            data=body,
            headers={
                "X-QuakeToken": api_key,
                "Content-Type": "application/json",
                "User-Agent": "arl-lite/0.2",
            },
            method="POST",
        )
        return urllib.request.urlopen(req, timeout=timeout)

    loop = asyncio.get_running_loop()
    try:
        resp = await loop.run_in_executor(None, _do_request)
    except urllib.error.HTTPError as e:
        if e.code == 401 or e.code == 403:
            raise PermissionError(f"quake: auth failed ({e.code})")
        if e.code == 429:
            raise PermissionError("quake: rate limit (3 qps)")
        if "timeout" in str(e).lower() or "timed out" in str(e).lower():
            raise TimeoutError(f"quake timeout after {timeout}s")
        raise
    except urllib.error.URLError as e:
        if "timeout" in str(e).lower() or "timed out" in str(e).lower():
            raise TimeoutError(f"quake timeout after {timeout}s")
        raise

    try:
        raw = await loop.run_in_executor(None, resp.read)
    finally:
        resp.close()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"quake returned invalid JSON: {e}")

    # quake 返回结构:{"code": 0, "data": [...], "meta": {...}}
    if data.get("code") != 0:
        msg = data.get("message", "unknown")
        raise ValueError(f"quake API error: code={data.get('code')} message={msg}")

    records = data.get("data") or []
    seen: set[str] = set()
    for rec in records:
        # 单条畸形记录只跳过,不清空整源
        if not isinstance(rec, dict):
            continue
        # 子域可能在 service.http.host 或 domain 字段
        svc = rec.get("service")
        chain = svc.get("http", {}) if isinstance(svc, dict) else {}
        host = (chain.get("host") if isinstance(chain, dict) else None) or rec.get("domain") or ""
        if not isinstance(host, str):
            continue
        host = host.strip().lower()
        if not valid_hostname(host):
            continue
        # 去掉端口
        host = host.split(":")[0]
        if not host.endswith("." + domain) and host != domain:
            continue
        if host in seen:
            continue
        seen.add(host)
        yield host


async def collect_subdomains(
    domain: str,
    api_key: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[list[str], str | None, str | None]:
    subs: list[str] = []
    try:
        async for sub in query_quake(domain, api_key=api_key, timeout=timeout):
            subs.append(sub)
        return subs, None, None
    except PermissionError as e:
        err_str = str(e).lower()
        if "rate limit" in err_str:
            return [], str(e), "rate_limit"
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
    except Exception as e:
        return [], f"{type(e).__name__}: {e}", "unknown"
