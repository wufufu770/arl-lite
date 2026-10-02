"""r35:两条闸门,专门拦「引擎自己造一条活再自己宣布做完」

## 事故

r34 落盘那一轮的日志:

    item      : verify 人写待办的 verify 必须可被机器核验
    ! queue was empty, seeded 1 item(s)

那条待办的验收是 `python3 -m pytest tests/test_backlog_verify_is_commandable.py`。
那个文件**没写**。实跑 exit≠0,验收不通过 —— 而它被标成了 `done`,
note 是 `done r33 · source=operator`。

r34 那一轮的全部内容就是"别再产生这种记录",它自己当场又造了一条。

## 两个成因,两道闸门

**成因一(结构)**:`seed_if_empty()` 在 PLAN 阶段跑,**早于**本轮领取:

    23:51:09  seed_if_empty: +1 items       <- 本轮开头播种
    23:53:22  round 33 DONE, item=verify    <- 同一轮领走并 finish

中间那 132 秒跑的是门禁,不是这条活的实现。引擎能自己造一条活,
再自己宣布做完。队列每次耗尽都会发生。

**成因二(无闸门)**:就算堵住成因一,操作者仍然可以断言一个假完成。
r34 全部门禁 6/0/0、487 条测试全过,却把一条验收命令跑不通的条目标成
done —— **没有哪道门禁看"刚被完成的条目,它自己的 verify 现在过不过"**。
门禁查的是代码的性质,不是这一轮的记账是否属实。

## 下面这些测试各自杀掉哪一个变异

第一组杀"播种完照样领";第二组杀"verify 失败也照样标 done"。
两者都故意写成"读起来像在测别的东西",因为它们真正的考点都是
**不该发生的事没有发生**,而不是某段代码跑出了某个值。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from arl_lite.devloop.gates import GateResult
from arl_lite.devloop.protocol import Loop
from arl_lite.devloop.queue import Item, Queue

REPO = Path(__file__).resolve().parents[1]

GREEN = [GateResult("fake", True, "ok", 0, 0, True)]


def _mk(tmp_path, monkeypatch, backlog: str) -> Loop:
    lp = Loop(REPO, state_dir=tmp_path)
    (tmp_path / "devloop").mkdir(exist_ok=True)
    (tmp_path / "devloop" / "backlog.md").write_text(backlog, encoding="utf-8")
    monkeypatch.setattr(lp.gates, "run_all", lambda repo, only=None: list(GREEN))
    return lp


@pytest.fixture
def loop(tmp_path, monkeypatch):
    """空队列 + 一条能播种出来的 backlog —— 复刻 r34 事故的现场"""
    return _mk(
        tmp_path, monkeypatch,
        "# backlog\n\n- [P1] change : 待办 A | 细节 A | test -f /dev/null\n",
    )


def _q(loop) -> Queue:
    return Queue(loop.dev_dir / "queue.json")


# ── 闸门一:本轮播种的条目不得在本轮被领走 ──

def test_item_seeded_this_round_is_not_completed_in_that_round(loop):
    """r34 那条假 done 的直接复刻 —— 现在它必须不再发生

    关键在"**同一轮**"这三个字:条目确实会被播种(不变式 #4 要求
    队列永远非空),但它属于**下一轮**的燃料。
    """
    out = loop.round()

    items = _q(loop).load()
    assert any(i.status == "pending" for i in items), (
        f"播种出来的条目没有被留下: {[(i.id, i.status) for i in items]}"
    )
    assert all(i.status != "done" for i in items), (
        "本轮刚播种的条目被标成了 done —— 这正是 r34 的假账"
    )
    assert not out.record.item_id, (
        f"本轮把刚播种的 {out.record.item_id!r} 记成了自己完成的活"
    )


def test_seeded_round_is_reported_as_honest_noop(loop):
    """轮初没活就如实空转,并说清「这轮干了什么」

    空转一轮是诚实的,假完成不是。但也不能只字不提地空转 ——
    记录里得留下"这轮播种了 N 条给下一轮"。
    """
    out = loop.round()

    assert out.record.result == "NOOP", (
        f"轮初没活却报了 {out.record.result!r}"
    )
    assert any("next" in m for m in out.messages), (
        f"没说清这轮的播种是给下一轮的: {out.messages}"
    )


def test_seeding_still_happens_so_the_queue_is_never_empty(loop):
    """闸门一不能顺手把不变式 #4 也破了 —— 队列仍必须被填上

    这是上面那两条的反证:如果修法是"干脆别播种",队列就会空掉,
    循环失去下一步。播种照做,只是不认领。
    """
    loop.round()

    items = _q(loop).load()
    assert len(items) >= 1, "没有播种,队列空了"
    assert any(i.status == "pending" for i in items)


def test_a_real_pending_item_is_still_claimed_and_completed(loop):
    """闸门一只拦"本轮播种的",不拦本来就有的活

    没有这条,一个"什么都不做"的实现也能让上面三条全绿。
    """
    q = _q(loop)
    q.add(Item(id="real", title="本来就有的活", priority=1, kind="change",
               verify="test -f pyproject.toml", detail="", tags=[]))

    out = loop.round()

    assert out.record.item_id == "real"
    assert next(i for i in _q(loop).load() if i.id == "real").status == "done"


# ── 闸门二:验收命令现在跑不通的条目,不许被标 done ──

def test_done_is_refused_when_the_items_own_verify_fails(tmp_path, monkeypatch):
    """r34 那条假账的另一种复现:队列非空,但验收命令是跑不通的

    这道闸门是更根本的一道 —— 它不管条目是怎么来的,只看
    **这条活自己的验收标准现在过不过**。
    """
    lp = _mk(tmp_path, monkeypatch, "# backlog\n")
    q = Queue(lp.dev_dir / "queue.json")
    q.add(Item(id="fake-done", title="没写的活", priority=1, kind="change",
               verify="test -f 绝对不存在的文件", detail="", tags=[]))

    out = lp.round()

    it = next(i for i in _q(lp).load() if i.id == "fake-done")
    assert it.status == "pending", (
        f"验收命令跑不通却标成了 done:{it.status}"
    )
    assert out.record.result == "DONE_WITH_FAILURES", (
        f"拒绝标 done 却没有如实报失败:{out.record.result!r}"
    )


def test_refusal_names_the_failing_verify(loop):
    """拒绝时要**说清是哪条验收命令没过**,否则人无从下手

    只写"失败了"等于把判断责任推回去 —— 人得能一眼看到
    自己该去跑什么。
    """
    q = _q(loop)
    q.add(Item(id="fake", title="x", priority=1, kind="change",
               verify="test -f 绝对不存在的文件", detail="", tags=[]))

    out = loop.round()

    assert any("test -f" in m for m in out.messages), (
        f"拒绝时没说清是哪条验收没过:{out.messages}"
    )


def test_prose_verify_does_not_block_completion(loop):
    """散文 verify **不许**拿来挡完成

    这条是整道闸门最容易被拆掉的地方。实测 `backlog.md` 里人写的
    verify 三种形态都跑得出非 0 退出码:

        `docs/ 里有一节说明…`   -> 126
        `core/risk.py:46 按…`  -> 127
        `造 60 条关联…(改前…)` -> 2  (反引号/括号语法错)

    而 2 同时是合法命令的正常失败码 —— 拿退出码当"是不是命令"的
    判据就是在猜,猜错的方向是**误挡真实完成**。一道会被噪声逼着
    拆掉的闸门,连本该拦住假账的那部分也一起没了(和 r16 删整层
    假活同一个动机)。所以散文一律 unknown,不构成结论。
    """
    q = _q(loop)
    q.add(Item(id="prose-item", title="散文验收的活", priority=1, kind="doc",
               verify="docs/ 里有一节说明 confidence 与 risk 两者正交(可 grep)",
               detail="", tags=[]))

    out = loop.round()

    it = next(i for i in _q(loop).load() if i.id == "prose-item")
    assert it.status == "done", (
        f"散文 verify 被当成了未通过,活被永久卡死:{it.status}"
    )
    assert out.record.result == "DONE"


def test_constant_true_verify_still_completes(loop):
    """复查信号用的 `verify=true` 必须照常完成

    信号条目(id 以 `no-due-maintenance-review` 开头)的完成判据就是
    "人确认过",不是某个文件存在。r35 不能把它判成未通过。
    """
    q = _q(loop)
    q.add(Item(id="no-due-maintenance-review-x", title="复查信号", priority=1,
               kind="research", verify="test -f pyproject.toml", detail="", tags=[]))

    out = loop.round()

    it = next(i for i in _q(loop).load() if i.id == "no-due-maintenance-review-x")
    assert it.status == "done", f"verify=true 的信号被挡住了:{it.status}"


# ── 三态判定本身 ──

@pytest.mark.parametrize("verify,expect", [
    ("python3 -m pytest tests/绝对不存在.py", Queue.VERIFY_FAIL),
    ("test -f 绝对不存在的文件", Queue.VERIFY_FAIL),
    ("true", Queue.VERIFY_PASS),
    ("docs/ 里有一节说明两者正交", Queue.VERIFY_UNKNOWN),
    ("造 60 条关联(改前会被截掉)", Queue.VERIFY_UNKNOWN),
    ("", Queue.VERIFY_UNKNOWN),
])
def test_verify_result_is_three_valued(verify, expect):
    """散文一律 unknown —— 判据是白名单,不是猜退出码

    参数化里特意放了两种散文:`docs/ …` 实测退出码 126(首词是目录),
    `造 60 条…(…)` 实测 2(括号语法错)。若改回"退出码非 0 即 fail",
    这两条会立刻变成 fail,于是所有散文待办被永久卡死。
    """
    assert Queue.verify_result(verify) == expect, (
        f"{verify!r} 判成了 {Queue.verify_result(verify)!r},期望 {expect!r}"
    )
