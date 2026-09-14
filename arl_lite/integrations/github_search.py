"""arl_lite.integrations.github_search

GitHub Code Search API 封装 — 纯 stdlib。

API 文档:https://docs.github.com/en/rest/search

认证:
- 匿名:60 req/h
- 带 token:5000 req/h

注意:GitHub Code Search API 要求用 query DSL(不是简单关键词):
- "user:foo repo:bar password"
- "{target} password"
- org:myorg aws_access_key
"""
from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("arl_lite.integrations.github_search")

API_URL = "https://api.github.com/search/code"


# 探测模板(用 {target}/{domain} 占位符)
BUILTIN_QUERIES = [
    '"{target}" password',
    '"{target}" api_key',
    '"{target}" secret',
    '"{target}" .env',
    '"{target}" token',
    '"{target}" AWS_ACCESS_KEY',
    '"{target}" private_key',
    '"{target}" id_rsa',
]


def search_github(
    query: str,
    token: str = "",
    per_page: int = 10,
    timeout: int = 15,
) -> list[dict]:
    """搜 GitHub code

    Args:
        query: search DSL query
        token: GitHub personal access token(可选)
        per_page: 每页结果数(最大 100)
        timeout: 超时秒

    Returns:
        results list(简化字段)
    """
    params = {"q": query, "per_page": per_page}
    url = API_URL + "?" + urllib.parse.urlencode(params)
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "arl-lite/0.5",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            data = json.loads(resp.read(10 * 1024 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read(10 * 1024 * 1024).decode("utf-8", errors="replace")
        # 422 = validation failed(太宽的 query)
        # 403 = rate limit / forbidden
        # 失败必须显式抛出:静默 [] 会让限流/结构变化伪装成"健康无泄漏"
        if e.code == 403:
            raise PermissionError(f"github rate limited/forbidden (403): {body[:200]}")
        if e.code == 401:
            raise PermissionError(f"github auth failed (401) — token missing/invalid")
        if e.code == 422:
            raise ValueError(f"github query rejected (422): {body[:200]}")
        raise RuntimeError(f"github http {e.code}: {body[:200]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"github request failed: {e}") from e

    # 解析 items
    items = data.get("items", [])
    results = []
    for item in items:
        results.append({
            "name": item.get("name", ""),
            "path": item.get("path", ""),
            "html_url": item.get("html_url", ""),
            "repo": item.get("repository", {}).get("full_name", ""),
            "score": item.get("score", 0),
            "content": item.get("text_matches", [{}])[0].get("fragment", "") if item.get("text_matches") else "",
        })
    return results
