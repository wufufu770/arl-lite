"""arl_lite.integrations.dnsdumpster

DNSDumpster 被动子域枚举(免费,无需 token,需要绕过 CSRF)。
- API:https://dnsdumpster.com/
- 表单提交:csrfmiddlewaretoken + targetip(无 CSRF 也能抓到部分,完整需要先 GET 拿 token)
- 限制:页面会触发 CAPTCHA,大量查询会被拦截

纪律:
- 先 GET 拿 csrf token,再 POST
- HTML 解析(表格行)
- CAPTCHA 检测 → 标 ok=False, error_type=rate_limit
"""
from __future__ import annotations

from . import valid_hostname

import re
import urllib.request
import urllib.error
import urllib.parse
import http.cookiejar
import asyncio
import logging
from typing import AsyncIterator

log = logging.getLogger("arl_lite.integrations.dnsdumpster")

DNSDUMPSTER_URL = "https://dnsdumpster.com/"
DEFAULT_TIMEOUT = 30

# 表格行 <tr><td class="col-md-4">sub.example.com</td><td>...</td></tr>
HOSTNAME_RE = re.compile(
    r'<td[^>]*class="col-md-4"[^>]*>([a-z0-9\-\.]+\.[a-z]{2,})</td>',
    re.IGNORECASE
)


async def query_dnsdumpster(domain: str, timeout: int = DEFAULT_TIMEOUT) -> AsyncIterator[str]:
    """异步查询 dnsdumpster

    流程:GET 拿 CSRF → POST 提交 → 解析 HTML 表格
    """
    loop = asyncio.get_running_loop()

    # 1. 拿 CSRF token(用 cookie jar)
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [("User-Agent", "Mozilla/5.0 arl-lite/0.2")]

    def _get():
        return opener.open(DNSDUMPSTER_URL, timeout=timeout)

    try:
        resp = await loop.run_in_executor(None, _get)
        body = await loop.run_in_executor(None, resp.read)
        resp.close()
    except urllib.error.URLError as e:
        if "timeout" in str(e).lower() or "timed out" in str(e).lower():
            raise TimeoutError(f"dnsdumpster timeout after {timeout}s")
        raise

    html = body.decode("utf-8", errors="ignore")

    # 提取 csrf token
    csrf_match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', html)
    if not csrf_match:
        raise ValueError("dnsdumpster: no csrfmiddlewaretoken found (CAPTCHA or site change?)")
    csrf = csrf_match.group(1)

    # 2. POST 查询
    post_data = urllib.parse.urlencode({
        "csrfmiddlewaretoken": csrf,
        "targetip": domain,
    }).encode()

    def _post():
        return opener.open(DNSDUMPSTER_URL, data=post_data, timeout=timeout)

    try:
        resp = await loop.run_in_executor(None, _post)
        body = await loop.run_in_executor(None, resp.read)
        resp.close()
    except urllib.error.URLError as e:
        if "timeout" in str(e).lower() or "timed out" in str(e).lower():
            raise TimeoutError(f"dnsdumpster POST timeout after {timeout}s")
        raise

    text = body.decode("utf-8", errors="ignore")

    # CAPTCHA 检测
    if "captcha" in text.lower() or "verify you are a human" in text.lower():
        raise ValueError("dnsdumpster CAPTCHA triggered (rate limited)")

    seen: set[str] = set()
    for m in HOSTNAME_RE.finditer(text):
        sub = m.group(1).strip().lower()
        if not valid_hostname(sub):
            continue
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
    subs: list[str] = []
    try:
        async for sub in query_dnsdumpster(domain, timeout=timeout):
            subs.append(sub)
        return subs, None, None
    except TimeoutError as e:
        return [], str(e), "timeout"
    except urllib.error.URLError as e:
        return [], f"URLError: {e}", "network"
    except ValueError as e:
        # CAPTHCA / CSRF 失败 → 算 rate_limit(实际产品里就标记这个源不可用)
        err_str = str(e).lower()
        if "captcha" in err_str:
            return [], str(e), "rate_limit"
        return [], str(e), "parse"
    except Exception as e:
        return [], f"{type(e).__name__}: {e}", "unknown"
