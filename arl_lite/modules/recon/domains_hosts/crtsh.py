"""arl_lite.modules.recon.domains_hosts.crtsh

crt.sh 证书透明度查询模块(被动,纯 HTTP,无需外部工具)。

行为:
1. 调 crt.sh API
2. 解析返回的 JSON
3. 把每个子域 upsert 到 domains 表
4. source_status 自动记录
"""
from __future__ import annotations

import logging
import time

from ....core.base_module import BaseModule, ModuleResult
from ....integrations.crtsh import collect_subdomains

log = logging.getLogger("arl_lite.modules.crtsh")


class CrtshModule(BaseModule):
    name = "crtsh"
    category = "recon/domains-hosts"
    description = "通过 crt.sh 证书透明度日志查询子域(被动,无需 token)"
    input_type = "domain"
    output_type = "domain"
    required_tools = []  # crt.sh 不需要外部工具,纯 HTTP
    author = "arl-lite"
    version = "0.1.0"

    async def run(self, target: str, **kwargs) -> ModuleResult:
        start = time.time()
        timeout = int(kwargs.get("timeout", 60))

        subs, err, err_type = await collect_subdomains(target, timeout=timeout)
        sr = self.make_source_result(
            data=subs, source=self.name, start_time=start,
            error=err, error_type=err_type
        )

        # 入库
        from ....db.storage import Storage
        workspace = self.config.get("workspace", "default")
        storage = self.storage or Storage(workspace=workspace)

        for sub in subs:
            storage.add_domain(
                task_id=self.task_id, domain=sub,
                source=self.name, confidence=60
            )

        return ModuleResult(
            success=sr.ok, target=target, found=len(subs),
            duration_seconds=time.time() - start,
            sources=[sr], errors=[err] if err else [],
            metadata={"source": "crt.sh", "timeout": timeout}
        )
