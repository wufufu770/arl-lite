"""arl_lite.integrations.crtsh

crt.sh 证书透明度查询(纯 HTTP API,无外部工具依赖)。

crt.sh 是 Comodo 的证书透明度日志搜索引擎,免费、无需 token、能查任意域名的所有历史证书。
- API: https://crt.sh/?q=%.example.com&output=json
- 返回:JSON 数组,每条是证书记录(name_value 字段含域名)
- 限制:偶尔超时,响应可能很大

纪律:
- 用标准库 urllib(避免 httpx 依赖,纯零外部依赖也能跑)
- 超时 60s(证书站慢)
- 异常分级(超时/网络/JSON 解析)
- 子域提取:*.example.com / example.com 都保留
"""
from __future__ import annotations

import asyncio

import json
import urllib.request
import urllib.error
import urllib.parse
import logging
from typing import AsyncIterator

log = logging.getLogger("arl_lite.integrations.crtsh")

CRTSH_URL = "https://crt.sh/"
DEFAULT_TIMEOUT = 60


async def query_crtsh(domain: str, timeout: int = DEFAULT_TIMEOUT) -> AsyncIterator[str]:
    """异步查询 crt.sh 证书透明度日志

    Yields:
        子域字符串(已经过去 *. 前缀和转小写)
    """
    q = f"%.{domain}"
    url = f"{CRTSH_URL}?q={urllib.parse.quote(q)}&output=json"

    def _do_request():
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "arl-lite/0.1 (+https://github.com/arl-lite)"},
        )
        return urllib.request.urlopen(req, timeout=timeout)

    loop = asyncio.get_running_loop()
    try:
        resp = await loop.run_in_executor(None, _do_request)
    except urllib.error.URLError as e:
        if "timeout" in str(e).lower() or "timed out" in str(e).lower():
            raise TimeoutError(f"crt.sh timeout after {timeout}s")
        raise

    try:
        data = await loop.run_in_executor(None, resp.read)
    finally:
        resp.close()

    try:
        records = json.loads(data)
    except json.JSONDecodeError as e:
        raise ValueError(f"crt.sh returned invalid JSON: {e}")

    if not isinstance(records, list):
        raise ValueError(f"crt.sh returned non-list: {type(records).__name__}")

    seen = set()
    for rec in records:
        # 非 dict 条目(crt.sh 偶发异常响应/代理注入)跳过而不是炸整批
        if not isinstance(rec, dict):
            continue
        name_value = rec.get("name_value") or ""
        if not isinstance(name_value, str):
            name_value = str(name_value)
        # name_value 可能含多行(多个域名)
        for line in name_value.splitlines():
            sub = line.strip().lower()
            # 过滤:必须以 .example.com 结尾 / 排除通配符裸域外的奇怪字符
            if not sub or not sub.endswith("." + domain) and sub != domain:
                continue
            if "@" in sub:  # 邮箱
                continue
            if sub.startswith("*"):  # 跳过泛域(保留 origin)
                continue
            if sub in seen:
                continue
            seen.add(sub)
            yield sub


async def collect_subdomains(
    domain: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[list[str], str | None, str | None]:
    """便利函数:一次性返回所有子域 + 错误信息(三态)

    Returns:
        (subs, error, error_type)
    """
    subs: list[str] = []
    try:
        async for sub in query_crtsh(domain, timeout=timeout):
            subs.append(sub)
        return subs, None, None
    except TimeoutError as e:
        return [], str(e), "timeout"
    except urllib.error.URLError as e:
        return [], f"URLError: {e}", "network"
    except json.JSONDecodeError as e:
        return [], f"JSON parse error: {e}", "parse"
    except Exception as e:
        return [], f"{type(e).__name__}: {e}", "unknown"

