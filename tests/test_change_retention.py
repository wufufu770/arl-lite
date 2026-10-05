"""r51:`asset_changes` 是一条只增不减、又够不着的单向管道

## 动手前量的三条事实

一,**没有清理**:`grep 'DELETE FROM asset_changes'` 在 `db/storage.py` 里
零命中(全项目只有 `correlations` 和 `workspaces` 有 DELETE)。
二,**够不着**:`list_changes` 是 `ORDER BY id DESC LIMIT ?`,`monitor changes`
的 `--limit` 默认 **50** —— 更早的变更在 CLI 上彻底看不到,而库里明明存着。
三,**没有时间维度**:`monitor changes` 没有 `--since`。

## 这不是性能问题,不能说成是

基线判据那条查询走 `idx_changes_ws_hash`,`EXPLAIN` 确认是 SEARCH 不是 SCAN。
问题是**够不着 + 删不掉**。拿一个不成立的理由去推动一个改动,比没有理由更坏。

## r51 动手时先撞到的一个真缺陷(不是猜的)

时间戳有**两种格式**:`record_change` 写 `datetime.utcnow().isoformat()`
(T 分隔),而列的 `DEFAULT CURRENT_TIMESTAMP` 是空格分隔。`parse_ts` 的文档
早就记着「空格排在 T 前面」,但当时没有任何时间范围查询用它。

实测:库里一行 `detected_at='2026-10-03 07:19:23'`,窗口起点
`'2026-10-03T00:00:00'`(今天零点):

    朴素字符串比较: 命中 0 行    ← 07:19 明明在零点之后
    REPLACE 归一后: 命中 1 行

`record_change` 总是显式写 T 格式,所以今天生产里碰不到。但**拿同一段比较
去做删除**,方向就反了:该删的行永远删不掉,表永远缩不下去。所以不能靠
「现在碰不到」。

## 写测试之前手动跑,抓到一个方向反了的 bug

第一版 `prune_changes` 用了 `_since_clause`(>=),而它要删的是**更旧**的
(<)。实测 `monitor prune --yes --older-than 30d` 删掉的是**最新**那条,
旧的原封不动 —— 命令不报错、退出码 0、行数确实少了,看起来完全成功。

所以两个方向必须是两个**名字**:`_since_clause`(取较新的)和
`_older_than_clause`(删较旧的)。用错方向要在 code review 里一眼能看出来,
而不是靠一个布尔参数。

## 本轮的三件事

一,`--since` 解析器从 `cmd_diff` 里**抽成一个函数**,三处共用
(`diff` / `changes --since` / `prune --older-than`)。复制第二份就是 r45
那条教训的重演。
二,`monitor changes --since`,比较走格式归一。
三,`monitor prune --older-than`,**默认只试算**,要真删得显式 `--yes`。
给删除命令加 `--dry-run` 标志等于默认就删,少打一个字母就没了。

## 最容易被忽略的一条:清理会改变基线判据

`is_baseline_noise` 读的就是这张表,而且**读全部历史**。删掉旧行等于把它的
输入截短 —— 很久以前抖过几次的资产,清理之后就不再被当成抖动。这大概率是
好事(那本来就是记着的局限),但它意味着**同一批数据在清理前后会得到不同
的答案**,「误报率实测」这类测量因此在某天悄悄失去可比性。所以 `prune` 的
输出必须把这件事说出来,而不是让用户自己发现。
"""
from __future__ import annotations

import contextlib
import io
from datetime import datetime, timedelta

import pytest

from arl_lite.cli import _parse_since, main
from arl_lite.core.monitor import (count_changes_older_than, list_changes,
                                   prune_changes, record_change)
from arl_lite.db.storage import Storage


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """独立工作区,HOME 在**函数体内**重定向

    `test_no_real_home_writes` 用 AST 找「哪些函数改过 HOME」,只认同一函数里
    的 monkeypatch.setenv;靠 fixture 间接重定向它看不出来,会把这里的
    `Storage(...)` 判成会写用户真实数据目录(r49 踩过一次)。
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    return Storage(workspace="t"), tmp_path


def _run(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = main(list(argv))
        except SystemExit as e:      # argparse 的用法错会直接退出
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


def _insert(st, detected_at: str, asset_hash="h1", workspace_id=None):
    """直接塞一行,时间戳格式由调用方决定

    `record_change` 总是显式写 T 格式,所以空格格式只能这样造 —— 而那正是
    列的 `DEFAULT CURRENT_TIMESTAMP` 会产生的形状。
    """
    with st._conn() as c:
        c.execute(
            "INSERT INTO asset_changes (workspace_id, asset_hash, asset_type,"
            " change_type, detected_at) VALUES (?,?,?,?,?)",
            (workspace_id if workspace_id is not None else st.workspace_id,
             asset_hash, "host", "ADDRESS_CHANGED", detected_at))


def _iso(days_ago: float) -> str:
    return (datetime.utcnow() - timedelta(days=days_ago)).isoformat()


def _count(st) -> int:
    with st._conn() as c:
        return c.execute("SELECT COUNT(*) FROM asset_changes").fetchone()[0]


# ── 一、主判据:prune 删的必须是**旧**的,不是新的 ──

def test_prune_deletes_the_old_rows_not_the_new_ones(ws):
    """方向:删「早于起点」的,留「晚于起点」的

    这条是 r51 第一版的真 bug:用了 `>=`,于是 `prune --yes` 删掉的是**最新**
    那条、旧的原封不动。命令不报错、退出码 0、行数确实少了 —— 看起来完全成功。
    """
    st, _ = ws
    _insert(st, _iso(90))            # 90 天前
    _insert(st, _iso(1))             # 昨天
    _insert(st, _iso(0))             # 今天
    r = prune_changes(st, _iso(30), dry_run=False)
    assert r["deleted"] == 1, f"该只删 90 天前那一条,实际 {r}"
    with st._conn() as c:
        left = sorted(r[0] for r in c.execute("SELECT detected_at "
                                              "FROM asset_changes"))
    assert len(left) == 2
    assert "2026-01" not in str(left), f"旧的还在:{left}"


def test_dry_run_is_the_default_and_deletes_nothing(ws):
    """默认**什么都不删**

    给删除命令加 `--dry-run` 标志等于默认就删,少打一个字母就没了。
    所以默认是 dry-run,真删必须显式 `dry_run=False`。
    """
    st, _ = ws
    _insert(st, _iso(90))
    _insert(st, _iso(0))
    r = prune_changes(st, _iso(30))
    assert r["dry_run"] is True
    assert r["deleted"] == 0
    assert r["matched"] == 1, "试算仍要报出会删几条"
    assert _count(st) == 2, "试算不许真的删"


def test_dry_run_count_matches_what_actually_gets_deleted(ws):
    """试算说的条数必须等于真删的条数 —— dry-run 骗人是最经典的一种"""
    st, _ = ws
    for d in (200, 100, 60, 2, 0):
        _insert(st, _iso(d))
    assert prune_changes(st, _iso(30))["matched"] == 3
    assert prune_changes(st, _iso(30), dry_run=False)["deleted"] == 3
    assert count_changes_older_than(st, _iso(30)) == 0, "删完不该还数得出来"


def test_prune_is_workspace_scoped(ws, tmp_path, monkeypatch):
    """删一个工作区的行,不能碰到另一个工作区的

    SQL 少一个 `workspace_id = ?` 就是跨工作区删数据,而那**不报错**。
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    a = Storage(workspace="a")
    b = Storage(workspace="b")
    _insert(a, _iso(90), asset_hash="a-old")
    _insert(b, _iso(90), asset_hash="b-old")
    _insert(a, _iso(0), asset_hash="a-new")
    r = prune_changes(a, _iso(30), dry_run=False)
    assert r["deleted"] == 1
    with b._conn() as c:
        n = c.execute("SELECT COUNT(*) FROM asset_changes").fetchone()[0]
    assert n == 1, f"工作区 b 被误删了(剩 {n} 行)"


def test_prune_carries_a_workspace_condition_structurally(ws):
    """`DELETE` 必须带 `workspace_id` 条件 —— 这条用 AST 钉,不靠行为

    ## 为什么行为测试区分不了(r51 变异测试实测出来的)

    上面那条 `test_prune_is_workspace_scoped` 在**当前布局下**抓不住
    「漏掉 `workspace_id = ?`」这个变异:每个工作区是**独立的 db 文件**
    (`<root>/<name>/data.db`),两个工作区的数据压根不在一个库里,
    所以去掉那个条件在行为上完全等价。变异 M7 实测存活。

    和 r48 查实的「hash 里的 `workspace_id` 恒等于 1」是同一件事:这个
    条件今天几乎是空转的。但它**写对**的成本是一个 `AND`,而它空转的成本
    是「哪天改成单库多工作区,清理会静默跨工作区删数据」。

    行为测不出来的东西就别假装测得出来 —— 用 AST 把这个意图钉住,并把
    「当前布局让它空转」这件事写清楚,免得下一个人以为这条行为测试很有效。
    """
    import ast
    import pathlib
    tree = ast.parse(
        (pathlib.Path(__file__).resolve().parents[1]
         / "arl_lite" / "core" / "monitor.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "prune_changes")
    deletes = []
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"):
            sql = node.args[0]
            if isinstance(sql, ast.JoinedStr):
                text = "".join(v.value for v in sql.values
                               if isinstance(v, ast.Constant))
                if text.strip().upper().startswith("DELETE"):
                    deletes.append((text, node))
    assert deletes, "prune_changes 里找不到 DELETE —— 判据本身坏了"
    for text, _node in deletes:
        assert "workspace_id = ?" in text, (
            f"DELETE 少了 workspace_id 条件:{text!r}。"
            f"当前布局下行为测试区分不出来(每个工作区一个 db 文件),"
            f"但它写对只有一个 AND 的成本")


# ── 二、时间戳两种格式必须都能命中 ──

def test_space_separated_rows_are_found_within_the_same_day(ws):
    """空格分隔的时间戳,当天零点起的窗口也要能查到

    实测:行是 `'2026-10-03 07:19:23'`,窗口起点 `'2026-10-03T00:00:00'`
    (今天零点)—— 朴素字符串比较命中 0 行,因为空格(0x20)排在 T(0x54)前面。
    """
    st, _ = ws
    today = datetime.utcnow()
    space_fmt = today.replace(microsecond=0).isoformat().replace("T", " ")
    _insert(st, space_fmt)
    midnight = today.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    got = list_changes(st, since=midnight)
    assert len(got) == 1, (
        f"空格格式的行在当天窗口里查不到 —— 时间比较用了裸字符串。"
        f"行={space_fmt!r} 起点={midnight!r}")


def test_space_separated_rows_are_prunable(ws):
    """同一个洞在删除方向上就是「该删的永远删不掉」

    拿同一段比较去做删除,漏掉的行就是表永远缩不下去的那部分。
    """
    st, _ = ws
    old = _iso(90)
    _insert(st, old)                          # T 格式
    _insert(st, old.replace("T", " "))        # 空格格式
    _insert(st, _iso(0))                      # 今天,不该删
    r = prune_changes(st, _iso(30), dry_run=False)
    assert r["deleted"] == 2, (
        f"两种格式都该删,只删了 {r['deleted']} 条 —— "
        f"空格格式的行永远删不掉")


# ── 三、`--since` 给用户一个真的时间窗 ──

def test_since_window_excludes_older_rows(ws):
    st, _ = ws
    _insert(st, _iso(90), asset_hash="old")
    _insert(st, _iso(0), asset_hash="new")
    got = list_changes(st, since=_iso(30))
    assert [r["asset_hash"] for r in got] == ["new"]


def test_since_bad_value_is_an_error_not_an_empty_result(ws):
    """坏值要报错,不是「查不到」

    静默返回空的话,用户会以为「这段时间没有变更」——而真相是他写错了参数。
    """
    st, _ = ws
    _insert(st, _iso(0))
    rc, out, err = _run("monitor", "changes", "-w", "t", "--since", "garbage")
    assert rc == 2, f"该报错:\n{out}\n{err}"
    assert "--since" in err
    for bad in ("", "7x", "abc"):
        rc, _o, err = _run("monitor", "changes", "-w", "t", "--since", bad)
        assert rc == 2, f"--since {bad!r} 该被拒绝"


# ── 四、`--since` 的解析只有一份 ──
# r50 那条:抽出 helper 之前,`cmd_diff` 里是内联的一份。复制第二份就是
# r45 的教训(同一张表两个来源迟早漂)。所以三处共用一个函数。

@pytest.mark.parametrize("value", ["7d", "24h", "0d", "0h", "2026-09-01",
                                    "2026-09-01T10:00:00"])
def test_parse_since_accepts_documented_forms(value):
    """文档里写了的写法都认"""
    got = _parse_since(value)
    assert isinstance(got, str) and got, f"{value!r} → {got!r}"


def test_parse_since_relative_forms_move_the_window():
    """`Nd` / `Nh` 真的把窗口往前挪,不是原样返回"""
    from arl_lite.core.monitor import parse_ts
    d7 = parse_ts(_parse_since("7d"))
    d1 = parse_ts(_parse_since("1d"))
    h1 = parse_ts(_parse_since("24h"))
    assert d1 > d7, "1d 的窗口起点比 7d 晚"
    assert h1 > d1, "24h 的窗口起点比 1d 晚"


@pytest.mark.parametrize("bad", ["", "   ", "7x", "garbage", "d", "h",
                                 "1w", "2026-13-99", None, 7])
def test_parse_since_refuses_junk(bad):
    """能 parse 就当时间那种写法会静默收下垃圾值,这里一律拒"""
    with pytest.raises(ValueError):
        _parse_since(bad)


def test_parse_since_rejects_negative_windows():
    """`-3d` 不是「三天前」而是「三天后」,那是反的"""
    with pytest.raises(ValueError, match=">= 0"):
        _parse_since("-3d")


# ── 五、prune 必须说出「基线判据会变」 ──
# 这是本轮最容易被忽略的一条:`is_baseline_noise` 读的就是这张表的全部历史。
# 清理一旦有了,它就会开始被清理影响 —— 同一批数据在清理前后答案不同,
# 「误报率实测」这类测量会悄悄失去可比性。

def test_prune_output_says_the_baseline_verdict_will_change(ws):
    """真要删的时候,输出必须点明基线判据的输入被截短了"""
    st, _ = ws
    _insert(st, _iso(90))
    _insert(st, _iso(0))
    rc, out, err = _run("monitor", "prune", "-w", "t",
                        "--older-than", "30d", "--yes")
    assert rc == 0, err
    assert "基线判据" in out, (
        f"删了行却没说基线判据会变 —— 用户无从知道自己的测量变不可比了:\n{out}")


def test_prune_dry_run_also_says_it(ws):
    """试算也要说 —— 否则用户看完试算以为无副作用,加了 --yes 就中招"""
    st, _ = ws
    _insert(st, _iso(90))
    rc, out, err = _run("monitor", "prune", "-w", "t", "--older-than", "30d")
    assert rc == 0, err
    assert "试算" in out and "--yes" in out, out
    assert "基线判据" in out, f"试算没说副作用:\n{out}"


def test_prune_with_nothing_to_delete_says_nothing_happened(ws):
    """没有可删的行时,不该报「基线判据会变」

    那会让每次例行清理都吵一次,久了就没人看了。
    """
    st, _ = ws
    _insert(st, _iso(0))
    rc, out, err = _run("monitor", "prune", "-w", "t", "--older-than", "30d")
    assert rc == 0, err
    assert "0 条" in out, out
    assert "基线判据" not in out, f"没删任何行却报了基线警告:\n{out}"


# ── 六、CLI 层的行为 ──

def test_cli_prune_without_yes_keeps_the_rows(ws):
    """不加 `--yes` 一行都不能少"""
    st, _ = ws
    _insert(st, _iso(90))
    _insert(st, _iso(0))
    _run("monitor", "prune", "-w", "t", "--older-than", "30d")
    assert _count(st) == 2


def test_cli_prune_requires_an_explicit_window(ws):
    """不给窗口就该被 argparse 挡下

    没有默认窗口的删除命令很重要:「清理 90 天前的」必须由人说出来,
    而不是工具替他决定保留多久。
    """
    rc, out, err = _run("monitor", "prune", "-w", "t")
    assert rc != 0
    assert "--older-than" in (out + err)


def test_cli_prune_bad_window_is_an_error(ws):
    st, _ = ws
    rc, out, err = _run("monitor", "prune", "-w", "t", "--older-than", "wat")
    assert rc == 2, f"{out}{err}"
    assert _count(st) == 0, "窗口坏了却还是动了数据"


def test_cli_changes_since_composes_with_asset_filter(ws):
    """`--since` 和 `--asset` 要能一起用 —— 两者都是过滤,不是互斥的"""
    st, _ = ws
    st.add_host(1, "web.example.com", ip="1.1.1.1")
    st.add_host(2, "api.example.com", ip="3.3.3.3")
    with st._conn() as c:
        h = {r["host"]: r["hash"] for r in c.execute("SELECT host, hash FROM hosts")}
    record_change(st, "host", "ADDRESS_CHANGED", h["web.example.com"],
                  before={"ip": "1.1.1.1"}, after={"ip": "2.2.2.2"})
    _insert(st, _iso(0), asset_hash=h["api.example.com"])
    _insert(st, _iso(90), asset_hash=h["api.example.com"])

    out = _run("monitor", "changes", "-w", "t", "--since", "30d",
               "--type", "host", "--asset", "api.example.com")[1]
    assert "api.example.com" in out
    assert "1 change(s)" in out, f"90 天前那条不该在 30 天窗口里:\n{out}"
    assert "web.example.com" not in out, f"--asset 没生效:\n{out}"
