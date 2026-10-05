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

# baseline 里缺某个字段时,拿哪个源码常量当"当前值"来比。
#
# 只有在这里登记过的字段才能这样回落:登记的含义是"这个字段的判定
# 依据是这个常量",所以常量就是它事实上的当前值。r20 加 devloop_code_loc
# 是因为 r19 之后红线阈值来自 `_DEVELOOP_CODE_LOC_LIMIT`。
#
# 不登记的字段仍然按"缺失就不凭空造"处理 —— 那个保守规则只对
# 「本来就没有确定当前值」的字段有意义。
_SOURCE_DEFAULTS: dict = {}


def _register_source_defaults() -> None:
    """从 gates 读常量登记进回落表(避免 import 环)"""
    from . import gates as _g
    _SOURCE_DEFAULTS["devloop_code_loc"] = _g._DEVELOOP_CODE_LOC_LIMIT


_register_source_defaults()


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


def _headroom(measured: int, current: int) -> int:
    """提升后至少要留多少余量。

    ## 为什么不能提到正好等于实测值

    r20 第一次把红线提到 2488(实测值)之后,门禁立刻从红转绿,但
    `test_devloop_red_line_uses_code_lines` 的 `current < limit` 变成了
    `2488 < 2488` —— **零余量**。而这轮我自己又加了几行代码,门禁马上
    又红了。

    也就是说「提到实测值」这个动作本身几乎不解决问题:下一行代码就
    打回原形,等于逼着人每写几行就来提一次。那不是留痕机制,那是骚扰。

    所以留一点余量:向上取整到 50 的倍数,至少多给 25 行。
    够写几行正常改动,又不会把红线放飞到没有约束
    (`test_devloop_red_line_uses_code_lines` 守住余量 ≤35%)。
    """
    need = measured + 25
    return int((need + 49) // 50) * 50


def _plan_changes(
    gate_name: str,
    measured: Any,
    current: Any,
    allowed_fields: tuple,
) -> dict:
    """算出该把哪些字段从 current 抬到 measured。

    只升不降;只动 allowed_fields 里的字段。

    ## baseline 里缺字段时怎么办

    原规则是"缺失的字段不凭空造"。r20 遇到一个具体问题:红线字段
    `devloop_code_loc` 此前**从来不在 baseline 里**——它一直是源码常量
    `_DEVELOOP_CODE_LOC_LIMIT`(r11 定的不可提升路径)。于是第一次提升时,
    `current.get(key)` 返回 None,按旧规则直接跳过,门禁纹丝不动,
    而 CLI 却报了 ACCEPTED。

    那是比"提升失败"更糟的结果:**它说成功了,但什么都没发生。**

    所以改成:字段在 baseline 里缺失、但**在源码里有对应常量**时,
    拿常量当当前值来比。这样"从 2400 提到 2481"是一条真实可记录的
    变化,而不是凭空造一个数。

    仍然拒绝的是:两边都没有数字(纯靠 measured 凭空造值)。
    """
    changes: dict = {}
    if isinstance(current, dict) and isinstance(measured, dict):
        # r27:只提升**导致门禁转红**的那些字段。
        #
        # 以前这里是"遍历所有 promotable_fields,谁大提谁"。实测(r27):
        # 为修 test_baseline 要给 devloop_code_loc 留痕提升,accept 顺手把
        # total_loc 也从 14254 提到了 14514 —— 而 total_loc 当时**并没有红**
        # (14514 < 上限 14554)。规则 1 挡的是"提前买预算",可一旦有别的字段
        # 真的红了,闸门一过就顺带把没红的也买了,等于绕过了自己。
        #
        # 门禁在 measured["_over"] 里报出真正超标的字段名。没有这个键的
        # 门禁保持旧行为(全提)—— 那不是放过,是不该由这里替它们做判断。
        over = measured.get("_over")
        fields = [k for k in allowed_fields if over is None or k in over]
        for key in fields:
            old = current.get(key)
            new = measured.get(key)
            if old is None:
                # baseline 里没有 → 回落到源码常量(若该常量是这次的判定依据)
                old = _SOURCE_DEFAULTS.get(key)
            # 两边都得是数字才有"大小"可言;字符串/None/bool 一律不动
            if not isinstance(old, (int, float)) or isinstance(old, bool):
                continue
            if not isinstance(new, (int, float)) or isinstance(new, bool):
                continue
            if new > old:
                changes[key] = (old, new)
                # 补余量:提到实测值等于零余量,下一行代码又红
                if key in _SOURCE_DEFAULTS:
                    padded = _headroom(int(new), int(old))
                    if padded > new:
                        changes[key] = (old, padded)
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
