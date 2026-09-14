"""arl_lite.mcp — Model Context Protocol server

让 AI Agent 通过 stdio JSON-RPC 2.0 协议访问 arl-lite 资产数据。

启动:
    python3 -m arl_lite mcp
    python3 -m arl_lite mcp --workspace demo

4 个内置 tools:
- query_assets  查询资产
- search_findings FTS5 搜索
- get_risk  风险画像
- run_correlate  跑关联分析

零 pip 依赖(纯 stdlib json + sys + signal)
"""
from .server import MCPServer

__all__ = ["MCPServer"]
