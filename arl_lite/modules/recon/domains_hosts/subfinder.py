"""arl_lite.modules.recon.domains_hosts.subfinder

subfinder 子域枚举模块(主动)。

行为:
1. 检查 subfinder 是否安装(没有就 3 态返回失败,不抛异常)
2. 调用 subfinder 拿结果
3. 把每个子域 upsert 到 domains 表
4. 记录 source_status(成功/失败 + 耗时 + 错误类型)
"""
from __future__ import annotations

import logging
import time

from ....core.base_module import BaseModule, ModuleResult
from ....integrations.subfinder import collect_subdomains, is_available

log = logging.getLogger("arl_lite.modules.subfinder")


class SubfinderModule(BaseModule):
    name = "subfinder"
    category = "recon/domains-hosts"
    description = "通过 subfinder 枚举子域名(主动,聚合 30+ 被动源)"
    input_type = "domain"
    output_type = "domain"
    required_tools = ["subfinder"]
    author = "arl-lite"
    version = "0.1.0"

    def pre_check(self) -> tuple[bool, str]:
        if not is_available():
            return False, "subfinder not installed. Install: go install -v github.com/projectdiscovery/subfinder/v2/cmd/subfinder@latest"
        return True, ""

    async def run(self, target: str, **kwargs) -> ModuleResult:
        start = time.time()
        timeout = int(kwargs.get("timeout", 300))

        ok, msg = self.pre_check()
        if not ok:
            log.warning(f"subfinder pre_check failed: {msg}")
            sr = self.make_source_result(
                data=[], source=self.name, start_time=start,
                error=msg, error_type="network"
            )
            return ModuleResult(
                success=False, target=target, found=0,
                duration_seconds=time.time() - start,
                sources=[sr], errors=[msg],
                metadata={"reason": "tool_missing"}
            )

        subs, err, err_type = await collect_subdomains(target, timeout=timeout)
        sr = self.make_source_result(
            data=subs, source=self.name, start_time=start,
            error=err, error_type=err_type
        )

        # 入库(由 TaskRunner 在跑完所有 module 后统一记录 source_status)
        # 这里只上送数据,不入库(留给 TaskRunner 或 Module 自己)
        # Phase 1 简化:Module 直接入库
        from ....db.storage import Storage
        workspace = self.config.get("workspace", "default")
        storage = self.storage or Storage(workspace=workspace)

        for sub in subs:
            storage.add_domain(
                task_id=self.task_id, domain=sub,
                source=self.name, confidence=70
            )

        return ModuleResult(
            success=sr.ok, target=target, found=len(subs),
            duration_seconds=time.time() - start,
            sources=[sr], errors=[err] if err else [],
            metadata={"tool": "subfinder", "timeout": timeout}
        )
