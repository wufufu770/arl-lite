"""arl_lite.devloop.protocol — 循环引擎

BUILD → TEST → IMPROVE → PLAN → STATE 的状态机。

核心不变式(违反即中止,不留半吊子状态):
1. 每轮必须产出可验证增量——至少跑过一道门禁
2. 门禁失败必须写进状态,不允许被"处理掉"
3. 连续 barren 轮次达阈值 → 进入 RETREAT
4. 队列耗尽时自动播种,保证永远有下一步
5. 任何阶段抛错都要落到 RETREAT,而不是留在半途

设计取舍:引擎不做"自动改代码"。BUILD 阶段由人或 agent 执行,
引擎负责的是**选待办 → 跑门禁 → 记状态 → 规划下一步 → 判退路**。
自动改代码需要一个能自我验证的改写器,那是另一个量级的东西。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger("arl_lite.devloop.protocol")

from . import state as st
from .state import (
    LoopState, StateStore, RoundRecord,
    PHASE_BUILD, PHASE_TEST, PHASE_IMPROVE, PHASE_PLAN, PHASE_RETREAT, PHASE_IDLE,
    RESULT_DONE, RESULT_DONE_WITH_FAILURES, RESULT_NOOP, RESULT_RETREATED,
)

# 退路阈值:连续这么多轮没有净增量就退
RETREAT_THRESHOLD = 3

# 单轮最多重试改进的次数——防止 agent 在同一个失败里打转
MAX_IMPROVE_ATTEMPTS = 2


@dataclass
class StepResult:
    """单步(一个阶段)的结果"""
    phase: str
    ok: bool
    detail: str = ""
    data: dict = field(default_factory=dict)

    def __str__(self) -> str:
        mark = "OK " if self.ok else "FAIL"
        return f"[{mark}] {self.phase}: {self.detail}"


@dataclass
class RoundOutcome:
    """一轮跑完的完整结果——CLI 的返回值"""
    record: RoundRecord
    next_item: str = ""
    retreated: bool = False
    messages: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.record.result in (RESULT_DONE, RESULT_DONE_WITH_FAILURES)

    def summary(self) -> str:
        r = self.record
        lines = [
            f"round {r.round}  ->  {r.result}",
            f"  item      : {r.item_id or '(none)'} {r.item_title}",
            f"  gates     : {r.gates_passed} passed, {r.gates_failed} failed, {r.gates_warned} warned",
            f"  duration  : {r.duration:.1f}s",
        ]
        if r.item_id and r.completion_source == "operator" and r.result in (
            RESULT_DONE, RESULT_DONE_WITH_FAILURES
        ):
            lines.append(
                "  completion: asserted by operator (no build executor; "
                "the engine only verified the gates)"
            )
        if r.blocking_failures:
            lines.append(f"  blocking  : {', '.join(r.blocking_failures)}")
        if r.metrics:
            lines.append("  metrics   : " + ", ".join(f"{k}={v}" for k, v in r.metrics.items()))
        if self.retreated:
            lines.append("  RETREAT   : barren rounds hit threshold")
        if self.next_item:
            lines.append(f"  next      : {self.next_item}")
        for m in self.messages:
            lines.append(f"  ! {m}")
        return "\n".join(lines)


class Loop:
    """自持迭代循环。

    用法:
        loop = Loop(repo_root)
        outcome = loop.round()           # 跑一整轮
        loop.status()                    # 看当前状态

    执行者(build_fn)由外部注入——可以是人工步骤、agent、或脚本。
    引擎本身不碰源码,只管状态和门禁。
    """

    def __init__(
        self,
        repo_root: Path,
        state_dir: Path | None = None,
        build_fn: Callable[[object], tuple[bool, str]] | None = None,
    ):
        self.repo = Path(repo_root)
        self.dev_dir = Path(state_dir) if state_dir else self.repo / "devloop"
        self.dev_dir.mkdir(parents=True, exist_ok=True)
        self.store = StateStore(self.dev_dir / "state.json")
        self._build_fn = build_fn

        # 延迟导入:模块由并行 agent 实现,这里保持松耦合
        from . import gates as _gates
        from . import queue as _queue
        self.gates = _gates
        self.queue_mod = _queue

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    def status(self) -> LoopState:
        return self.store.load()

    def status_text(self) -> str:
        s = self.status()
        q = self.queue_mod.Queue(self.dev_dir / "queue.json")
        items = q.load()
        stt = q.stats()
        lines = [
            f"arl-lite devloop  (schema v{s.version})",
            f"  round      : {s.round}",
            f"  phase      : {s.phase}",
            f"  result     : {s.result}",
            f"  barren     : {s.barren_rounds}/{RETREAT_THRESHOLD}",
            f"  retreats   : {s.retreats}",
            f"  gates      : {s.total_gates_passed} passed / {s.total_gates_failed} failed (cumulative)",
            f"  items      : {stt.get('pending', 0)} pending"
            f" / {stt.get('in_progress', 0)} in_progress"
            f" / {stt.get('done', 0)} done / {stt.get('dropped', 0)} dropped",
        ]
        # 被别人认领的条目。多 agent 场景下"队列里没活可选"经常不是
        # 队列空了,而是活被别人拿走了 —— 不显示出来会让人以为队列有毛病。
        claimed = q.claimed_by()
        if claimed:
            lines.append(f"  claimed    : {len(claimed)} 条正在被别人做")
            for iid, owner in sorted(claimed.items()):
                age = ""
                by_id = next((i for i in items if i.id == iid), None)
                if by_id and by_id.claimed_at:
                    age = f", 已领 {_age_str(_now() - by_id.claimed_at)}"
                lines.append(f"                 {iid} ← {owner or '(无主)'}{age}")
        # 有人正在写队列时提示一下。"队列看着没变"和"另一个 agent 正在
        # 写、等一下就好了"必须能区分开,否则会把正常的并发写误判成故障。
        from . import lock as _lock
        qpath = self.dev_dir / "queue.json"
        if _lock.is_locked(qpath):
            holder = _lock.lock_holder(qpath) or "(未知)"
            lines.append(f"  writing    : 队列正被 {holder} 持锁写入中")

        nxt = q.next()
        if nxt:
            lines.append(f"  next       : [{_prio_name(nxt.priority)}] {nxt.id} — {nxt.title}")
        if s.history:
            last = s.history[-1]
            lines.append(f"  last round : {last.result} ({last.gates_passed}P/{last.gates_failed}F) {last.duration:.1f}s")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 单个阶段
    # ------------------------------------------------------------------

    def phase_build(self, state: LoopState, item) -> StepResult:
        """BUILD:选定待办,交给执行者。"""
        self.store.set_phase(state, PHASE_BUILD)
        if item is None:
            return StepResult(PHASE_BUILD, True, "no item (queue will be seeded)")

        state.current_item = item.id
        state.current_note = item.title
        self.store.save(state)

        if self._build_fn is None:
            return StepResult(
                PHASE_BUILD, True,
                f"selected [{_prio_name(item.priority)}] {item.id} — "
                f"execute manually, then run 'devloop test'",
                {"item": item.id},
            )

        try:
            ok, detail = self._build_fn(item)
        except Exception as e:  # 执行者炸了不能拖死循环
            return StepResult(PHASE_BUILD, False, f"build raised {type(e).__name__}: {e}")
        return StepResult(PHASE_BUILD, ok, detail or item.title, {"item": item.id})

    def phase_test(self, state: LoopState, only: list[str] | None = None) -> StepResult:
        """TEST:跑门禁。这是唯一的事实来源。"""
        self.store.set_phase(state, PHASE_TEST)
        try:
            results = self.gates.run_all(self.repo, only=only)
        except Exception as e:
            return StepResult(PHASE_TEST, False, f"gate runner raised {type(e).__name__}: {e}")

        passed = sum(1 for r in results if r.passed and r.blocking)
        failed = [r for r in results if not r.passed and r.blocking]
        warned = sum(1 for r in results if not r.passed and not r.blocking)

        for r in results:
            state.gate_log.append({
                "round": state.round + 1,
                "gate": r.name,
                "passed": r.passed,
                "blocking": r.blocking,
                "detail": r.detail[:200],
            })

        detail = ", ".join(f"{r.name}={r.measured}" for r in results)
        return StepResult(
            PHASE_TEST,
            not failed,
            detail,
            {
                "results": results,
                "passed": passed,
                "failed": len(failed),
                "warned": warned,
                "blocking_names": [r.name for r in failed],
            },
        )

    def phase_improve(self, state: LoopState, test: StepResult, attempts: int = 0) -> StepResult:
        """IMPROVE:门禁失败时收敛。

        这里不自动改代码——引擎不知道该怎么修。记录"需要人工/agent 处理"
        并进入 PLAN。把改进动作交给执行者注入。
        """
        self.store.set_phase(state, PHASE_IMPROVE)
        if test.ok:
            return StepResult(PHASE_IMPROVE, True, "all blocking gates green")

        if attempts >= MAX_IMPROVE_ATTEMPTS:
            return StepResult(
                PHASE_IMPROVE, False,
                f"blocking gates still failing after {attempts} attempts: "
                f"{', '.join(test.data.get('blocking_names', []))}",
            )
        return StepResult(
            PHASE_IMPROVE, False,
            f"needs fix: {', '.join(test.data.get('blocking_names', []))} "
            f"(attempt {attempts + 1}/{MAX_IMPROVE_ATTEMPTS})",
        )

    def phase_plan(self, state: LoopState) -> StepResult:
        """PLAN:确保队列永远有下一步。

        注意 in_progress 也算"有下一步"—— 当前正在处理的那条就是下一步。
        (历史上这里只看 pending,导致 has_pending=True 但 nxt=None 的
        自相矛盾,报出 "queue has no pending item")
        """
        self.store.set_phase(state, PHASE_PLAN)
        q = self.queue_mod.Queue(self.dev_dir / "queue.json")
        items = q.load()
        # 用同一实例的 items 判断,避免 load/seed 之间的对象分裂
        active = [i for i in items if i.status in ("pending", "in_progress")]
        if not active:
            before = len(items)
            added = q.seed_if_empty()
            items = q.load()
            if added <= 0:
                return StepResult(
                    PHASE_PLAN, False,
                    "queue exhausted and seeding produced nothing — "
                    "the protocol has no next step; add items manually",
                )
            state.current_note = f"seeded {added} new item(s)"
            q.save(items)
            return StepResult(PHASE_PLAN, True, f"queue exhausted, seeded {added} new item(s)")
        nxt = active[0]
        return StepResult(
            PHASE_PLAN, True,
            f"next: {nxt.id} — {nxt.title} [{nxt.status}]",
        )

    def ensure_next_step(self, state: LoopState) -> tuple[int, str]:
        """整轮**结束后**再查一次:队列耗尽就立刻播种。

        ## 为什么必须有这一步(而不是只靠 phase_plan)

        `round()` 的阶段顺序是
            取待办 → BUILD → TEST → IMPROVE → PLAN → 更新队列状态

        PLAN 跑在"更新队列"**之前**。那一刻当前条目还是 in_progress,
        于是 phase_plan 认为"还有活干"不播种;紧接着引擎把它标成 done,
        队列就彻底空了 —— 而且**再没有任何代码路径会回来播种**。

        第 9 轮真实撞上了这个:队列只剩一条,跑完变成 0 pending / 8 done,
        循环没有下一步了。不变式 #4「队列耗尽自动播种,永远有下一步」被打破。

        修法是让"永远有下一步"成为整轮的**后置条件**而不是轮中途的一次猜测:
        不管中途发生了什么,轮次结束时队列必须非空。

        Returns:
            (新增条数, 描述)
        """
        q = self.queue_mod.Queue(self.dev_dir / "queue.json")
        items = q.load()
        if any(i.status in ("pending", "in_progress") for i in items):
            return 0, ""
        added = q.seed_if_empty()
        if added <= 0:
            return 0, (
                "queue exhausted and seeding produced nothing — "
                "the protocol has no next step; add items to devloop/backlog.md"
            )
        state.current_note = f"seeded {added} new item(s) after round"
        self.store.save(state)
        return added, f"seeded {added} new item(s) after round"

    def phase_retreat(self, state: LoopState) -> StepResult:
        """RETREAT:连续 barren 后的退路。

        刻意**不做 git reset**——那会丢失工作且难以恢复。
        退路是"停下来把状态摊开给人看",让操作者决定。
        """
        self.store.set_phase(state, PHASE_RETREAT)
        state.retreats += 1
        state.barren_rounds = 0
        return StepResult(
            PHASE_RETREAT, False,
            f"{state.retreats}th retreat: {RETREAT_THRESHOLD} barren rounds in a row. "
            f"State preserved at {self.dev_dir / 'state.json'}. "
            f"Review history, then continue or adjust the queue.",
        )

    def recover_stale_in_progress(self) -> int:
        """把**真正没人管**的 in_progress 复位成 pending。返回复位条数。

        ## 为什么需要

        `round()` 是同步的:一个轮次开始时把队首标成 in_progress,
        跑到 TEST 阶段才落回 done 或 pending。**只要一轮正常跑完,
        队列里就不该残留 in_progress**。

        一旦残留了(进程被 kill、手工改过状态、上一个实现有 bug),
        后果是软死局:
        - 队列非空(有 in_progress)→ `ensure_next_step` 认为没耗尽, 不播种
        - 但 `next()` 只认 pending → 选不出待办 → 整轮 NOOP

        实测第 11 轮就是这样空转的。所以每轮开头把上一轮的残留复位,
        让"有人在处理"这个状态**不跨轮存活**。

        ## 多 agent 之后这条规则必须改

        认领机制(见 queue.Queue.claim)落地后,"所有 in_progress 都是
        上轮残留"这个前提**不成立**了 —— agent 可以合法地跨轮持有认领。

        早先的实现无条件复位全部 in_progress,于是:

            agent A: claim task-x      → in_progress, owner=A,A 在改代码
            agent B: devloop round     → 复位 → task-x 回 pending
            agent B: next()             → 捡起 task-x,去改同一处代码

        文件锁防不了这个 —— 锁保证"写不撕裂",这里是**语义完整地
        覆盖了别人的进度**。两个 agent 同时改同一处代码,正是这把锁
        想防的后果,从后门进来了。

        所以复位改成**认领感知**:活着的认领一律保留,只复位没人管的
        (无 owner / owner 进程已退出 / 认领超时的)。判定细则见
        `Queue.is_stale_claim`。
        """
        q = self.queue_mod.Queue(self.dev_dir / "queue.json")
        # 复位逻辑住在 Queue 里,不在这里。早先版本把 lock+load+判+save
        # 整套写在 protocol 里,而 Queue 那边同时又有一套加锁的原子操作
        # —— 同一个"读-改-写要加锁"的规则存在两个实现。规则一旦有两处,
        # 迟早有一处漏掉,而漏掉的那处不会报错,只会安静地丢更新。
        n, held = q.recover_stale()
        if n:
            log.warning(
                "recover_stale_in_progress: 复位 %d 条无人认领的 in_progress", n
            )
        if held:
            # info 而不是静默:这是**正常的**多 agent 状态,但它改变了本轮
            # 能看到什么(那些条被别人拿走了),不提示会让人以为队列有毛病。
            log.info(
                "recover_stale_in_progress: %d 条 in_progress 有人认领,保留: %s",
                len(held), held,
            )
        return n

    # ------------------------------------------------------------------
    # 整轮
    # ------------------------------------------------------------------

    def round(
        self,
        only_gates: list[str] | None = None,
        build: Callable | None = None,
    ) -> RoundOutcome:
        """跑一整轮:BUILD → TEST → IMPROVE → PLAN → 落盘。"""
        state = self.store.load()
        if not state.started_at:
            state.started_at = time.time()
        record = RoundRecord(round=state.round + 1, started_at=time.time())
        messages: list[str] = []

        # 退路优先:已经 barren 到底了,这轮不干活先摊牌
        if state.barren_rounds >= RETREAT_THRESHOLD:
            r = self.phase_retreat(state)
            record.result = RESULT_RETREATED
            record.blocking_failures.append("barren_threshold")
            record.note = r.detail
            messages.append(r.detail)
            self.store.commit_round(state, record)
            return RoundOutcome(record, retreated=True, messages=messages)

        # 1. 取待办
        #    队列空时先播种——首轮就得有活干,不能让循环空转一轮
        q = self.queue_mod.Queue(self.dev_dir / "queue.json")
        items = q.load()

        # 1.5 先把上一轮遗留的 in_progress 复位。轮次是同步的,
        #     正常跑完不该有残留;有残留就会让整轮 NOOP(见该方法 docstring)
        stale = self.recover_stale_in_progress()
        if stale:
            items = q.load()
            messages.append(f"recovered {stale} stale in_progress item(s) from a previous round")

        if not any(i.status in ("pending", "in_progress") for i in items):
            before = len(items)
            q.seed_if_empty()
            items = q.load()
            seeded = len(items) - before
            if seeded > 0:
                messages.append(f"queue was empty, seeded {seeded} item(s)")

        item = q.next(items)
        if item is not None:
            q.mark_in_progress(item)
            # 必须保存**同一个 list**——重新 load() 会拿到新对象,
            # 原地改的 status/attempts 就丢了
            q.save(items)
            record.item_id = item.id
            record.item_title = item.title

        # 2. BUILD
        builder = build or self._build_fn
        if builder is not None and item is not None:
            self._build_fn = builder
        b = self.phase_build(state, item)
        if not b.ok:
            messages.append(f"BUILD failed: {b.detail}")
        # 记下这一轮的 done 由谁断言。纯人工模式下引擎只看到"门禁全绿",
        # 看不到"活干了没有"——不区分,状态文件就是在替执行者背书。
        record.completion_source = "build_fn" if builder is not None else "operator"
        if item is not None and builder is None:
            messages.append(
                f"no build executor: '{item.id}' 的完成由人工断言,引擎只验证了门禁"
            )

        # 3. TEST
        t = self.phase_test(state, only=only_gates)
        record.gates_passed = t.data.get("passed", 0)
        record.gates_failed = t.data.get("failed", 0)
        record.gates_warned = t.data.get("warned", 0)
        record.blocking_failures = t.data.get("blocking_names", [])
        record.metrics = {
            r.name: r.measured for r in t.data.get("results", [])
            if r.measured is not None
        }

        # 4. IMPROVE
        im = self.phase_improve(state, t)
        if not im.ok:
            messages.append(f"IMPROVE: {im.detail}")

        # 5. PLAN
        p = self.phase_plan(state)
        if not p.ok:
            messages.append(f"PLAN: {p.detail}")

        # 6. 判定本轮结果 + 更新队列
        #     全程复用同一个 q 和 items —— 多个 Queue 实例操作同一文件会
        #     互相覆盖(load 拿回新对象,原地改的字段就丢了)
        if item is None:
            record.result = RESULT_NOOP
            record.note = "no pending item"
        else:
            target = next((i for i in items if i.id == item.id), None)
            if t.ok:
                record.result = RESULT_DONE
                if target is not None:
                    q.mark_done(target, state.round + 1)
                state.items_done += 1
            else:
                # 门禁没过 = 失败,如实记录(不退回去假装没事)
                record.result = RESULT_DONE_WITH_FAILURES
                if target is not None:
                    # 退回 pending。attempts 已在 mark_in_progress 时 +1,
                    # 连续失败会被这里累加,识别反复卡住的待办
                    target.status = "pending"
                    target.note = f"gates failed: {', '.join(record.blocking_failures)}"
            q.save(items)

        # 6.5 不变式 #4 的后置条件:轮次结束时队列必须还有下一步。
        #     PLAN 跑在队列更新之前,只看它会漏掉"本轮刚好把最后一条做掉"
        #     的情况(第 9 轮真实踩过)。
        added, note = self.ensure_next_step(state)
        if note:
            messages.append(note)
            record.note = (record.note + " | " if record.note else "") + note

        # 7. 落盘
        #    注意:这里要在 save 之后取,否则拿到的还是 save 前的快照
        q2 = self.queue_mod.Queue(self.dev_dir / "queue.json")
        nxt = q2.next()
        self.store.commit_round(state, record)

        return RoundOutcome(
            record,
            next_item=f"{nxt.id} — {nxt.title}" if nxt else "",
            retreated=False,
            messages=messages,
        )

    def test_only(self, only: list[str] | None = None) -> RoundOutcome:
        """只跑门禁,不记轮次——给"改完先验一下"用。"""
        state = self.store.load()
        record = RoundRecord(round=state.round, started_at=time.time())
        t = self.phase_test(state, only=only)
        record.gates_passed = t.data.get("passed", 0)
        record.gates_failed = t.data.get("failed", 0)
        record.gates_warned = t.data.get("warned", 0)
        record.blocking_failures = t.data.get("blocking_names", [])
        record.result = RESULT_DONE if t.ok else RESULT_DONE_WITH_FAILURES
        for r in t.data.get("results", []):
            mark = "OK" if r.passed else ("WARN" if not r.blocking else "FAIL")
            print(f"  {mark:4} {r.name:26} {r.detail}")
        record.finished_at = time.time()
        print(f"\n  {record.gates_passed} passed, {record.gates_failed} failed, {record.gates_warned} warned")
        return RoundOutcome(record)


def _prio_name(p: int) -> str:
    return {0: "P0", 1: "P1", 2: "P2", 3: "P3"}.get(p, f"P{p}")


def _now() -> float:
    """当前 unix 时间。抽出来是为了测试能替掉它。"""
    return time.time()


def _age_str(sec: float) -> str:
    """把秒数说成人话。给人看的东西不该要求心算。"""
    if sec < 0:
        return "0s"
    if sec < 90:
        return f"{sec:.0f}s"
    if sec < 5400:
        return f"{sec / 60:.0f}m"
    if sec < 172800:
        return f"{sec / 3600:.1f}h"
    return f"{sec / 86400:.1f}d"
