"""arl_lite.devloop.state — 循环状态落盘

状态是循环的记忆。丢了就等于从头开始,所以:
- 原子写(tmp + os.replace):崩溃不留半截文件
- **每次写用独立的 tmp**(r29):共用一个 tmp 名时,并发会把半截内容
  当成正式文件,实测 1200 次写里 10+ 次状态被读成损坏
- 读-改-写整段进临界区(`mutate`):save 原子不等于组合起来原子,
  两个 agent 各自 load→save 会整段覆盖(实测 1200 次只剩 14 条历史)
- 显式 schema 版本:格式演进时可迁移
- 历史有上限:防止无限增长

零依赖:只用 stdlib。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .lock import file_lock

SCHEMA_VERSION = 1

# 历史保留上限——状态文件不是日志,只保留最近的
MAX_HISTORY = 50
MAX_GATE_LOG = 200

# 循环阶段(状态机的状态)
PHASE_IDLE = "IDLE"
PHASE_BUILD = "BUILD"
PHASE_TEST = "TEST"
PHASE_IMPROVE = "IMPROVE"
PHASE_PLAN = "PLAN"
PHASE_RETREAT = "RETREAT"

VALID_PHASES = {
    PHASE_IDLE, PHASE_BUILD, PHASE_TEST,
    PHASE_IMPROVE, PHASE_PLAN, PHASE_RETREAT,
}

# 轮次结果
RESULT_DONE = "DONE"
RESULT_DONE_WITH_FAILURES = "DONE_WITH_FAILURES"   # 完成了但门禁有失败——必须显式记录,不能吞
RESULT_NOOP = "NOOP"                                # 没找到可做的事
RESULT_RETREATED = "RETREATED"                      # 触发退路


@dataclass
class RoundRecord:
    """一轮的完整记录——循环的最小可追溯单元"""
    round: int = 0
    phase: str = PHASE_IDLE
    result: str = RESULT_NOOP
    item_id: str = ""
    item_title: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    gates_passed: int = 0
    gates_failed: int = 0
    gates_warned: int = 0
    blocking_failures: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    # 这一轮的 done 是**谁**断言的。
    #   "build_fn"     —— 注入了 build 回调,引擎问过执行者,它说做完了
    #   "operator"     —— 纯人工:门禁绿了就标 done,引擎无从核实
    #   "signal_ack"   —— 本轮处理的是复查信号(见 queue._SIGNAL_ID)。
    #                     它**不是工作**,只是"确认眼下没有到期维护项"。
    #                     混进 "operator" 就等于让 history 声称"完成了一项
    #                     任务",而实际上一行代码都没动。
    #
    # 为什么必须记:引擎的设计是"不自动改代码",没有 build_fn 时它只能
    # 看到"门禁全绿",看不到"活到底干了没有"。实测里多次出现门禁全绿但
    # 那一轮其实没做队列里那条待办的情况(误报率实测被连标两次 done)。
    # 不区分这两种 done,状态文件就是在替执行者背书它没做过的事。
    completion_source: str = "operator"

    # 本轮的 item_id 是**谁**定的。
    #   "explicit" —— 执行者用 item_id= 明确声明"我这轮做的是这条"
    #   "auto"     —— 引擎按优先级自己挑的队列首项
    #
    # 为什么必须记(r28):`round` 原来根本没有 item_id 参数,永远取队列
    # 首项。于是执行者做完的是另一条,记录里 item_id 指向的却是一条
    # **没人做过**的待办 —— 而且引擎顺手把它标成了 done。
    # 光有 completion_source 还不够:"operator" 只能说明"由人工断言",
    # 说明不了"人工做的是不是这一条"。两件事都记,假账才露得出来。
    item_selection: str = "auto"

    @property
    def duration(self) -> float:
        if not self.started_at or not self.finished_at:
            return 0.0
        return self.finished_at - self.started_at

    def to_dict(self) -> dict:
        d = asdict(self)
        d["duration"] = round(self.duration, 2)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "RoundRecord":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class LoopState:
    """整个循环的持久状态"""
    version: int = SCHEMA_VERSION
    round: int = 0                    # 已完成的轮次
    phase: str = PHASE_IDLE
    result: str = RESULT_NOOP
    current_item: str = ""            # 正在处理的待办 id
    current_note: str = ""
    started_at: float = 0.0           # 协议首次启动时间
    updated_at: float = 0.0

    # 退路机制:连续无净增量的轮数
    barren_rounds: int = 0
    retreats: int = 0
    # r43:barren 改成下一轮开始时补判,这个字段记「已经补判到哪一轮」,
    # 防止同一个轮次被重复计数(每轮都判一次的话计数会飞)。
    barren_judged_round: int = 0

    # 累计指标
    total_gates_passed: int = 0
    total_gates_failed: int = 0
    items_done: int = 0
    items_dropped: int = 0

    # 最近几轮(有上限)
    history: list[RoundRecord] = field(default_factory=list)

    # 门禁执行日志
    gate_log: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "round": self.round,
            "phase": self.phase,
            "result": self.result,
            "current_item": self.current_item,
            "current_note": self.current_note,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "barren_rounds": self.barren_rounds,
            "barren_judged_round": self.barren_judged_round,
            "retreats": self.retreats,
            "total_gates_passed": self.total_gates_passed,
            "total_gates_failed": self.total_gates_failed,
            "items_done": self.items_done,
            "items_dropped": self.items_dropped,
            "history": [r.to_dict() for r in self.history],
            "gate_log": self.gate_log[-MAX_GATE_LOG:],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LoopState":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        st = cls(**known)
        st.history = [RoundRecord.from_dict(r) for r in d.get("history", [])]
        st.gate_log = list(d.get("gate_log", []))
        return st


class StateStore:
    """状态的读写。原子写,崩溃可恢复。"""

    def __init__(self, path: Path):
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> LoopState:
        """读状态。文件不存在/损坏都返回全新状态——循环必须能自愈启动。"""
        if not self.path.exists():
            st = LoopState(started_at=time.time(), updated_at=time.time())
            return st
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            # 状态文件坏了不能卡死循环:备份后重建
            try:
                bak = self.path.with_suffix(".corrupt")
                self.path.rename(bak)
                print(f"[!] state file corrupt, backed up to {bak.name}: {e}")
            except OSError:
                pass
            return LoopState(started_at=time.time(), updated_at=time.time())

        version = raw.get("version", 0)
        if version > SCHEMA_VERSION:
            # 状态文件来自更新的版本,不猜,保守处理
            print(
                f"[!] state schema v{version} > supported v{SCHEMA_VERSION}, "
                f"starting fresh (old file kept)"
            )
            return LoopState(started_at=time.time(), updated_at=time.time())
        return LoopState.from_dict(raw)

    def save(self, state: LoopState) -> None:
        """原子写。

        ## 这里**故意不取锁**(r29)

        上一版给 `save` 套了 `file_lock`,理由是"并发要加锁"。变异测试
        把这把锁去掉,4 条并发测试**全部照样通过** —— 它不增加任何保证:

        - `os.replace` 本身已经是原子的,读者要么看到旧文件要么看到新文件;
        - tmp 名已经每次唯一,两个写者根本碰不到同一个文件。

        多余的锁不是"稳妥",是每次写都白等一次、还让人以为
        "有锁就安全了",从而写出裸 `load` → 改 → `save` 的代码 ——
        那才是真正丢数据的地方,锁救不了。`mutate()` 才是那把锁该在的地方。
        """
        self._write(state)

    def _write(self, state: LoopState) -> None:
        """真写。tmp 名每次唯一 —— 这是并发安全的关键,不是锁。

        r29 实测(8 进程 x 150 次 = 1200 次,旧实现用固定的 `state.json.tmp`):
        741 次 `os.replace` 找不到 tmp(被别的进程搬走了),10+ 次把半截或
        交错的内容当成正式文件读回来。

        病根就一个:**tmp 名固定**。A 写了一半,B 覆盖,A 把 B 的半截
        rename 上去,B 再 rename 时文件已经不在了。tmp 名带上 pid + 随机
        后缀,每次写各占各的,谁也碰不到谁。
        """
        state.updated_at = time.time()
        state.history = state.history[-MAX_HISTORY:]
        state.gate_log = state.gate_log[-MAX_GATE_LOG:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
        tmp = self.path.with_suffix(
            f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.path)  # 原子替换
        except OSError:
            # 失败时别把半截文件留在盘上冒充状态
            tmp.unlink(missing_ok=True)
            raise

    def mutate(self, fn):
        """读-改-写**整段**进临界区,然后落盘。返回落盘后的状态。

        ## 为什么光给 save 加锁不够

        `save` 原子,只保证"每次写不撕裂"。它不保证"读到的和写回的是
        同一份"——两个 agent 各自 `load` → 改 → `save` 时,后写的那个
        拿的是**旧快照**,会把先写的整个覆盖掉。

        r29 实测:8 进程 x 150 次,1200 次写最后只剩 14 条历史。
        丢的不是"撕裂",是"整段被覆盖",所以那次实验里状态文件看着
        完好,数据却没了大半。

        不变式 #8 写的就是这件事:「读-改-写整段进临界区,
        load/save 各自原子,组合起来不原子」。

        ## 锁不跨门禁

        `round()` 跑门禁要一分多钟,整轮持锁会把并发的 agent 堵到
        锁超时。所以长耗时段必须在临界区**之外**,只把"读→改→写"这三步
        收进来 —— 那才是会互相踩的部分。
        """
        with file_lock(self.path):
            st = self.load()
            fn(st)
            self._write(st)
            return st

    # ---- 阶段迁移 ----

    def set_phase(self, state: LoopState, phase: str) -> None:
        """切阶段。**只把 phase 这一个字段落盘**(r30)。

        原来是把调用方手上的整份 state 写回去。`round()` 一轮要切四次
        阶段(BUILD/TEST/IMPROVE/PLAN),每一次都是一次"用旧快照覆盖
        整个文件" —— 两个 agent 并发跑 round 时,后进入阶段的那个会把
        先进入的 round 号、history、计数全部盖掉。

        语义因此变成:调用方手里的 `state` 是**本轮的工作区**,
        落盘由 `commit_round` 统一提交;`set_phase` 只负责把一个字段
        同步到盘上,并顺手更新调用方那份,免得两边显示不一致。
        """
        if phase not in VALID_PHASES:
            raise ValueError(f"invalid phase: {phase!r} (valid: {sorted(VALID_PHASES)})")

        def _apply(st: LoopState) -> None:
            st.phase = phase

        self.mutate(_apply)
        state.phase = phase

    def commit_round(self, state: LoopState, record: RoundRecord,
                     done_delta: int = 0, log_mark: int = 0,
                     retreat_delta: int = 0, barren_delta: int = 0,
                     barren_reset: bool = False,
                     barren_judged_round: int = 0) -> LoopState:
        """结束一轮:累加指标、记历史、判退路。

        ## r30:在**刚读到的**状态上重放,而不是写回调用方那份旧快照

        原来最后一句是 `self.save(state)` —— 把调用方在轮次开始时
        `load()` 到的快照整个写回。两个 agent 并发跑 round 时,两边读到
        的是同一个 round 号,后写的把先写的 round 计数、history、累计
        门禁数全盖掉。那不是"重复记录",是纯粹的丢失。

        这里的操作全是加法/追加,在最新状态上重放是安全的:
        计数 `+=`、history `append`、barren 判定只依赖本轮 record。
        `state.round` 也改成锁内 `+= 1`,所以两个并发 round 拿到的是
        连续的两个号,不会撞。

        ## 为什么要显式传这些增量

        `phase_retreat` 里的 `state.retreats += 1` 以前是靠本方法最后那句
        `save(state)` **顺带**落盘的。改成在锁内只重放已知字段之后,
        那一笔就没人提交了 —— 所以必须显式传进来,不能指望"反正都会存"。

        Args:
            done_delta: 本轮新增的"完成"条数(0 或 1)。原来是在调用方那边
                `state.items_done += 1`,那是绝对值写回,并发下必丢。
            log_mark: 调用方 `load()` 之后 `state.gate_log` 的长度。
                `state.gate_log[log_mark:]` 才是**本轮**新增的日志 ——
                不划这一刀的话,连历史日志都会再追加一遍。
            retreat_delta: 本轮退路次数(0 或 1),同上。
            barren_delta: 上一轮补判出的 barren 增量(r43 新增)。
            barren_reset: 上一轮有产出,连续 barren 计数归零。
            barren_judged_round: 已经补判过的最后一轮,防止每轮重复计。
        """
        record.finished_at = time.time()
        pending_log = list(state.gate_log[log_mark:])

        def _apply(st: LoopState) -> None:
            st.round += 1
            record.round = st.round
            st.result = record.result
            st.total_gates_passed += record.gates_passed
            st.total_gates_failed += record.gates_failed
            st.items_done += done_delta
            st.retreats += retreat_delta
            st.history.append(record)
            # r43:barren 不在这里判了。
            # 原来这一行是 `result == NOOP 就 +1`,可窗口是在两轮**之间**
            # 闭合的 —— `devloop done-item` 交的活在落盘那一刻还没发生。
            # r43 实测 44 轮里 8 个 NOOP 轮有 7 个窗口内是有提交的,全被
            # 记成 barren,退路于是在有产出的连跑上触发(r42:那轮 0.0 秒,
            # 门禁都没跑,队列还是空的)。判定改由调用方在**下一轮**开始时
            # 补判,结果经这三个参数显式带进来 —— 和上面几个 delta 一样,
            # 不能指望"反正都会存"。
            if barren_reset:
                st.barren_rounds = 0
            else:
                st.barren_rounds = max(0, st.barren_rounds + barren_delta)
            st.barren_judged_round = max(st.barren_judged_round, barren_judged_round)
            st.phase = PHASE_IDLE
            st.gate_log = (st.gate_log + pending_log)[-MAX_GATE_LOG:]

        st = self.mutate(_apply)
        # 保住旧契约:commit_round 一直会就地更新调用方传进来的那个对象。
        # 把它变成"只返回一个新对象"是一次静默的行为变更 —— 有测试
        # (也确实有代码)直接 commit_round(s, rec) 然后就读 s.xxx。
        for fname in LoopState.__dataclass_fields__:
            setattr(state, fname, getattr(st, fname))
        return st
