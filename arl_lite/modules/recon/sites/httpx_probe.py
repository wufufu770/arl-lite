"""arl_lite.modules.recon.sites.httpx_probe

HTTP 站点探活模块(纯 urllib,无 httpx 依赖)。
"""
from __future__ import annotations

import json
import logging
import time

from ....core.base_module import BaseModule, ModuleResult
from ....integrations.httpx_probe import probe

log = logging.getLogger("arl_lite.modules.httpx_probe")


class HttpxProbeModule(BaseModule):
    name = "httpx_probe"
    category = "recon/sites"
    description = "HTTP 站点探活(标题/状态码/Server/技术栈)"
    input_type = "host"
    output_type = "site"
    required_tools = []
    author = "arl-lite"
    version = "0.2.0"

    async def run(self, target: str, **kwargs) -> ModuleResult:
        start = time.time()
        # target 可能是单个 host,或者逗号分隔的 host:port 列表
        # 也可能上游传 "host:80,host:443" 形式
        # 这里简化:target 是 host,ports 从 kwargs 来
        timeout = int(kwargs.get("timeout", 10))
        concurrency = int(kwargs.get("concurrency", 50))
        ports = kwargs.get("ports") or [80, 443, 8080, 8443]
        schemes = kwargs.get("schemes") or ["http", "https"]

        results = []
        async for site in probe(
            target, ports=ports, schemes=schemes,
            timeout=timeout, concurrency=concurrency,
        ):
            results.append(site)
            cert = site.get("cert") or {}
            self.storage.add_site(
                task_id=self.task_id,
                url=site["url"],
                host=site["host"],
                ip=None,
                port=site["port"],
                scheme=site["scheme"],
                title=site["title"],
                server=site.get("server", ""),
                status_code=site["status"],
                tech=site["tech"],
                module=self.name,
                # 证书校验失败的 https 站点内容可能被 MITM 伪造,置信度降级
                confidence=70 if site.get("tls_verified") in (True, None) else 30,
                # 证书信息(资产归属判定的核心维度,见 integrations/tls_cert.py)
                cert_sha256=cert.get("sha256", ""),
                cert_issuer_cn=cert.get("issuer_cn", ""),
                cert_issuer_org=cert.get("issuer_org", ""),
                cert_subject_cn=cert.get("subject_cn", ""),
                cert_san=json.dumps(cert.get("san", []), ensure_ascii=False),
                cert_not_after=cert.get("not_after", ""),
                cert_expired=1 if cert.get("expired") else 0,
                cert_self_signed=1 if cert.get("self_signed") else 0,
                cert_days_left=cert.get("days_left"),
            )

        sr = self.make_source_result(
            data=results, source=self.name, start_time=start,
        )

        return ModuleResult(
            success=sr.ok, target=target, found=len(results),
            duration_seconds=time.time() - start,
            sources=[sr], errors=[],
            metadata={"scanned_ports": ports, "schemes": schemes}
        )
