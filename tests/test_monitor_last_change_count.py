"""r54:`monitors.last_change_count` 记的是新增资产数,列名承诺的是变更数

## 实测的退化路径(不是推测)

同一个 watcher 跑两轮,差别只在「这一轮有没有东西消失」:

    3 个新增,0 个消失 -> asset_changes 记了 3 条,  last_change_count = 3
    0 个新增,7 个消失 -> asset_changes 记了 7 条,  last_change_count = 0

**7 条 DISAPPEARED 进了库,那一列写的是 0。** 第一版之所以"看起来对",
是因为纯新增场景下 `new_total` 恰好等于变更数 —— 两个概念在那个场景
重合,在有消失的场景里就分叉了。

## 这和 r52 的 `last_count` 是同一个病

字段名承诺的语义和承载的语义对不上,就是 bug(决策 #5)。r52 修的是
`WatchTarget.last_count`(「上次资产数」只算 hosts+domains);这一列是
`last_change_count`(「上次变更数」只算新增)。同一个模块、同一种错。

## 为什么还给它加 CLI 出口

因为这一列**全项目零消费点** —— 只在 `schema.sql` 和那条 `UPDATE` 里
出现过,没有任何代码读它。所以即使传参修对了,也没有任何人能发现它
写对了或写错了。r50 的教训是「只进日志不够,要落在用户下次还会看的地方」;
这一列本来就存在,缺的只是**有人看**。

## `never` 而不是 0

`last_change_count` 的 schema DEFAULT 是 0。没跑过的 monitor 也显示 0,
会把「从没跑过」伪装成「跑过、这轮没变更」—— 用户会以为这个 target
已经在监控了。所以按 `last_run_at` 区分。
"""
from __future__ import annotations

import contextlib
import io
from datetime import datetime, timedelta

import pytest

from arl_lite.cli import main
from arl_lite.core.monitor import Monitor
from arl_lite.core.watcher import WatchTarget, Watcher
from arl_lite.db.storage import Storage

STALE = (datetime.utcnow() - timedelta(days=30)).isoformat()


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # workspace 名必须是 `default` —— CLI 的 `--workspace` 默认值就是它
    # (cli.py:338),用别的名字造的数据 `monitor list` 根本看不到,
    # 判据会恒假绿。
    return Storage(workspace="default"), tmp_path


def _new_host(prefix, i):
    return f"{prefix}{i}.example.com"


class _Runner:
    """产出「新资产」和「这轮不碰的老资产」两种东西"""
    def __init__(self, storage=None, workspace_id=None, **_kw):
        self.storage = storage
        self.new_hosts: list[str] = []
        self.gone_hosts: list[str] = []

    async def run(self, target, modules=None, preset=None, config=None):
        for h in self.new_hosts:
            self.storage.add_host(1, h, ip="1.2.3.4")
        for h in self.gone_hosts:
            with self.storage._conn() as c:
                c.execute("UPDATE hosts SET last_seen = ? WHERE host = ?",
                          (STALE, h))
        return None


def _install(monkeypatch, new_hosts=(), gone_hosts=()):
    r = _Runner()
    r.new_hosts = list(new_hosts)
    r.gone_hosts = list(gone_hosts)

    def _factory(storage=None, workspace_id=None, **_kw):
        r.storage = storage
        return r

    monkeypatch.setattr("arl_lite.core.task_runner.TaskRunner", _factory)
    return r


def _add_monitor(storage, target="example.com"):
    with storage._conn() as c:
        c.execute("INSERT INTO monitors (workspace_id, target, monitor_type)"
                  " VALUES (?,?,?)", (storage.workspace_id, target, "full"))
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]


def _run(storage, wt=None):
    w = Watcher(storage)
    if wt is None:
        wt = WatchTarget("example.com", modules=["dns"])
    w._targets[wt.target] = wt
    w._run_target(wt)
    return wt


def _last_change_count(storage, mid):
    with storage._conn() as c:
        return c.execute("SELECT last_change_count FROM monitors WHERE id = ?",
                         (mid,)).fetchone()[0]


def _recorded(storage):
    with storage._conn() as c:
        rows = dict(c.execute("SELECT change_type, COUNT(*) FROM asset_changes"
                              " GROUP BY change_type").fetchall())
    return rows


# ── 一、主判据:只有消失时那一列不能是 0 ──

def test_disappeared_only_run_records_a_nonzero_change_count(ws, monkeypatch):
    """7 个资产消失、0 个新增 -> 那一列必须是 7,不是 0

    r54 之前写的是 0,而 `asset_changes` 里实实在在有 7 条 DISAPPEARED。
    """
    storage, _ = ws
    mid = _add_monitor(storage)
    gone = [_new_host("g", i) for i in range(7)]
    for h in gone:                      # 先入库,让 first_seen 早于本轮
        storage.add_host(1, h, ip="9.9.9.9")
    _install(monkeypatch, (), gone)

    _run(storage)

    assert _recorded(storage).get("DISAPPEARED") == 7, (
        f"没造出消失记录,这条判据等于没验:{_recorded(storage)}")
    assert _recorded(storage).get("NEW_ASSET") in (None, 0), (
        f"这一轮不该有新增,判据前提不成立:{_recorded(storage)}")
    assert _last_change_count(storage, mid) == 7, (
        f"库里有 7 条变更,last_change_count 写的是 "
        f"{_last_change_count(storage, mid)} —— 它记的是新增资产数,"
        f"而列名承诺的是变更数")


def test_change_count_equals_changes_written_not_assets_added(ws, monkeypatch):
    """对照:同一轮里新增和消失都有,记的必须是**两类之和**

    这条钉的是「变更」这个概念本身的边界。r54 之前两类只算一类,
    而在纯新增场景下两类数值相同 —— 所以必须有一个同时含两类的场景,
    否则「只算新增」和「算变更」永远分不开。
    """
    storage, _ = ws
    mid = _add_monitor(storage)
    gone = [_new_host("g", i) for i in range(4)]
    for h in gone:
        storage.add_host(1, h, ip="9.9.9.9")
    _install(monkeypatch, [_new_host("n", i) for i in range(3)], gone)

    _run(storage)

    rec = _recorded(storage)
    total = sum(rec.values())
    assert rec.get("NEW_ASSET") == 3 and rec.get("DISAPPEARED") == 4, (
        f"前提不成立:{rec}")
    assert _last_change_count(storage, mid) == total == 7, (
        f"写了 {total} 条变更,last_change_count = "
        f"{_last_change_count(storage, mid)}")


# ── 二、它得真的被记录下来,不是忘了写 ──

def test_change_count_is_written_even_when_no_monitor_matches(ws, monkeypatch):
    """monitor 的 target 对不上时不该碰任何一行

    防止「顺手给所有 monitor 都记一遍」——那会把这个数弄脏。
    """
    storage, _ = ws
    mid = _add_monitor(storage, target="other.example.com")
    _install(monkeypatch, [_new_host("n", i) for i in range(3)], ())
    _run(storage)
    assert _last_change_count(storage, mid) == 0, (
        "target 对不上也写了 —— 这个数会记到不相干的 monitor 上")


def test_only_the_matching_monitor_gets_the_count(ws, monkeypatch):
    """有多个 monitor 时,只写 target 匹配的那一个

    变异测试抓出来的洞(C-scope 存活):上一条只造了**一个**不匹配的
    monitor,而整个库里只有它一个 —— 于是「按 target 过滤」和
    「全都写一遍」在这套数据上**行为完全一样**,把判断换成恒真
    也没人喊。

    所以这里必须造两个:匹配的和不匹配的各一个,而且不匹配的那个
    已经跑过一轮(有非零的旧值),这样「被误写」会立刻看得见 ——
    否则 DEFAULT 0 会把误写藏起来。
    """
    storage, _ = ws
    matched = _add_monitor(storage, target="example.com")
    other = _add_monitor(storage, target="other.example.com")
    with storage._conn() as c:          # 另一个 monitor 先跑过一轮
        c.execute("UPDATE monitors SET last_run_at = ?, last_change_count = ?"
                  " WHERE id = ?", ("2026-01-01T00:00:00", 99, other))

    _install(monkeypatch, [_new_host("n", i) for i in range(3)], ())
    _run(storage)

    assert _last_change_count(storage, matched) == 3, (
        f"匹配的 monitor 该记 3,记的是 "
        f"{_last_change_count(storage, matched)}")
    assert _last_change_count(storage, other) == 99, (
        f"不匹配的 monitor 被改了 —— 它的值从 99 变成 "
        f"{_last_change_count(storage, other)},last_run_at 也不该被动")


def test_change_count_accumulates_per_run_not_permanent(ws, monkeypatch):
    """两轮各检出 3 条,第二轮之后那列应该是 3 而不是 6

    `last_change_count` 的名字是「**last**」—— 它是最近一轮的数,不是累计。
    累计在 `asset_changes` 表里,那才是能查全部的地方。
    """
    storage, _ = ws
    mid = _add_monitor(storage)
    _install(monkeypatch, [_new_host("a", i) for i in range(3)], ())
    wt = _run(storage)
    assert _last_change_count(storage, mid) == 3

    _install(monkeypatch, [_new_host("b", i) for i in range(3)], ())
    _run(storage, wt)
    assert _last_change_count(storage, mid) == 3, (
        f"这一轮检出 3 条,last_change_count 变成了 "
        f"{_last_change_count(storage, mid)} —— 它记的是累计而不是最近一轮")
    assert sum(_recorded(storage).values()) == 6, "两轮应该都留在库里"


# ── 三、CLI 出口:修对了得有人能看见 ──

def test_monitor_list_shows_the_change_count(ws):
    """`arl-lite monitor list` 要把 `last_change_count` 显示出来

    这一列在 r54 之前是**零消费点** —— 只在 schema 和那条 UPDATE 里出现过。
    不显示的话,传参修对了也没人验证得了。
    """
    storage, _ = ws
    with storage._conn() as c:
        c.execute("INSERT INTO monitors (workspace_id, target, monitor_type,"
                  " last_run_at, last_change_count) VALUES (?,?,?,?,?)",
                  (storage.workspace_id, "example.com", "full",
                   "2026-01-01T00:00:00", 7))
    out = _run_cli(["monitor", "list"])
    assert "7" in out, f"变更数没显示出来:\n{out}"
    assert "changes=" in out, f"没标出这个数是什么:\n{out}"


def test_monitor_list_says_never_for_a_monitor_that_never_ran(ws):
    """没跑过的 monitor 显示 `never`,不是 0

    那一列的 schema DEFAULT 是 0。印 0 会把「从没跑过」伪装成
    「跑过、这轮没变更」—— 用户会以为这个 target 已经在监控了。
    """
    storage, _ = ws
    with storage._conn() as c:
        c.execute("INSERT INTO monitors (workspace_id, target, monitor_type)"
                  " VALUES (?,?,?)", (storage.workspace_id, "example.com", "full"))
    out = _run_cli(["monitor", "list"])
    assert "changes=never" in out, f"没跑过却显示了具体数字:\n{out}"
    assert "changes=0" not in out, (
        f"从没跑过的 monitor 显示了 0 —— 那会读成「跑过、没变更」:\n{out}")


def _run_cli(argv):
    """跑一条 CLI 命令并拿回 stdout

    不再重设 HOME:`ws` fixture 已经把 HOME 指到 tmp_path,而
    `Storage` 的工作区根就是从 HOME 推出来的(`get_default_workspace_root`),
    所以这里造的数据和 CLI 读的是同一个库。上一版用另一个
    `home_target` fixture 又设一遍 HOME,纯属多余。
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        main(argv)
    return buf.getvalue()
