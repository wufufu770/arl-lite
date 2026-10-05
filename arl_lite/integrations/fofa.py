"""arl_lite.integrations.fofa

FOFA 被动子域枚举(需要 API key,商业版)。
- API:https://fofa.info/api/v1/search/all
- Auth:email + key(base64 编码字段)
- Query:domain="example.com"
- 返回:JSON,results[3] 是 host

纪律:
- API key 缺 → ok=False, error_type=auth
- fofa 错误码:fofa 错误码非 0
"""
from __future__ import annotations

from . import valid_hostname

import base64
import json
import urllib.request
import urllib.error
import urllib.parse
import asyncio
import logging
import os
from typing import AsyncIterator

log = logging.getLogger("arl_lite.integrations.fofa")

FOFA_URL = "https://fofa.info/api/v1/search/all"
DEFAULT_TIMEOUT = 30
DEFAULT_SIZE = 100


async def query_fofa(
    domain: str,
    api_key: str | None = None,
    email: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    size: int = DEFAULT_SIZE,
) -> AsyncIterator[str]:
    """异步查询 FOFA

    Args:
        domain: 目标域名
        api_key: FOFA API key
        email: FOFA 绑定邮箱(优先入参,否则 FOFA_EMAIL env)
    """
    api_key = api_key or os.environ.get("FOFA_KEY")
    email = email or os.environ.get("FOFA_EMAIL")
    if not api_key or not email:
        raise PermissionError("fofa: API key + email required (set FOFA_KEY + FOFA_EMAIL env)")

    # qbase64 = base64(domain="example.com")
    q = f'domain="{domain}"'
    qb64 = base64.b64encode(q.encode()).decode()
    fields = "host,ip,port"  # 至少需要 host
    # 走 POST body:key 不再出现在 URL/代理/服务端 access log
    # (FOFA /search/all 官方同时支持 GET query 与 POST form)
    body = urllib.parse.urlencode({
        "qbase64": qb64,
        "email": email,
        "key": api_key,
        "size": size,
        "fields": fields,
    }).encode()

    def _do_request():
        req = urllib.request.Request(
            FOFA_URL,
            data=body,
            headers={
                "User-Agent": "arl-lite/0.2",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        return urllib.request.urlopen(req, timeout=timeout)

    loop = asyncio.get_running_loop()
    try:
        resp = await loop.run_in_executor(None, _do_request)
    except urllib.error.HTTPError as e:
        if e.code == 401 or e.code == 403:
            raise PermissionError(f"fofa: auth failed ({e.code})")
        if e.code == 429:
            raise TimeoutError(f"fofa rate limited (429)")
        if "timeout" in str(e).lower() or "timed out" in str(e).lower():
            raise TimeoutError(f"fofa timeout after {timeout}s")
        raise
    except urllib.error.URLError as e:
        if "timeout" in str(e).lower() or "timed out" in str(e).lower():
            raise TimeoutError(f"fofa timeout after {timeout}s")
        raise

    try:
        raw = await loop.run_in_executor(None, resp.read)
    finally:
        resp.close()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"fofa returned invalid JSON: {e}")

    # fofa 错误码:{"error": true, "errmsg": "..."} 或 {"code": -1}
    if data.get("error"):
        msg = data.get("errmsg", "unknown")
        raise PermissionError(f"fofa API error: {msg}")

    results = data.get("results") or []
    if not isinstance(results, list):
        raise ValueError(f"fofa: unexpected results type {type(results).__name__}")
    # fields 顺序决定索引:host 是第 0 个;行必须是序列(API 协议变化
    # 时显式报错,别让 row[0] 取到首字符产出"看起来健康"的 0 结果)
    seen: set[str] = set()
    for row in results:
        if not isinstance(row, (list, tuple)) or not row:
            raise ValueError("fofa: unexpected results row shape (expected non-empty list)")
        host = str(row[0]).strip().lower()
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
    email: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[list[str], str | None, str | None]:
    subs: list[str] = []
    try:
        async for sub in query_fofa(domain, api_key=api_key, email=email, timeout=timeout):
            subs.append(sub)
        return subs, None, None
    except PermissionError as e:
        err_str = str(e).lower()
        if "rate limit" in err_str or "frequenc" in err_str:
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
