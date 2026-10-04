"""arl_lite.mcp.server

MCP (Model Context Protocol) server — 暴露 arl-lite 能力给 AI Agent。

设计:
- stdio JSON-RPC 2.0
- 4 个 tools: query_assets / search_findings / get_risk / run_correlate
- 零 pip 依赖(纯 stdlib)
- 简单、健壮、可中断
- 不影响 arl-lite 主进程
"""
from __future__ import annotations

from .. import __version__

import json
import logging
import sys
from typing import Any

log = logging.getLogger("arl_lite.mcp.server")


def _reject_constant(name: str):
    """json.loads 的 parse_constant 钩子:NaN/Infinity 一律拒绝"""
    raise ValueError(f"non-JSON constant: {name}")


# 未 initialize 时的唯一错误文案来源。给 AI 客户端看,必须自带出路:
# 只说「没初始化」而不说下一步做什么,调用方无从纠正。
_NOT_INITIALIZED = "not initialized: send an 'initialize' request first"


def _error_text(e: BaseException) -> str:
    """把异常变成一句能照着改的文本 —— 异常类名 + 真实消息。

    只回 `type(e).__name__` 等于把 handlers 里写好的诊断信息全丢掉:
    「missing required argument: table」和「limit must be 1..1000」
    会塌缩成同一句 `ValueError`,调用方无从分辨自己错在哪。
    """
    detail = str(e).strip()
    head = f"tool error: {type(e).__name__}"
    return f"{head}: {detail}" if detail else head


class MCPServer:
    """极简 MCP server — stdio JSON-RPC 2.0

    协议:
    1. 读一行 JSON:{"jsonrpc":"2.0","id":N,"method":"M","params":{...}}
    2. 写一行 JSON:{"jsonrpc":"2.0","id":N,"result":...} or {"error":...}
    3. method:initialize/tools/call/resources/read
    """

    def __init__(self, workspace: str = "default") -> None:
        self.workspace = workspace
        self.tools: dict[str, dict] = {}
        self._register_default_tools()
        self._initialized = False

    def _register_default_tools(self) -> None:
        """注册 4 个默认 tools"""
        self.tools = {
            "query_assets": {
                "name": "query_assets",
                "description": "查询 arl-lite 工作空间里的资产(域名/IP/端口/站点/finding/correlation)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "table": {
                            "type": "string",
                            "enum": ["domains", "hosts", "ports", "sites", "findings", "correlations", "tasks"],
                            "description": "表名",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "最多返回多少行(1-1000,默认 50)",
                            "default": 50,
                            "minimum": 1,
                            "maximum": 1000,
                        },
                    },
                    "required": ["table"],
                },
            },
            "search_findings": {
                "name": "search_findings",
                "description": "FTS5 全文搜索 findings / sites / domains",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "table": {
                            "type": "string",
                            "enum": ["sites", "domains", "findings"],
                            "description": "FTS5 表名",
                        },
                        "keyword": {
                            "type": "string",
                            "description": "搜索关键词",
                            "minLength": 1,
                        },
                        "limit": {
                            "type": "integer",
                            "description": "最多返回多少行(1-100)",
                            "default": 20,
                            "minimum": 1,
                            "maximum": 100,
                        },
                    },
                    "required": ["table", "keyword"],
                },
            },
            "get_risk": {
                "name": "get_risk",
                "description": "获取工作空间的风险画像(从 correlations 计算)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "top_n": {
                            "type": "integer",
                            "description": "返回 top N 高风险资产(1-100,默认 10)",
                            "default": 10,
                            "minimum": 1,
                            "maximum": 100,
                        },
                    },
                },
            },
            "run_correlate": {
                "name": "run_correlate",
                "description": "跑关联分析(YAML 规则)并返回新命中的关联",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "min_risk": {
                            "type": "integer",
                            "description": "最小风险等级(0-10,默认 0)",
                            "default": 0,
                            "minimum": 0,
                            "maximum": 10,
                        },
                        "limit": {
                            "type": "integer",
                            "description": "最多返回多少条(1-500,默认 50)",
                            "default": 50,
                            "minimum": 1,
                            "maximum": 500,
                        },
                    },
                },
            },
        }

    def handle_request(self, req: dict) -> dict | None:
        """处理一个 JSON-RPC request,返回 response dict(或 None for notification)"""
        if not isinstance(req, dict):
            return {
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": f"invalid request: {type(req).__name__}"},
            }
        method = req.get("method", "")
        req_id = req.get("id")
        # params 必须是 dict:客户端发 "params":"hello"/null/[1] 时,
        # 后续 params.get() 的 AttributeError 会杀死整个 server 进程
        params = req.get("params", {})
        if not isinstance(params, dict):
            params = {}

        if method == "initialize":
            return self._handle_initialize(req_id, params)
        if method == "tools/list":
            return self._handle_tools_list(req_id)
        if method == "tools/call":
            return self._handle_tools_call(req_id, params)
        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}

        # 没 method — 错误(即使没 id 也要响应)
        if not method:
            return {
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32600, "message": "missing method"},
            }
        # 未知 method(无 id 是通知,不响应)
        if req_id is None:
            return None
        return {
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }

    def _handle_initialize(self, req_id: Any, params: dict) -> dict:
        self._initialized = True
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {
                    "name": "arl-lite-mcp",
                    "version": __version__,
                },
                "capabilities": {"tools": {}},
            },
        }

    def _handle_tools_list(self, req_id: Any) -> dict:
        if not self._initialized:
            return {
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32000, "message": _NOT_INITIALIZED},
            }
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {"tools": list(self.tools.values())},
        }

    def _handle_tools_call(self, req_id: Any, params: dict) -> dict:
        if not self._initialized:
            return {
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32000, "message": _NOT_INITIALIZED},
            }
        name = params.get("name", "")
        args = params.get("arguments", {})
        if not isinstance(args, dict):
            args = {}

        # name 非 str 会让 `name not in self.tools` 抛 TypeError(dict 成员
        # 测试要求可哈希),同样能杀死 server
        if not isinstance(name, str) or name not in self.tools:
            return {
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32602, "message": f"unknown tool: {name!s}"},
            }

        try:
            result = self._call_tool(name, args)
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}],
                    "isError": False,
                },
            }
        except Exception as e:
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": _error_text(e)}],
                    "isError": True,
                },
            }

    def _call_tool(self, name: str, args: dict) -> Any:
        """实际执行 tool"""
        # 延迟 import(避免启动时连 db)
        from ..db.storage import Storage
        from pathlib import Path as _Path
        import os as _os

        # 动态 workspace_root(支持 test 改 HOME)
        ws_root = _Path(_os.environ.get("HOME", _Path.home())) / ".arl-lite" / "workspaces"

        if name == "query_assets":
            table = args.get("table", "")
            if not table:
                raise ValueError("missing required argument: table")
            if not (1 <= args.get("limit", 50) <= 1000):
                raise ValueError("limit must be 1..1000")
            limit = args.get("limit", 50)
            storage = Storage(workspace=self.workspace, workspace_root=ws_root)
            return storage.query(table, limit=limit)

        if name == "search_findings":
            table = args.get("table", "")
            keyword = args.get("keyword", "")
            if not table:
                raise ValueError("missing required argument: table")
            if not keyword:
                raise ValueError("missing required argument: keyword")
            if not (1 <= args.get("limit", 20) <= 100):
                raise ValueError("limit must be 1..100")
            limit = args.get("limit", 20)
            storage = Storage(workspace=self.workspace, workspace_root=ws_root)
            return storage.search(table, keyword, limit=limit)

        if name == "get_risk":
            from ..core.risk_score import top_risks, risk_summary
            top_n = args.get("top_n", 10)
            if not (1 <= top_n <= 100):
                raise ValueError("top_n must be 1..100")
            storage = Storage(workspace=self.workspace, workspace_root=ws_root)
            top = top_risks(storage, limit=top_n)
            summary = risk_summary(storage)
            return {
                "summary": summary,
                "top": [
                    {
                        "target": r.target,
                        "score": r.risk_score,
                        "level": r.risk_level,
                        "rule_count": r.rule_count,
                    }
                    for r in top
                ],
            }

        if name == "run_correlate":
            from ..core.correlation_engine import run_all_rules, save_correlations
            min_risk = args.get("min_risk", 0)
            if not (0 <= min_risk <= 10):
                raise ValueError("min_risk must be 0..10")
            limit = args.get("limit", 50)
            if not (1 <= limit <= 500):
                raise ValueError("limit must be 1..500")
            storage = Storage(workspace=self.workspace, workspace_root=ws_root)
            rules_dir = _Path(__file__).parent.parent / "modules" / "analysis" / "rules"
            hits = run_all_rules(storage, rules_dir)
            n = save_correlations(storage, hits)
            hits = [h for h in hits if h.risk >= min_risk][:limit]
            return {
                "new_correlations_saved": n,
                "returned": len(hits),
                "hits": [
                    {
                        "rule": h.rule_name,
                        "target": h.target,
                        "target_type": h.target_type,
                        "risk": h.risk,
                        "headline": h.headline,
                    }
                    for h in hits
                ],
            }

        raise ValueError(f"unknown tool: {name}")

    def serve_forever(self) -> int:
        """stdio serve 循环,EOF 或 KeyboardInterrupt 退出"""
        log.info("MCP server started (workspace=%s)", self.workspace)
        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    # parse_constant:拒绝 NaN/Infinity(严格 JSON 不允许,
                    # json.dumps(allow_nan=False) 回吐它们会产出非法响应行)
                    req = json.loads(line, parse_constant=_reject_constant)
                except json.JSONDecodeError:
                    err = {
                        "jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "parse error: invalid JSON"},
                    }
                    print(json.dumps(err, ensure_ascii=False), flush=True)
                    continue
                except ValueError:
                    err = {
                        "jsonrpc": "2.0", "id": None,
                        "error": {"code": -32600, "message": "invalid request: NaN/Infinity not allowed"},
                    }
                    print(json.dumps(err, ensure_ascii=False), flush=True)
                    continue
                try:
                    response = self.handle_request(req)
                except Exception as e:
                    # 单条请求的处理异常绝不能杀死 server(否则一条畸形报文
                    # 就终结整个会话)
                    log.exception("MCP handle_request crashed")
                    response = {
                        "jsonrpc": "2.0", "id": req.get("id") if isinstance(req, dict) else None,
                        "error": {"code": -32603, "message": f"internal error: {_error_text(e)}"},
                    }
                if response is not None:
                    try:
                        out = json.dumps(response, ensure_ascii=False, default=str, allow_nan=False)
                    except ValueError:
                        out = json.dumps({
                            "jsonrpc": "2.0", "id": response.get("id") if isinstance(response, dict) else None,
                            "error": {"code": -32603, "message": "internal error: non-serializable response"},
                        }, ensure_ascii=False)
                    print(out, flush=True)
        except (KeyboardInterrupt, EOFError):
            log.info("MCP server stopped")
        return 0
