"""r50:变更风暴时被 200 条上限静默截断

## 实测的退化路径(不是推测)

造 250 个新 host 落在一个窗口里:

    detect_changes 返回: 250 条
    watcher 实际会记:   200 条
    asset_changes 里:   200 条 NEW_ASSET
    被静默丢掉:          50 条
    watcher 侧产生的日志: 一条都没提到截断

## 丢掉的是**永久**的(实测)

第 1 轮只记 200 条之后,把 `since_iso` 换成第 1 轮结束之后(模拟第 2 轮):

    第 2 轮再检出(补偿?): 0

因为 `detect_changes` 按 `first_seen >= since_iso` 取,而下一轮的
`since_iso` 是本轮开始之后 —— 那些资产的 `first_seen` 已经早于窗口起点。
所以这不是「显示不全」,是**变更记录本身缺了**,而且永远补不上。
丢掉的资产下一轮不再算新增,而 r40/r41 的基线判据正好读这张表。

## 为什么「均匀抽样」不是改进(所以本轮不做)

上面那条查询**没有 `ORDER BY`**,SQLite 按什么顺序返回不保证 ——
所以「留前 200 条」本来就已经是随机留。抽样只是换一批丢,
永久丢失率一模一样。既然丢是必然的,唯一诚实的做法是**让丢这件事可见**。

## 所以本轮只做两件事

一,截断要**自报**:每处截断打 warning(检出多少/记了多少/丢多少),
并且丢的条数**落到能查的地方** —— `WatchTarget.dropped_change_count`
累计 + `arl-lite watch list` 把它显示出来。只进日志不够:日志会被翻过去。

二,汇总日志里把「检出」和「记入」分成两个数。原来那行 `new=250` 数的是
`_count_assets` 的差(资产表里多出来的行),和「记了多少条变更」不是一回事,
两个数字并排出现却没人解释差在哪。
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import logging
import pathlib
import time
from datetime import datetime, timedelta

import pytest

from arl_lite.cli import main
from arl_lite.core.monitor import Monitor, record_change
from arl_lite.core.watcher import (CHANGE_RECORD_CAP, WatchTarget, Watcher,
                                   account_change_recording,
                                   record_change_capped)
from arl_lite.db.storage import Storage

REPO = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return Storage(workspace="t"), tmp_path


def _rows(storage, table="hosts"):
    with storage._conn() as c:
        return [dict(r) for r in c.execute(f"SELECT * FROM {table}")]


def _new_changes(storage, change_type="NEW_ASSET"):
    with storage._conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM asset_changes WHERE change_type = ?",
            (change_type,)).fetchone()[0]


def _seed_hosts(storage, n):
    for i in range(n):
        storage.add_host(1, f"h{i:03d}.example.com", ip="1.1.1.1")


def _since_before_seeding():
    return (datetime.utcnow() - timedelta(minutes=10)).isoformat()


# ── 一、主判据:截断必须被说出来 ──

def test_capped_helper_reports_how_many_were_dropped(ws):
    """返回值是「丢了几条」,不是「写了几条」"""
    st, _ = ws
    _seed_hosts(st, 5)
    rows = Monitor(st).detect_changes("host", _since_before_seeding())
    assert len(rows) == 5
    assert record_change_capped(st, "host", "NEW_ASSET", rows, cap=2) == 3
    assert _new_changes(st) == 2


def test_no_truncation_reports_zero(ws):
    """没触顶时返回值是 0 —— 调用方不能靠「有没有 warning」判断"""
    st, _ = ws
    _seed_hosts(st, 3)
    rows = Monitor(st).detect_changes("host", _since_before_seeding())
    assert record_change_capped(st, "host", "NEW_ASSET", rows, cap=10) == 0
    assert _new_changes(st) == 3


def test_cap_bigger_than_batch_is_not_a_negative_number(ws):
    """`cap > len(rows)` 时返回值是 0,不是负数

    调用方如果自己写 `len(rows) - cap`,这里会得到 -7,而负数在日志里
    看着像「多记了几条」。这条钉住「缺口由 helper 算,不由调用方算」。
    """
    st, _ = ws
    _seed_hosts(st, 3)
    rows = Monitor(st).detect_changes("host", _since_before_seeding())
    dropped = record_change_capped(st, "host", "NEW_ASSET", rows, cap=1000)
    assert dropped == 0, f"不该是负数:{dropped}"
    assert dropped >= 0


def test_default_cap_is_the_documented_constant(ws):
    """不传 cap 时用 `CHANGE_RECORD_CAP`,而它就是那个 200"""
    st, _ = ws
    _seed_hosts(st, CHANGE_RECORD_CAP + 5)
    rows = Monitor(st).detect_changes("host", _since_before_seeding())
    assert len(rows) == CHANGE_RECORD_CAP + 5
    assert record_change_capped(st, "host", "NEW_ASSET", rows) == 5
    assert _new_changes(st) == CHANGE_RECORD_CAP


def test_cap_zero_records_nothing_and_says_so(ws):
    """`cap=0` 是合法输入(等于「这轮不记」),不是异常"""
    st, _ = ws
    _seed_hosts(st, 3)
    rows = Monitor(st).detect_changes("host", _since_before_seeding())
    assert record_change_capped(st, "host", "NEW_ASSET", rows, cap=0) == 3
    assert _new_changes(st) == 0


@pytest.mark.parametrize("bad", [-1, -100])
def test_negative_cap_is_refused(ws, bad):
    """负数 cap 会让 `rows[:-1]` 悄悄砍掉**最后一条**,那是最容易漏的那条"""
    st, _ = ws
    with pytest.raises(ValueError, match="cap"):
        record_change_capped(st, "host", "NEW_ASSET", [], cap=bad)


def test_bad_snapshot_key_is_refused(ws):
    """`snapshot` 只能是 after/before —— 写错会记出单边快照语义相反的记录"""
    st, _ = ws
    with pytest.raises(ValueError, match="snapshot"):
        record_change_capped(st, "host", "NEW_ASSET", [], snapshot="both")


# ── 二、丢掉的是永久的:这条决定了方案 ──

def test_dropped_assets_are_never_reported_in_later_runs(ws):
    """第 1 轮没记上的,第 2 轮**不会**补上

    这是本轮最重要的一条实测:r50 之前那版 `[:200]` 切片丢掉的资产,
    下一轮 `first_seen` 已经早于窗口起点,再也不会被检出。所以
    「下轮会补」是错的,截断必须自己留痕。
    """
    st, _ = ws
    _seed_hosts(st, CHANGE_RECORD_CAP + 10)
    run1 = Monitor(st).detect_changes("host", _since_before_seeding())
    assert len(run1) == CHANGE_RECORD_CAP + 10
    record_change_capped(st, "host", "NEW_ASSET", run1)

    time.sleep(0.01)
    run2_start = datetime.utcnow().isoformat()      # 第 2 轮的 since_iso
    time.sleep(0.01)
    run2 = Monitor(st).detect_changes("host", run2_start)
    assert run2 == [], (
        "第 2 轮又检出这些资产了 —— 那本轮的实现不是按 first_seen 开窗口,"
        "r50 记录的前提不成立")
    assert _new_changes(st) == CHANGE_RECORD_CAP, (
        "补上了的话,r50 的前提(丢掉是永久的)就是错的")


# ── 三、留痕要落在能查的地方,不只在日志 ──

def test_watch_target_carries_a_cumulative_drop_counter():
    """`WatchTarget` 有累计丢弃数,并且能存能读回来"""
    wt = WatchTarget("example.com")
    assert wt.dropped_change_count == 0
    wt.dropped_change_count += 7
    wt.last_dropped_change_count = 7
    back = WatchTarget.from_dict(wt.to_dict())
    assert back.dropped_change_count == 7
    assert back.last_dropped_change_count == 7
    assert "dropped_change_count" in wt.to_dict()


# ── 三之二、累加那一段必须被测到 ──
# r50 第一版把这两行内联在 `_run_target` 里,变异测试实测:把
# `wt.dropped_change_count += dropped_this_run` 改成 `pass`,19 条测试**全绿**。
# helper 层测得挺好,而「丢多少条真的被记下来了」这段**接线完全没人验**。
# 所以它被抽成了 `account_change_recording`,接线还在同一个地方,但能测了。

def test_accounting_accumulates_across_runs():
    """连续三轮各丢一批,累计数要真的累加 —— 且和 helper 的返回值一致"""
    wt = WatchTarget("example.com")
    assert account_change_recording(wt, 250, 200) == 50
    assert wt.dropped_change_count == 50
    assert wt.last_dropped_change_count == 50
    assert account_change_recording(wt, 40, 200) == 0, "没截断时不该累加"
    assert wt.dropped_change_count == 50, "没截断的一轮把累计清掉了"
    assert account_change_recording(wt, 300, 200) == 100
    assert wt.dropped_change_count == 150
    assert wt.last_dropped_change_count == 100, "「最近一轮」是本轮的数,不是累计"


def test_accounting_does_not_report_a_negative_drop():
    """`detected < recorded` 说明两个数被算错了,不能报成负的丢弃数

    报负数会让人以为上界算出了负条数 —— 把一个 bug 伪装成另一个 bug
    的证据。夹到 0 至少不撒谎。
    """
    wt = WatchTarget("example.com")
    assert account_change_recording(wt, 10, 25) == 0
    assert wt.dropped_change_count == 0


def test_accounting_is_actually_called_from_run_target():
    """`_run_target` 必须真的调它 —— 抽成函数不等于接线还在

    这和 r42 的 `CHANGE_TYPES` 是同一个陷阱:定义在、helper 测得挺好、
    但生产里零引用。所以判据用 AST 数调用点。
    """
    tree = ast.parse((REPO / "arl_lite" / "core" / "watcher.py")
                     .read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_run_target")
    assert any(isinstance(n, ast.Call)
               and isinstance(n.func, ast.Name)
               and n.func.id == "account_change_recording"
               for n in ast.walk(fn)), (
        "_run_target 里没有 account_change_recording 调用 —— "
        "丢多少条就又没人记了")


def test_old_state_file_without_the_counter_still_loads():
    """老状态文件没有这两个字段(r50 才加的)—— 升级不该因此起不来"""
    old = {"target": "example.com", "modules": ["dns"],
           "interval_seconds": 3600, "last_run": "2026-01-01T00:00:00",
           "last_count": 5, "run_count": 2, "new_count": 3}
    wt = WatchTarget.from_dict(old)
    assert wt.dropped_change_count == 0 and wt.last_dropped_change_count == 0
    # 升级之后必须还能累加,不然第一次截断就崩
    wt.dropped_change_count += 1
    assert wt.dropped_change_count == 1


@pytest.mark.parametrize("weird", [None, "3", [1], True])
def test_garbage_counter_does_not_break_startup(weird):
    """状态文件里存成 null/字符串/列表都不该让 watcher 起不来

    旧状态是人手改过的、也有别的工具写过。`+= 1` 撞上非数字会抛
    TypeError,而那会让整个 watch 永久瘫痪 —— 一个计数器字段而已。
    """
    d = {"target": "example.com",
         "dropped_change_count": weird, "last_dropped_change_count": weird}
    wt = WatchTarget.from_dict(d)
    assert wt.dropped_change_count == 0
    wt.dropped_change_count += 1
    assert wt.dropped_change_count == 1


def test_watch_list_shows_the_dropped_count(tmp_path, monkeypatch, capsys):
    """`arl-lite watch list` 要把丢弃数显示出来

    只进日志是不够的:日志会被翻过去,而用户下次还会看的是这条命令。
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    # r70 起 watch 清单按工作区存:~/.arl-lite/watch/<workspace>/watch.json。
    # 这里造 default 工作区的那份。改的只是**造数据的位置**,
    # 断言(它要守的「丢弃数必须显示出来」)一个字没动 ——
    # 前提是断言的判据不能因为实现变了就跟着放松(r 的规矩:判错的前提要改判据,
    # 但这里前提没变,只是数据落地位置改了)。
    state_dir = tmp_path / ".arl-lite" / "watch" / "default"
    state_dir.mkdir(parents=True)
    (state_dir / "watch.json").write_text(json.dumps([
        {"target": "example.com", "modules": ["dns"], "interval_seconds": 3600,
         "dropped_change_count": 50, "last_dropped_change_count": 50},
        {"target": "quiet.example.com", "modules": ["dns"],
         "interval_seconds": 3600, "dropped_change_count": 0},
    ]), encoding="utf-8")
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        main(["watch", "list"])
    out = buf.getvalue()
    assert "50" in out, f"丢弃数没显示出来:\n{out}"
    assert "不会补上" in out or "不完整" in out, (
        f"只给个数字不说它意味着什么,用户不知道该不该管:\n{out}")
    # 没丢过的 target 不该被印一行警告
    assert out.count("[!]") == 1, f"不该给没截断的 target 也印警告:\n{out}"


# ── 四、代码里不许再有裸切片 ──
# `new_rows[:200]` 那种写法正是问题本身:上限硬编码、丢多少没人知道。
# 判据用 AST,不是文本子串。

def test_no_bare_slicing_of_detected_rows_in_watcher():
    """`_run_target` 里不该再出现对 `new_rows` / `gone` 的裸切片"""
    tree = ast.parse((REPO / "arl_lite" / "core" / "watcher.py")
                     .read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_run_target")
    bare = []
    for node in ast.walk(fn):
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                and node.value.id in ("new_rows", "gone")):
            bare.append(node.value.id)
    assert not bare, (
        f"_run_target 里还有裸切片:{sorted(set(bare))} —— "
        f"上限和丢多少条都必须走 record_change_capped")


def test_change_cap_is_a_named_constant_not_a_magic_number():
    """上限得是个有名字的常量,不是散在代码里的 200"""
    src = (REPO / "arl_lite" / "core" / "watcher.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # 找 CHANGE_RECORD_CAP 的字面量定义,确认它是 200 而不是随手写的数
    consts = [n.value.value for n in ast.walk(tree)
              if isinstance(n, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == "CHANGE_RECORD_CAP"
                      for t in n.targets)
              and isinstance(n.value, ast.Constant)]
    assert consts == [200], f"CHANGE_RECORD_CAP 不对:{consts}"
    # 而在 _run_target 里不该再出现裸的 200 切片上界
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_run_target")
    magic = [n.slice.value for n in ast.walk(fn)
             if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
             and isinstance(n.slice.value, int)]
    assert not magic, f"_run_target 里还有魔法数字切片:{magic}"


# ── 五、两个数字必须能被分开说清 ──

def test_capped_helper_is_actually_called_from_run_target():
    """`record_change_capped` 必须真的被 `_run_target` 调用

    这是 r42 的 `CHANGE_TYPES` 陷阱:定义在、测试也覆盖得挺好,但生产里
    **零引用**。所以「helper 写对了」不等于「截断会自报」—— 万一调用点
    又被改回裸切片,前面所有 helper 层的测试照样全绿,而 watcher 照样静默。
    用 AST 找调用点,不用文本子串(注释里提到函数名不算)。
    """
    tree = ast.parse((REPO / "arl_lite" / "core" / "watcher.py")
                     .read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_run_target")
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and (isinstance(n.func, ast.Name)
                  and n.func.id == "record_change_capped")]
    # 两处:NEW_ASSET 一处、DISAPPEARED 一处。少一处就有一类变更还在静默截断。
    assert len(calls) == 2, (
        f"_run_target 里只找到 {len(calls)} 处 record_change_capped 调用,"
        f"应该是 2 处(新增 + 消失)。少一处就有一类变更还在静默丢弃。")
    # 两处的 snapshot 参数必须分别指向 before / after —— 写反了会记出
    # 语义相反的单边快照,而那是完全看不出来的一类错。
    snaps = {kw.value.value for n in calls for kw in n.keywords
             if kw.arg == "snapshot" and isinstance(kw.value, ast.Constant)}
    assert snaps == {"after", "before"}, (
        f"两处的 snapshot 参数是 {sorted(snaps)},"
        f"应该分别是 after(新增)和 before(消失)")
