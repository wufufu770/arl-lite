"""r55:`monitor changes` 静默截断到 50 条,首行还说「50 change(s)」

## 实测的退化路径(不是推测)

`asset_changes` 里造 200 条,`arl-lite monitor changes` 什么都不给:

    $ arl-lite monitor changes
    [i] 50 change(s):
    ...50 行...

库里 200 条,只印 50 条,**一个字都没提**还有 150 条没显示。
`--limit` 的默认值就是 50,而首行那个数看起来像是「一共有多少」——
它其实是「显示了多少」。

## 为什么这不是「显示不全」而是 bug

配 `--since` 时它变成一个**错误结论**:r51 刚给这条命令加了时间窗,
用户筛「最近 7 天」看到 50 条,会得出「这周只有 50 个变更」。
而 50 只是 LIMIT 的默认值,和那一周实际有多少条毫无关系。
r50 的教训是「静默截断比截断本身更坏」,这里是它的 CLI 版。

## 本轮的要害:那个「总数」不许自己写一条 COUNT

`count_changes` 如果单独拼一遍 WHERE,两边的过滤条件迟早漂,
于是报出「共 200 条」而当前筛选下其实只有 50 条 ——
**那比不报更坏**,因为它是个看起来精确的假数字。r51 的 dry-run 骗人
是同一个教训(「数出来的」和「删掉的」用两套判据)。

所以两个函数共用 `_changes_where`,判据里直接对着这条不变式钉:
`count_changes` 的结果必须等于 `list_changes(limit=足够大)` 的长度。

## `--json` 那条路故意不改

机器消费要 payload 原样,加一句人话进去就破坏可解析性。
判据把这个「故意不修」钉住,免得下一轮有人好心加上去。
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import pathlib

import pytest

from arl_lite.cli import main
from arl_lite.core.monitor import count_changes, list_changes, record_change
from arl_lite.db.storage import Storage

REPO = pathlib.Path(__file__).resolve().parents[1]

TOTAL = 200
HOSTS = 100          # 一半是 host、一半是 domain


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    storage = Storage(workspace="default")   # CLI 的 --workspace 默认值
    for i in range(TOTAL):
        at = "host" if i % 2 == 0 else "domain"
        record_change(storage, at, "NEW_ASSET", f"hash{i:04d}",
                      after={"label": f"n{i}"})
    return storage, tmp_path


def _run(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        main(["monitor", "changes", *argv])
    return buf.getvalue()


def _shown(out):
    return len([l for l in out.splitlines() if l.startswith("  [")])


def _head(out):
    return out.splitlines()[0]


# ── 一、主判据:被截断就要说 ──

def test_truncated_output_states_the_real_total(ws):
    """200 条查默认 50 条,首行必须说「50 of 200」

    r55 之前首行是「50 change(s):」—— 那个数是**显示了多少**,
    但长得像「一共有多少」。
    """
    storage, _ = ws
    out = _run()
    assert _shown(out) == 50, f"默认应该显示 50 行,实际 {_shown(out)}"
    assert count_changes(storage) == TOTAL, "造数就失败了,判据等于没验"
    assert "200" in _head(out), (
        f"首行没提真实总数,用户会以为只有 50 条:\n{_head(out)}")
    assert "of" in _head(out), f"首行看不出是「其中一部分」:\n{_head(out)}"


def test_total_is_the_count_before_the_limit_not_after(ws):
    """那个总数必须是截断**前**的条数,不是 `len(rows)`

    这条钉的是「数字的来源」:`len(rows)` 恒等于显示行数,
    拿它当总数就是 r55 的 bug 本身。
    """
    storage, _ = ws
    for limit in (10, 50, 150):
        out = _run("--limit", str(limit))
        assert _shown(out) == limit
        assert f"{limit} of {TOTAL}" in _head(out), (
            f"--limit {limit} 时首行应说「{limit} of {TOTAL}」,实际:{_head(out)}")


# ── 二、没被截断时不许啰嗦 ──

def test_not_truncated_says_plain_total(ws):
    """能全放下时首行就是总数,不加「of N」

    每次都喊「只显示了最新 50 条」会在真正被截断时被淹掉 ——
    狼来了喊久了就不灵了。
    """
    out = _run("--limit", str(TOTAL + 10))
    assert _shown(out) == TOTAL
    assert " of " not in _head(out), f"没截断却说了「其中一部分」:\n{_head(out)}"
    assert f"{TOTAL} change(s)" in _head(out)


def test_exactly_at_the_limit_is_not_reported_as_truncated(ws):
    """库里正好等于 limit 条时,不算「被截断」

    行数顶到 limit 和真的截断是两件事。后者才有话说。
    """
    out = _run("--limit", str(TOTAL))
    assert _shown(out) == TOTAL
    assert " of " not in _head(out), (
        f"条数正好等于 limit 却说被截断了:\n{_head(out)}")


# ── 三、要害:总数和列表不许用两套过滤条件 ──

def test_total_respects_the_type_filter(ws):
    """加 `--type host` 之后,总数必须是 100 而不是 200

    这是 r55 最要害的一条:总数和列表如果各拼一遍 WHERE,漂了就会
    报出一个看起来精确的假数字。库里一半是 host,所以这个数字
    必须是 100。
    """
    storage, _ = ws
    out = _run("--type", "host", "--limit", "10")
    assert "10 of 100" in _head(out), (
        f"加了 --type host 之后总数应该是 100,实际:{_head(out)}")
    rows = [r for r in _run("--type", "host", "--limit", "500").splitlines()
            if r.startswith("  [")]
    assert len(rows) == HOSTS, f"host 行数不对:{len(rows)}"
    assert all("host" in r for r in rows), "混进了别的类型"


def test_total_respects_the_change_type_filter(ws):
    """`--change-type` 也得算进总数

    变异测试抓出来的洞(M5 存活):首版只造了 1 条 DISAPPEARED,而
    `--limit 10` 比它大 —— 于是 `shown < limit` 成立,走的是
    「没截断」那条分支,**根本没发 COUNT**。计数器的洞藏在一条
    永远不会走到的路径后面。

    所以这里造够条数把 COUNT 逼出来:60 条 DISAPPEARED 配 limit 10,
    少筛一个 change_type 条件的话总数会变成 260,一眼就看出来。
    """
    storage, _ = ws
    for i in range(60):
        record_change(storage, "host", "DISAPPEARED", f"gone{i:04d}",
                      before={"label": f"bye{i}"})
    out = _run("--change-type", "DISAPPEARED", "--limit", "10")
    assert _shown(out) == 10, f"应该显示 10 行,实际 {_shown(out)}"
    assert "10 of 60" in _head(out), (
        f"总数应是 60(只数 DISAPPEARED),实际:{_head(out)}")


def test_total_respects_the_since_window(ws):
    """`--since` 也得算进总数 —— 漏掉它报的是全历史条数

    和上面同一个洞(M6 存活):首版没有「总数 + --since」的判据,
    而没有它的话,`--since` 漏筛会报出一个包含全历史数据的假总数 ——
    而 `--since` 恰恰是 r51 刚加的功能,是用户最常用的那条路。
    """
    storage, _ = ws
    # 造 60 条「老」变更,一条新的都不造 —— 这样 since=1h 之外只有 0 条
    with storage._conn() as c:
        c.execute("UPDATE asset_changes SET detected_at = '2020-01-01 00:00:00'")
    for i in range(60):
        record_change(storage, "host", "NEW_ASSET", f"fresh{i:04d}",
                      after={"label": f"n{i}"})
    out = _run("--since", "1h", "--limit", "10")
    assert _shown(out) == 10, f"应该显示 10 行,实际 {_shown(out)}"
    assert "10 of 60" in _head(out), (
        f"近 1 小时内只应有 60 条,实际:{_head(out)}")
    # 反向确认:不筛 since 的话是 260 条
    out_all = _run("--limit", "10")
    assert "10 of 260" in _head(out_all), (
        f"全量应是 60+200=260 条,实际:{_head(out_all)}")


def test_count_matches_an_unbounded_list_under_every_filter(ws):
    """**不变式**:`count_changes` == `list_changes(limit=很大)` 的长度

    这条是本轮的核心断言 —— 它不看 CLI 输出,直接对着「数出来的」和
    「查出来的」两个函数。只要两边有一处条件漂了,它就红,而且不管
    漂成什么样(少一个 `AND`、漏一个类型、方向反了)都红。
    """
    storage, _ = ws
    cases = [
        dict(),
        dict(asset_type="host"),
        dict(asset_type="domain"),
        dict(change_type="NEW_ASSET"),
        dict(change_type="DISAPPEARED"),
        dict(asset_type="host", change_type="NEW_ASSET"),
        dict(asset_hash="hash0000"),
        dict(asset_type="host", asset_hash="hash0002"),
    ]
    for kw in cases:
        n = count_changes(storage, **kw)
        rows = list_changes(storage, limit=10 ** 6, **kw)
        assert n == len(rows), (
            f"过滤条件 {kw}:count_changes 报 {n},列出来 {len(rows)} 条 —— "
            f"两边用的 WHERE 不是同一份")


# ── 四、`--json` 那条路故意不动 ──

def test_json_output_stays_machine_parseable(ws):
    """`--json` 不加那句人话,也不加 `of N` 的残骸

    JSON 消费方要能直接 parse。r55 只修人类可读那条路 —— 加进去
    就破坏可解析性。这条把「故意不修」钉住,免得下一轮有人好心加。
    """
    out = _run("--json", "--limit", "10")
    payload = json.loads(out)          # 解析失败会直接抛
    assert isinstance(payload, list) and len(payload) == 10
    assert "of" not in out.splitlines()[0], "JSON 里混进了人类可读的那句话"


# ── 五、结构:两个函数必须共用那一份 WHERE ──
# 上面那条不变式是**行为**判据。它拦得住「漂了」,拦不住「有人干脆
# 把 `count_changes` 写成 `return len(list_changes(...))`」—— 那样结果
# 永远一致,数字却是截断后的,比漂了更坏(且更难查)。

def test_both_functions_go_through_the_shared_where_builder():
    """`list_changes` 和 `count_changes` 都必须调 `_changes_where`"""
    tree = ast.parse((REPO / "arl_lite" / "core" / "monitor.py")
                     .read_text(encoding="utf-8"))
    users = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                    and sub.func.id == "_changes_where"):
                users.add(node.name)
    assert {"list_changes", "count_changes"} <= users, (
        f"共用 WHERE 的只有 {sorted(users)} —— 两个函数各拼一遍条件,"
        f"迟早漂,而漂了报出的是个看起来精确的假总数")


def test_count_changes_does_not_derive_from_a_limited_list():
    """`count_changes` 不许靠 `len(list_changes(...))` 实现

    那样它数的是**截断后**的条数,而输出会告诉用户「共 50 条」——
    看起来自洽,实际永远等于 limit,比漂了更难查。

    查 **Call 节点**而不是 unparse 后的文本:docstring 里写着
    「和 `list_changes` 同一份」也算提及,那是给人看的,不是调用。
    判据要比它守的事窄(决策 #12)—— 首版就是栽在这,把文档当成了代码。
    """
    tree = ast.parse((REPO / "arl_lite" / "core" / "monitor.py")
                     .read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "count_changes")
    calls = [ast.unparse(n) for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "list_changes"]
    assert not calls, (
        f"count_changes 调了 list_changes:{calls} —— "
        f"它数的会是截断后的条数,不是库里一共多少条")


def test_count_changes_really_counts_in_sql():
    """它得真的发一条 `COUNT(*)` 出去,而不是在 Python 里数数组

    同样只认**字符串常量里真的写了那条 SQL**,不是函数体里出现过
    「COUNT(*)」这几个字(docstring 里可以提)。
    """
    tree = ast.parse((REPO / "arl_lite" / "core" / "monitor.py")
                     .read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "count_changes")
    sqls = [n.value for n in ast.walk(fn)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and "asset_changes" in n.value]
    assert any("COUNT(*)" in s for s in sqls), (
        f"没有真的 COUNT(*) —— 认到的 SQL 片段是 {sqls}")
