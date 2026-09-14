"""arl_lite.integrations.otx

AlienVault OTX 被动子域枚举(免费,无需 token)。
- API:https://otx.alienvault.com/api/v1/indicator/domain/{domain}/passive_dns
- 返回:JSON,字段 `hostname` 即子域
- 限制:无明确速率限制,稳定

纪律:
- 翻页:pagination
- 过滤 passive_dns 里的 hostname 字段
"""
from __future__ import annotations

from . import valid_hostname

import json
import urllib.request
import urllib.error
import asyncio
import logging
from typing import AsyncIterator

log = logging.getLogger("arl_lite.integrations.otx")

OTX_URL = "https://otx.alienvault.com/api/v1/indicator/domain/{domain}/passive_dns"
DEFAULT_TIMEOUT = 30
MAX_PAGES = 5  # 防止无限翻页


async def query_otx(domain: str, timeout: int = DEFAULT_TIMEOUT, max_pages: int = MAX_PAGES) -> AsyncIterator[str]:
    """异步查询 OTX"""
    loop = asyncio.get_running_loop()
    seen: set[str] = set()
    page = 0

    while page < max_pages:
        url = OTX_URL.format(domain=domain) + (f"?page={page}" if page > 0 else "")

        def _do_request():
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "arl-lite/0.2"},
            )
            return urllib.request.urlopen(req, timeout=timeout)

        try:
            resp = await loop.run_in_executor(None, _do_request)
        except urllib.error.URLError as e:
            if "timeout" in str(e).lower() or "timed out" in str(e).lower():
                raise TimeoutError(f"otx timeout after {timeout}s")
            raise

        try:
            body = await loop.run_in_executor(None, resp.read)
        finally:
            resp.close()

        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise ValueError(f"otx returned invalid JSON: {e}")

        records = data.get("passive_dns", [])
        if not records:
            break

        new_count = 0
        for rec in records:
            # 单条畸形记录只跳过,不清空整源
            if not isinstance(rec, dict):
                continue
            hostname = rec.get("hostname")
            if not isinstance(hostname, str):
                continue
            hostname = hostname.strip().lower()
            if not valid_hostname(hostname):
                continue
            # 过滤:可能 hostname 是 IP 或者完整域名
            if "." not in hostname:
                continue
            if not hostname.endswith("." + domain) and hostname != domain:
                continue
            if hostname in seen:
                continue
            seen.add(hostname)
            new_count += 1
            yield hostname

        # next 字段可能 null → 终止
        if not data.get("next") or new_count == 0:
            break
        page += 1


async def collect_subdomains(
    domain: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[list[str], str | None, str | None]:
    subs: list[str] = []
    try:
        async for sub in query_otx(domain, timeout=timeout):
            subs.append(sub)
        return subs, None, None
    except TimeoutError as e:
        return [], str(e), "timeout"
    except urllib.error.HTTPError as e:
        if e.code == 401 or e.code == 403:
            return [], f"otx auth failed ({e.code}) — API key missing/invalid", "auth"
        if e.code == 429:
            return [], "otx rate limited (429)", "rate_limit"
        return [], f"otx http {e.code}", "network"
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
