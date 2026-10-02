"""arl_lite.devloop — 自持迭代协议

一个带状态、带门禁、带退路的"构建→测试→改进→规划"闭环。

设计目标:让 arl-lite 能一直改进下去,每轮都有可验证的增量,
且任何时刻都存在明确的下一步。

核心不变式(protocol.py 会强制检查):
1. 每轮必须产出至少一个可验证增量(有 gate 跑过)
2. 门禁失败必须降级而不是掩盖(状态写 DONE_WITH_FAILURES)
3. 连续 N 轮无净增量 → 自动进入 RETREAT(退路)
4. 待办队列永远非空(耗尽时从 backlog.md 补充)
5. 状态落盘原子化,崩溃后可恢复

多 agent 并行下追加的不变式(第 15 轮):

6. 认领唯一:同一时刻一条待办只能被一个 agent 持有
7. **活着的认领不被轮次抹掉** —— in_progress 不等于上轮残留,
   复位必须是认领感知的(见 queue.Queue.is_stale_claim)
8. 读-改-写整段进临界区:load/save 各自原子,组合起来不原子
   (queue.Queue.claim/release/finish/recover_stale 是唯一正确入口)

第 6-8 条都是"看起来能跑、并发下静默丢东西"的那一类 ——
不报错,不告警,只是让两个 agent 改了同一处代码。

零依赖:只用 stdlib。
"""
from __future__ import annotations

__all__ = ["protocol", "gates", "queue", "state", "config"]
