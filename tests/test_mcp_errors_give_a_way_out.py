"""MCP 错误面必须给出路 —— 端到端走 stdio 真实进程。

背景:arl_lite/mcp/server.py 原来把所有工具异常压成 `tool error: {type(e).__name__}`,
handlers 里写好的诊断信息(`missing required argument: table`、`limit must be 1..1000`)
全被丢掉。实测 6 种不同的用户错误塌缩成同一句 `ValueError`,调用方无从分辨自己错在哪。
`not initialized` 也只说「没初始化」不说下一步做什么。

这些是给 AI 客户端看的消息 —— 不可执行的报错比不给更坏。
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


def _mcp_session(requests: list[dict], home: Path, handshake: bool = True) -> dict[int, dict]:
    """把一批请求喂给真实的 `arl-lite mcp` 进程,返回 {id: 响应}。

    每个场景用独立干净 HOME(共用 HOME 会造成顺序污染假数据)。
    handshake=False 时不发 initialize —— 用来测未初始化的路径。
    """
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(REPO)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    lines = (HANDSHAKE if handshake else []) + requests
    payload = "".join(json.dumps(r) + "\n" for r in lines)
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "arl_lite.cli", "mcp"],
        input=payload, env=env, cwd=tempfile.gettempdir(),
        capture_output=True, text=True, timeout=120,
    )
    out: dict[int, dict] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if isinstance(obj.get("id"), int):
            out[obj["id"]] = obj
    return out


def _call(name: str, arguments: dict, req_id: int = 9) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


def _tool_text(resp: dict) -> str:
    """从 tools/call 响应里取出给调用方看的文本。"""
    return " | ".join(c.get("text", "") for c in resp["result"]["content"])


# (场景, tool, arguments, 消息里必须出现的诊断片段)
ERROR_CASES = [
    ("query_assets 缺 table",       "query_assets",    {},                                    "missing required argument: table"),
    ("search_findings 缺 table",    "search_findings", {"keyword": "x"},                     "missing required argument: table"),
    ("search_findings 缺 keyword",  "search_findings", {"table": "sites"},                   "missing required argument: keyword"),
    ("query_assets 未知 table",     "query_assets",    {"table": "nope"},                     "not in whitelist"),
    ("search_findings FTS 不可用",  "search_findings", {"table": "nope", "keyword": "x"},     "FTS not available"),
    ("query_assets limit 越界",     "query_assets",    {"table": "domains", "limit": 9999},   "limit must be 1..1000"),
    ("search_findings limit 越界",  "search_findings", {"table": "sites", "keyword": "x", "limit": 9999}, "limit must be 1..100"),
    ("get_risk top_n 越界",         "get_risk",        {"top_n": 0},                         "top_n must be 1..100"),
    ("run_correlate min_risk 越界", "run_correlate",   {"min_risk": 99},                     "min_risk must be 0..10"),
    ("run_correlate limit 越界",    "run_correlate",   {"limit": 0},                         "limit must be 1..500"),
]


@pytest.fixture(scope="module")
def error_responses() -> dict[int, dict]:
    with tempfile.TemporaryDirectory() as td:
        yield _mcp_session(
            [_call(n, a, req_id=100 + i) for i, (_, n, a, _) in enumerate(ERROR_CASES)],
            Path(td),
        )


@pytest.mark.parametrize("idx,case", list(enumerate(ERROR_CASES)),
                         ids=[c[0] for c in ERROR_CASES])
def test_each_mistake_names_its_own_fix(idx, case, error_responses):
    """每种用户错误都必须报出自己的那一条诊断,而不是一个裸的异常类名。"""
    _, name, args, expected = case
    resp = error_responses[100 + idx]
    text = _tool_text(resp)
    assert resp["result"]["isError"] is True, f"{name} 应当报 isError"
    assert expected in text, (
        f"{name}: 报错里没有诊断片段 {expected!r} —— 实际收到 {text!r}。"
        f"异常消息被吞掉时所有场景都会塌缩成 'tool error: ValueError'。"
    )


def test_no_mcp_error_is_just_a_bare_exception_class(error_responses):
    """任何工具报错都不能只剩一个异常类名。

    反向不变量:只回 `type(e).__name__` 就等于丢掉全部诊断信息。
    连 `str(e)` 为空的异常也必须回一句不带裸类名的文本。
    """
    bare = []
    for i, (label, _, _, _) in enumerate(ERROR_CASES):
        text = _tool_text(error_responses[100 + i])
        head = text.split(":", 2)  # "tool error" / 类名 / 详情
        if len(head) < 3 or not head[2].strip():
            bare.append(f"{label} -> {text!r}")
    assert not bare, f"这些报错只剩一个裸异常类名,没有任何可执行信息:{bare}"


def test_distinct_mistakes_stay_distinguishable(error_responses):
    """不同类型的错误不能塌缩成同一句话。

    这是本条判据的核心:修好之前,缺 table / 缺 keyword / limit 越界 / 未知 table
    全部返回同一句 `tool error: ValueError`,调用方只能靠猜。
    """
    # 同一 session 里逐条收集各自消息(这些都是只读错误路径,不碰库)
    with tempfile.TemporaryDirectory() as td:
        resp = _mcp_session(
            [_call(n, a, req_id=100 + i) for i, (_, n, a, _) in enumerate(ERROR_CASES)],
            Path(td),
        )
    texts = [_tool_text(resp[100 + i]) for i in range(len(ERROR_CASES))]
    groups: dict[str, list[str]] = {}
    for (label, _, _, _), t in zip(ERROR_CASES, texts):
        groups.setdefault(t, []).append(label)
    collapsed = {t: labels for t, labels in groups.items() if len(labels) > 1}
    # 允许「缺 table」在 query_assets / search_findings 间共用同一句
    # (它们是同一个错误),但除此之外任何两条都不该同句。
    allowed = {frozenset({"query_assets 缺 table", "search_findings 缺 table"})}
    unexpected = {
        t: labels for t, labels in collapsed.items()
        if frozenset(labels) not in allowed
    }
    assert not unexpected, (
        f"这些互不相同的错误塌缩成了同一句报错,调用方无从分辨:{unexpected}"
    )


def test_not_initialized_says_how_to_fix_it():
    """`not initialized` 必须自带出路:告诉调用方先发 initialize。"""
    with tempfile.TemporaryDirectory() as td:
        resp = _mcp_session(
            [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "get_risk", "arguments": {}}},
            ],
            Path(td),
            handshake=False,
        )
    for rid, label in ((1, "tools/list"), (2, "tools/call")):
        msg = resp[rid]["error"]["message"]
        # 注意:不能只 assert "initialize" in msg —— "not initialized" 本身就
        # 含有 "initialize" 子集(initialized = initialize + d),那条断言恒真,
        # 恰恰被它要排除的旧文案喂饱(M3 变异实测存活)。真正的契约是
        # 「not initialized 之后还必须有出路」,所以查诊断词之后是否还有内容。
        tail = msg.split("not initialized", 1)[-1].strip()
        assert tail, (
            f"{label} 的 not-initialized 报错没有说出路: {msg!r}。"
            f"只说「没初始化」而不说下一步做什么,调用方无从纠正。"
        )
    assert resp[1]["error"]["message"] == resp[2]["error"]["message"], (
        "tools/list 和 tools/call 的 not-initialized 报错应当同源,不能各写一份"
    )


def test_healthy_calls_are_not_broken_by_error_reporting():
    """改动只影响错误路径:正常调用仍然 isError=False。"""
    with tempfile.TemporaryDirectory() as td:
        resp = _mcp_session(
            [
                _call("get_risk", {}, req_id=9),
                _call("query_assets", {"table": "domains"}, req_id=10),
                _call("run_correlate", {}, req_id=11),
            ],
            Path(td),
        )
    for rid, name in ((9, "get_risk"), (10, "query_assets"), (11, "run_correlate")):
        assert resp[rid]["result"]["isError"] is False, (
            f"{name} 本该正常返回,却报了错:{_tool_text(resp[rid])}"
        )
