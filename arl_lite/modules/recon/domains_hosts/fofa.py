"""arl_lite.modules.recon.domains_hosts.fofa

FOFA 被动子域查询模块(需要 API key + email)。
"""
from __future__ import annotations

import logging
import time

from ....core.base_module import BaseModule, ModuleResult
from ....integrations.fofa import collect_subdomains

log = logging.getLogger("arl_lite.modules.fofa")


class FofaModule(BaseModule):
    name = "fofa"
    category = "recon/domains-hosts"
    description = "通过 FOFA 网络空间测绘被动查询子域(需要 API key + email)"
    input_type = "domain"
    output_type = "domain"
    required_tools = []
    author = "arl-lite"
    version = "0.2.0"

    async def run(self, target: str, **kwargs) -> ModuleResult:
        start = time.time()
        timeout = int(kwargs.get("timeout", 30))
        api_key = kwargs.get("api_key") or self.config.get("fofa_api_key")
        email = kwargs.get("email") or self.config.get("fofa_email")

        subs, err, err_type = await collect_subdomains(
            target, api_key=api_key, email=email, timeout=timeout
        )
        sr = self.make_source_result(
            data=subs, source=self.name, start_time=start,
            error=err, error_type=err_type
        )

        for sub in subs:
            self.storage.add_domain(
                task_id=self.task_id, domain=sub,
                source=self.name, confidence=80
            )

        return ModuleResult(
            success=sr.ok, target=target, found=len(subs),
            duration_seconds=time.time() - start,
            sources=[sr], errors=[err] if err else [],
            metadata={"source": "fofa.info", "timeout": timeout, "has_key": bool(api_key and email)}
        )
