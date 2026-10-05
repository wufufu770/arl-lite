"""MCP 工具不得静默忽略未声明的参数 —— 端到端走 stdio 真实进程。

背景:4 个 MCP 工具(query_assets / search_findings / get_risk / run_correlate)
只读启动时的 `self.workspace`,从不读 per-call 的 `workspace` key。
实测传 `workspace: teamA` 拿到的是**启动工作区**的数据,`isError` 还是 False ——
AI 客户端会拿它当 teamA 的资产清单去汇报。

静默忽略比报错危险得多:它把一个错误答案包装成成功答案。
所以未声明的 key 一律报错,且合法 key 列表**从工具自己的 inputSchema 推导**
(schema 是唯一来源,不许另抄一份 —— 抄一份必然漂移)。

本判据不硬编 schema:它先 `tools/list` 问服务器自己声明了什么,
再拿报错里的合法列表跟它逐字比。手抄名单当场对不上。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HANDSHAKE = [
    {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
]


def _session(requests: list[dict], home: Path) -> dict[int, dict]:
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(REPO)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    payload = "".join(json.dumps(r) + "\n" for r in HANDSHAKE + requests)
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "arl_lite.cli", "mcp"],
        input=payload, env=env, cwd=tempfile.gettempdir(),
        capture_output=True, text=True, timeout=120,
    )
    out: dict[int, dict] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            obj = json.loads(line)
            if isinstance(obj.get("id"), int):
                out[obj["id"]] = obj
    return out


def _call(name: str, arguments: dict, req_id: int) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


def _text(resp: dict) -> str:
    return " | ".join(c.get("text", "") for c in resp["result"]["content"])


@pytest.fixture(scope="module")
def schemas() -> dict[str, list[str]]:
    """问服务器自己声明了什么(不硬编 schema,避免测试和实现一起漂)。"""
    with tempfile.TemporaryDirectory() as td:
        resp = _session([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}], Path(td))
    tools = resp[1]["result"]["tools"]
    return {
        t["name"]: sorted((t.get("inputSchema") or {}).get("properties") or {})
        for t in tools
    }


# 拼错的近邻 key:证明这不是模糊/前缀匹配,而是按 schema 精确判定
MISSPELLED = {
    "query_assets": {"tabel": "domains"},
    "search_findings": {"keywordk": "x"},
    "get_risk": {"top_N": 5},
    "run_correlate": {"minRisk": 1},
}


def test_every_tool_is_covered(schemas):
    """4 个工具一个都不能漏 —— 本轮实测的正是「四个都只读 self.workspace」。"""
    assert set(schemas) == {"query_assets", "search_findings", "get_risk", "run_correlate"}, (
        f"工具集合变了,判据要跟着改:{sorted(schemas)}"
    )


def test_workspace_is_never_silently_ignored(schemas):
    """给 workspace 必须报错,不能悄悄按启动工作区返回。"""
    names = sorted(schemas)
    with tempfile.TemporaryDirectory() as td:
        resp = _session(
            [_call(n, {"workspace": "teamA"}, req_id=50 + i) for i, n in enumerate(names)],
            Path(td),
        )
    for i, name in enumerate(names):
        r = resp[50 + i]["result"]
        assert r["isError"] is True, (
            f"{name} 收到 workspace=teamA 却没报错,却按启动工作区返回了数据。"
            f"这是把错误答案包装成成功答案:{_text(resp[50 + i])}"
        )
        assert "workspace" in _text(resp[50 + i]), (
            f"{name} 的报错里没点名 workspace:{_text(resp[50 + i])}"
        )


def test_workspace_error_says_where_workspace_actually_goes():
    """workspace 传不进来的那条路要说清楚:它由启动时的 -w 决定。"""
    with tempfile.TemporaryDirectory() as td:
        resp = _session([_call("query_assets", {"table": "domains", "workspace": "teamA"}, 61)], Path(td))
    text = _text(resp[61])
    assert "-w" in text, (
        f"workspace 的报错没指出真正的入口(启动时的 -w):{text!r}"
    )


def test_misspelled_keys_are_rejected(schemas):
    """拼错的 key 要报错 —— 否则拼写错误会静默变成「用默认值跑」。"""
    names = sorted(MISSPELLED)
    with tempfile.TemporaryDirectory() as td:
        resp = _session(
            [_call(n, MISSPELLED[n], req_id=70 + i) for i, n in enumerate(names)],
            Path(td),
        )
    for i, name in enumerate(names):
        r = resp[70 + i]["result"]
        assert r["isError"] is True, (
            f"{name} 的拼写错误 key {list(MISSPELLED[name])} 被静默接受了:"
            f"{_text(resp[70 + i])}"
        )
        bad_key = next(iter(MISSPELLED[name]))
        assert bad_key in _text(resp[70 + i]), (
            f"{name} 的报错没点名是哪个 key 不认识:{_text(resp[70 + i])}"
        )


def test_accepted_list_matches_advertised_schema(schemas):
    """报错里列的合法 key,必须与 tools/list 声明的逐字一致。

    这是「唯一来源」的行为化守卫:合法名单若是从别处手抄的,
    schema 一改就会对不上,当场报红。
    """
    names = sorted(schemas)
    with tempfile.TemporaryDirectory() as td:
        resp = _session(
            [_call(n, {"__probe__": 1}, req_id=90 + i) for i, n in enumerate(names)],
            Path(td),
        )
    for i, name in enumerate(names):
        text = _text(resp[90 + i])
        marker = f"{name} accepts: "
        assert marker in text, f"{name} 的报错没列出合法 key:{text!r}"
        listed = [p.strip() for p in text.split(marker, 1)[1].split(";")[0].split(",")]
        assert listed == schemas[name], (
            f"{name} 报错里列的合法 key {listed} 与 tools/list 声明的 "
            f"{schemas[name]} 对不上 —— 合法名单不是从 schema 推导的"
        )


def test_declared_keys_are_never_rejected(schemas):
    """schema 声明过的 key 全部必须能传 —— 防止「一律拒绝」式的过度修正。

    没有这一条,把未知 key 判据写成「任何调用都报错」也能通过上一条测试。
    """
    calls, expect = [], []
    req_id = 110
    for name, keys in sorted(schemas.items()):
        args: dict = {}
        if "table" in keys:
            args["table"] = "sites" if name == "search_findings" else "domains"
        if "keyword" in keys:
            args["keyword"] = "x"
        for k in keys:
            args.setdefault(k, {"limit": 5, "top_n": 5, "min_risk": 1}.get(k, 1))
        calls.append(_call(name, args, req_id))
        expect.append((name, args))
        req_id += 1
    with tempfile.TemporaryDirectory() as td:
        resp = _session(calls, Path(td))
    for i, (name, args) in enumerate(expect):
        r = resp[110 + i]["result"]
        assert r["isError"] is False, (
            f"{name} 传的全是 schema 声明过的 key ({sorted(args)}) 却仍被拒:"
            f"{_text(resp[110 + i])} —— 判据变成了无差别拒绝"
        )
