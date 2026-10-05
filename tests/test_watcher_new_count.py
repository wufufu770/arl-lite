"""r52:watcher 报「新增 N」只数了 3 种资产,ports 和 sites 整个漏掉

## 实测的退化路径(不是推测)

`watcher._run_target` 跑一轮,五张资产表各新增 1 行,库里确实有 5 个新资产:

    storage 里的真实新增: domains=1 hosts=1 ports=1 sites=1 findings=1
    watcher 报的 new:      1
    WatchTarget.new_count: 1        ← 累计,**永久性**偏差,不会自愈
    WatchTarget.last_count: 2       ← 字段名写着「资产数」,实际是 hosts+domains

`new_by_type` 的字典推导写死了 `("hosts", "domains", "findings")` 三个键,
`ports` 和 `sites` 压根没进差值计算。所以用户看到的 `new=1` 和库里的
5 个新资产对不上,而且**没有一个字说这件事**。

## 为什么这是 bug 而不是「显示不全」

三个消费点都被同一个错的值喂:

1. `wt.new_count += new_total` —— 累计,偏差永久累积
2. `notify_task_done(found=new_total)` —— 推给用户的「本次找到 N 个」
3. `Monitor.record_run(m["id"], new_total)` —— 监控表的变更数

而字段名 `last_count` 承诺的是「上次资产数」,承载的却是「其中两种」——
名字承诺的语义和承载的语义对不上,这本身就是 bug(决策 #5)。

## 同一份清单在本模块里抄过两次

抄三处就已经漂了:上面那个三元组、和下面那个五元组的 for 循环
(`("domain","domains"),...`)。第二个 for 循环是全的,第一个是缺的 ——
**同一个文件里两份清单不一致**,说明手抄的方向一定是漂(决策 #9)。

所以判据不只钉「现在 5 种都对」,还钉「清单来自 `Monitor._ASSET_TABLES`」:
行为测试守住现在,结构判据守住下次加资产种类时不重复这个错。

## 反向的错误也要钉住

修法**不能**是「改成从 `get_stats()` 的键取」—— 那个方法的键里还有
`tasks` 和 `correlations`(storage.py:get_stats),那两张表不是资产。
把它们的行数算进「新增资产」是同一个错的镜像。该不该算,判据是
「什么是资产」,而那个答案只存在于契约表里。

## 网络是隔离掉的,不是绕过的

`_run_target` 里 TaskRunner 是延迟 import 的,monkeypatch 打在
`arl_lite.core.task_runner.TaskRunner` 上,整轮真的跑完:
真的调 `_count_assets`、真的 diff、真的写 `asset_changes`。
只有采集器被换掉 —— 换成往库里塞 5 种资产的那个。
"""
from __future__ import annotations

import ast
import logging
import pathlib

import pytest

from arl_lite.core.monitor import Monitor
from arl_lite.core.watcher import WatchTarget, Watcher
from arl_lite.db.storage import Storage

REPO = pathlib.Path(__file__).resolve().parents[1]

# 契约表说有几张资产表,判据就从那儿取 —— 不在测试里另抄一份
# (抄一份的话,契约表加一种资产时这个文件会静默地继续验旧的 5 种)
ASSET_TABLES = tuple(Monitor._ASSET_TABLES.values())


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return Storage(workspace="t"), tmp_path


def _seed_all_five(storage):
    """五张资产表各塞 1 行 —— 只给「不跑 runner、直接预置资产」的场合用"""
    storage.add_domain(1, "example.com", "dns")
    storage.add_host(1, "a.example.com", ip="1.1.1.1")
    storage.add_port(1, "1.1.1.1", 443)
    storage.add_site(1, "https://a.example.com", "a.example.com",
                     "1.1.1.1", 443, "https")
    storage.add_finding(1, "a.example.com", "fingerprint", title="nginx")
    return set(ASSET_TABLES)


class _FakeRunner:
    """替掉真 TaskRunner:不联网,只往库里写资产

    只换采集器。`_run_target` 剩下的每一步都真的跑:
    `_count_assets` 真的查 COUNT(*)、真的 diff、真的写 asset_changes。
    换掉的是「数据从哪来」,不是「被验的那段逻辑」。
    """
    def __init__(self, storage=None, workspace_id=None, **_kw):
        self.storage = storage
        self.workspace_id = workspace_id

    async def run(self, target, modules=None, preset=None, config=None):
        self.storage.add_domain(1, f"{target}", "dns")
        self.storage.add_host(1, f"a.{target}", ip="1.1.1.1")
        self.storage.add_port(1, "1.1.1.1", 443)
        self.storage.add_site(1, f"https://a.{target}", f"a.{target}",
                              "1.1.1.1", 443, "https")
        self.storage.add_finding(1, f"a.{target}", "fingerprint",
                                 title="nginx")
        return None


def _run_one_round(storage, seen, **watcher_kw):
    """跑一轮真实的 `_run_target`,返回 (watcher, target)"""
    w = Watcher(storage, on_run=lambda t, found, dur, nc:
                 seen.append((found, nc)), **watcher_kw)
    wt = WatchTarget("example.com", modules=["dns"])
    w._targets[wt.target] = wt
    w._run_target(wt)
    return w, wt


def _count(storage, table):
    with storage._conn() as c:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ── 一、主判据:五张资产表都要算进去 ──

def test_five_asset_types_all_counted_in_the_new_total(ws, monkeypatch):
    """五张表各 1 行 → watcher 必须报 5

    r52 之前报 1(只 diff 了 hosts/domains/findings 三张)。
    ports 和 sites 各有 1 个真实新资产,报告里一个字都没有。
    """
    st, _ = ws
    monkeypatch.setattr("arl_lite.core.task_runner.TaskRunner", _FakeRunner)
    seen: list = []
    _run_one_round(st, seen)

    assert len(seen) == 1, f"on_run 没被调用:{seen}"
    found, new_count = seen[0]
    assert found == len(ASSET_TABLES), (
        f"watcher 报 found={found},但库里真多了 {len(ASSET_TABLES)} 个资产"
        f"({sorted(ASSET_TABLES)}) —— 有资产表没被算进新增")
    assert new_count == found, "on_run 的 found 和 new_count 不是同一个数"


@pytest.mark.parametrize("table", ASSET_TABLES)
def test_any_single_asset_type_alone_is_not_silently_dropped(ws, monkeypatch, table):
    """单独新增一种资产,报告里必须看得见

    参数化跑 5 种:「只漏了 ports」这种局部退化会被单独逮住,
    而总数相等(5==5)的断言可能恰好把漏的和不多的抵掉。
    """

    def _seed_one(storage, _table=table):
        {
            "domains": lambda s: s.add_domain(1, "example.com", "dns"),
            "hosts": lambda s: s.add_host(1, "a.example.com", ip="1.1.1.1"),
            "ports": lambda s: s.add_port(1, "1.1.1.1", 443),
            "sites": lambda s: s.add_site(1, "https://a.example.com",
                                          "a.example.com", "1.1.1.1",
                                          443, "https"),
            "findings": lambda s: s.add_finding(1, "a.example.com",
                                                "fingerprint", title="nginx"),
        }[_table](storage)

    st, _ = ws
    monkeypatch.setattr("arl_lite.core.task_runner.TaskRunner", _FakeRunner)
    seen: list = []
    w = Watcher(st, on_run=lambda t, f, d, nc: seen.append((f, nc)))
    wt = WatchTarget("example.com", modules=["dns"])
    w._targets[wt.target] = wt

    # 只让 runner 塞这一种,别让它塞全套
    class _OneRunner(_FakeRunner):
        async def run(self, target, modules=None, preset=None, config=None):
            _seed_one(self.storage)
            return None
    monkeypatch.setattr("arl_lite.core.task_runner.TaskRunner", _OneRunner)
    w._run_target(wt)

    assert seen and seen[0][0] == 1, (
        f"只新增了一个 {table},watcher 报 {seen} —— "
        f"这种资产的新增被静默吞掉了")


# ── 二、非资产表不能混进来(反向的错误) ──

def test_task_and_correlation_rows_are_not_counted_as_assets(ws, monkeypatch):
    """`tasks`/`correlations` 的行数不许进「新增资产」

    这是修法最容易走错的方向:`get_stats()` 返回的键里就有这两张表,
    「把清单从 get_stats 取」看起来更通用,实际会把任务记录数报成资产数。
    该不该算的判据是「什么是资产」,那个答案只在契约表里。
    """
    st, _ = ws

    class _NoisyRunner(_FakeRunner):
        async def run(self, target, modules=None, preset=None, config=None):
            await super().run(target, modules)
            with self.storage._conn() as c:
                c.execute("INSERT INTO tasks (workspace_id, name, target,"
                          " modules, status) VALUES (?,?,?,?,?)",
                          (self.workspace_id, "t", target, "[]", "done"))
                c.execute("INSERT INTO correlations (workspace_id, rule_name,"
                          " target, target_type) VALUES (?,?,?,?)",
                          (self.workspace_id, "exposed_storage", "1.1.1.1", "ip"))
            return None
    monkeypatch.setattr("arl_lite.core.task_runner.TaskRunner", _NoisyRunner)

    seen: list = []
    _run_one_round(st, seen)

    # 先确认非资产行真的造出来了 —— 造不出来的话,后面那个断言
    # 恒真(决策 #3:能被默认值兜住的断言等于没写)
    assert _count(st, "tasks") == 1 and _count(st, "correlations") == 1, (
        "没能造出非资产行,这条判据等于没验")
    assert seen[0][0] == len(ASSET_TABLES), (
        f"报了 {seen[0][0]},真实资产只有 {len(ASSET_TABLES)} 个 —— "
        f"tasks/correlations 被当成资产算了(或者资产压根没算)")


# ── 三、累计数和 last_count ──

def test_new_count_is_persisted_per_target_and_accumulates(ws, monkeypatch):
    """`new_count` 跨轮**累加** —— 第二轮没有新增时也不能被清零

    这是 r52 变异测试抓出来的洞(M11 存活):第一版只跑一轮,
    而单轮里 `new_count += n` 和 `new_count = n` 完全等价 ——
    累计这个语义根本没被验到,而它正是「偏差会不会自愈」的关键:
    少算的资产如果只错一轮,下一轮就平了。
    """
    st, _ = ws
    monkeypatch.setattr("arl_lite.core.task_runner.TaskRunner", _FakeRunner)
    seen: list = []
    w, wt = _run_one_round(st, seen)
    first = wt.new_count
    assert first == len(ASSET_TABLES)

    # 第二轮:采集器什么都不产出,所以 new_total = 0
    class _QuietRunner(_FakeRunner):
        async def run(self, target, modules=None, preset=None, config=None):
            return None
    monkeypatch.setattr("arl_lite.core.task_runner.TaskRunner", _QuietRunner)
    w._run_target(wt)

    assert seen[-1][0] == 0, "第二轮不该报出新增"
    assert wt.new_count == first, (
        f"第二轮没有新资产,new_count 却从 {first} 变成了 {wt.new_count} —— "
        f"累计被覆盖了,「累计」这个字段名就不成立了")
    # 这个字段会写进 watch.json 并被 `watch list` 显示,所以必须能往返
    back = WatchTarget.from_dict(wt.to_dict())
    assert back.new_count == wt.new_count


def test_assets_being_removed_is_not_reported_as_negative_new(ws, monkeypatch):
    """资产被删/并发清库时,新增数是 0,不是负数

    r52 变异测试抓出来的第二个洞(M12 存活):第一版每个场景都只增不减,
    所以差值恒非负,`max(0)` 那层保护等于没被测到。
    报出 `new=-1` 会让「本次发现 -1 个资产」这种话出现在通知里 ——
    那是把一个 bug 伪装成另一个 bug 的证据。
    """
    st, _ = ws
    st.add_host(1, "old.example.com", ip="2.2.2.2")   # 先有一个,好删掉

    class _DeletingRunner(_FakeRunner):
        async def run(self, target, modules=None, preset=None, config=None):
            with self.storage._conn() as c:
                c.execute("DELETE FROM hosts")
            return None
    monkeypatch.setattr("arl_lite.core.task_runner.TaskRunner", _DeletingRunner)

    seen: list = []
    _w, wt = _run_one_round(st, seen)
    assert _count(st, "hosts") == 0, "没删掉 host,这条判据等于没验"
    assert seen[0][0] == 0, (
        f"host 少了 1 行,watcher 报新增 {seen[0][0]} —— "
        f"差值没夹到 0,会把「资产被删」报成「新增负数」")
    assert wt.new_count == 0, "负的新增被累加进了累计"


def test_last_count_covers_every_asset_type(ws, monkeypatch):
    """`last_count` 是「全部资产数」,不是「其中两种」

    字段名承诺的是资产数。r52 之前它只加 hosts + domains,
    库里 5 个资产它报 2 —— 名字和承载的语义对不上。
    """
    st, _ = ws
    monkeypatch.setattr("arl_lite.core.task_runner.TaskRunner", _FakeRunner)
    seen: list = []
    _w, wt = _run_one_round(st, seen)

    with st._conn() as c:
        real = sum(dict(c.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone())["n"]
                   for t in ASSET_TABLES)
    assert real == len(ASSET_TABLES)
    assert wt.last_count == real, (
        f"last_count={wt.last_count},但库里有 {real} 个资产")


def test_last_count_ignores_non_asset_tables(ws, monkeypatch):
    """`last_count` 同样不许把 tasks/correlations 算进来"""
    st, _ = ws

    class _NoisyRunner(_FakeRunner):
        async def run(self, target, modules=None, preset=None, config=None):
            await super().run(target, modules)
            with self.storage._conn() as c:
                c.execute("INSERT INTO tasks (workspace_id, name, target,"
                          " modules, status) VALUES (?,?,?,?,?)",
                          (self.workspace_id, "t", target, "[]", "done"))
            return None
    monkeypatch.setattr("arl_lite.core.task_runner.TaskRunner", _NoisyRunner)
    seen: list = []
    _w, wt = _run_one_round(st, seen)
    assert _count(st, "tasks") == 1, "没造出 task 行,这条判据等于没验"
    assert wt.last_count == len(ASSET_TABLES), (
        f"last_count={wt.last_count},含了非资产表")


# ── 四、汇总日志逐类型,不手写 ──

def test_summary_log_names_every_asset_table(ws, monkeypatch, caplog):
    """汇总那行日志里每种资产都要出现

    原来它是手写的三个 `f(...)`。加资产种类时会**只在这里漏** ——
    而报告的总数是对的话,日志就成了唯一说谎的地方。
    """
    st, _ = ws
    monkeypatch.setattr("arl_lite.core.task_runner.TaskRunner", _FakeRunner)
    seen: list = []
    with caplog.at_level(logging.INFO, logger="arl_lite.core.watcher"):
        _run_one_round(st, seen)

    line = next((r.getMessage() for r in caplog.records
                 if "done in" in r.getMessage() and "new=" in r.getMessage()), "")
    assert line, "没抓到汇总那行日志"
    missing = [t for t in ASSET_TABLES if f"{t}=" not in line]
    assert not missing, f"汇总日志没提这些资产表:{missing}\n实际:{line}"


# ── 五、改 for 循环没改坏 critical finding 检测 ──
# r52 把变更检测那个五元 for 循环也换成了契约表派生。
# 那个循环里有 `if table == "findings"` 的分支,换掉来源后它还认不认得?

def test_critical_findings_are_still_collected(ws, monkeypatch):
    """severity=critical 的 finding 仍要单独通知

    这条守的是 r52 自己的改动:for 循环的 iterable 从手抄五元组换成了
    `Monitor._ASSET_TABLES.items()`,`table == "findings"` 这个分支
    必须还对得上。守不住的话就是「修一个漏算,弄坏一个检测」。
    """
    st, _ = ws
    got: list = []

    class _CritRunner(_FakeRunner):
        async def run(self, target, modules=None, preset=None, config=None):
            self.storage.add_finding(1, f"a.{target}", "vuln",
                                     title="RCE", severity="critical")
            self.storage.add_finding(1, f"b.{target}", "fingerprint",
                                     title="nginx", severity="info")
            return None
    monkeypatch.setattr("arl_lite.core.task_runner.TaskRunner", _CritRunner)
    # 只关心 critical 那条通知,task_done 拦掉免得它去摸 webhook 的接口
    monkeypatch.setattr("arl_lite.notify.notify_task_done",
                        lambda *a, **kw: True)
    got: list = []
    monkeypatch.setattr("arl_lite.notify.notify_critical_finding",
                        lambda hook, f: got.append(f))

    w = Watcher(st, webhook_config=object())
    wt = WatchTarget("example.com", modules=["dns"])
    w._targets[wt.target] = wt
    w._run_target(wt)

    assert len(got) == 1, (
        f"critical finding 通知了 {len(got)} 次,应该只有 1 次 —— "
        f"改 for 循环的来源时把 `table == \"findings\"` 那个分支弄丢了")
    assert got[0].get("severity") == "critical"
    assert got[0].get("title") == "RCE"


# ── 六、结构:清单只能来自契约表 ──
# 行为测试守住「现在 5 种都对」。可它拦不住下一种退化:
# 下次加第 6 种资产时,行为测试的参数化会自动跟着 `Monitor._ASSET_TABLES`
# 变宽(上面 `ASSET_TABLES` 就是从它派生的),但**实现里的手抄清单不会变** ——
# 那时只有结构判据能喊。

def test_run_target_has_no_handwritten_asset_table_tuple():
    """`_run_target` 里不该再有「表名字符串组成的元组」

    用 AST,不用文本子串:注释里提到表名不算数(决策 —— 结构检查用 AST)。
    判据只管一件事 —— 有没有手抄的清单元组,所以它比它守的东西宽一点,
    正好:手抄成别的形状(列表/多个元组)也该被逮到。
    """
    tree = ast.parse((REPO / "arl_lite" / "core" / "watcher.py")
                     .read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_run_target")
    tables = set(ASSET_TABLES)
    handwritten = []
    for node in ast.walk(fn):
        if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            continue
        strs = [e.value for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if tables & set(strs):
            handwritten.append([s for s in strs])
    assert not handwritten, (
        f"_run_target 里还有手抄的资产表清单:{handwritten} —— "
        f"必须从 Monitor._ASSET_TABLES 派生。r52 之前这里抄过两份,"
        f"而且两份不一致(3 元 vs 5 元)。")


def _contract_refs(node):
    """`node` 里所有**真的引用了** `Monitor._ASSET_TABLES` 的 AST 节点

    r80:认节点不认文本。原来那句 `assert "Monitor._ASSET_TABLES" in ast.unparse(fn)`
    是假绿的 —— unparse 会把字符串字面量原样留在结果里,所以一个
    `_hint = "Monitor._ASSET_TABLES"` 就能把真调用换成手抄清单还照样通过
    (r52 那个 bug 的原样形状,实测过)。
    """
    return [n for n in ast.walk(node)
            if isinstance(n, ast.Attribute) and n.attr == "_ASSET_TABLES"
            and isinstance(n.value, ast.Name) and n.value.id == "Monitor"]


def test_both_asset_list_reads_come_from_the_contract_table():
    """数新增的清单和逐类检测的清单,来源必须是 `Monitor._ASSET_TABLES`"""
    tree = ast.parse((REPO / "arl_lite" / "core" / "watcher.py")
                     .read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_run_target")
    # 派生出来的那个局部变量要真的来自契约表
    assert _contract_refs(fn), (
        "_run_target 里没引用契约表 —— 资产清单从别处来了")
    # 逐类检测那个循环也得走它(它是「数新增」和「做检测」唯一的公共来源)
    loops = [n for n in ast.walk(fn)
             if isinstance(n, ast.For) and _contract_refs(n.iter)]
    assert loops, (
        "变更检测那个循环不再从契约表取 —— 它和数新增的清单会各自漂移")


def test_contract_table_and_detected_types_stay_in_step():
    """逐类检测遍历的类型 == 契约表的类型,一个不多一个不少

    这条钉的是「数」的那一半:r52 之前 `new_by_type` 只有 3 个键,
    遍历的却是 5 种 —— 两个数,差 2 种,谁也不提醒谁。
    """
    tree = ast.parse((REPO / "arl_lite" / "core" / "watcher.py")
                     .read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_run_target")
    # `new_by_type = {t: ... for t in asset_tables}` 里那个可迭代对象。
    # 字典推导自己没有 target —— target 在它外层的 Assign 上。
    key = next((n for n in ast.walk(fn)
                if isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == "new_by_type"
                        for t in n.targets)
                and isinstance(n.value, ast.DictComp)), None)
    assert key is not None, "找不到 new_by_type 的字典推导"
    it = key.value.generators[0].iter
    assert isinstance(it, ast.Name) and it.id == "asset_tables", (
        f"new_by_type 的键不是从 asset_tables 来的,而是 {ast.unparse(it)}")

    # asset_tables 得真的是从契约表派生的那个
    assigns = [n for n in ast.walk(fn)
               if isinstance(n, ast.Assign)
               and any(getattr(t, "id", None) == "asset_tables" for t in n.targets)]
    assert len(assigns) == 1, f"asset_tables 被赋值 {len(assigns)} 次"
    assert _contract_refs(assigns[0].value), (
        f"asset_tables 不是从契约表派生的:{ast.unparse(assigns[0].value)}")
