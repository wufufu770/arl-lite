"""arl_lite.modules.recon.domains_hosts.quake

360 Quake 被动子域查询模块(需要 API token)。
"""
from __future__ import annotations

import logging
import time

from ....core.base_module import BaseModule, ModuleResult
from ....integrations.quake import collect_subdomains

log = logging.getLogger("arl_lite.modules.quake")


class QuakeModule(BaseModule):
    name = "quake"
    category = "recon/domains-hosts"
    description = "通过 360 Quake 被动查询子域(需要 API token)"
    input_type = "domain"
    output_type = "domain"
    required_tools = []
    author = "arl-lite"
    version = "0.2.0"

    async def run(self, target: str, **kwargs) -> ModuleResult:
        start = time.time()
        timeout = int(kwargs.get("timeout", 30))
        api_key = kwargs.get("api_key") or self.config.get("quake_api_key")

        subs, err, err_type = await collect_subdomains(target, api_key=api_key, timeout=timeout)
        sr = self.make_source_result(
            data=subs, source=self.name, start_time=start,
            error=err, error_type=err_type
        )

        for sub in subs:
            self.storage.add_domain(
                task_id=self.task_id, domain=sub,
                source=self.name, confidence=75
            )

        return ModuleResult(
            success=sr.ok, target=target, found=len(subs),
            duration_seconds=time.time() - start,
            sources=[sr], errors=[err] if err else [],
            metadata={"source": "quake.360.net", "timeout": timeout, "has_key": bool(api_key)}
        )
