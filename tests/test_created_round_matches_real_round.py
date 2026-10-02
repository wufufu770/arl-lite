"""r35:`created_round` 名不副实 —— 它是播种计数器,不是轮次

## 现象

r34 实测:round 33 播种出来的条目,`created_round=14`。差 19 轮。

```
next_round = max(i.created_round for i in items) + 1
```

## 为什么会漂移 19 轮

因为这个计数器**只在自己身上长**,和 `state.json` 的 `round` 没有任何
连接。队列里剩下哪些条目,`max()` 就只能看见哪些。历史上被 `unmark`、
被 `drop`、被 `repair` 拿走的记录都不在 `max()` 的视野里,于是计数器和
真实轮次从某个时刻起就各走各的。

## 为什么这个字段名是契约

`done_round` 记的是**真轮次**(`finish(round_no=state.round + 1)`)。
于是同一条记录上会出现:

    created_round=14   done_round=33

读的人(和下一轮的我)会以为这条活做了 19 轮。而真实情况是:它是在第 33 轮
被播种的,`14` 来自一个早已和真实轮次脱钩的计数器。

r22 就是在这种记录上栽的 —— 拿字段的"缺失"当证据。这一次是拿字段的
**值**当时间轴,方向不同,病一样。

## 修法

调用方传真轮次进去,`created_round` 记真轮次;不传时退回旧行为
(播种是公开方法,别偷偷改它的契约)。修完的不变式:

    created_round <= done_round

—— 和 `finish` 同一口径,一条记录上的两个轮次字段从此自洽。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from arl_lite.devloop.gates import GateResult
from arl_lite.devloop.protocol import Loop
from arl_lite.devloop.queue import Item, Queue

REPO = Path(__file__).resolve().parents[1]
GREEN = [GateResult("fake", True, "ok", 0, 0, True)]


def _mk(tmp_path, monkeypatch) -> Loop:
    lp = Loop(REPO, state_dir=tmp_path)
    (tmp_path / "devloop").mkdir(exist_ok=True)
    monkeypatch.setattr(lp.gates, "run_all", lambda repo, only=None: list(GREEN))
    return lp


def _q(loop) -> Queue:
    return Queue(loop.dev_dir / "queue.json")


def test_seeded_item_records_the_real_round_not_a_counter(tmp_path, monkeypatch):
    """空队列在第 7 轮播种,条目必须记 7,不能记某个跟轮次无关的数

    先把 state 推到第 7 轮,再让它播种 —— 这样"真轮次"是个具体数字,
    而"计数器"会给出别的。
    """
    lp = _mk(tmp_path, monkeypatch)
    (tmp_path / "devloop" / "backlog.md").write_text(
        "# backlog\n\n- [P1] change : 活 A | 细节 | test -f /dev/null\n",
        encoding="utf-8")

    def bump(st):
        st.round = 7
    lp.store.mutate(bump)

    lp.round()

    it = next(i for i in _q(lp).load() if not i.status == "done")
    assert it.created_round == 8, (
        f"记成了 created_round={it.created_round},期望真实轮次 8"
    )


def test_created_round_never_exceeds_done_round(tmp_path, monkeypatch):
    """同一条记录上,创建轮次不得晚于完成轮次

    这条不关心具体数字,只钉住"两个轮次字段自洽"。r34 之前
    `created_round=14 / done_round=33` 看着自洽(14 < 33),但那是
    巧合 —— 计数器一旦涨过真实轮次,就会出现 created > done。
    """
    lp = _mk(tmp_path, monkeypatch)
    (tmp_path / "devloop" / "backlog.md").write_text(
        "# backlog\n\n- [P1] change : 活 B | 细节 | true\n",
        encoding="utf-8")
    q = _q(lp)
    q.add(Item(id="done-item", title="会被完成的活", priority=1, kind="change",
               verify="true", detail="", tags=[]))

    lp.round()

    it = next(i for i in _q(lp).load() if i.id == "done-item")
    assert it.status == "done"
    assert it.created_round <= it.done_round, (
        f"created_round={it.created_round} 晚于 "
        f"done_round={it.done_round} —— 计数器漂到真实轮次前面了"
    )


def test_seeding_without_round_no_keeps_the_old_behavior(tmp_path):
    """不传 round_no 时退回 `max+1` —— 公开方法的契约不能被偷改

    这条钉住的是"我没顺手改掉别人的契约"。若实现改成 round_no 必填
    或者默认 None 时也用真实轮次,这条会红。
    """
    q = Queue(tmp_path / "queue.json")
    (tmp_path / "backlog.md").write_text(
        "# backlog\n\n- [P1] change : 活 C | 细节 | true\n", encoding="utf-8")

    q.seed_if_empty()

    it = q.load()[0]
    assert it.created_round == 1, (
        f"不传 round_no 时的旧行为变了:created_round={it.created_round}"
    )


def test_an_in_progress_item_counts_as_work_in_flight(tmp_path):
    """有别的 agent 正在干(in_progress)时不得播种

    这条是被变异测试逼出来的:把判空条件从
    `in ("pending", "in_progress")` 改成只认 `"pending"`,
    25 条测试照样全绿。

    而漏掉 `in_progress` 是真的会出事:那个 agent 正在改代码,引擎却
    播出一批新活,下一轮就可能有人去动它正在改的地方 —— 队列锁防不了
    这个,锁只保证"写不撕裂",这里是**语义上合法地**抢活。
    """
    q = Queue(tmp_path / "queue.json")
    (tmp_path / "backlog.md").write_text(
        "# backlog\n\n- [P1] change : 活 G | 细节 | true\n", encoding="utf-8")
    q.add(Item(id="busy", title="别人正在做的活", priority=1, kind="change",
               verify="true", detail="", tags=[]))
    it = q.claim(owner="other-agent", item_id="busy")
    assert it is not None and it.status == "in_progress"

    added = q.seed_if_empty()

    assert added == 0, (
        f"有 in_progress 活干却播了 {added} 条 —— "
        f"会有人去动别的 agent 正在改的地方"
    )
    assert len(Queue(tmp_path / "queue.json").load()) == 1


def test_seeding_twice_does_not_duplicate_the_backlog(tmp_path):
    """连播两次不得把同一条 backlog 变成两条

    `seed_if_empty` 以前**不判空** —— 它无条件播种,三个调用方各自在
    外部守了一遍。实测连播两次会把 `活 D` 变成 `活 D` 和 `活 D-r2`,
    队列凭空多出一倍待办。函数叫 `if_empty` 却不判空,这是本项目
    反复出现的第四处"名字不是它承诺的东西"(见 7.25 的三处)。
    """
    q = Queue(tmp_path / "queue.json")
    (tmp_path / "backlog.md").write_text(
        "# backlog\n\n- [P1] change : 活 D | 细节 | true\n", encoding="utf-8")

    first = q.seed_if_empty()
    second = q.seed_if_empty()

    ids = [i.id for i in Queue(tmp_path / "queue.json").load()]
    assert first == 1, f"第一次播了 {first} 条"
    assert second == 0, f"队列非空时又播了 {second} 条:{ids}"
    assert len(ids) == 1, f"队列被播重了:{ids}"


def test_created_round_is_exactly_the_round_passed_in(tmp_path):
    """传进去的轮次就是记下来的轮次,不多不少

    计数器实现(`max(created_round)+1`)在这条上会露馅:第一次播种
    恰好也等于 1,但传 7 就会记成 1 而不是 7。
    """
    for want in (1, 7, 33):
        d = tmp_path / f"case{want}"
        d.mkdir()
        q = Queue(d / "queue.json")
        (d / "backlog.md").write_text(
            f"# backlog\n\n- [P1] change : 活 {want} | 细节 | true\n",
            encoding="utf-8")
        q.seed_if_empty(round_no=want)
        got = q.load()[0].created_round
        assert got == want, (
            f"传 round_no={want},记成了 created_round={got} "
            f"(计数器实现会记成 {max(1, want - 32)})"
        )
