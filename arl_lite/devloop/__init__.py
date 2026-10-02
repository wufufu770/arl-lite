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

零依赖:只用 stdlib。
"""
from __future__ import annotations

__all__ = ["protocol", "gates", "queue", "state", "config"]
