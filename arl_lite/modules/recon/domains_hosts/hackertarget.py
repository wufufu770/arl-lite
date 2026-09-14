"""arl_lite.modules.recon.domains_hosts.hackertarget

Hackertarget 被动子域查询模块(无需 token)。
"""
from __future__ import annotations

import logging
import time

from ....core.base_module import BaseModule, ModuleResult
from ....integrations.hackertarget import collect_subdomains

log = logging.getLogger("arl_lite.modules.hackertarget")


class HackertargetModule(BaseModule):
    name = "hackertarget"
    category = "recon/domains-hosts"
    description = "通过 HackerTarget 被动查询子域(无需 token)"
    input_type = "domain"
    output_type = "domain"
    required_tools = []
    author = "arl-lite"
    version = "0.2.0"

    async def run(self, target: str, **kwargs) -> ModuleResult:
        start = time.time()
        timeout = int(kwargs.get("timeout", 30))

        subs, err, err_type, ip_map = await collect_subdomains(target, timeout=timeout)
        sr = self.make_source_result(
            data=subs, source=self.name, start_time=start,
            error=err, error_type=err_type
        )

        for sub in subs:
            self.storage.add_domain(
                task_id=self.task_id, domain=sub,
                source=self.name, confidence=55,
                resolved_ip=ip_map.get(sub),
            )

        return ModuleResult(
            success=sr.ok, target=target, found=len(subs),
            duration_seconds=time.time() - start,
            sources=[sr], errors=[err] if err else [],
            metadata={"source": "hackertarget.com", "timeout": timeout}
        )
