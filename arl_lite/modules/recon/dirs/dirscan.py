"""arl_lite.modules.recon.dirs.dirscan

目录扫描 — 纯 stdlib HTTP,并发探测常见路径。

设计:
- 内置 80+ 路径 wordlist(常见后台/敏感文件/API/备份)
- 启发式过滤:基于 status_code + content_length + body 特征
- 并发:asyncio + semaphore
- 3 态返回:ok / data / error
- 无 pip 依赖,2G 内存友好
"""
from __future__ import annotations

import logging
import time

from ....core.base_module import BaseModule, ModuleResult
from ....integrations.dirscan import scan_paths

log = logging.getLogger("arl_lite.modules.dirscan")


# 80+ 常见路径(精选 — 不依赖 wordlist 文件,避免 IO)
DEFAULT_WORDLIST = [
    # 后台
    "admin", "admin.php", "admin/login", "admin/index", "administrator",
    "manager", "manage", "backend", "backoffice", "console",
    "cms", "wp-admin", "wp-login.php", "login", "logout", "user/login",
    "auth", "auth/login", "signin", "signup", "register",
    # API
    "api", "api/v1", "api/v2", "api/v3", "v1", "v2",
    "graphql", "swagger", "swagger-ui", "swagger.json", "swagger.yaml",
    "openapi.json", "openapi.yaml", "docs", "redoc",
    # 配置/管理
    "config", "config.json", "config.yaml", "config.yml", "config.php",
    "settings", "settings.json", "settings.xml",
    "status", "health", "healthz", "healthcheck", "ready", "readyz",
    "info", "info.php", "phpinfo.php", "test.php", "debug",
    "metrics", "prometheus", "actuator", "actuator/env",
    # 备份/敏感
    "backup", "backup.zip", "backup.tar.gz", "backup.sql", "backup.bak",
    "dump.sql", "db.sql", "database.sql",
    "old", "old.zip", "old.bak", "test", "test.php",
    ".git", ".git/HEAD", ".git/config", ".gitignore", ".env",
    ".htaccess", ".htpasswd", ".DS_Store",
    # 文档
    "robots.txt", "sitemap.xml", "humans.txt", "security.txt",
    "changelog.txt", "readme.md", "README.md", "LICENSE",
    "package.json", "composer.json", "Gemfile", "requirements.txt",
    # 错误/默认
    "404", "500", "503", "maintenance",
    # 业务
    "upload", "uploads", "files", "file", "download", "downloads",
    "static", "assets", "public", "resources", "media",
    "images", "img", "css", "js", "fonts",
    "data", "tmp", "temp", "cache", "log", "logs",
]


class DirscanModule(BaseModule):
    name = "dirscan"
    category = "recon/dirs"
    description = "目录/文件扫描(80+ 内置路径,启发式过滤)"
    input_type = "url"
    output_type = "site"
    required_tools = []
    author = "arl-lite"
    version = "0.5.0"

    async def run(self, target: str, **kwargs) -> ModuleResult:
        """扫描一个 URL

        Args:
            target: URL(必须含 scheme + host,例 http://x.com)
        """
        start = time.time()
        if not target.startswith("http://") and not target.startswith("https://"):
            target = f"http://{target}"

        timeout = int(kwargs.get("timeout", 10))
        concurrency = int(kwargs.get("concurrency", 30))
        wordlist = kwargs.get("wordlist") or DEFAULT_WORDLIST
        max_paths = int(kwargs.get("max_paths", len(wordlist)))
        paths = wordlist[:max_paths]

        try:
            results = await scan_paths(
                target, paths,
                timeout=timeout, concurrency=concurrency,
            )
        except Exception as e:
            log.exception(f"dirscan failed for {target}: {e}")
            return ModuleResult(
                success=False, target=target, found=0,
                duration_seconds=time.time() - start,
                sources=[],
                errors=[f"{type(e).__name__}: {e}"],
            )

        # 只入库有效响应:错误行(status=0/SSL 错)进 sites 表是脏数据
        valid = [r for r in results if r.get("status", 0) and not r.get("error")]

        # 端口从 target 实际取,别硬编码 80/443(自定义端口会记错)
        from urllib.parse import urlparse
        parsed = urlparse(target if "//" in target else f"http://{target}")
        scheme = parsed.scheme or "http"
        port = parsed.port or (443 if scheme == "https" else 80)
        host = parsed.hostname or target

        for r in valid:
            self.storage.add_site(
                task_id=self.task_id,
                url=r["url"],
                host=host,
                ip=None,
                port=port,
                scheme=scheme,
                title=r.get("title", ""),
                status_code=r.get("status"),
                tech=None,
                module=self.name,
                confidence=80,
            )

        # 把"有意思的"(非 404)记为 finding(同样只看有效行)
        for r in valid:
            if r.get("interesting"):
                self.storage.add_finding(
                    task_id=self.task_id,
                    target=r["url"],
                    finding_type="exposed_path",
                    title=f"Path exposed: {r['path']}",
                    description=f"HTTP {r['status']}, len={r.get('length', 0)}",
                    evidence=f"path={r['path']} status={r['status']} size={r.get('length', 0)}",
                    severity=self._severity_for(r),
                )

        # 全部探测失败(目标死/SSL 错)如实报错,不伪装成功
        failed = [r for r in results if r.get("error") or not r.get("status", 0)]
        err_msg = f"{len(failed)}/{len(results)} paths failed" if failed and not valid else None

        sr = self.make_source_result(
            data=valid, source=self.name, start_time=start,
            error=err_msg, error_type="network" if err_msg else None,
        )
        return ModuleResult(
            success=sr.ok, target=target, found=len(valid),
            duration_seconds=time.time() - start,
            sources=[sr], errors=[err_msg] if err_msg else [],
            metadata={
                "scanned_paths": len(paths),
                "interesting": sum(1 for r in valid if r.get("interesting")),
            }
        )

    def _severity_for(self, r: dict) -> str:
        """根据 status + path 决定 severity"""
        path = r.get("path", "").lower()
        if r.get("status") in (200, 201):
            # 敏感路径
            for kw in ("admin", "manager", "console", "config", "backup", ".git",
                       ".env", "htpasswd", "phpinfo", "swagger", "api"):
                if kw in path:
                    return "high"
            return "medium"
        if r.get("status") in (301, 302, 303, 307, 308):
            return "low"
        return "info"
