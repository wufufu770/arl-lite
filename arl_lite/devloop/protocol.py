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

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

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
            f"  items      : {stt.get('pending', 0)} pending / {stt.get('done', 0)} done / {stt.get('dropped', 0)} dropped",
        ]
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
        """PLAN:确保队列永远有下一步。"""
        self.store.set_phase(state, PHASE_PLAN)
        q = self.queue_mod.Queue(self.dev_dir / "queue.json")
        items = q.load()
        # 用同一实例的 items 判断,避免 load/seed 之间的对象分裂
        has_pending = any(i.status in ("pending", "in_progress") for i in items)
        if not has_pending:
            before = len(items)
            q.seed_if_empty()
            items = q.load()
            added = len(items) - before
            if added <= 0:
                return StepResult(
                    PHASE_PLAN, False,
                    "queue exhausted and seeding produced nothing — "
                    "the protocol has no next step; add items manually",
                )
            state.current_note = f"seeded {added} new item(s)"
            q.save(items)
            return StepResult(PHASE_PLAN, True, f"queue exhausted, seeded {added} new item(s)")
        nxt = next((i for i in items if i.status == "pending"), None)
        return StepResult(
            PHASE_PLAN, nxt is not None,
            f"next: {nxt.id} — {nxt.title}" if nxt else "queue has no pending item",
        )

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
