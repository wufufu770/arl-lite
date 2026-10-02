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
import os
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

        # r30:只提交这两个字段。原来是 `self.store.save(state)` —— 整份
        # 旧快照写回,会把并发的另一个 round 盖掉。
        def _apply(st) -> None:
            st.current_item = item.id
            st.current_note = item.title

        self.store.mutate(_apply)
        state.current_item = item.id
        state.current_note = item.title

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
            added = q.seed_if_empty(round_no=state.round + 1)
            items = q.load()
            if added <= 0:
                return StepResult(
                    PHASE_PLAN, False,
                    "queue exhausted and seeding produced nothing — "
                    "the protocol has no next step; add items manually",
                )
            # r31:这行原来写的是 q.save(items) —— 改的是 state.current_note,
            # 保存的却是**原封不动**的队列。一次无锁、无意义、但确实写盘的
            # save,像是早期把 self.store.save 写错了对象留下的。删掉。
            note_text = f"seeded {added} new item(s)"
            self.store.mutate(lambda st: setattr(st, "current_note", note_text))
            state.current_note = note_text
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
        added = q.seed_if_empty(round_no=state.round + 1)
        if added <= 0:
            return 0, (
                "queue exhausted and seeding produced nothing — "
                "the protocol has no next step; add items to devloop/backlog.md"
            )
        # r30:只把 current_note 这一个字段落盘。原来是 `self.store.save(state)`
        # —— 整份旧快照写回,会把并发的另一个 round 盖掉。
        note_text = f"seeded {added} new item(s) after round"
        self.store.mutate(lambda st: setattr(st, "current_note", note_text))
        state.current_note = note_text
        return added, note_text

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

    @staticmethod
    def _check_explicit_item(q, item_id, record):
        """显式指定的待办必须**当前可做**,否则直接拒绝(r28)

        只做校验和记账,不负责认领 —— 认领由带锁的 `Queue.claim()` 完成
        (r31)。这两件事必须分开:校验是"要不要做这一条"的判断,
        认领是"把它从 pending 变成 in_progress"的动作,后者必须原子。

        对一条已经 done 的条目记"本轮完成了它",是本协议见过最恶劣的
        一类假账(7.17 / 7.18 各记了一例同族症状)。
        """
        if item_id:
            found = next((i for i in q.load() if i.id == item_id), None)
            if found is None:
                raise LookupError(f"no such item in queue: {item_id!r}")
            if found.status not in ("pending", "in_progress"):
                raise ValueError(
                    f"item {item_id!r} is {found.status!r}, not workable; "
                    f"refusing to record a round against it"
                )
            record.item_selection = "explicit"
        else:
            record.item_selection = "auto"

    @staticmethod
    def _owner() -> str:
        """本轮的认领者标识。

        r31 之前 round 认领**不记 owner**,于是 `recover_stale_in_progress`
        只能报「认领者无法探测」,退路判断被迫靠"认领多久了"这种猜测。
        """
        return f"round-pid-{os.getpid()}"

    def round(
        self,
        only_gates: list[str] | None = None,
        build: Callable | None = None,
        item_id: str | None = None,
    ) -> RoundOutcome:
        """跑一整轮:BUILD → TEST → IMPROVE → PLAN → 落盘。

        Args:
            only_gates: 只跑这些门禁;None = 全跑
            build: BUILD 回调;None 表示"由人做",引擎只验门禁
            item_id: **本轮实际做完的是哪一条**(r28 新增)

        ## 为什么要有 item_id

        原来这个函数只按优先级取队列首项,然后在门禁全绿时把它标成 done。
        可是执行者做完的往往是另一条 —— r27 实测:做完三条真活,round 记的
        却是第四条,而且那条**没人做过**,引擎照样把它标成 done。

        光有 `completion_source` 挡不住:它只说明"done 由谁断言",
        说明不了"人工做的是不是这一条"。所以两项都得记
        (`completion_source` + `item_selection`),假账才露得出来。

        显式指一条**当前不可做**的条目(已 done / dropped)会直接拒绝:
        记一笔"完成了它"的假账,比拒绝更坏。
        """
        state = self.store.load()
        # r30:本轮新增日志从这条水位线开始划。commit_round 靠它把
        # `state.gate_log[log_mark:]` 重放到最新状态上 —— 不划这一刀,
        # 连历史日志都会再追加一遍。
        log_mark = len(state.gate_log)
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
            self.store.commit_round(state, record, done_delta=0, log_mark=log_mark,
                                    retreat_delta=1)
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

        # r35:队列在轮初为空时,播种是给**下一轮**备的料,本轮不能领。
        #
        # 原来这里是 seed 完直接往下走 claim,于是刚播下的条目立刻成为
        # 队首、被同一轮 finish 掉。r34 实测造出一条假 done:
        # 23:51:09 seed +1 → 23:53:22 round DONE,中间那 132 秒跑的是
        # 门禁,不是那条活的实现,而记录写着"本轮完成了它"。
        # 引擎能自己造一条活再自己宣布做完 —— 队列每次耗尽都会发生,
        # 是结构性缺陷不是偶发。
        #
        # 改成"轮初没活就如实空转一轮":播种照做(不变式 #4 要求队列
        # 永远非空),但 item 留 None,让既有的 RESULT_NOOP 分支记账。
        # 空转一轮是诚实的,假完成不是。
        seeded_this_round = 0
        if not any(i.status in ("pending", "in_progress") for i in items):
            before = len(items)
            q.seed_if_empty(round_no=state.round + 1)
            items = q.load()
            seeded_this_round = len(items) - before
            if seeded_this_round > 0:
                messages.append(
                    f"queue was empty at round start; seeded "
                    f"{seeded_this_round} item(s) for the **next** round — "
                    f"this round does no work (r35: a freshly seeded item "
                    f"cannot be completed in the round that created it)"
                )

        # 显式指定的校验**无条件**先跑。它不依赖队列非空,而是问
        # "你点名的那条到底能不能做" —— 跳过它会让本轮静默忽略操作者的
        # 显式指令,那是比报错更坏的失败(人以为在点 A,引擎在干 B)。
        # 顺带保证了:点名一条 pending 时队列必然非空,不会走到下面
        # 的 seeded 分支,所以"跳过 claim"永远不会顶掉一个合法请求。
        self._check_explicit_item(q, item_id, record)

        # r35:本轮播种的条目一律不领。
        if seeded_this_round:
            item = None
        else:
            # r31:走带锁的 `q.claim()`,不再自己 load→改→save。
            # claim 在一把锁里完成 load→挑→标 in_progress→save,所以两个并发
            # round 拿到的一定是**不同的条目**;而裸 load→save 时它们可能同时
            # 挑中同一条,然后互相把对方的 status 改回去。
            item = q.claim(owner=self._owner(), item_id=item_id)
            if item is None and item_id:
                raise LookupError(
                    f"item {item_id!r} 校验之后变得不可认领了"
                    f"(可能被别的 agent 领走或完成);拒绝拿别的条目记账"
                )
        if item is not None:
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
        is_signal = item is not None and self.queue_mod.is_signal_id(item.id)
        if builder is not None:
            record.completion_source = "build_fn"
        elif is_signal:
            # 信号不是工作。标成 operator 等于让 history 声称
            # "完成了一项任务",而实际一行代码都没动。
            record.completion_source = "signal_ack"
        else:
            record.completion_source = "operator"
        if item is not None and builder is None:
            what = "复查信号的关闭" if is_signal else "完成"
            messages.append(
                f"no build executor: '{item.id}' 的{what}由人工断言,"
                f"引擎只验证了门禁"
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
        #
        #     r30:`items_done` 不在这里 += 1。调用方手里的 state 是轮次开始
        #     时的旧快照,把它写回去就是 lost update。改成传**增量**给
        #     commit_round,由它在锁内基于刚读到的状态累加。
        done_delta = 0
        if item is None:
            record.result = RESULT_NOOP
            record.note = "no pending item"
        else:
            # r35:最后一道闸门 —— 条目自己的验收命令现在过不过?
            #
            # 这道闸门是 r34 那条假 done 逼出来的。r34 全部门禁 6/0/0、
            # 487 条测试全过,却把一条 `pytest tests/没写的文件.py`
            # 标成了 done:**没有哪道门禁看"刚被完成的条目,它自己的
            # verify 现在过不过"**。门禁查的是代码的性质,不是这一轮
            # 的记账是否属实。
            #
            # 只在 `fail`(确认是检查命令、确认跑得起来、确认没过)时拦。
            # `unknown`(散文 verify)不拦 —— 拿散文去挡完成,会把所有人
            # 写的待办永久卡死,那道闸门活不过三轮就会被拆掉。
            vr = q.verify_result(item.verify) if t.ok else "unknown"
            # r37:`verify="true"` 是恒真命令,信号之外不许拿它当验收。
            #
            # r36 实测的漏洞:塞一条 `verify="true"` 的**真活**进队列,
            # 跑完整 round —— 它被标成 done,note 写着 "done r1 ·
            # source=operator"。而那行代码一行没写。
            #
            # 为什么这个洞特别难堵:上一行 `vr` 判的是 `pass` 不是 `fail`,
            # 所以 r35 那道闸门**放行**;r36 写的
            # `test_backlog_verify_is_commandable.py` 也**抓不到**它 ——
            # `true` 确实在白名单里、确实是合法命令、确实语法正确。
            # 判据恒真是第 16 轮「恒真测试」那篇的极端形式:这次连
            # 测试都绿着。
            #
            # 信号条目(`no-due-maintenance-review*`)用它完全合法 —— 它的
            # 完成判据本来就是「人确认过」,不是某条命令。所以按 id 分流,
            # 不按 `verify` 的内容分流。
            # 恒真与否是 verify **内容**的性质,和机器能不能评它无关。
            # 早一版写成 `vr == VERIFY_PASS and is_constant_true(...)`,
            # 结果 `:` 漏网 —— 它首词不在白名单里,`vr` 是 `unknown`,
            # 于是恒真判断被短路掉了。而 `:` 恰恰是最该拦的那种。
            vacuous = (t.ok and not is_signal
                       and q.is_constant_true(item.verify))
            if t.ok and (vr == q.VERIFY_FAIL or vacuous):
                why = ("its own verify does not pass"
                       if not vacuous else
                       "its verify is the constant command `true`, which "
                       "cannot distinguish done from not-done")
                record.result = RESULT_DONE_WITH_FAILURES
                record.blocking_failures.append(
                    f"verify_failed: {item.verify}" if not vacuous
                    else f"vacuous_verify: {item.verify}")
                q.release(item.id, note=f"refused done: {why} -> {item.verify}")
                messages.append(
                    f"refused to mark {item.id!r} done: {why} ({item.verify}). "
                    f"门禁全绿不等于这条活做完了 —— 门禁查代码,"
                    f"这条查的是记账是否属实。信号条目可以用 `true`,"
                    f"真活不行:恒真的判据回答不了「做完没有」。"
                )
            elif t.ok:
                record.result = RESULT_DONE
                # r31:带锁的收尾入口,顺带清掉 owner/claimed_at。
                # 原来在裸 items 上 mark_done 完再 q.save(items) 整份写回。
                # r34:把"谁断言的完成"也写进队列记录。state.json 里的
                # completion_source 只覆盖最近几轮,队列里这条 done 却是
                # 长期留存的审计凭据 —— 它空着,就分不出引擎写的和
                # 人手编的假账(r33 在 confidence-risk 上栽的就是这个)。
                q.finish(item.id, ok=True, round_no=state.round + 1,
                         note=f"done r{state.round + 1} · "
                              f"source={record.completion_source}")
                done_delta = 1
            else:
                # 门禁没过 = 失败,如实记录(不退回去假装没事)
                record.result = RESULT_DONE_WITH_FAILURES
                # 退回 pending + 记下是哪几道门没过。attempts 已在 claim
                # 时 +1,连续失败会被累加,识别反复卡住的待办。
                q.release(item.id, note=f"gates failed: "
                                        f"{', '.join(record.blocking_failures)}")

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
        # 拿回锁内重放后的最新状态,否则 round 结束时 state.round 仍是
        # 旧值(commit_round 现在是在最新状态上 +1,不再回写调用方那份)
        state = self.store.commit_round(state, record, done_delta, log_mark)

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
