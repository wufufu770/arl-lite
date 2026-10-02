"""r28: `round` 记的 item 必须和"实际做完的那条"是同一条

## 为什么需要这个文件

`Loop.round` 原来只按优先级取队列首项,门禁全绿就把它标成 done。可执行者
做完的往往是另一条 —— r27 实测就是那样:做完三条真活,round 记的却是
第四条,而那条**没人做过**,引擎照样标成 done,并在 `state.json` 的 history
里写下"本轮完成了 <那条>"。

`completion_source` 挡不住这个:它只说明"done 由谁断言",说明不了"人工做
的是不是这一条"。所以 r28 补了 `item_selection`,并让 `round` 接受显式
`item_id`。

还有一处:`no-due-maintenance-review` 复查信号**不是工作**(内容是"当前无
到期维护项,请人工确认下一步",不可完成)。把它和真活一视同仁地标成 done,
history 里就出现"完成了一项任务",而实际一行代码都没动。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from arl_lite.devloop import queue as qmod
from arl_lite.devloop.gates import GateResult
from arl_lite.devloop.protocol import Loop
from arl_lite.devloop.queue import Item, Queue

REPO = Path(__file__).resolve().parents[1]

GREEN = [GateResult("fake", True, "ok", 0, 0, True)]


def _seed(q: Queue, item_id: str, priority: int = 2) -> Item:
    q.add(Item(id=item_id, title=f"标题 {item_id}", priority=priority,
               kind="change", verify="true", detail="", tags=[]))  # r35: 占位 verify 必须是**通过**的,收尾闸门会真跑它
    return next(i for i in q.load() if i.id == item_id)


@pytest.fixture
def loop(tmp_path, monkeypatch):
    """一个门禁全绿、队列在 tmp 里的 Loop"""
    lp = Loop(REPO, state_dir=tmp_path)
    (tmp_path / "devloop").mkdir(exist_ok=True)
    (tmp_path / "devloop" / "backlog.md").write_text(
        "# backlog\n\n- [P1] change : 播种出来的假条目 |  | test -f 不存在\n",
        encoding="utf-8")
    monkeypatch.setattr(lp.gates, "run_all", lambda repo, only=None: list(GREEN))
    return lp


def _q(loop) -> Queue:
    return Queue(loop.dev_dir / "queue.json")


# --- 显式优先 ---

def test_explicit_item_id_wins_over_priority(loop):
    """队列里 P0 在前,但本轮做的是另一条 —— 必须记那一条

    旧实现没有 item_id,永远取首项,于是记录指向的是没人做过的 P0。
    """
    _seed(_q(loop), "p0_first", priority=0)
    _seed(_q(loop), "actually_done", priority=3)

    out = loop.round(item_id="actually_done")

    assert out.record.item_id == "actually_done", (
        f"记成了 {out.record.item_id!r} —— 那条本轮没人做"
    )
    assert out.record.item_selection == "explicit"


def test_explicit_item_actually_gets_marked_done(loop):
    """显式指的那条要真的被标 done,不是只改改记录"""
    _seed(_q(loop), "p0_first", priority=0)
    _seed(_q(loop), "actually_done", priority=3)

    loop.round(item_id="actually_done")

    st = {i.id: i.status for i in _q(loop).load()}
    assert st["actually_done"] == "done"
    assert st["p0_first"] == "pending", "没做的条目被顺手标成 done 了"


def test_auto_selection_is_recorded_as_auto(loop):
    """不给 item_id 时取首项,并且**如实记下是自动挑的**"""
    _seed(_q(loop), "p0_first", priority=0)
    _seed(_q(loop), "other", priority=3)

    out = loop.round()

    assert out.record.item_id == "p0_first"
    assert out.record.item_selection == "auto", (
        "自动挑的却不记 —— 那和没记录是一回事"
    )


# --- 拒绝:宁可报错也不记假账 ---

def test_explicit_unknown_item_is_refused(loop):
    _seed(_q(loop), "p0_first", priority=0)
    with pytest.raises(LookupError):
        loop.round(item_id="never_existed")
    assert _q(loop).load()[0].status == "pending", "出错了还把条目动了"


def test_explicit_finished_item_is_refused(loop):
    """指一条已经 done 的 = 记一笔"本轮完成了它"的假账 —— 直接拒绝"""
    _seed(_q(loop), "already", priority=1)
    q = _q(loop)
    items = q.load()
    q.mark_done(next(i for i in items if i.id == "already"), 1)
    # 必须存**同一个 list**。写 q.save(q.load()) 的话,存回去的是重新
    # 读出来的、没被 mark_done 改过的副本,条目其实还是 pending ——
    # 这正是 r22 记过的恒真陷阱(那里的 `q.save(q.load())` 让三个变异
    # 全绿通过)。本文件第一版就栽在同一个地方。
    q.save(items)

    before_round = loop.store.load().round
    with pytest.raises(ValueError, match="not workable"):
        loop.round(item_id="already")

    assert loop.store.load().round == before_round, "被拒的 round 仍然落了盘"
    assert next(i for i in _q(loop).load() if i.id == "already").status == "done"


# --- 信号不是工作 ---

def test_signal_round_is_not_recorded_as_finished_work(loop):
    """复查信号的 completion_source 必须是 signal_ack,不是 operator

    标成 operator 等于在 history 里写"完成了一项任务",而它一行代码都没动。
    """
    sid = "no-due-maintenance-review-r9"
    _seed(_q(loop), sid, priority=2)

    out = loop.round(item_id=sid)

    assert qmod.is_signal_id(sid), "判据本身对信号失效了"
    assert out.record.completion_source == "signal_ack", (
        f"信号被记成 {out.record.completion_source!r} —— history 会声称完成了工作"
    )


def test_real_work_round_is_recorded_as_operator(loop):
    """对照组:真活仍然是 operator —— 否则上面那条可能只是恒真"""
    _seed(_q(loop), "real_work", priority=2)
    out = loop.round(item_id="real_work")
    assert out.record.completion_source == "operator"


def test_signal_id_matcher_covers_derived_copies():
    """真实队列里就有 -rN 派生副本,判据漏了它等于没写"""
    assert qmod.is_signal_id("no-due-maintenance-review")
    assert qmod.is_signal_id("no-due-maintenance-review-r1")
    assert qmod.is_signal_id("no-due-maintenance-review-r12")
    assert not qmod.is_signal_id("real-work")
    assert not qmod.is_signal_id("no-due-maintenance-reviewish")
