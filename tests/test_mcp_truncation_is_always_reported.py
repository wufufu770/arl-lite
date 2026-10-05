"""MCP 工具不许静默截断 —— 而且 `run_correlate` 以前根本跑不起来

## 起因一:CLI 上建好的规矩,MCP 这条路径一条都没接上

`cli.py` 的 `_limit_notice_text` 早就把规矩定死了,连理由都写好了:

> 「静默地给全了」让用户分不清「这就是全部」和「恰好没超 limit」。

r56/r58/r64/r66 一路把这个规矩建到了 `query` / `search` /
`monitor changes` / `correlate` / `export` 上。**MCP 完全在射程之外。**

r96 实测(12 行数据、`limit=5`):

    query_assets     → 裸数组 5 条,没有任何字段说明还有 7 条
    search_findings  → 裸数组 4 条(真实 12 条),同样什么都不说
    get_risk         → {summary, top},`top` 是前 N 还是全部?分不清
    run_correlate    → `returned` 是返回了几条,但**一共命中几条**没有;
                       `new_correlations_saved` 是**落库数**,极易被读成「就这么多」

对 CLI 来说这让人少看几条。对 MCP 来说更糟 —— 读结果的是一个
**AI 客户端**,它会拿这 5 条当资产清单去汇报,而 `isError` 还是 False。

## 起因二:更要紧的 —— `run_correlate` 一命中就崩

修上面的截断时顺手造了真实命中(6 台开着 23/6379/9200/3306/27017 的机器),
立刻:

    AttributeError: 'CorrelationHit' object has no attribute 'target_type'

`CorrelationHit` 的 `target` 是个 **dict**,压根没有 `target_type` 这个属性。
**这个工具从来没成功执行过** —— 之前没暴露,是因为没有任何测试真的造出过
命中,0 命中时那段循环根本不进。

而正确的取法一直好好地内联在 `correlation_engine.save_correlations` 里,
MCP **自己抄了一份,抄错了**。这正是仓库决策 #9 说的「两处手抄同一段逻辑,
迟早漂」—— 漂了,而且漂成了崩。r96 把它抽成 `hit_identity()`,两边共用。

## 这份判据守什么

1. 四个工具**各自**都报 `returned` / `total` / `truncated`
2. 四个工具的信封**键名一致** —— 两返回裸数组、两返回对象的现状是缺陷
3. 没截断时 `truncated` 必须是 `False`,不能是缺字段
4. `total` 数不出来时是 `null`,不是 `0`(把查询失败伪装成空结果)
5. `min_risk` 是主动筛选,`total` 取过滤**后**的数 —— 和 CLI 同一个道理
6. `run_correlate` 有命中时不许崩,且 hit 的 `target`/`target_type` 要对
7. `hit_identity` 是**单一来源** —— MCP 和 `save_correlations` 必须共用它,
   防的就是这次这种「抄一份抄错了」
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from arl_lite.mcp import server as mcp_server  # noqa: E402

ENVELOPE_KEYS = {"returned", "total", "truncated"}
ALL_TOOLS = ("query_assets", "search_findings", "get_risk", "run_correlate")


@pytest.fixture
def ws(tmp_path):
    """一个装好数据的真实工作区:MCP 四个工具共用

    - 12 个域名(给 `query_assets` / `search_findings` 制造截断)
    - 6 台开着 23/6379/9200/3306/27017/22 的机器(给 `run_correlate` 制造
      **真实命中** —— 没有命中,那条路径压根不会执行)
    - 12 条 correlations(给 `get_risk` 制造可评分的资产)

    ## 为什么是**每测试独立**,不是模块级共享

    `run_correlate` 会 `save_correlations` **写进工作区**。模块级共享的话,
    它跑完之后 `get_risk` 看到的资产数就从 12 变成 30 了(规则新命中了
    6 台机器),于是 `assert r["total"] == 12` 在「先跑 correlate」时红、
    「后跑」时绿 —— **判据依赖跑在什么顺序下**,而那正是 r95 刚治好的病。

    实测过这个坑:第一版就是模块级共享,`test_get_risk_...` 排前面绿、
    排后面红(`total` 变成 30)。代价只有 0.04s/测试,换独立没有理由不换。
    """
    from arl_lite.db.storage import Storage

    home = tmp_path
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        root = home / ".arl-lite" / "workspaces"
        st = Storage(workspace="default", workspace_root=root)
        for i in range(12):
            st.add_domain(1, f"site{i:02d}.corp.example", "dns", risk=i % 11)
        for i in range(6):
            ip = f"10.0.0.{i + 1}"
            st.add_host(1, ip, ip=ip)
            for port in (23, 6379, 9200, 3306, 27017, 22):
                st.add_port(1, ip, port, state="open", service="probe")
        with st._conn() as conn:
            for i in range(12):
                conn.execute(
                    "INSERT OR IGNORE INTO correlations (workspace_id, rule_name,"
                    " target, target_type, risk, headline, confidence,"
                    " confidence_status) VALUES (?,?,?,?,?,?,?,?)",
                    (1, "probe_rule", f"site{i:02d}.corp.example", "domain",
                     i % 11, f"probe hit {i}", 50, "report"))
            conn.commit()
        srv = mcp_server.MCPServer(workspace="default")
        yield srv
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


# --- 1) 四个工具各自都报 ---

def test_query_assets_says_how_many_rows_there_were(ws):
    """12 行数据、limit=5 → 必须说清「一共 12 条」

    改之前返回的是**裸数组**,连键都没有。
    """
    r = ws._call_tool("query_assets", {"table": "domains", "limit": 5})
    assert ENVELOPE_KEYS <= set(r), f"信封缺键:{sorted(set(r))}"
    assert len(r["rows"]) == 5
    assert r["returned"] == 5
    assert r["total"] == 12, f"总数说错了:{r['total']}(真实 12)"
    assert r["truncated"] is True, "被截断了却没说截断"


def test_search_findings_says_how_many_hits_there_were(ws):
    """搜索同理 —— 12 条命中、limit=4"""
    r = ws._call_tool("search_findings",
                      {"table": "domains", "keyword": "corp", "limit": 4})
    assert ENVELOPE_KEYS <= set(r), f"信封缺键:{sorted(set(r))}"
    assert r["returned"] == 4
    assert r["total"] == 12, f"总数说错了:{r['total']}"
    assert r["truncated"] is True


def test_get_risk_says_whether_top_is_everything(ws):
    """`top` 是前 N 还是全部 —— 改之前分不清

    12 个资产、`top_n=3`。
    """
    r = ws._call_tool("get_risk", {"top_n": 3})
    assert ENVELOPE_KEYS <= set(r), f"信封缺键:{sorted(set(r))}"
    assert r["returned"] == 3
    assert r["total"] == 12, f"总数说错了:{r['total']}"
    assert r["truncated"] is True


def test_run_correlate_reports_total_and_does_not_crash(ws):
    """**本轮最要紧的一条**:有命中时以前直接抛 AttributeError

    `CorrelationHit` 没有 `target_type` 属性,而且 `target` 是 dict 不是 str。
    改之前这段代码只要命中数 ≥ 1 就崩 —— 而没有任何测试造出过命中。
    """
    r = ws._call_tool("run_correlate", {"limit": 2})
    assert ENVELOPE_KEYS <= set(r), f"信封缺键:{sorted(set(r))}"
    assert r["total"] > 2, f"真实命中应该多于 limit,实测 total={r['total']}"
    assert r["returned"] == 2
    assert r["truncated"] is True
    hit = r["hits"][0]
    assert isinstance(hit["target"], str), (
        f"target 以前取的是 dict:{hit['target']!r} —— 那不是能给人看的主键")
    assert hit["target_type"] in ("ip", "host", "domain", "site", "aggregate", "other"), (
        f"target_type 不认识:{hit['target_type']!r}")


# --- 2) 四个工具的键名一致 ---

def test_all_four_tools_share_the_same_envelope_keys(ws):
    """两返回裸数组、两返回对象 —— 这个现状本身就是缺陷

    AI 客户端只能逐个工具猜返回形状。统一之后它只需要记一套。
    """
    calls = {
        "query_assets": {"table": "domains", "limit": 5},
        "search_findings": {"table": "domains", "keyword": "corp", "limit": 4},
        "get_risk": {"top_n": 3},
        "run_correlate": {"limit": 2},
    }
    missing = {
        name: sorted(ENVELOPE_KEYS - set(ws._call_tool(name, args)))
        for name, args in calls.items()
    }
    missing = {k: v for k, v in missing.items() if v}
    assert not missing, f"这些工具没报截断信息:{missing}"


# --- 3) 没截断时也要说「没截断」 ---

def test_not_truncated_is_still_reported_as_false(ws):
    """limit 给够时 `truncated` 必须是 `False`,不能是缺字段

    缺字段和 `False` 对客户端是两回事:前者要猜,后者是答案。
    """
    r = ws._call_tool("query_assets", {"table": "domains", "limit": 1000})
    assert r["returned"] == r["total"] == 12
    assert r["truncated"] is False, (
        f"没截断却没说清楚:{r['truncated']!r} —— 缺字段和 False 不是一回事")


# --- 4) `total` 数不出来时是 null,不是 0 ---

def test_unknown_total_is_reported_as_null_not_zero():
    """**把查询失败伪装成空结果**,和 cli.py 的 `total is None` 同一个病

    `Storage.count_search` 在 FTS5 出错、search 退回 LIKE 时数的是另一个
    集合,拿不出诚实的总数,于是返回 `None`。这时信封必须写 `null`。

    判 `_page` 这个纯函数:它数不出来的时候,唯一能做的就是别编一个数出来。
    """
    env = mcp_server._page(["a", "b"], None, "rows")
    assert env["total"] is None, f"数不出来被写成了 {env['total']!r}"
    assert env["truncated"] is None, (
        f"总数未知时 `truncated` 必须也是未知,不能是 False:{env['truncated']!r}"
        f" —— False 会被读成「没截断」")
    ok = mcp_server._page(["a", "b"], 2, "rows")
    assert ok["truncated"] is False and ok["total"] == 2


# --- 5) min_risk 是主动筛选,不是截断 ---

def test_min_risk_filters_the_total_not_the_limit(ws):
    """`min_risk` 过滤**之后**的数才是 total,和 CLI 同一个道理

    `cli.py:675` 写得很清楚:`min_risk` 是用户**主动**要求的筛选,
    `limit` 才是隐式的上界。把两者的数混起来,调用方就分不清
    「我筛掉了多少」和「服务器截掉了多少」。
    """
    all_hits = ws._call_tool("run_correlate", {"limit": 500})
    hi = ws._call_tool("run_correlate", {"limit": 500, "min_risk": 8})
    assert hi["total"] < all_hits["total"], (
        f"min_risk 过滤后 total 反而没变小:{hi['total']} vs {all_hits['total']}")
    assert hi["total"] == all_hits["total"] - sum(
        1 for h in all_hits["hits"] if h["risk"] < 8)


# --- 6) hit_identity 是单一来源 ---

def test_mcp_and_storage_share_one_hit_identity():
    """防的就是这次这种「抄一份抄错了」

    `save_correlations` 和 MCP 的 `run_correlate` 以前各有一份 target 提取
    逻辑,后者还是错的。判它们**共用同一个函数** —— 不是查名字,是查
    `save_correlations` 的函数体里真的调了它。
    """
    import ast

    src = (REPO / "arl_lite" / "core" / "correlation_engine.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    save = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "save_correlations")
    called = {c.func.id for c in ast.walk(save)
              if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert "hit_identity" in called, (
        "save_correlations 又内联了一份 target 提取逻辑 —— 那正是 r96 漂成崩的原因")

    mcp_src = (REPO / "arl_lite" / "mcp" / "server.py").read_text(encoding="utf-8")
    assert "h.target_type" not in mcp_src, (
        "MCP 又出现了 h.target_type —— CorrelationHit 没有这个属性")
