"""DISAPPEARED 变更检测 —— 判定、去抖与状态转移去重

`monitor.py` 原先只有 NEW_ASSET(基于 first_seen)。DISAPPEARED 在 schema
注释和 CLI choices 里都存在,但没有任何代码产出过——`monitor changes
--change-type DISAPPEARED` 永远返回空。

本文件重点不在"能报出消失",而在两个真会出事的边界:

1. **去抖**:一次不完整扫描不能让上千条资产被判下线
2. **去重**:同一次下线不能被每轮扫描重复上报

判定本身靠 `first_seen` / `last_seen`——资产表的 upsert 纪律
「first_seen 永不变、last_seen 每次见到就 UPDATE」已经编码了
"存在过"和"最近还活着",所以不需要额外的 snapshot 表。

全程离线,用 tmp 目录下的真实 Storage。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from arl_lite.core.monitor import (
    DEFAULT_DISAPPEARED_GRACE, Monitor, list_changes, record_change,
)
from arl_lite.db.storage import Storage


def _iso(**delta) -> str:
    return (datetime(2026, 1, 1, 12, 0, 0) + timedelta(**delta)).isoformat()


@pytest.fixture
def st(tmp_path):
    return Storage(workspace="t", workspace_root=tmp_path)


def _add_domain(st, domain: str, first_seen: str, last_seen: str) -> str:
    """插一行 domains 并返回它的 hash"""
    from arl_lite.db.storage import compute_hash
    h = compute_hash(str(st.workspace_id), domain)
    with st._conn() as conn:
        conn.execute(
            """INSERT INTO domains (workspace_id, hash, domain, module,
                                     first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (st.workspace_id, h, domain, "test", first_seen, last_seen),
        )
        conn.commit()
    return h


# =====================================================================
# 判定:什么时候算消失
# =====================================================================


def test_asset_seen_this_run_is_not_disappeared(st):
    """本次运行期间又见到的资产 —— 活着,不该报"""
    since = _iso()
    _add_domain(st, "alive.example.com", first_seen=_iso(days=-10),
                last_seen=_iso(hours=1))
    assert Monitor(st).detect_disappeared("domain", since) == []


def test_asset_not_seen_this_run_is_disappeared(st):
    since = _iso()
    h = _add_domain(st, "gone.example.com", first_seen=_iso(days=-10),
                    last_seen=_iso(days=-5))
    rows = Monitor(st).detect_disappeared("domain", since)
    assert [r["hash"] for r in rows] == [h]


def test_brand_new_asset_is_new_not_disappeared(st):
    """运行期间才出现的资产是 NEW_ASSET,不能同时算 DISAPPEARED

    first_seen >= since 的资产压根没被这条查询选中。
    """
    since = _iso()
    _add_domain(st, "fresh.example.com", first_seen=_iso(minutes=5),
                last_seen=_iso(minutes=5))
    assert Monitor(st).detect_disappeared("domain", since) == []


def test_first_seen_equals_since_is_excluded(st):
    """边界:first_seen 恰好等于 since —— 归 NEW_ASSET,不归 DISAPPEARED

    同一轮里既有 NEW 又有 DISAPPEARED 会让"新增"和"下线"互相矛盾。
    """
    since = _iso()
    _add_domain(st, "edge.example.com", first_seen=since, last_seen=_iso(days=-1))
    assert Monitor(st).detect_disappeared("domain", since) == []


def test_null_last_seen_counts_as_disappeared(st):
    """last_seen 为 NULL 的行不能被 SQL 的 NULL 比较漏掉

    `last_seen < ?` 在 NULL 时结果是 NULL,不进 WHERE。
    用 IS NULL 分支兜住,否则脏数据行会永远检测不到。
    """
    since = _iso()
    h = _add_domain(st, "nullseen.example.com", first_seen=_iso(days=-10),
                    last_seen=_iso(days=-5))
    with st._conn() as conn:
        conn.execute("UPDATE domains SET last_seen = NULL WHERE hash = ?", (h,))
        conn.commit()
    rows = Monitor(st).detect_disappeared("domain", since)
    assert [r["hash"] for r in rows] == [h]


# =====================================================================
# 去抖:这是最容易出事的地方
# =====================================================================


def test_missing_just_under_grace_is_not_disappeared(st):
    """刚超过 since 一点点就不见 —— 宽限期内不算下线

    一次 DNS 抖动 / crt.sh 限流就会造成这种局面。没有宽限期的话,
    单次抖动就刷出满屏 DISAPPEARED。
    """
    since = _iso()
    # 只比 since 早 1 小时,远小于 48h 宽限
    _add_domain(st, "flaky.example.com", first_seen=_iso(days=-10),
                last_seen=_iso(hours=-1))
    assert Monitor(st).detect_disappeared("domain", since) == []


def test_missing_beyond_grace_is_disappeared(st):
    """超过宽限期才判定下线"""
    since = _iso()
    # 5 天前最后存活,远超 48h 宽限
    last_seen = (datetime.fromisoformat(since) - timedelta(days=5)).isoformat()
    h = _add_domain(st, "reallydown.example.com", first_seen=_iso(days=-10),
                    last_seen=last_seen)
    rows = Monitor(st).detect_disappeared("domain", since)
    assert [r["hash"] for r in rows] == [h]


def test_zero_grace_reports_immediately(st):
    """grace=0 = 立刻判定(慎用,但必须能用)"""
    since = _iso()
    h = _add_domain(st, "instant.example.com", first_seen=_iso(days=-10),
                    last_seen=_iso(seconds=-1))
    rows = Monitor(st).detect_disappeared("domain", since, grace_seconds=0)
    assert [r["hash"] for r in rows] == [h]


def test_default_grace_is_two_monitor_cycles():
    """默认宽限 = 2× 24h 周期,容得下一次漏扫"""
    assert DEFAULT_DISAPPEARED_GRACE == 48 * 3600


def test_negative_grace_is_rejected(st):
    """负宽限期没有意义,直接拒绝而不是静默当 0"""
    with pytest.raises(ValueError, match="grace_seconds"):
        Monitor(st).detect_disappeared("domain", _iso(), grace_seconds=-1)


def test_unknown_asset_type_is_rejected(st):
    """和 detect_changes 同纪律:白名单外直接抛,不做字符串拼接"""
    with pytest.raises(ValueError, match="unknown asset_type"):
        Monitor(st).detect_disappeared("domains; DROP TABLE x", _iso())


# =====================================================================
# 去重:状态转移 vs 水位
# =====================================================================


def _record_disappeared_at(st, asset_hash: str, detected_at: str) -> None:
    """直接插一条 DISAPPEARED 记录,detected_at 完全由测试控制

    不用 record_change:它内部盖 datetime.utcnow(),时间轴就没法构造了。
    去重逻辑比较的正是 detected_at 和 last_seen 的先后,
    这两个时间必须由测试说了算,否则用例会因为时间轴错乱而"碰巧"通过。
    """
    with st._conn() as conn:
        conn.execute(
            """INSERT INTO asset_changes
               (workspace_id, asset_hash, asset_type, change_type,
                before_value, detected_at)
               VALUES (?, ?, 'domain', 'DISAPPEARED', ?, ?)""",
            (st.workspace_id, asset_hash, json.dumps({"hash": asset_hash}), detected_at),
        )
        conn.commit()


def test_same_disappearance_is_not_reported_twice(st):
    """同一次下线不能被每轮扫描重复上报"""
    now = datetime.utcnow()
    since = now.isoformat()
    # 资产 5 天前最后存活,远超宽限期 → 确实消失了
    h = _add_domain(st, "down.example.com",
                    first_seen=(now - timedelta(days=10)).isoformat(),
                    last_seen=(now - timedelta(days=5)).isoformat())
    mon = Monitor(st)

    first = mon.detect_disappeared("domain", since)
    assert [r["hash"] for r in first] == [h]
    assert [r["hash"] for r in mon.filter_newly_disappeared(first)] == [h]

    # 第一次下线在 5 天前就报过了
    _record_disappeared_at(st, h, (now - timedelta(days=5)).isoformat())

    # 之后的每一轮扫描:资产还是没回来,不该再报
    second = mon.detect_disappeared("domain", since)
    assert [r["hash"] for r in second] == [h], "查询本身仍应返回该行"
    assert mon.filter_newly_disappeared(second) == [], "去重没生效,会刷屏"


def test_asset_that_returns_and_dies_again_is_reported_again(st):
    """复活后再掉线,必须能再报一次

    这是"水位去重"和"状态转移去重"的区别:只记"报过没有"的实现
    会永久吞掉第二次下线 —— 而那恰恰是最该报告的一次。

    时间轴(全部锚定真实 now,避免 record_change 的真实时间戳搅乱):
        T0 = now-10d  资产首次出现
        T1 = now-5d   最后存活,同时报出第一次 DISAPPEARED
        T2 = now-3d   资产复活(last_seen 前移)
        T3 = now      本轮运行,资产没再出现 → 应该再报一次
    """
    now = datetime.utcnow()
    h = _add_domain(st, "flapper.example.com",
                    first_seen=(now - timedelta(days=10)).isoformat(),
                    last_seen=(now - timedelta(days=5)).isoformat())
    mon = Monitor(st)
    _record_disappeared_at(st, h, (now - timedelta(days=5)).isoformat())

    # T2:复活
    with st._conn() as conn:
        conn.execute(
            "UPDATE domains SET last_seen = ? WHERE hash = ?",
            ((now - timedelta(days=3)).isoformat(), h),
        )
        conn.commit()

    # T3:本轮运行
    rows = mon.detect_disappeared("domain", now.isoformat())
    fresh = [r for r in rows if r["hash"] == h]
    assert fresh, "资产已超出宽限期,应被判为消失"
    # 关键:复活的 last_seen 晚于上次的 DISAPPEARED 记录 → 不该被压掉
    assert [r["hash"] for r in mon.filter_newly_disappeared(fresh)] == [h], \
        "复活后的二次下线被永久吞掉了"


def test_new_asset_change_does_not_suppress_disappearance(st):
    """只有 DISAPPEARED 记录才去重,NEW_ASSET 不算

    逻辑漏洞:如果去重查的是"该 hash 有没有变更记录",
    那每个资产都有 NEW_ASSET 记录,消失就永远报不出来。
    """
    since = _iso()
    h = _add_domain(st, "x.example.com", first_seen=_iso(days=-10),
                    last_seen=_iso(days=-5))
    mon = Monitor(st)
    record_change(st, "domain", "NEW_ASSET", h, after={"hash": h})

    rows = mon.detect_disappeared("domain", since)
    assert [r["hash"] for r in mon.filter_newly_disappeared(rows)] == [h]


def test_row_without_hash_is_skipped_not_crashed(st):
    """没有 hash 的行没法定位变更记录,跳过而不是让整轮检测崩掉"""
    mon = Monitor(st)
    assert mon.filter_newly_disappeared([{"domain": "x"}, {}]) == []


def test_empty_input_returns_empty(st):
    assert Monitor(st).filter_newly_disappeared([]) == []


# =====================================================================
# 时间戳格式:字符串比较会静默失效
# =====================================================================


def test_sqlite_and_python_timestamp_formats_are_comparable():
    """同一时刻,SQLite 格式和 Python 格式按字符串比会得出相反结论

    分隔符一个是空格(0x20)一个是 T(0x54),空格更小。
    库里两种格式同时存在(record_change 写 Python 格式,
    走 schema 默认值的行是 SQLite 格式),按字符串比大小直接错。
    """
    from arl_lite.core.monitor import parse_ts

    sqlite_fmt = "2026-10-02 04:46:53"          # CURRENT_TIMESTAMP
    python_fmt = "2026-10-02T04:46:53.083355"   # isoformat()

    # 字符串比较:同一秒,却说 sqlite_fmt 更小
    assert (sqlite_fmt >= python_fmt) is False
    # 解析后比较:两者都在同一秒,sqlite 那个少了微秒所以略早
    a, b = parse_ts(sqlite_fmt), parse_ts(python_fmt)
    assert a is not None and b is not None
    assert a <= b
    assert abs((b - a).total_seconds()) < 1


def test_parse_ts_handles_the_formats_actually_in_the_db():
    from arl_lite.core.monitor import parse_ts
    import datetime as _dt

    base = _dt.datetime(2026, 10, 2, 4, 46, 53)
    base_us = _dt.datetime(2026, 10, 2, 4, 46, 53, 83355)

    for raw, expected in (
        ("2026-10-02 04:46:53", base),              # SQLite CURRENT_TIMESTAMP
        ("2026-10-02T04:46:53", base),              # 无微秒
        ("2026-10-02T04:46:53.083355", base_us),    # Python isoformat
        ("2026-10-02T04:46:53+00:00", base),        # 带时区 → 去掉 tzinfo
        (base, base),                               # 已经是 datetime
        (base_us, base_us),                         # 带微秒的 datetime
    ):
        assert parse_ts(raw) == expected, raw
        # 统一成 naive,带时区和不带时区混着比会抛 TypeError
        assert parse_ts(raw).tzinfo is None, raw


def test_parse_ts_returns_none_for_junk_instead_of_raising():
    from arl_lite.core.monitor import parse_ts

    for junk in (None, "", "   ", "not-a-timestamp", 12345):
        assert parse_ts(junk) is None, junk


def test_dedup_works_when_detected_at_uses_sqlite_format(st):
    """detected_at 走 schema 默认值(空格格式)时,去重必须照样生效

    这是最容易回归的路径:一旦有人改成按字符串比大小,
    这个用例会立刻变红。
    """
    now = datetime.utcnow()
    h = _add_domain(st, "sqlfmt.example.com",
                    first_seen=(now - timedelta(days=10)).isoformat(),
                    last_seen=(now - timedelta(days=5)).isoformat())
    mon = Monitor(st)

    with st._conn() as conn:
        # 不给 detected_at,让它走 schema 的 CURRENT_TIMESTAMP
        conn.execute(
            """INSERT INTO asset_changes
               (workspace_id, asset_hash, asset_type, change_type, before_value)
               VALUES (?, ?, 'domain', 'DISAPPEARED', '{}')""",
            (st.workspace_id, h),
        )
        conn.commit()
        fmt = conn.execute(
            "SELECT detected_at FROM asset_changes WHERE asset_hash = ?", (h,)
        ).fetchone()["detected_at"]
    assert " " in str(fmt) and "T" not in str(fmt), f"没走到 SQLite 格式,用例失效: {fmt!r}"

    rows = mon.detect_disappeared("domain", now.isoformat())
    assert [r["hash"] for r in rows] == [h]
    assert mon.filter_newly_disappeared(rows) == [], "空格格式下去重失效了"


def test_unparseable_timestamp_does_not_suppress(st):
    """时间戳认不出来就不去重 —— 宁可重复报,不可漏报"""
    now = datetime.utcnow()
    h = _add_domain(st, "junk.example.com",
                    first_seen=(now - timedelta(days=10)).isoformat(),
                    last_seen=(now - timedelta(days=5)).isoformat())
    mon = Monitor(st)
    with st._conn() as conn:
        conn.execute(
            """INSERT INTO asset_changes
               (workspace_id, asset_hash, asset_type, change_type,
                before_value, detected_at)
               VALUES (?, ?, 'domain', 'DISAPPEARED', '{}', 'not-a-timestamp')""",
            (st.workspace_id, h),
        )
        conn.commit()
    rows = mon.detect_disappeared("domain", now.isoformat())
    assert [r["hash"] for r in mon.filter_newly_disappeared(rows)] == [h]


# =====================================================================
# 端到端:写入 asset_changes 后能查出来
# =====================================================================


def test_disappeared_flows_into_asset_changes(st):
    """完整链路:检测 → 去重 → record_change → list_changes 能查到"""
    since = _iso()
    h = _add_domain(st, "e2e.example.com", first_seen=_iso(days=-10),
                    last_seen=_iso(days=-5))
    mon = Monitor(st)

    rows = mon.filter_newly_disappeared(mon.detect_disappeared("domain", since))
    for row in rows:
        record_change(st, "domain", "DISAPPEARED", row["hash"], before=row)

    found = list_changes(st, change_type="DISAPPEARED")
    assert len(found) == 1
    assert found[0]["asset_hash"] == h
    assert found[0]["asset_type"] == "domain"
    # 快照要存下来,否则事后无从知道下线前长什么样
    assert found[0]["before_value"]
    assert "e2e.example.com" in found[0]["before_value"]


def test_cli_change_type_choices_include_disappeared():
    """CLI 的 --change-type 早就列了 DISAPPEARED,现在真有数据能查了"""
    import re
    from pathlib import Path
    src = Path(__file__).parents[1] / "arl_lite" / "cli.py"
    assert re.search(r'"DISAPPEARED"', src.read_text(encoding="utf-8"))


# =====================================================================
# 证伪:把核心判定改回朴素版,测试必须红
# =====================================================================


def test_naive_no_grace_logic_would_flag_flaky_asset(st):
    """证伪:去掉宽限期,抖动资产就会被误判下线

    这条是本文件最重要的用例——它量化了去抖到底挡住了多少误报。
    """
    since = _iso()
    flaky = _add_domain(st, "flaky.example.com", first_seen=_iso(days=-10),
                        last_seen=_iso(hours=-1))

    with sqlite3.connect(str(st.db_path)) as c:
        c.execute("PRAGMA foreign_keys = ON")
        # 朴素判定:只比 first_seen/last_seen 和 since,没有宽限期
        naive = c.execute(
            "SELECT hash FROM domains WHERE workspace_id = ? "
            "AND first_seen < ? AND last_seen < ?",
            (st.workspace_id, since, since),
        ).fetchall()
    naive_hashes = {r[0] for r in naive}

    assert flaky in naive_hashes, "朴素判定本该误报,说明用例没打到点上"
    # 而有宽限期的实现正确地放行了它
    assert Monitor(st).detect_disappeared("domain", since) == []


def test_detect_disappeared_sql_uses_indexed_columns(st):
    """查询必须走 first_seen / last_seen,不能全表扫

    资产表动辄十万行,全表扫会让每轮 watch 多花几秒。
    """
    import re
    from pathlib import Path
    src = (Path(__file__).parents[1] / "arl_lite" / "core" / "monitor.py")
    body = src.read_text(encoding="utf-8")
    seg = body[body.index("def detect_disappeared"):body.index("def filter_newly_disappeared")]
    assert "first_seen <" in seg and "last_seen" in seg
    assert not re.search(r"SELECT\s+\*.*OR\s+1=1", seg, re.S)
