"""r47:`_ASSET_LABEL_FIELDS` 是一份没人钉的手写词表

## r46 引入的退化路径(实测)

`_ASSET_LABEL_FIELDS`(cli.py)手写了 5 种资产的标识列。把 `host` 的
标识列改成不存在的列:

    改前: [ADDRESS_CHANGED   ] host  web.example.com          2026-10-03T05:21:57
    改后: [ADDRESS_CHANGED   ] host  68cd3922cdc45da2         2026-10-03T05:21:57
          捕获到的 WARNING 条数: 0

`SELECT hostname_renamed FROM hosts` 抛 `OperationalError` → 旧的
`except Exception: pass` 全吞 → 继续退到快照 → 快照里没有 host →
退到 `hash[:16]`。**用户看到 hash 前缀只会以为「标识本来就长这样」。**

这就是 r45 刚花一整轮清掉的毛病(硬编码词表没人管),我在下一轮又
自己引入了一份。

## 两条修法,缺一不可

一,**列名对着 schema 钉一遍**:每个标识列都必须真实存在于对应表。
列一改名就红 —— 不靠「跑一遍看输出对不对」这种要人眼判断的事。

二,**静默降级要分两种**:行不存在(资产可能已删)是正常的,静默兜底;
SQL 报错(列名错了 / 表没了)必须 `log.warning`。静默降级比降级本身更坏。

## 5 种资产都要有行为断言

r46 的测试只覆盖 host 和 site,`port` / `finding` / `domain` 三张表
任何一列被改名都不会有东西变红。这里 5 种全钉。
"""
from __future__ import annotations

import contextlib
import io
import logging
import sqlite3

import pytest

from arl_lite.cli import (_ASSET_LABEL_FIELDS, _ASSET_TYPES_TO_TABLE,
                          _change_label, main)
from arl_lite.core.monitor import record_change
from arl_lite.db.storage import Storage


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """一个独立工作区,并让 `main()` 里的 Storage 也指到它

    同 `test_monitor_changes_output.py`:只改 HOME,**不要**给 fixture
    的 Storage 传 workspace_root —— `cmd_monitor_changes` 自己
    `Storage(workspace=...)`,两边不是同一个目录的话测的是空 workspace。
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    st = Storage(workspace="t")
    return st, tmp_path


def _run(*argv) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        main(list(argv))
    return buf.getvalue()


# ── 一、列名对着 schema 钉一遍 ──
# 这是本轮的主判据:词表里每一列都必须真实存在于对应表。列一改名就红。

def _schema_columns() -> dict[str, set[str]]:
    """每张表的真实列名 —— 直接把 schema.sql 跑进内存库读出来

    不用正则去抠 `CREATE TABLE` 文本:那样读的是「schema 写了什么」,
    而这里的判据是「这张表真的能不能这么查」。
    """
    import pathlib
    sql = (pathlib.Path(__file__).resolve().parents[1]
           / "arl_lite" / "db" / "schema.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    conn.executescript(sql)
    out = {}
    for _at, table in _ASSET_TYPES_TO_TABLE.items():
        out[table] = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    conn.close()
    return out


def test_every_asset_type_has_a_label_source():
    """5 种资产都得有标识列 —— 漏一种就是那种资产的标识永远认不出来"""
    for at in _ASSET_TYPES_TO_TABLE:
        assert at in _ASSET_LABEL_FIELDS, (
            f"{at} 没有登记标识列,`monitor changes` 只能显示 hash"
        )
        assert _ASSET_LABEL_FIELDS[at], f"{at} 的标识列是空的"


def test_every_label_column_exists_in_its_table():
    """**每一条**标识列都必须真实存在于对应表

    这是 r47 的核心断言。`SELECT` 里的列名写错时 SQLite 抛
    `OperationalError`,而这条退化路径已经被证实是零警告的 ——
    所以只能靠静态对着 schema 拦,不能靠「跑一遍看输出」。

    注意用 `in` 逐个查而不是整条 SELECT 一次:前者能报出**是哪一列**
    错了,后者只告诉你整个查询挂了。
    """
    cols_by_table = _schema_columns()
    for at, fields in _ASSET_LABEL_FIELDS.items():
        table = _ASSET_TYPES_TO_TABLE.get(at)
        assert table, f"{at} 没有对应的表,标识列无从查起"
        cols = cols_by_table.get(table, set())
        assert cols, f"表 {table} 不在 schema 里(或读不到列)"
        for f in fields:
            assert f in cols, (
                f"{at} 的标识列 {f!r} 在表 {table} 里不存在。"
                f"该表实际有的列: {sorted(cols)}"
            )


def test_label_query_actually_runs_for_every_asset_type(ws):
    """把每一种的标识查询真跑一遍 —— 上两条都是静态的,这条是动态的

    静态断言只能证明「列名在 schema 里」,证明不了「这条 SELECT 在
    一个真实库上能跑通」。两者都要。
    """
    st, _ = ws
    for at in _ASSET_TYPES_TO_TABLE:
        _change_label(st, {"asset_hash": "h-x", "asset_type": at})


# ── 二、5 种资产各一条:标识必须是人能认的东西 ──
# 用真实的行走一遍生产路径,拿到表里的 hash,再 record_change,
# 最后看 CLI 输出的那一行。

def _hash_of(st, table) -> str:
    with st._conn() as c:
        return c.execute(f"SELECT hash FROM {table}").fetchone()["hash"]


def _label_line(out: str, change_type: str, asset_type: str) -> str:
    """挑出那一条变更的标识行

    必须**同时**按 change_type 和 asset_type 过滤:下面那些测试一次造好
    5 种资产的变更,change_type 全是同一个,只按类型挑会永远拿到第一条
    (domain),于是 host/port/finding 三条测的其实都是 domain 的标识。
    """
    return next(ln for ln in out.splitlines()
                if change_type in ln and asset_type in ln)


# 每种资产一条变更,用不同的 change_type 免得行号挑不准
_ONE_CHANGE_PER_TYPE = {
    "domain": "ADDRESS_CHANGED", "host": "TITLE_CHANGED",
    "port": "TECH_CHANGED", "site": "STATUS_CHANGED",
    "finding": "FINGERPRINT_CHANGED",
}
# 快照里只装变动的那一个字段 —— 必须保证它**不**落在该资产的标识列里,
# 否则「退到快照」会抢先给出一个值,测的就不是「查表」这条路了
_ONE_FIELD_SNAPSHOT = {
    "domain": "ip", "host": "banner", "port": "service",
    "site": "title", "finding": "severity",
}


@pytest.mark.parametrize("asset_type,table", [
    ("domain", "domains"), ("host", "hosts"), ("port", "ports"),
    ("site", "sites"), ("finding", "findings"),
])
def test_all_five_asset_types_show_a_readable_identity(ws, asset_type, table):
    """5 种资产的标识都得认得出来,不能是 hash 前缀

    `68cd3922cdc45da2` 对人没有意义,而身份字段本来就在库里。
    r46 只钉了 host 和 site,另外 3 种是裸的。
    """
    st, _ = ws
    st.add_domain(1, "d.example.com", "subdomain")
    st.add_host(1, "web.example.com", ip="1.1.1.1")
    st.add_port(1, "1.1.1.1", 443, protocol="tcp", service="https")
    st.add_site(1, "http://shop.example.com", "shop.example.com",
                "1.1.1.1", 80, "http", title="Shop")
    st.add_finding(1, "http://shop.example.com", "weak_tls",
                   title="过期证书", cve="CVE-2024-0001")
    h = _hash_of(st, table)

    ct = _ONE_CHANGE_PER_TYPE[asset_type]
    fld = _ONE_FIELD_SNAPSHOT[asset_type]
    record_change(st, asset_type, ct, h,
                  before={fld: "old"}, after={fld: "new"})
    line = _label_line(_run("monitor", "changes", "-w", "t"), ct, asset_type)

    assert h[:16] not in line, (
        f"{asset_type} 的标识退化成 hash 了 —— 认表里的列名可能写错了。\n"
        f"该表 {table} 登记的标识列是 {_ASSET_LABEL_FIELDS[asset_type]}"
    )
    # 至少要有一个真实身份字段的值出现在那一行里
    expect = {
        "domain": "d.example.com",
        "host": "web.example.com",
        "port": "1.1.1.1",          # ports 表里身份列是 ip
        "site": "http://shop.example.com",
        "finding": "CVE-2024-0001",
    }[asset_type]
    assert expect in line, f"{asset_type} 的标识行里没有 {expect!r}:\n{line}"


# ── 三、静默降级要分两种 ──

def test_sql_error_warns_instead_of_silently_degrading(ws, monkeypatch, caplog):
    """标识列名写错 → SQL 报错 → 必须 warning,不能闷着头退化成 hash

    这是本轮实测过的那条退化路径。静默降级比降级本身更坏:输出看起来
    完全正常(只是标识难看了一点),没有任何地方会提示出错了。

    变异元组给**两个**不存在的列,而且快照里那个字段(`ip`)两个都碰不到,
    于是三级回退真的退到 hash。这里刻意不用单列:单列时
    `", ".join(fields)` 和 `fields[0]` 是同一个字符串,「警告有没有列出
    全部列」那条判据就测不出来了(M4 存活过一轮才发现)。
    """
    st, _ = ws
    st.add_host(1, "web.example.com", ip="1.1.1.1")
    h = _hash_of(st, "hosts")
    monkeypatch.setitem(_ASSET_LABEL_FIELDS, "host",
                        ("hostname_renamed", "ip_renamed"))
    record_change(st, "host", "ADDRESS_CHANGED", h,
                  before={"ip": "1.1.1.1"}, after={"ip": "2.2.2.2"})

    with caplog.at_level(logging.WARNING, logger="arl_lite.cli"):
        out = _run("monitor", "changes", "-w", "t")

    assert h[:16] in out, "前提没成立:改坏列名后输出没退化成 hash,这条测不到东西"
    warns = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warns, (
        "标识列名写错导致 SQL 报错,却零警告 —— 用户只会以为"
        "「标识本来就长这样」"
    )
    msg = warns[0].getMessage()
    assert "hosts" in msg, f"警告里没说是哪张表:{msg}"
    # 两列都要在。只报一列的话,修完第一列还是坏的,而人已经以为改完了。
    for col in ("hostname_renamed", "ip_renamed"):
        assert col in msg, f"警告里漏了列 {col!r},等于没给人可操作的信息:{msg}"


def test_missing_row_falls_back_silently(ws, caplog):
    """行不存在是**正常**的(资产可能已删),不能也去 warn

    方向和上一条相反,两条都要:什么都 warn 会把真正的错淹没在噪音里。
    这里的行本来就不存在(只 record_change,不建资产),而快照里那个
    字段(`title`)也不在 site 的标识列(`url`)里,所以三级回退真的
    退到了 hash。
    """
    st, _ = ws
    record_change(st, "site", "TITLE_CHANGED", "h-not-in-table",
                  before={"title": "A"}, after={"title": "B"})
    with caplog.at_level(logging.WARNING, logger="arl_lite.cli"):
        out = _run("monitor", "changes", "-w", "t")
    assert "h-not-in-ta" in out, f"行不存在时该退到 hash:\n{out}"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
        "资产已删导致的行不存在是正常情况,不该 warn"
    )


def test_sql_error_on_table_warns_and_does_not_crash(ws, monkeypatch, caplog):
    """表名错了(→ `no such table`)也要 warn,而且不能崩

    展示层因为一条坏数据崩掉不值当,但闷着退化成 hash 更坏 —— 两条
    都要,少一条都是残的。

    ## 这里为什么是改表名而不是 `DROP TABLE`(实测结论)

    想过「把 hosts 表删掉」来模拟,实测**走不到**:`Storage.__init__`
    每次都跑 `_init_schema()` → `executescript(schema.sql)`(全是
    `CREATE TABLE IF NOT EXISTS`),而 `cmd_monitor_changes` 自己
    `Storage(workspace=...)`,所以刚 DROP 掉,新连接一建表就回来了。
    实测 `st2._conn()` 里 `sqlite_master` 仍有 `hosts`。所以「表没了」
    这条退化路径在 CLI 上其实不可达,改用改映射来覆盖同一个 except 分支。
    """
    st, _ = ws
    st.add_host(1, "web.example.com", ip="1.1.1.1")
    h = _hash_of(st, "hosts")
    record_change(st, "host", "TITLE_CHANGED", h,
                  before={"banner": "old"}, after={"banner": "new"})
    monkeypatch.setitem(_ASSET_TYPES_TO_TABLE, "host", "hosts_renamed")

    with caplog.at_level(logging.WARNING, logger="arl_lite.cli"):
        out = _run("monitor", "changes", "-w", "t")   # 不能抛
    assert "TITLE_CHANGED" in out, "表名错了不该让整份报表出不来"
    assert h[:16] in out, "该退到 hash 而不是崩掉"
    warns = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warns, "表名错了却一声不吭"
    assert "hosts_renamed" in warns[0].getMessage(), warns[0].getMessage()


# ── 四、查表优先这个次序本身要被钉住 ──

def test_table_wins_over_snapshot_fallback(ws, monkeypatch):
    """查表优先于快照 —— 快照里只有变动的那一个字段

    r46 已经有一条 `test_identity_does_not_drift_with_the_changed_field`
    从输出侧钉这件事(针对 host)。这里从查询侧钉:即便快照里**真的**
    放着一个可读的身份值,只要表里有更新的,也必须给表里的。
    否则「三级回退」其实退化成「两级」,而退化方向是往不稳定那边去。
    """
    st, _ = ws
    st.add_host(1, "web.example.com", ip="1.1.1.1")
    h = _hash_of(st, "hosts")
    row = {"asset_hash": h, "asset_type": "host",
           "after_value": '{"host": "stale.example.com"}',
           "before_value": '{"host": "older.example.com"}'}
    assert _change_label(st, row) == "web.example.com", (
        "快照兜底抢在查表前面了 —— 标识会退化成会变的字段值"
    )
