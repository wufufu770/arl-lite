"""arl_lite.modules.recon.domains_hosts.virustotal

VirusTotal 被动子域查询模块(需要 API key)。
API key 配置:set VT_API_KEY 环境变量 或 api_key= 入参
"""
from __future__ import annotations

import logging
import time

from ....core.base_module import BaseModule, ModuleResult
from ....integrations.virustotal import collect_subdomains

log = logging.getLogger("arl_lite.modules.virustotal")


class VirustotalModule(BaseModule):
    name = "virustotal"
    category = "recon/domains-hosts"
    description = "通过 VirusTotal 被动查询子域(需要 API key)"
    input_type = "domain"
    output_type = "domain"
    required_tools = []  # 无外部工具,只需 API key
    author = "arl-lite"
    version = "0.2.0"

    async def run(self, target: str, **kwargs) -> ModuleResult:
        start = time.time()
        timeout = int(kwargs.get("timeout", 30))
        api_key = kwargs.get("api_key") or self.config.get("virustotal_api_key")

        subs, err, err_type = await collect_subdomains(target, api_key=api_key, timeout=timeout)
        sr = self.make_source_result(
            data=subs, source=self.name, start_time=start,
            error=err, error_type=err_type
        )

        for sub in subs:
            self.storage.add_domain(
                task_id=self.task_id, domain=sub,
                source=self.name, confidence=70
            )

        return ModuleResult(
            success=sr.ok, target=target, found=len(subs),
            duration_seconds=time.time() - start,
            sources=[sr], errors=[err] if err else [],
            metadata={"source": "virustotal.com", "timeout": timeout, "has_key": bool(api_key)}
        )
