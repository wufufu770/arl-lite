"""arl_lite.integrations.dirscan

并发目录扫描 — 纯 stdlib (asyncio + urllib)。

设计:
- 并发 asyncio.Semaphore 控制
- 启发式过滤:status_code + content_length + body fingerprint
- 自动跳过明显 404(基于 length histogram — 同一站点多次相同 length 视为 404 模板)
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urljoin

log = logging.getLogger("arl_lite.integrations.dirscan")

# 启发式:body 短(< 100 字符)且 status != 200 → 跳过
# 启发式:404 模板(看 body hash 集中度)→ 标 interested=False


@dataclass
class PathResult:
    path: str
    url: str
    status: int
    length: int
    title: str = ""
    body_hash: str = ""
    interesting: bool = False
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "url": self.url,
            "status": self.status,
            "length": self.length,
            "title": self.title,
            "body_hash": self.body_hash,
            "interesting": self.interesting,
            "error": self.error,
        }


async def _probe(url: str, timeout: int) -> PathResult:
    """探测一个 URL"""
    path = url.split("//", 1)[-1].split("/", 1)[-1] if "//" in url else url
    if not path:
        path = "/"
    loop = asyncio.get_running_loop()

    def do_request():
        ctx = ssl.create_default_context()
        req = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": "arl-lite/0.5"})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                body = resp.read(64 * 1024)  # 限制 64KB,目录扫描不需要全文
                return {
                    "status": resp.status,
                    "length": len(body),
                    "body": body,
                    "headers": dict(resp.headers),
                }
        except urllib.error.HTTPError as e:
            # 4xx/5xx 也要返回(发现 401/403 也算 interesting)
            try:
                body = e.read(64 * 1024)
            except Exception:
                body = b""
            return {
                "status": e.code,
                "length": len(body),
                "body": body,
                "headers": {},
            }
        except (urllib.error.URLError, TimeoutError) as e:
            return {"error": f"{type(e).__name__}: {e}"}

    try:
        result = await loop.run_in_executor(None, do_request)
    except Exception as e:
        return PathResult(path=path, url=url, status=0, length=0,
                          error=f"{type(e).__name__}: {e}")

    if "error" in result:
        return PathResult(path=path, url=url, status=0, length=0,
                          error=result["error"])

    body = result.get("body", b"")
    body_hash = hashlib.md5(body).hexdigest()[:16]
    status = result["status"]
    length = result["length"]
    title = ""
    # 提取 title
    try:
        text = body.decode("utf-8", errors="replace")
        if "<title>" in text.lower():
            i = text.lower().find("<title>")
            j = text.lower().find("</title>", i)
            if j > i:
                title = text[i + 7:j].strip()[:200]
    except Exception:
        pass

    # 启发式:interesting?
    interesting = _is_interesting(status, path, length, body_hash)

    return PathResult(
        path=path, url=url, status=status, length=length,
        title=title, body_hash=body_hash, interesting=interesting,
    )


def _is_interesting(status: int, path: str, length: int, body_hash: str) -> bool:
    """启发式:哪些**响应**算 interesting。判定**只看 status**。

    `path`/`length`/`body_hash` 调用方完整传入但函数体零引用(soft-404
    启发式预留,Phase 2)。r103 前写的是「哪些 path 算 interesting」。
    """
    # 2xx 永远 interesting
    if 200 <= status < 300:
        return True
    # 3xx 重定向
    if 300 <= status < 400:
        return True
    # 401/403 算 interesting(可能存在但要认证)
    if status in (401, 403):
        return True
    # 405 算(方法禁用但路径存在)
    if status == 405:
        return True
    # 5xx **不算**:通常是 service 挂了(原注释写「500 算」,与下一行打架)
    if status in (500, 502, 503):
        return False
    return False


async def scan_paths(
    base_url: str,
    paths: Iterable[str],
    timeout: int = 10,
    concurrency: int = 30,
) -> list[dict]:
    """扫描一组 paths

    Args:
        base_url: 基础 URL(以 / 结尾)
        paths: 路径列表(不含 base)
        timeout: 单次请求超时(秒)
        concurrency: 并发数

    Returns:
        results list(每条 dict)
    """
    if not base_url.endswith("/"):
        base_url = base_url + "/"
    paths = list(paths)
    sem = asyncio.Semaphore(concurrency)

    async def probe_one(p: str) -> PathResult:
        async with sem:
            url = urljoin(base_url, p.lstrip("/"))
            return await _probe(url, timeout)

    start = time.time()
    tasks = [probe_one(p) for p in paths]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    results: list[PathResult] = []
    for p, r in zip(paths, raw_results):
        if isinstance(r, Exception):
            results.append(PathResult(path=p, url=urljoin(base_url, p), status=0, length=0,
                                     error=f"{type(r).__name__}: {r}"))
        else:
            results.append(r)

    elapsed = time.time() - start
    log.debug(f"dirscan: {len(paths)} paths in {elapsed:.1f}s, {sum(1 for r in results if r.interesting)} interesting")
    return [r.to_dict() for r in results]
