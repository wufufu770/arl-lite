"""arl_lite.devloop.accept — baseline 提升:门禁失败的第一类解法

## 为什么需要这个模块

`loc_budget` 之类的门禁拿**版本库里的 baseline** 当参照。一旦某轮真的做完了
实打实的新功能(比如 TLS 证书采集,净增 540 行),门禁会红。这时候操作者只有
两条路,两条都不好:

1. 手改 `devloop/baselines.json` —— 历史里看不见是谁在什么时候因为什么放宽的,
   和"偷偷放宽阈值"只有操作习惯上的区别,没有机制上的区别。
2. 硬拆模块把行数压回去 —— 为了凑一个数字去扭曲代码结构。

第 1 条直接违反协议自己的不变式(门禁失败必须可见),第 2 条让门禁反过来
支配设计。两条路都会让循环**卡死或变质**,而"永远有下一步"是协议的第一目标。

所以提升必须是一等操作:显式命令、强制理由、留痕。

## 三条硬规则

1. **门禁必须正在失败** —— 已经绿的门禁不接受提升。
   堵住"提前买预算":先涨基准再写代码,等于给自己批空白支票。
2. **必须给理由,最少 10 个字符** —— 没有理由的放宽就是偷偷放宽。
3. **只升不降,且只升 gate 自己点头的字段** —— 别的字段(比如 devloop 自身的
   膨胀红线)碰都不碰。

## 留痕写进版本库

提升记录追加到 `baselines.json` 的 `_promotions` 列表里,而不是只写本地
state.json——因为 state.json 每机一份、不进版本库,那里的痕迹没人 review 得到。
写进 baselines.json 意味着每一条放宽都会出现在 `git diff` 和 code review 里。

历史记录只增不删:不能把不体面的旧记录删掉,那样这个功能就白做了。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import gates

# baselines.json 里存放提升记录的键。它不是 gate 名,门禁不会去读它。
PROMOTIONS_KEY = "_promotions"

# 理由最少多少字符。太短的("fix"、"ok")挡不住事后无脑复用。
MIN_REASON_LEN = 10

# 留多少条历史。留太多 baselines.json 会变成日志文件,
# 留太少又会把不体面的记录挤掉——所以取一个「够回忆,不至于刷屏」的值。
MAX_PROMOTIONS_KEPT = 50


@dataclass(frozen=True)
class AcceptOutcome:
    """一次提升尝试的结果

    Attributes:
        ok: 是否真的写进去了
        gate: 门禁名
        detail: 给人看的一句话(拒绝时说明拒绝理由)
        changes: 字段级变化 {field: (old, new)};拒绝时为空
        reason: 操作者给的理由
        recorded: 是否已追加到 _promotions
    """

    ok: bool
    gate: str
    detail: str
    changes: dict = field(default_factory=dict)
    reason: str = ""
    recorded: bool = False

    def summary(self) -> str:
        if not self.ok:
            return f"REFUSED {self.gate}: {self.detail}"
        moved = ", ".join(f"{k}: {o} -> {n}" for k, (o, n) in self.changes.items())
        return f"ACCEPTED {self.gate}: {moved}\n  reason: {self.reason}"


def _plan_changes(
    gate_name: str,
    measured: Any,
    current: Any,
    allowed_fields: tuple,
) -> dict:
    """算出该把哪些字段从 current 抬到 measured。

    只升不降;只动 allowed_fields 里的字段;缺失的字段不凭空造。
    """
    changes: dict = {}
    if isinstance(current, dict) and isinstance(measured, dict):
        for key in allowed_fields:
            old = current.get(key)
            new = measured.get(key)
            # 两边都得是数字才有"大小"可言;字符串/None 一律不动
            if not isinstance(old, (int, float)) or isinstance(old, bool):
                continue
            if not isinstance(new, (int, float)) or isinstance(new, bool):
                continue
            if new > old:
                changes[key] = (old, new)
    else:
        # 标量 baseline:promotable_fields 为空即表示"整个标量可升"
        if allowed_fields:
            return changes
        if isinstance(current, (int, float)) and not isinstance(current, bool):
            if isinstance(measured, (int, float)) and not isinstance(measured, bool):
                if measured > current:
                    changes["value"] = (current, measured)
    return changes


def _apply(repo: Path, gate_name: str, changes: dict) -> None:
    data = gates.load_baseline(repo)
    cur = data.get(gate_name)
    if isinstance(cur, dict):
        for k, (_old, new) in changes.items():
            cur[k] = new
    else:
        # 标量:用唯一那个字段的新值
        data[gate_name] = next(iter(changes.values()))[1]
    gates.save_baseline(repo, data)


def _record(repo: Path, gate_name: str, changes: dict, reason: str, round_no: int) -> None:
    data = gates.load_baseline(repo)
    hist = data.get(PROMOTIONS_KEY)
    if not isinstance(hist, list):
        hist = []
    hist.append({
        "gate": gate_name,
        "round": round_no,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "changes": {k: {"from": o, "to": n} for k, (o, n) in changes.items()},
        "reason": reason,
    })
    data[PROMOTIONS_KEY] = hist[-MAX_PROMOTIONS_KEPT:]
    gates.save_baseline(repo, data)


def accept_baseline(
    repo: Path,
    gate_name: str,
    reason: str,
    round_no: int | None = None,
) -> AcceptOutcome:
    """把某个门禁的 baseline 提升到当前实测值。

    Args:
        repo: 项目根目录
        gate_name: 门禁名
        reason: 为什么可以放宽(必填,会被记进版本库)
        round_no: 关联的 devloop 轮次;None 时取当前状态

    Returns:
        AcceptOutcome——ok=False 表示拒绝,detail 说明原因。
        拒绝**不是异常**:操作者需要看到理由,不是 traceback。
    """
    try:
        gate = gates.get_gate(gate_name)
    except KeyError as e:
        return AcceptOutcome(False, gate_name, str(e))

    # 规则 2:理由先查——省得白跑一遍慢门禁
    if not reason or not reason.strip():
        return AcceptOutcome(False, gate_name, "reason is required; refusing to widen silently")
    if len(reason.strip()) < MIN_REASON_LEN:
        return AcceptOutcome(
            False, gate_name,
            f"reason too short ({len(reason.strip())} chars, need >= {MIN_REASON_LEN})",
            reason=reason,
        )

    if not getattr(gate, "promotable", False):
        return AcceptOutcome(
            False, gate_name,
            "gate is not promotable; fixing the finding is the only way through "
            "(this gate's threshold is a red line, not a moving budget)",
            reason=reason,
        )

    result = gate.run(Path(repo))

    # 规则 1:只给正在失败的门禁开提升口子
    if result.passed:
        return AcceptOutcome(
            False, gate_name,
            f"gate is already green ({result.detail}); refusing to pre-buy headroom",
            reason=reason,
        )

    current = gates.load_baseline(repo).get(gate_name)
    allowed = tuple(getattr(gate, "promotable_fields", ()) or ())
    changes = _plan_changes(gate_name, result.measured, current, allowed)
    if not changes:
        return AcceptOutcome(
            False, gate_name,
            f"nothing to promote (baseline={current!r}, measured={result.measured!r}, "
            f"allowed fields={list(allowed) or 'all'})",
            reason=reason,
        )

    if round_no is None:
        from .state import StateStore
        round_no = StateStore(Path(repo) / "devloop" / "state.json").load().round

    _apply(Path(repo), gate_name, changes)
    _record(Path(repo), gate_name, changes, reason.strip(), round_no)
    return AcceptOutcome(
        True, gate_name,
        "baseline promoted and recorded in devloop/baselines.json",
        changes=changes,
        reason=reason.strip(),
        recorded=True,
    )


def promotion_history(repo: Path) -> list[dict]:
    """读回提升历史(给 CLI 展示)"""
    hist = gates.load_baseline(repo).get(PROMOTIONS_KEY)
    return hist if isinstance(hist, list) else []


def format_history(repo: Path) -> str:
    """把提升历史渲染成给人看的几行"""
    hist = promotion_history(repo)
    if not hist:
        return "(no baseline promotions yet)"
    lines = []
    for h in hist[-MAX_PROMOTIONS_KEPT:]:
        moved = ", ".join(
            f"{k} {v.get('from')}->{v.get('to')}"
            for k, v in (h.get("changes") or {}).items()
        )
        lines.append(f"r{str(h.get('round', '?')).ljust(3)} {h.get('at', '?')}  {h.get('gate')}: {moved}")
        lines.append(f"      reason: {h.get('reason', '')}")
    return "\n".join(lines)
