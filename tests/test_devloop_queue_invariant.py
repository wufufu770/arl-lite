"""队列永不枯竭 —— 不变式 #4 的回归防线

协议的第一目标是「永远有下一步」。这条不变式被真实场景破过一次:

第 9 轮跑完后队列 0 pending / 8 done,**没有触发播种**。
根因是 `round()` 的阶段顺序:

    1 取待办  → 2 BUILD → 3 TEST → 4 IMPROVE → 5 PLAN → 6 更新队列

第 5 步 PLAN 执行时,当前条目还是 `in_progress`(第 6 步才标 done)。
`phase_plan` 里 `has_pending` 把 in_progress 也算作待办 → 不播种;
而取下一条时只认 `pending` → 找不到 → 返回 "queue has no pending item"。

等第 6 步把它标 done,队列就彻底空了,而且**再没有任何代码路径会回来播种**。

修法:把「耗尽检查 + 播种」移到队列更新**之后**,让"永远有下一步"
成为整轮的**后置条件**,而不是轮中途的一次猜测。

本文件全部离线:用 tmp 目录当仓库,门禁用假门禁替身,
不跑 pytest(否则会递归)。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from arl_lite.devloop import gates
from arl_lite.devloop.gates import GateResult
from arl_lite.devloop.protocol import Loop
from arl_lite.devloop.queue import Item, Queue

REPO = Path(__file__).parents[1]


class _AlwaysGreenGate:
    """替身门禁:永远绿。只在测队列行为,门禁本身另有测试覆盖。"""

    def __init__(self, name):
        self.name = name
        self.blocking = True
        self.promotable = False
        self.promotable_fields = ()

    def run(self, repo: Path) -> GateResult:
        return GateResult(self.name, True, "ok", 0, blocking=True)


@pytest.fixture
def loop(tmp_path, monkeypatch):
    """一个只跑到门禁替身的 Loop,仓库骨架齐全"""
    (tmp_path / "arl_lite").mkdir()
    (tmp_path / "arl_lite" / "x.py").write_text("# placeholder\n", encoding="utf-8")
    # 播种要读 backlog.md,给一份最小的
    dev = tmp_path / "devloop"
    dev.mkdir()
    (dev / "backlog.md").write_text(
        "# backlog\n\n- [P1] test : 补一个排队论用例 |  | 验证反例\n"
        "- [P2] doc  : 写一页架构说明 |  | 验证链接可达\n",
        encoding="utf-8",
    )
    gates.update_baseline(tmp_path, "loc_budget", {"total_loc": 1})
    for name in gates.all_gate_names():
        monkeypatch.setitem(gates._REGISTRY, name, _AlwaysGreenGate(name))
    return Loop(tmp_path)


def _pending_ids(loop: Loop) -> list[str]:
    items = Queue(loop.dev_dir / "queue.json").load()
    return [i.id for i in items if i.status == "pending"]


def _drain(loop: Loop, max_rounds: int = 30) -> int:
    """反复跑轮次直到队列耗尽,返回跑了多少轮

    至少跑一轮:空队列上 `round()` 自己会先播种(取待办那一步),
    所以"跑之前就判断空然后 break"会把首轮播种也一起跳过。
    """
    n = 0
    for _ in range(max_rounds):
        q = Queue(loop.dev_dir / "queue.json")
        items = q.load()
        if n and not any(i.status in ("pending", "in_progress") for i in items):
            break
        loop.round()
        n += 1
    return n


# =====================================================================
# 核心回归:单条目场景(第 9 轮就是这样坏的)
# =====================================================================


def test_queue_is_never_left_empty_after_a_round(loop):
    """跑完一轮后队列必须还有 pending —— 不变式 #4

    第 9 轮的真实场景:队列里只剩一条,跑完就被标 done,
    且因为 PLAN 跑得太早没有播种,循环彻底没了下一步。
    """
    q = Queue(loop.dev_dir / "queue.json")
    q.save([Item(id="only-one", title="唯一一条待办", detail="x",
                 priority=1, kind="change", verify="y")])

    loop.round()

    assert _pending_ids(loop), (
        f"跑完一轮后队列空了,循环没有下一步了。状态={_pending_ids(loop)}"
    )


def test_single_item_queue_gets_seeded(loop):
    """单条目耗尽后必须补上新条目,而不是停在 0"""
    q = Queue(loop.dev_dir / "queue.json")
    q.save([Item(id="only-one", title="唯一一条待办", detail="x",
                 priority=1, kind="change", verify="y")])

    loop.round()

    items = Queue(loop.dev_dir / "queue.json").load()
    ids = [i.id for i in items]
    assert "only-one" in ids, "原有条目不该被删掉"
    assert len(ids) > 1, f"耗尽后没补新条目: {ids}"


def test_repeated_rounds_keep_the_loop_alive(loop):
    """连续跑很多轮,队列永远不空

    这是不变式 #4 最直接的表述:跑多久都不会走到死胡同。
    """
    _drain(loop)
    assert _pending_ids(loop), "耗尽后没播种"
    _drain(loop)
    assert _pending_ids(loop), "第二轮耗尽后没播种"
    _drain(loop)
    assert _pending_ids(loop), "第三轮耗尽后没播种"


# =====================================================================
# PLAN 阶段自身的语义
# =====================================================================


def test_plan_reports_in_progress_as_a_real_next_step(loop):
    """当前 in_progress 的条目就是"下一步",PLAN 不该说没有

    原来的 bug:`has_pending` 把 in_progress 算作有活干,
    但取具体下一条时只认 pending,于是自相矛盾地报 "no pending item"。
    """
    q = Queue(loop.dev_dir / "queue.json")
    q.save([Item(id="busy", title="正在做", detail="x",
                 priority=1, kind="change", verify="y")])
    items = q.load()
    q.mark_in_progress(items[0])
    q.save(items)

    r = loop.phase_plan(loop.status())
    assert r.ok is True, f"PLAN 应对 in_progress 条目给出下一步: {r.detail}"
    assert "busy" in r.detail


def test_plan_seeds_when_queue_is_genuinely_empty(loop):
    """真空了就得播种并如实报出新增数"""
    Queue(loop.dev_dir / "queue.json").save([])
    r = loop.phase_plan(loop.status())
    assert r.ok is True, f"空队列时 PLAN 应播种: {r.detail}"
    assert "seeded" in r.detail
    assert _pending_ids(loop)


# =====================================================================
# 状态记录的诚实性
# =====================================================================


def test_done_items_are_marked_done(loop):
    """修好播种之后,原本的完成标记不能被弄丢"""
    q = Queue(loop.dev_dir / "queue.json")
    q.save([Item(id="only-one", title="唯一一条待办", detail="x",
                 priority=1, kind="change", verify="y")])
    loop.round()
    items = {i.id: i for i in Queue(loop.dev_dir / "queue.json").load()}
    assert items["only-one"].status == "done"


def test_round_records_the_seeding_in_state(loop):
    """播种这件事要落进状态,否则事后无从知道循环靠什么活着"""
    q = Queue(loop.dev_dir / "queue.json")
    q.save([Item(id="only-one", title="唯一一条待办", detail="x",
                 priority=1, kind="change", verify="y")])
    outcome = loop.round()
    assert any("seed" in m.lower() for m in outcome.messages), outcome.messages


# =====================================================================
# id 唯一性:重复 id 会让循环原地空转
# =====================================================================
#
# 真实事故:seed_if_empty 传给旋转逻辑的是 active_ids(只含 pending/in_progress),
# 而 docstring 写的是"已存在且处于 done/dropped 则加 -r 后缀"。文档和代码
# 不是一回事,于是队列全 done 时不触发旋转,补出一批完全同 id 的条目。
#
# 而引擎所有按 id 定位的地方(mark_in_progress / mark_done / round 取待办)
# 都是 `next(i for i in items if i.id == ...)`,永远命中第一条。于是引擎
# 反复翻转那条早已 done 的记录,新条目永远卡 pending —— 循环原地空转。
#
# 实测真实队列被搞成 8 个 id 各两条、8 条全"活跃",而 status 只显示
# 聚合计数,肉眼完全看不出来。


def test_seeding_never_creates_duplicate_ids(loop):
    """播种产出的 id 必须全局唯一"""
    loop.round()
    loop.round()
    dupes = Queue(loop.dev_dir / "queue.json").find_duplicates()
    assert not dupes, f"播种造出了重复 id: {dupes}"


def test_ids_stay_unique_across_many_rounds(loop):
    """跑很多轮也不能攒出重复 id"""
    for _ in range(12):
        q = Queue(loop.dev_dir / "queue.json")
        if not any(i.status in ("pending", "in_progress") for i in q.load()):
            break
        loop.round()
    dupes = Queue(loop.dev_dir / "queue.json").find_duplicates()
    assert not dupes, f"多轮后出现重复 id: {dupes}"


def test_seeding_after_full_drain_rotates_ids(loop):
    """全部 done 后再播种,补出来的必须是新变体而不是同名副本"""
    q = Queue(loop.dev_dir / "queue.json")
    q.save([Item(id="job-a", title="甲", detail="x", priority=1, kind="change")])
    loop.round()  # 甲做完 → done,并触发播种

    ids = [i.id for i in Queue(loop.dev_dir / "queue.json").load()]
    assert ids.count("job-a") == 1, f"原始条目被复制了: {ids}"
    assert any(i != "job-a" for i in ids), f"耗尽后没补出新条目: {ids}"


def test_loop_actually_makes_progress_each_round(loop):
    """每轮都必须真的推进一条,不能原地打转

    这是重复 id 最直接的表现:引擎每轮都"选中"同一个 id,
    但改的是另一条记录,于是这条永远选不中、也永远完不成。
    """
    seen_done: set[str] = set()
    for _ in range(8):
        q = Queue(loop.dev_dir / "queue.json")
        if not any(i.status in ("pending", "in_progress") for i in q.load()):
            break
        loop.round()
        now_done = {i.id for i in q.load() if i.status == "done"}
        assert now_done - seen_done, "这一轮没有任何新条目被标记完成 —— 原地空转了"
        seen_done |= now_done


# ── repair_duplicates ──────────────────────────────────────────────────


def test_repair_removes_duplicate_records(loop):
    q = Queue(loop.dev_dir / "queue.json")
    q.save([
        Item(id="dup", title="旧", detail="d", status="done", done_round=1, attempts=2),
        Item(id="dup", title="旧", detail="d", status="pending"),
        Item(id="dup", title="旧", detail="d", status="in_progress"),
        Item(id="solo", title="独苗", detail="d", status="pending"),
    ])
    assert q.find_duplicates() == {"dup": 3}

    removed = q.repair_duplicates()
    assert removed == 2
    items = q.load()
    assert len(items) == 2
    assert q.find_duplicates() == {}


def test_repair_keeps_the_most_advanced_record(loop):
    """同一个 id 保留进度最靠前的那条"""
    q = Queue(loop.dev_dir / "queue.json")
    q.save([
        Item(id="dup", title="done 记录", detail="d", status="done", done_round=1),
        Item(id="dup", title="pending 记录", detail="d", status="pending"),
        Item(id="dup", title="in_progress 记录", detail="d", status="in_progress"),
    ])
    q.repair_duplicates()
    kept = q.load()
    assert len(kept) == 1
    assert kept[0].status == "in_progress"
    assert kept[0].title == "in_progress 记录"


def test_repair_is_idempotent(loop):
    q = Queue(loop.dev_dir / "queue.json")
    q.save([Item(id="dup", title="dup", detail="d", status="done"), Item(id="dup", title="dup", detail="d", status="pending")])
    assert q.repair_duplicates() == 1
    assert q.repair_duplicates() == 0
    assert len(q.load()) == 1


def test_repair_on_clean_queue_is_a_noop(loop):
    q = Queue(loop.dev_dir / "queue.json")
    q.save([Item(id="a", title="a", detail="d", status="pending"), Item(id="b", title="b", detail="d", status="done")])
    before = [i.id for i in q.load()]
    assert q.repair_duplicates() == 0
    assert [i.id for i in q.load()] == before


def test_repair_preserves_original_order(loop):
    q = Queue(loop.dev_dir / "queue.json")
    q.save([
        Item(id="a", title="a", detail="d", status="pending"),
        Item(id="b", title="b", detail="d", status="done"),
        Item(id="b", title="b", detail="d", status="pending"),
        Item(id="c", title="c", detail="d", status="pending"),
    ])
    q.repair_duplicates()
    assert [i.id for i in q.load()] == ["a", "b", "c"]


def test_repaired_queue_can_drive_the_loop_again(loop):
    """修完的队列必须能重新驱动循环 —— 不然只是看着干净"""
    q = Queue(loop.dev_dir / "queue.json")
    q.save([
        Item(id="a", title="甲", detail="d", status="done", done_round=1),
        Item(id="a", title="甲", detail="d", status="pending"),
    ])
    q.repair_duplicates()
    loop.round()
    # 修完只剩一条 pending 的 a,跑完应该变 done 并重新播种出唯一 id 的新条目
    items = q.load()
    assert not q.find_duplicates(), f"修复后跑一轮又出现重复: {items}"
    assert any(i.status == "done" for i in items)


# ── unmark ─────────────────────────────────────────────────────────────


def test_unmark_puts_a_false_done_back_to_pending(loop):
    """引擎没做那件事却标了 done,unmark 能改回去"""
    q = Queue(loop.dev_dir / "queue.json")
    q.save([Item(id="liar", title="没真做", detail="d", status="done", done_round=3)])

    ok, detail = q.unmark("liar", "本轮实际没做这项")
    assert ok is True, detail

    it = q.load()[0]
    assert it.status == "pending"
    assert it.done_round is None
    assert "unmarked" in it.note and "本轮实际没做" in it.note


def test_unmark_unknown_item_fails_loudly(loop):
    Queue(loop.dev_dir / "queue.json").save([Item(id="real", title="r", detail="d", status="done")])
    ok, detail = q_unmark_helper(loop, "ghost")
    assert ok is False
    assert "no such item" in detail


def q_unmark_helper(loop: Loop, item_id: str):
    return Queue(loop.dev_dir / "queue.json").unmark(item_id)


def test_unmark_refuses_when_id_is_ambiguous(loop):
    """id 重复时不猜改哪一条 —— 提示先 repair

    重复 id 下"改哪条"是歧义的,猜错等于把记录改得更乱。
    """
    q = Queue(loop.dev_dir / "queue.json")
    q.save([
        Item(id="dup", title="甲", detail="d", status="done"),
        Item(id="dup", title="甲", detail="d", status="done"),
    ])
    ok, detail = q.unmark("dup")
    assert ok is False
    assert "repair" in detail
    # 不该有任何改动
    assert len(q.load()) == 2
    assert all(i.status == "done" for i in q.load())


def test_unmark_already_pending_is_a_noop(loop):
    q = Queue(loop.dev_dir / "queue.json")
    q.save([Item(id="p", title="p", detail="d", status="pending")])
    ok, detail = q.unmark("p")
    assert ok is False
    assert "already pending" in detail


def test_real_repo_queue_has_no_duplicate_ids():
    """真实队列的完整性 —— 这条曾经红过

    防止同类问题再次悄悄溜进真实状态。
    """
    real = Queue(REPO / "devloop" / "queue.json")
    dupes = real.find_duplicates()
    assert not dupes, (
        f"真实队列有重复 id: {dupes}\n"
        f"  跑 `arl-lite devloop repair` 修复"
    )


def test_real_repo_queue_has_at_most_one_in_progress():
    """真实队列里 in_progress 的条目最多一条 —— 这条曾经红过

    ## 为什么不是"一条都没有"

    原本写的是"不该有卡在 in_progress 的条目",结果在第 10 轮红了一次:
    `devloop round` 的 TEST 阶段会在队列更新**之前**跑 pytest,
    那一刻当前条目正当着 in_progress(引擎直到门禁跑完才落 done/pending)。
    于是测试读到了轮次中间态,把合法状态当成损坏。

    单独跑 pytest 时不会红 —— 所以它只在 `devloop round` 里偶发,
    这种"换个入口才复现"的失败比稳定失败更难查。

    真正的不变式是"**轮次结束后**不该有残留",但那没法从一轮自己的
    测试运行里断言(测试就跑在这一轮中间)。所以退一步断言随时都成立的
    性质:一轮最多一条 in_progress。

    这仍然抓得住真实事故的signature —— 当时是 8 条同时 in_progress。
    """
    real = Queue(REPO / "devloop" / "queue.json")
    in_progress = [i.id for i in real.load() if i.status == "in_progress"]
    assert len(in_progress) <= 1, (
        f"真实队列有 {len(in_progress)} 条同时 in_progress: {in_progress}\n"
        f"  一轮最多一条;多条说明引擎在按 id 改同一条的另一份副本。"
    )
