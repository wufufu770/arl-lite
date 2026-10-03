"""r44:资产自身的字段变了,系统得看得见

## 缺的是什么

r42 查实:`CHANGE_TYPES` 只有 `NEW_ASSET` / `DISAPPEARED`,注释里说的
另外 4 种(`TITLE_CHANGED` / `TECH_CHANGED` / `FINGERPRINT_CHANGED` /
`STATUS_CHANGED`)**从来没实现**。

实测(本文件写之前):一个 host 的 IP 从 `1.1.1.1` 换到 `2.2.2.2` 再换回来,

    asset_changes 条数: 0

库里没留下任何「IP 换过」的痕迹。而这恰恰是资产监控最主要的那类信号 ——
现在只能报「多了」和「没了」。

## 不需要新增快照表(这是 r44 查实的关键事实)

资产表是原地 upsert 的、不留历史,乍看要对比「上一轮的值」就得加一张
快照表。但 `Storage._upsert_asset` 在写之前**已经查过一次该 hash 在不在**
(storage.py:653),只差把 `SELECT 1` 换成 `SELECT *` —— **旧行就是上一轮
扫描的状态**。同一句查询,不多一次往返,schema 一个字不用动。

生产路径也确认过了:20 多个采集器全部走 `add_domain` / `add_host` /
`add_port` / `add_site` / `add_finding` → `_upsert_asset`,是单一入口。

## 判据要和 UPDATE 一致,不是拿新旧值直接比

TEXT 类字段的 UPDATE 是 `COALESCE(NULLIF(new, ''), old)` —— **空值不覆盖
旧值**。所以判据也必须这样:新值为空时存着的值没变,不报。否则一次采集
失败(模块超时、解析不出来)会刷出一整片「字段被清空」的假告警。

## `bulk_insert` 不走这条路,而且是明写的

批量路径的 `ON CONFLICT DO UPDATE SET last_seen = ?` **压根不刷新业务
字段**,经它入库的资产字段真变了数据库里也不会变 —— 在那边做检测会永远
比出「没变」。r44 实测 `bulk_insert` 在 `arl_lite/` 里**零生产调用**
(只有测试用),所以这不是活的风险。`test_bulk_insert_has_no_production_caller`
盯着,哪天有人把它接进生产,那条测试会先红。

## r40/r41 的基线判据到这里才第一次真的生效

`record_change` 判基线要有双边快照,而 r42 之前生产里没有任何调用点
同时传 before 和 after。现在字段级检测补上了这一步。实测:

    单向演进 A->B1..B5  6 次入库 -> asset_changes 5 条(全报,不丢真变化)
    抖动     A<->B x6   6 次入库 -> asset_changes 3 条(第 4 次起判基线)
"""
from __future__ import annotations

import ast
import json
import tempfile
from pathlib import Path

import pytest

from arl_lite.core.monitor import (
    CHANGE_TYPES, FIELD_CHANGE_TYPES, DEFAULT_BASELINE_LEARN,
    attach_field_change_sink, list_changes,
)
from arl_lite.db.storage import Storage

REPO = Path(__file__).resolve().parent.parent
ARL_ROOT = REPO / "arl_lite"


@pytest.fixture
def st(tmp_path):
    """一个**接上了 sink** 的 Storage —— 和生产走 TaskRunner 时一样

    不接就一条变更都记不到,那不是被测逻辑在工作,是线没插上。
    接线本身由 `test_task_runner_wires_the_sink` 单独盯着。
    """
    s = Storage(workspace="t", workspace_root=tmp_path)
    attach_field_change_sink(s)
    return s


def _changes(st, change_type=None) -> list[dict]:
    return [c for c in list_changes(st, limit=99) if change_type is None
            or c["change_type"] == change_type]


# ── 主判据:字段变了就得有痕迹 ──

def test_ip_change_is_recorded(st):
    """host 换了 IP → 写一条 ADDRESS_CHANGED,前后值都在 diff 里"""
    st.add_host(1, "web.example.com", ip="1.1.1.1")
    st.add_host(1, "web.example.com", ip="2.2.2.2")
    rows = _changes(st, "ADDRESS_CHANGED")
    assert len(rows) == 1, f"换了 IP 却只有 {len(rows)} 条变更记录"
    d = json.loads(rows[0]["diff"])
    assert d == {"ip": {"before": "1.1.1.1", "after": "2.2.2.2"}}, d


def test_new_asset_produces_no_field_change(st):
    """首次入库不是「字段变了」—— 它是 NEW_ASSET 的事

    混在一起的话,一个刚发现的资产会先被记一条 ip 从 None 变到 X,
    看起来像「这个资产刚换了 IP」。
    """
    st.add_host(1, "web.example.com", ip="1.1.1.1")
    assert not _changes(st, "ADDRESS_CHANGED"), "首次入库被当成了字段变更"


def test_unchanged_field_produces_nothing(st):
    """值没变就不该有记录 —— 同一轮重扫是常态"""
    st.add_host(1, "web.example.com", ip="1.1.1.1")
    for _ in range(3):
        st.add_host(1, "web.example.com", ip="1.1.1.1")
    assert not _changes(st, "ADDRESS_CHANGED")


def test_field_changing_back_and_forth_is_recorded_each_time(st):
    """换回去也是一次变化 —— 除非它已经是基线抖动(见下面那组)"""
    st.add_host(1, "web.example.com", ip="1.1.1.1")
    st.add_host(1, "web.example.com", ip="2.2.2.2")
    st.add_host(1, "web.example.com", ip="1.1.1.1")
    assert len(_changes(st, "ADDRESS_CHANGED")) == 2, "换回去那次没记上"


# ── 判据必须和 UPDATE 一致,否则采集失败会刷出假告警 ──

def test_empty_new_value_does_not_erase_and_is_not_reported(st):
    """TEXT 字段的 UPDATE 是 `COALESCE(NULLIF(new,''), old)` —— 空不覆盖

    所以新值为空时:库里还留着旧值,也就**不该**报「变了」。
    判据若直接比新旧值,一次采集失败(模块超时/解析不出来)
    就会刷出一整片「这个资产的 IP 被清空了」的假告警。
    """
    st.add_host(1, "web.example.com", ip="1.1.1.1")
    st.add_host(1, "web.example.com", ip=None)
    assert not _changes(st, "ADDRESS_CHANGED"), "空值被当成了「变了」"
    with st._conn() as c:
        row = c.execute("SELECT ip FROM hosts").fetchone()
    assert row["ip"] == "1.1.1.1", f"空值把旧值抹掉了:{row['ip']}"


def test_empty_then_real_value_is_reported(st):
    """空值不算变,但空值之后的真变化要算"""
    st.add_host(1, "web.example.com", ip="1.1.1.1")
    st.add_host(1, "web.example.com", ip=None)
    st.add_host(1, "web.example.com", ip="2.2.2.2")
    rows = _changes(st, "ADDRESS_CHANGED")
    assert len(rows) == 1, f"空值之后那次的真变化没记上:{len(rows)}"
    assert json.loads(rows[0]["diff"])["ip"]["before"] == "1.1.1.1"


# ── 每个字段映射到自己的类型 ──

def test_each_field_gets_its_own_change_type(st):
    """换 IP 报 ADDRESS_CHANGED,换标题报 TITLE_CHANGED,换 tech 报 TECH_CHANGED

    都塞进一个类型里的话,消费方就得自己去猜「这条到底是啥变了」,
    而 `diff` 里明明写着字段名。
    """
    st.add_host(1, "web.example.com", ip="1.1.1.1")
    for title, tech in (("Home", "nginx"), ("Home 2", "nginx"),
                        ("Home 2", "apache")):
        st.add_site(1, "http://web.example.com", "web.example.com", "1.1.1.1",
                    80, "http", title=title, tech=tech)
    st.add_host(1, "web.example.com", ip="2.2.2.2")
    kinds = {c["change_type"] for c in _changes(st)}
    assert "ADDRESS_CHANGED" in kinds, kinds
    assert "TITLE_CHANGED" in kinds, kinds
    assert "TECH_CHANGED" in kinds, kinds


def test_derived_fields_are_deliberately_not_reported(st):
    """`cert_days_left` / `cert_expired` 是**算出来的**,变了不是资产变了

    `cert_days_left` 每过一天就少 1。报它等于每轮扫描刷一条。
    这里钉住「不报」,免得哪天有人把可变字段表一视同仁地全配上类型。
    """
    assert "cert_days_left" in Storage._MUTABLE_PLAIN["sites"]
    assert "cert_days_left" not in FIELD_CHANGE_TYPES, (
        "cert_days_left 被配上类型了 —— 它每天都在变"
    )
    assert "cert_expired" not in FIELD_CHANGE_TYPES, (
        "cert_expired 到期就翻面,报它等于刷噪音"
    )


def test_field_types_are_all_real_types(st):
    """契约表里的每个值都得是 `CHANGE_TYPES` 里的真类型"""
    bad = set(FIELD_CHANGE_TYPES.values()) - set(CHANGE_TYPES)
    assert not bad, f"契约表引用了不存在的变更类型:{bad}"


# ── r40/r41 的基线判据到这里才第一次真的生效 ──

def test_one_way_address_evolution_is_never_swallowed(st):
    """IP 一路换新值(机房迁移)→ 一次都不许被基线吃掉

    r41 修的就是这个误判,但在 r44 之前它只在测试里生效过 ——
    生产路径上根本没有双边快照的调用点。
    """
    st.add_host(1, "web.example.com", ip="10.0.0.1")
    for i in range(2, 8):
        st.add_host(1, "web.example.com", ip=f"10.0.0.{i}")
    assert len(_changes(st, "ADDRESS_CHANGED")) == 6, (
        f"单向演进被基线吃掉了,只剩 {len(_changes(st, 'ADDRESS_CHANGED'))} 条"
    )


def test_flapping_address_is_treated_as_baseline(st):
    """A↔B 来回换 → 第 N 次起判为基线,不再刷屏"""
    st.add_host(1, "web.example.com", ip="A")
    for i in range(DEFAULT_BASELINE_LEARN + 2):
        st.add_host(1, "web.example.com", ip="B" if i % 2 == 0 else "A")
    assert len(_changes(st, "ADDRESS_CHANGED")) == DEFAULT_BASELINE_LEARN, (
        "抖动没被基线压住,或者压得太早"
    )


# ── 接入点的覆盖范围 ──

def test_bulk_insert_has_no_production_caller():
    """`bulk_insert` 生产零调用,字段级检测才敢只在 `_upsert_asset` 上做

    它的 `ON CONFLICT DO UPDATE SET last_seen = ?` 压根不刷新业务字段 ——
    经它入库的资产,字段真变了数据库里也不会变,在那边检测会永远
    比出「没变」。哪天有人把它接进生产,这条测试先红,逼人面对这个决定。
    """
    hits = []
    for path in sorted(ARL_ROOT.rglob("*.py")):
        if path.name == "storage.py":
            continue                       # 定义本身不算
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else (
                    fn.attr if isinstance(fn, ast.Attribute) else None)
                if name == "bulk_insert":
                    hits.append(f"{path.relative_to(REPO)}:{node.lineno}")
    assert not hits, (
        f"bulk_insert 有了生产调用点 {hits} —— 字段级检测在批量路径上是假的,"
        f"得先决定它要不要刷新业务字段"
    )


def test_production_collectors_all_go_through_upsert(st):
    """采集器走的是 `add_*` → `_upsert_asset`,字段级检测才接得上

    反证:如果哪天某条采集路径绕过了 `_upsert_asset` 直连 SQL,
    它的字段变更就永远不会被记 —— 而表面上什么异常都没有。
    """
    n = 0
    for path in sorted((ARL_ROOT / "modules").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for api in ("add_domain", "add_host", "add_port", "add_site",
                    "add_finding"):
            n += text.count(f".{api}(")
    assert n >= 20, (
        f"只扫到 {n} 个 add_* 调用点 —— 扫��器本身可能失效了 "
        f"(2026-10 实测是 20+)"
    )


# ── 三条守卫:今天都够不着,得显式把它们推到眼前才测得到 ──
#
# 下面三条测的守卫,在当前代码里**都走不到**(每条后面都写了原因)。
# 变异测试里它们全都存活:不是因为守卫没用,是因为今天没有能触发
# 它们的输入。把「未来的输入」显式造出来,守卫才护得住。


def test_a_field_no_table_refreshes_is_not_reported(st, monkeypatch):
    """契约表里有个字段,但没有任何表会刷新它 → 不许报

    `port` 进了 `add_site` 的 `fields`,却不在 sites 的任何一张可变
    字段表里,所以那句 `ON CONFLICT ... DO UPDATE` **不会**更新它。
    数据库压根不动它,报出来就是一条永远「变了」的死记录。

    这里用 monkeypatch 模拟「以后有人往契约表里加了个新字段」——
    今天还没有这样一个字段,不模拟就测不到这道守卫。
    """
    monkeypatch.setitem(FIELD_CHANGE_TYPES, "port", "STATUS_CHANGED")
    assert "port" not in Storage._MUTABLE_TEXT["sites"]
    assert "port" not in Storage._MUTABLE_PLAIN["sites"]

    st.add_site(1, "http://web.example.com", "web.example.com", "1.1.1.1",
                80, "http", title="Home")
    st.add_site(1, "http://web.example.com", "web.example.com", "1.1.1.1",
                8080, "http", title="Home")
    assert not _changes(st, "STATUS_CHANGED"), (
        "数据库不刷新的字段被报成了变更 —— 那是一条永远「变了」的死记录"
    )


def test_recording_failure_does_not_break_ingestion(st):
    """变更记录是旁路:它挂了,资产数据照样要进库

    采集器只管把资产写进来,变更监控是搭在上面的。记录失败就中断入库,
    等于让一个观察功能把被观察的东西弄没了。

    这里直接塞一个会抛的 sink,而不是 monkeypatch `record_change` ——
    自己的 sink 内部已经 `except` 住了(它更清楚自己是旁路),
    那一层挡下的异常根本到不了 storage,测不到 storage 的那道守卫。
    """
    st.add_host(1, "web.example.com", ip="1.1.1.1")   # 先落一行,下面才是存量

    def boom(*a, **k):
        raise RuntimeError("变更记录写不进去")

    st.set_field_change_sink(boom)
    assert st.add_host(1, "web.example.com", ip="2.2.2.2") is False, (
        "入库的新旧判断被记录失败影响了 —— 它应该照旧返回 False"
    )
    with st._conn() as c:
        row = c.execute("SELECT ip FROM hosts").fetchone()
    assert row["ip"] == "2.2.2.2", "记录失败把入库也拖累了 —— 新值没写进去"


def test_sink_failure_is_logged_not_swallowed_silently(st, caplog):
    """记录失败要留下痕迹,不能静默"""
    st.add_host(1, "web.example.com", ip="1.1.1.1")
    st.set_field_change_sink(lambda *a: (_ for _ in ()).throw(ValueError("boom")))
    st.add_host(1, "web.example.com", ip="2.2.2.2")
    assert "field change not recorded" in caplog.text, (
        "记录失败被完全静默了 —— 下一个人只会看到「变更没记上」"
    )


def test_type_change_is_still_a_change(st):
    """`True → 1` 也是变化 —— `==` 会把它整条吞掉

    `True == 1` 在 Python 里为真。r41 在基线判据上栽过一次,这里是
    同一个陷阱在检测侧:用 `!=` 判,这类变更会静默消失。

    直接调 `_detect_field_changes` 喂,因为今天还没有哪个真实字段
    会从布尔变成整数(cert_expired / cert_self_signed 是 0/1 的 int,
    不是 bool)—— 不构造就测不到这道守卫。
    """
    old = {"state": True}
    out = st._detect_field_changes(old, {"state": 1},
                                   Storage._MUTABLE_TEXT.get("ports", ()),
                                   Storage._MUTABLE_PLAIN.get("ports", ()))
    assert out == {"state": {"before": True, "after": 1}}, (
        f"true→1 被当成了没变:{out}"
    )


def test_table_outside_the_asset_whitelist_is_skipped(st):
    """表名不在 `Monitor._ASSET_TABLES` 里 → 不做字段级检测

    拿不到 asset_type 就硬猜一个(比如默认当 host),等于把别的表上的
    变更记成 host 的 —— 那是**编造**,不是兜底。`asset_type` 本身是
    `record_change` 的第一等参数,错了一整条记录就是错的。
    """
    st.add_host(1, "web.example.com", ip="1.1.1.1")
    with st._conn() as c:
        old = c.execute("SELECT * FROM hosts").fetchone()
    before = len(_changes(st))
    changes = st._detect_field_changes(old, {"ip": "2.2.2.2"},
                                       Storage._MUTABLE_TEXT["hosts"],
                                       Storage._MUTABLE_PLAIN.get("hosts", ()))
    assert changes == {"ip": {"before": "1.1.1.1", "after": "2.2.2.2"}}, \
        "db 层只该交出「ip 变了」,不该对类型下判断"
    st._on_field_change("widgets", old["hash"], changes)   # 绕过 upsert 直接喂 sink
    assert len(_changes(st)) == before, "不在白名单的表被记了变更"


def test_task_runner_wires_the_sink():
    """生产摄入路径确实接上了 sink —— 线不能是断的

    `db` 层不认识 `core`,所以这根线只能由上面接。接的地方是
    `TaskRunner.__init__`:所有扫描都经过它,是摄入路径的唯一入口。
    没接的话,字段级检测在生产里**完全不工作**,而表面上一片正常。
    """
    import tempfile as _tf

    from arl_lite.core.task_runner import TaskRunner
    plain = Storage(workspace="t",
                    workspace_root=Path(_tf.mkdtemp(prefix="sink-")))
    assert plain._on_field_change is None, "Storage 不该自带回调"
    TaskRunner(storage=plain, workspace_id=1)
    assert plain._on_field_change is not None, (
        "TaskRunner 没接 sink —— 字段级检测在生产里是死的"
    )
