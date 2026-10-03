"""r53:一轮里只要有消失资产,截断丢弃数就恒为 0

## 实测的退化路径(不是推测)

`CHANGE_RECORD_CAP` = 200,每轮塞 210 个新 host,新增侧**确定**丢 10 条。
唯一变量是「那一轮还检出了多少消失资产」:

    210 新增 + 0  消失  ->  asset_changes 200 条,  watcher 报丢弃 10 条 ✓
    210 新增 + 20 消失  ->  asset_changes 220 条,  watcher 报丢弃  0 条 ✗

**同样丢 10 条,只因多了一批消失就归零。** 连那条
「本轮共检出 N 条变更,截掉 M 条且不会补上」的 warning 也不再打,
因为 `if dropped_this_run:` 为假。

## 根因:同一个「变更」概念,两个口径

    detected_total += len(new_rows)                    # 只在新增分支加
    recorded_total += len(new_rows) - dropped_new      # 新增分支
    recorded_total += len(gone)      - dropped_gone    # 消失分支也加

`detected` 数「本轮检出了几条」,漏了消失;`recorded` 数「本轮记下了几条」,
两类都算。于是只要这轮有消失,`detected < recorded` **恒成立**,
`account_change_recording` 那个 `max(0, ...)`(r50 加的)就把丢弃数夹成 0。

那个 `max(0)` 本身没错 —— 它是「别把 bug 报成负数」的防御。但当两个口径
不对称时,防御**正好挡住了本该报出来的丢失**。r50 建的整套「截断必须可见」
在有消失资产的轮次里静默失效,而消失检测是 watcher 的常规功能。

## 修法不是「删掉 max(0)」

删掉防御会让 `detected < recorded` 报出负的丢弃数,那是把一个 bug 伪装成
另一个 bug 的证据。真正的修法是**让两个口径对称**:消失也要并进 `detected`,
并且这件事收成一个函数(`account_detected_and_recorded`),因为「两处都记得
加」靠不住 —— r50 的教训正是「抽出来才测得到」。

## 所以判据盯的是「对称」,不是某个具体数字

210 和 10 都来自 `CHANGE_RECORD_CAP`,判据不硬编码它们:
契约变了判据自动跟着变,不然过几个月 cap 一改,判据就变成在验一个
已经不存在的实现。
"""
from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timedelta

import pytest

from arl_lite.core.monitor import Monitor
from arl_lite.core.watcher import (CHANGE_RECORD_CAP, WatchTarget, Watcher,
                                   account_detected_and_recorded)
from arl_lite.db.storage import Storage

REPO = pathlib.Path(__file__).resolve().parents[1]

OVER = CHANGE_RECORD_CAP + 10          # 新增侧确定丢 10 条
GONE = 20                              # 消失数要**大于**丢失数,才能盖住它


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return Storage(workspace="t"), tmp_path


def _age_hosts(storage, days=30):
    """把 hosts 的 last_seen 推到很久以前 —— 让消失检测认得它们

    宽限期 48h(见 `Monitor.detect_disappeared`),所以 30 天足够远。
    """
    old = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with storage._conn() as c:
        c.execute("UPDATE hosts SET last_seen = ?", (old,))


class _Runner:
    """塞一批新 host,可选地把已有的 host 变成「消失」

    用 `add_host` 真的入库 —— 消失检测读的是 `last_seen`,不造假数据。
    """
    def __init__(self, storage=None, workspace_id=None, **_kw):
        self.storage = storage
        self.workspace_id = workspace_id
        self.new_hosts = []
        self.gone_hosts = []

    async def run(self, target, modules=None, preset=None, config=None):
        for h in self.new_hosts:
            self.storage.add_host(1, h, ip="1.2.3.4")
        for h in self.gone_hosts:
            with self.storage._conn() as c:
                c.execute("UPDATE hosts SET last_seen = ? WHERE host = ?",
                          (_STALE, h))
        return None


def _preload(storage, hosts):
    """**在跑之前**把 host 建好,好让它们能被判成消失

    `detect_disappeared` 的条件是 `first_seen < since_iso AND last_seen <
    cutoff` —— 资产必须在运行**开始前**就存在。运行过程中新建的资产
    `first_seen` 在 `since_iso` 之后,那不是「消失」,是「新增」,
    判据里也正是这么写的。所以要造消失场景,得先入库再让这轮不碰它。
    """
    for h in hosts:
        storage.add_host(1, h, ip="9.9.9.9")
    return list(hosts)


# 固定成一个明确的老时间戳,免得测试里到处算 datetime
_STALE = (datetime.utcnow() - timedelta(days=30)).isoformat()


def _install(monkeypatch, new_hosts, gone_hosts=()):
    """把采集器换成 `_Runner`,并告诉它这轮要产出什么

    走工厂而不是 `lambda *a, **k: r`:`_run_target` 用
    `TaskRunner(storage=..., workspace_id=...)` 构造,工厂得真接住
    这些参数再交给 runner —— 上一版写成 lambda,runner 的 storage
    恒为 None,而 `_run_target` 把采集器的异常吞成日志,于是测试报的是
    「数不对」而不是「采集器炸了」。
    """
    r = _Runner()
    r.new_hosts = list(new_hosts)
    r.gone_hosts = list(gone_hosts)

    def _factory(storage=None, workspace_id=None, **_kw):
        r.storage = storage
        r.workspace_id = workspace_id
        return r

    monkeypatch.setattr("arl_lite.core.task_runner.TaskRunner", _factory)
    return r


def _fresh(prefix, n):
    return [f"{prefix}{i}.example.com" for i in range(n)]


def _run(storage, wt=None):
    """跑一轮。给了 `wt` 就复用它 —— 跨轮累计的判据必须用**同一个** target

    每次新建 `WatchTarget` 的话,`dropped_change_count` 每轮都从 0 起,
    测出来的是「最后一轮的数」而不是累计。上一版就是踩了这个坑,
    报成「累计停在 10」,看着像被测代码的 bug。
    """
    w = Watcher(storage)
    if wt is None:
        wt = WatchTarget("example.com", modules=["dns"])
    w._targets[wt.target] = wt
    w._run_target(wt)
    return wt


def _changes(storage):
    with storage._conn() as c:
        return dict(c.execute(
            "SELECT change_type, COUNT(*) FROM asset_changes "
            "GROUP BY change_type").fetchall())


# ── 一、helper 层:口径对称是它的性质 ──

def test_helper_adds_the_same_batch_to_both_counters():
    """一次变更,两个口径各加同样的份"""
    assert account_detected_and_recorded(0, 0, 5, 0) == (5, 5)
    assert account_detected_and_recorded(7, 3, 5, 0) == (12, 8)
    # 截断时:detected 加检出数,recorded 加实际记入数
    assert account_detected_and_recorded(0, 0, 5, 2) == (5, 3)


def test_helper_never_lets_recorded_outrun_detected():
    """`recorded` 必须**恒 ≤** `detected`

    这条就是 r53 那个 bug 的精确形式:两个口径不对称时,只要这轮
    有消失,`detected < recorded` 恒成立,`max(0, ...)` 就会把
    丢弃数夹成 0。判据不钉某个具体数字,只钉这个**不变量** ——
    它对任何 cap、任何检出数都成立。
    """
    for found in range(0, 40):
        for dropped in range(0, found + 1):
            det, rec = account_detected_and_recorded(0, 0, found, dropped)
            assert rec <= det, (
                f"found={found} dropped={dropped} -> detected={det} "
                f"recorded={rec},recorded 反超了 detected")


def test_helper_is_monotonic_in_both_counters():
    """两个口径都只增不减"""
    det, rec = 0, 0
    for found, dropped in ((10, 0), (5, 3), (7, 0), (0, 0)):
        ndet, nrec = account_detected_and_recorded(det, rec, found, dropped)
        assert ndet >= det and nrec >= rec
        det, rec = ndet, nrec


# ── 二、主判据:有无消失,丢的条数必须一样 ──

def test_disappeared_assets_do_not_erase_the_truncation_count(ws, monkeypatch):
    """r53 本体:210 新增 + 20 消失,必须照样报丢 10 条

    修之前这一轮报 0 条丢弃,而库里真少了 10 条永久变更。
    """
    storage, _ = ws
    gone = _preload(storage, _fresh("g", GONE))
    _install(monkeypatch, _fresh("n", OVER), gone)

    wt = _run(storage)
    assert _changes(storage).get("DISAPPEARED") == GONE, (
        f"没造出消失记录,这条判据等于没验:{_changes(storage)}")
    assert _changes(storage).get("NEW_ASSET") == CHANGE_RECORD_CAP, (
        f"新增侧入库数不对:{_changes(storage)} —— 没触到截断,验不了丢弃")

    assert wt.last_dropped_change_count == 10, (
        f"库里少了 {OVER - CHANGE_RECORD_CAP} 条永久变更,watcher 却报丢弃 "
        f"{wt.last_dropped_change_count} 条 —— 消失检测把截断留痕吞掉了")
    assert wt.dropped_change_count == 10


def test_same_dropped_count_with_and_without_disappeared(ws, monkeypatch):
    """对照实验:唯一变量是「那轮有没有消失资产」,丢的条数必须相同

    这条比上一条更能说明问题:两个场景跑的是同一段代码、同一个 cap,
    唯一区别是一批消失的资产。丢掉数不一样,就说明消失检测在干扰
    截断记账 —— 而消失检测和截断本该是**两件互不相干的事**。
    """
    bare, _ = ws
    _install(monkeypatch, _fresh("b", OVER))
    wt_bare = _run(bare)

    with_gone, _ = ws
    gone = _preload(with_gone, _fresh("g", GONE))
    _install(monkeypatch, _fresh("w", OVER), gone)
    wt_gone = _run(with_gone)

    assert _changes(with_gone).get("DISAPPEARED") == GONE
    assert wt_bare.last_dropped_change_count == wt_gone.last_dropped_change_count, (
        f"没有消失时报丢 {wt_bare.last_dropped_change_count} 条,"
        f"有消失时报丢 {wt_gone.last_dropped_change_count} 条 —— "
        f"多了一批消失就把截断记账搅乱了")
    assert wt_gone.last_dropped_change_count == OVER - CHANGE_RECORD_CAP


# ── 三、消失侧自己的截断也要留痕 ──

def test_truncation_on_the_disappeared_side_is_also_reported(ws, monkeypatch):
    """只有消失、没有新增,而且消失侧超了 cap,也要报丢失

    r53 之前这个场景报 0:detected=0,recorded=200,`max(0, -200)`=0。
    用户看到的是「本轮什么都没丢」,而库里少了 50 条消失记录。
    """
    storage, _ = ws
    gone = _fresh("g", OVER)
    # 先入库,再把 last_seen 推老 —— 让这一轮把它们判成消失
    for h in gone:
        storage.add_host(1, h, ip="1.2.3.4")
    _install(monkeypatch, [], gone)

    wt = _run(storage)
    assert _changes(storage).get("DISAPPEARED") == CHANGE_RECORD_CAP, (
        f"消失侧没触到截断,验不了:{_changes(storage)}")
    assert _changes(storage).get("NEW_ASSET") is None, (
        "这一轮不该有新增,判据的前提不成立")
    assert wt.last_dropped_change_count == OVER - CHANGE_RECORD_CAP, (
        f"消失侧丢了 {OVER - CHANGE_RECORD_CAP} 条,watcher 报 "
        f"{wt.last_dropped_change_count} 条")


# ── 四、跨轮累计:有消失的那一轮照样要累加 ──

def test_dropped_count_accumulates_across_runs_with_disappeared(ws, monkeypatch):
    """第二轮带消失,累计不能停在第一轮的数上

    `dropped_change_count` 是用户唯一能查的总数(`watch list` 显示它)。
    r53 之前第二轮有消失时它不动 —— 而「不动」在这里恰恰意味着丢。
    """
    storage, _ = ws
    _install(monkeypatch, _fresh("n0", OVER))
    wt = _run(storage)
    assert wt.dropped_change_count == 10, "第一轮就没记上"

    # 第二轮:换一批新 host,顺带把上一轮建的 20 个判成消失
    round1_hosts = _fresh("n0", OVER)
    _install(monkeypatch, _fresh("n1", OVER), round1_hosts[:GONE])
    wt = _run(storage, wt)          # ← 同一个 target,才是「第二轮」

    assert _changes(storage).get("DISAPPEARED") == GONE, (
        f"第二轮没检出消失:{_changes(storage)}")
    assert wt.last_dropped_change_count == 10, "第二轮的丢弃数被吞了"
    assert wt.dropped_change_count == 20, (
        f"累计停在 {wt.dropped_change_count},应该两轮各 10 条")


def test_detected_and_recorded_counts_in_the_warning_are_the_real_ones(
        ws, monkeypatch, caplog):
    """那条 warning 里的两个数必须真的是「检出」和「记入」

    变异测试抓出来的洞(M9 存活):`found` 传成「已记入的条数」时,
    **丢弃数这个标量完全不变**(recorded 跟着一起错,差值恰好抵消),
    所以盯 `dropped_change_count` 的判据全都抓不住它。

    被破坏的是 `detected_total` —— 它是用户唯一能看到的「本轮共检出
    N 条变更」。少报检出会让用户以为这轮只扫出 200 条,于是 210 这个数
    就此消失,没有任何地方对得上账。
    """
    import logging
    storage, _ = ws
    gone = _preload(storage, _fresh("g", OVER))
    _install(monkeypatch, [], gone)

    with caplog.at_level(logging.WARNING, logger="arl_lite.core.watcher"):
        _run(storage)

    assert _changes(storage).get("DISAPPEARED") == CHANGE_RECORD_CAP, (
        f"没触到截断,这条判据等于没验:{_changes(storage)}")
    line = next((r.getMessage() for r in caplog.records
                 if "共检出" in r.getMessage()), "")
    assert line, "没抓到「共检出」那条 warning"
    assert f"共检出 {OVER} 条变更" in line, (
        f"真实检出 {OVER} 条,日志说的不是这个数:{line}")
    assert f"只有 {CHANGE_RECORD_CAP} 条" in line, (
        f"实际入库 {CHANGE_RECORD_CAP} 条,日志说的不是这个数:{line}")


# ── 五、`disappeared_total` 说的话得和库里对得上 ──

def test_disappeared_log_count_matches_what_was_recorded(ws, monkeypatch, caplog):
    """汇总日志里的 `disappeared=N` 必须等于实际写进 `asset_changes` 的条数

    `disappeared_total` 和 `detected_total` 是两回事:后者是「检出」,
    前者只喂给这一行日志。上一版没有任何判据碰它 —— 把它改成 0,
    全部测试照样绿,而用户看的那行日志从此只报一半。
    """
    import logging
    storage, _ = ws
    gone = _preload(storage, _fresh("g", GONE))
    _install(monkeypatch, _fresh("n", OVER), gone)

    with caplog.at_level(logging.INFO, logger="arl_lite.core.watcher"):
        _run(storage)

    recorded = _changes(storage).get("DISAPPEARED", 0)
    line = next((r.getMessage() for r in caplog.records
                 if "disappeared=" in r.getMessage()), "")
    assert line, "没抓到 disappeared 汇总日志"
    assert f"disappeared={recorded}" in line, (
        f"库里记了 {recorded} 条消失,日志说的是:{line}")
    assert f"disappeared={GONE}" in line, f"和造出来的 {GONE} 条对不上:{line}"


# ── 六、结构:两处都必须走那一个函数 ──
# 行为测试守的是「现在对」。可它拦不住下一种退化:有人在新增分支把
# helper 调用删掉、直接内联两行 —— 行为照常全绿,而口径又不对称了。
# r50 的 `CHANGE_TYPES` 就是这么漏的:定义在、测试绿、生产零引用。

def test_both_change_branches_account_through_the_one_helper():
    """`_run_target` 里必须正好两处 `account_detected_and_recorded` 调用

    两处:新增一处、消失一处。少一处就有一类变更回到不对称的老路上,
    而那正是 r53 的形状。
    """
    tree = ast.parse((REPO / "arl_lite" / "core" / "watcher.py")
                     .read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_run_target")
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "account_detected_and_recorded"]
    assert len(calls) == 2, (
        f"找到 {len(calls)} 处调用,应该 2 处(新增 + 消失)。"
        f"少一处就有一类变更不再并进 detected,截断留痕又会归零。")


def test_neither_branch_inlines_its_own_accounting():
    """两个分支都不许再自己写 `recorded_total +=` 减法

    判据要窄:只查「对这两个计数器的直接赋值」。`disappeared_total` 是
    另一个东西(只给日志用),不在此列。
    """
    tree = ast.parse((REPO / "arl_lite" / "core" / "watcher.py")
                     .read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_run_target")
    inline = []
    for n in ast.walk(fn):
        if (isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name)
                and n.target.id in ("detected_total", "recorded_total")):
            inline.append(ast.unparse(n))
    assert not inline, (
        f"还有内联的计数累加:{inline} —— "
        f"两个口径的对称性只能由 account_detected_and_recorded 保证")
