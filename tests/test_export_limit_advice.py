"""r59:export 的提示推荐了一条走不通的路 —— 照做就崩

## 实测的退化路径(不是推测)

r57 的「数据不完整」警告里写着「或调高 db.storage.EXPORT_ROW_CAP」。
r58 抓到同一类错误的另一个形态(提示推荐了 `correlate` 没有的 `--json`),
所以这轮回头查上一轮自己写的东西:

    EXPORT_ROW_CAP 改成 20000
    $ arl-lite export --format json -o o.json
    ValueError: limit must be int 0..10000, got 20000

**建议是死路。** 用户照做,撞上一个和真正原因毫无关系的崩溃 ——
比不给建议更坏,因为他会以为「export 坏了」,而实际上要改的是他手上
那个他刚被建议去改的常量。

## 根因:两个 10000 耦合但不同源

`Storage.query` 有一个单次行数护栏(硬编码 10000),`EXPORT_ROW_CAP`
另有一个(也硬编码 10000)。两者都写 10000 只是**巧合**,没有任何代码
保证它们一致 —— 所以「把 A 调大去迁就 B」必然撞死。

## 修法不是删掉建议,是把路打通

删掉建议最省事,但「一次导全」是真实需求(12000 行的库分 2 次导,
用户还得自己拼接),删了等于把这个需求推给用户的手工劳动。

护栏本身是合理的 —— 它防的是「误传一个巨大 limit 把整张表 list 化」。
所以不拆护栏,而是让 `fetch_all` **分页**取:每页都在护栏内,页数不限,
内存占用是单页量级而不是全表量级。`query` 的 `ORDER BY id DESC` 保证
分页结果保持原顺序。

于是那句提示不再是空头支票:

    EXPORT_ROW_CAP=20000 时 ->  导出 12000 条, 退出码 0, 零警告

## 而护栏还在

`query(limit=20000)` 依然要拒 —— 不能为了 export 方便就把公共护栏拆了。
判据钉住这一点:修的是 `fetch_all` 的取法,不是护栏本身。
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import pathlib

import pytest

import arl_lite.db.storage as storage_mod
from arl_lite.cli import _EXPORT_TABLES, main
from arl_lite.db.storage import EXPORT_ROW_CAP, Storage


def _h(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:32]


def _bulk(storage, n, prefix="d"):
    with storage._conn() as c:
        c.executemany(
            "INSERT INTO domains (workspace_id, domain, source, hash,"
            " first_seen, last_seen, discovered_at) "
            "VALUES (?,?,?,?,datetime('now'),datetime('now'),datetime('now'))",
            [(storage.workspace_id, f"{prefix}{i}.x.com", "s", _h(f"x|{prefix}{i}"))
             for i in range(n)])


def _export(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(argv)
    return rc, out.getvalue(), err.getvalue()


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return Storage(workspace="default"), tmp_path


@pytest.fixture
def restore_cap():
    """每个测试都把 `EXPORT_ROW_CAP` 还原 —— 它是模块级常量,改了会漏到别的测试"""
    old = storage_mod.EXPORT_ROW_CAP
    yield
    storage_mod.EXPORT_ROW_CAP = old


# ── 一、主判据:提示里那条路真能走 ──

def test_raising_the_cap_actually_lets_you_export_everything(ws, tmp_path,
                                                            restore_cap):
    """把 EXPORT_ROW_CAP 调到够大 -> export 真的全导,退出 0,零警告

    这条是本轮的核心:它**照着提示做一遍**,而不是查两个常量的大小关系。
    上一轮的错误正是「看起来该成立、实际一执行就死」。
    """
    st, _ = ws
    _bulk(st, EXPORT_ROW_CAP + 2000)

    storage_mod.EXPORT_ROW_CAP = EXPORT_ROW_CAP + 2000
    dest = tmp_path / "o.json"
    rc, out, err = _export(["export", "--format", "json", "-o", str(dest)])

    data = json.loads(dest.read_text(encoding="utf-8"))
    assert len(data["domains"]) == EXPORT_ROW_CAP + 2000, (
        f"调大 cap 后还是只导出了 {len(data['domains'])} 条")
    assert rc == 0, f"全导了却退出 {rc}"
    assert "不完整" not in (out + err), f"全导了还在说不完整:\n{out}{err}"


def test_raising_the_cap_does_not_crash_the_export(ws, tmp_path, restore_cap):
    """调高 cap **不许抛异常** —— r59 之前这里就是那个 ValueError

    这条单独钉住「不崩」,不和条数混在一起:崩了和条数不对是两种故障,
    而上一轮的文案把人引到的正是「崩」。
    """
    st, _ = ws
    _bulk(st, 100)
    storage_mod.EXPORT_ROW_CAP = EXPORT_ROW_CAP * 5
    rc, out, err = _export(["export", "--format", "json",
                            "-o", str(tmp_path / "o.json")])
    assert rc == 0, f"调高 cap 之后 export 失败了(退出 {rc}):\n{err}"


# ── 二、分页取不许丢行、重复行、乱序 ──

def test_paging_keeps_every_row_exactly_once_in_order(ws, restore_cap):
    """跨三页取:行数对、无重复、顺序保持

    分页是 r59 新引入的机制,而「漏一行 / 重复一行 / 顺序乱了」正是
    分页最典型的三个错。`query` 是 `ORDER BY id DESC`,分页拼起来必须
    还是同一个顺序。
    """
    st, _ = ws
    total = EXPORT_ROW_CAP * 2 + 500          # 明确跨三页
    _bulk(st, total)
    storage_mod.EXPORT_ROW_CAP = total

    rows, counted = st.fetch_all("domains")
    assert counted == total, f"count_rows 说 {counted},造了 {total}"
    assert len(rows) == total, f"分页取到 {len(rows)} 行,应该 {total} 行"

    ids = [r["id"] for r in rows]
    assert len(set(ids)) == len(ids), (
        f"有重复行:{len(ids) - len(set(ids))} 条 —— 分页边界重复取了一次")
    assert ids == sorted(ids, reverse=True), "顺序乱了,不是 id DESC"
    assert [r["domain"] for r in rows] == [
        d for d, _ in sorted(((r["domain"], r["id"]) for r in rows),
                             key=lambda t: -t[1])], "取回的行和 id DESC 的顺序对不上"


def test_paging_is_a_no_op_when_cap_exceeds_the_table(ws, restore_cap):
    """cap 比表还大时取回全部,不多不少"""
    st, _ = ws
    _bulk(st, 7)
    storage_mod.EXPORT_ROW_CAP = EXPORT_ROW_CAP
    rows, total = st.fetch_all("domains")
    assert total == 7 and len(rows) == 7


def test_paging_stops_when_the_table_is_emptied_midway(ws, restore_cap,
                                                       monkeypatch):
    """分页途中表被清空 -> 安静地少拿,不崩

    真实场景:另一个进程在导出时清理了数据。这里 `total` 是在分页**之前**
    取的,所以它报的是当时的真实条数 —— 用户看到「库里 N 条,只导了 M 条」
    仍然是实话,只是 M 比 N 小。
    """
    st, _ = ws
    _bulk(st, EXPORT_ROW_CAP + 100)
    real_query = st.query
    calls = {"n": 0}

    def _query(table, **kw):
        calls["n"] += 1
        if calls["n"] >= 2:            # 第一页取完之后就清库
            with st._conn() as c:
                c.execute("DELETE FROM domains")
        return real_query(table, **kw)

    monkeypatch.setattr(st, "query", _query)
    storage_mod.EXPORT_ROW_CAP = EXPORT_ROW_CAP * 2
    rows, total = st.fetch_all("domains")
    assert total == EXPORT_ROW_CAP + 100, "total 应该还是分页前那个数"
    assert len(rows) == EXPORT_ROW_CAP, f"只该拿到第一页,实际 {len(rows)}"


# ── 三、护栏本身不许为了 export 方便被拆掉 ──

def test_the_query_page_guard_still_refuses_an_oversized_limit(ws):
    """`query(limit=20000)` 依然要拒

    修的是 `fetch_all` 的**取法**(分页),不是护栏本身。护栏防的是
    「误传巨大 limit 把整表 list 化」,拆了它就是拿别的场景的安全换
    一个场景的方便。
    """
    st, _ = ws
    with pytest.raises(ValueError, match="limit must be"):
        st.query("domains", limit=storage_mod._QUERY_PAGE_LIMIT + 1)


@pytest.mark.parametrize("bad", [-1, -100, -(10 ** 6)])
def test_negative_offset_is_refused(ws, bad):
    """负 offset 必须报错,不能静默给错数据

    r59 变异测试抓出来的洞(M8 存活):`query` 加了 `offset` 参数却**没人
    校验它**。SQLite 里 `LIMIT n OFFSET -5` 不报错,它把负数当成 0 ——
    于是调用方以为自己跳过了几行,实际从头开始拿,拿到的是**错的**结果
    而不是**少**的结果。静默给错数据比报错糟得多。
    """
    st, _ = ws
    _bulk(st, 30)
    with pytest.raises(ValueError, match="offset"):
        st.query("domains", limit=10, offset=bad)


def test_offset_past_the_end_returns_empty_not_an_error(ws):
    """offset 超出末尾 -> 空列表,不是异常

    分页循环靠这个停下来(`if not page: break`)。它要是抛异常,导出
    一个并发被删空的表就会整个崩掉。
    """
    st, _ = ws
    _bulk(st, 5)
    assert st.query("domains", limit=10, offset=5) == []
    assert st.query("domains", limit=10, offset=9999) == []


def test_the_page_limit_is_one_named_constant_not_two_literals():
    """护栏只能有一个来源

    r59 的根因就是两个 10000 各写一遍、耦合却不同源。判据用 AST 钉住
    `query` 里的上限校验引的是常量,不是字面量 —— 有人再抄一个数字进去
    就红。
    """
    import ast
    src = pathlib.Path("arl_lite/db/storage.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "query")
    magic = [n.value for n in ast.walk(fn)
             if isinstance(n, ast.Constant) and isinstance(n.value, int)
             and n.value >= 1000]
    assert not magic, (
        f"`query` 里又出现了魔法数字:{magic} —— 上限只能来自 "
        f"_QUERY_PAGE_LIMIT,否则它和 EXPORT_ROW_CAP 又是各写一份")


def test_export_row_cap_does_not_silently_bypass_the_guard(ws, restore_cap):
    """`EXPORT_ROW_CAP` 默认必须 <= 护栏 —— 否则默认路径就撞死

    分页让「调大它」变合法了,但**默认值**仍不该超过护栏:超了就等于
    每张表都至少翻两页,而收益为零(护栏是内存上限,不是行数上限)。
    """
    assert EXPORT_ROW_CAP <= storage_mod._QUERY_PAGE_LIMIT, (
        f"EXPORT_ROW_CAP={EXPORT_ROW_CAP} 超过护栏 "
        f"{storage_mod._QUERY_PAGE_LIMIT} —— 默认路径每张表都要翻页,"
        f"而那换不来任何东西")


# ── 四、文案不许指向死路 ──

def test_the_advice_in_the_warning_actually_works(ws, tmp_path, restore_cap):
    """警告里提到的表名是真表,不是占位符

    r57 那句写的是 `arl-lite query <table> --limit 10000` —— `<table>`
    是**占位符**,照抄会报错。r59 改成了真实表名(取第一张超限的表),
    这条钉住:提示里的命令,用户复制过去能直接跑。
    """
    st, _ = ws
    _bulk(st, EXPORT_ROW_CAP + 10)
    rc, out, err = _export(["export", "--format", "json",
                            "-o", str(tmp_path / "o.json")])
    advice = [l for l in (out + err).splitlines() if "分表分批" in l]
    assert advice, f"警告里没有拿全量的建议:\n{err}"
    line = advice[0]
    assert "<table>" not in line and "<" not in line, (
        f"建议里还留着占位符,照抄会报错:\n{line}")
    named = [t for t in _EXPORT_TABLES if t in line]
    assert named, f"建议里没指名任何一张真表:\n{line}"
    # 而且那真的是超限的那张
    assert "domains" in named, f"建议指名的不是实际超限的表:{named}"


def test_the_raised_cap_claim_is_not_a_lie(ws, tmp_path, restore_cap):
    """提示里说「调高 EXPORT_ROW_CAP」的同时,那条路必须真能走

    这条把「文案」和「实现」绑在一起验:文案承诺了可执行性,实现就
    得兑现。前几轮的同类错(`--json` 泄漏、死路 cap)都是这两者脱节。
    """
    st, _ = ws
    _bulk(st, EXPORT_ROW_CAP + 10)
    rc, out, err = _export(["export", "--format", "json",
                            "-o", str(tmp_path / "o.json")])
    assert "EXPORT_ROW_CAP" in (out + err), "提示里没提这条路,那本条判据不适用"
    # 照做
    storage_mod.EXPORT_ROW_CAP = EXPORT_ROW_CAP + 10
    rc2, _, err2 = _export(["export", "--format", "json",
                            "-o", str(tmp_path / "o2.json")])
    assert rc2 == 0 and "Traceback" not in err2 and "ValueError" not in err2, (
        f"照着提示做完却失败了 —— 这条建议是死路:\n{err2}")
