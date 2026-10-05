"""arl_lite.core.risk_score

风险画像:基于 correlations 输出每个 asset 的 risk score。

设计:
- 每个 asset(按 target 聚合)汇总所有命中规则
- risk_score = max(命中规则的 risk) + bonus(命中数量)
- risk_level: low(0-3) / medium(4-6) / high(7-8) / critical(9-10)

输出:
- top_risks(workspace 内 top N 高风险资产)
- risk_summary(workspace 整体风险概览)
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

log = logging.getLogger("arl_lite.core.risk_score")


@dataclass
class AssetRisk:
    target: str
    target_type: str
    risk_score: int  # 0-10 + bonus
    risk_level: str  # low/medium/high/critical
    rule_count: int
    rule_names: list[str] = field(default_factory=list)
    headlines: list[str] = field(default_factory=list)


def _score_to_level(score: int) -> str:
    if score >= 9:
        return "critical"
    elif score >= 7:
        return "high"
    elif score >= 4:
        return "medium"
    return "low"


def counted_rows(storage) -> list[dict]:
    """取**参与风险计算**的关联行:排除 `confidence_status='discard'`

    ## 为什么抽出来而不是各写一遍

    r21 改了报告分流(r22 改了 `compute_asset_risks`),
    但 `risk_summary` 是第三处读 correlations 的地方 —— 它照样把
    discard 算进 `by_level` 和 `max_risk`。

    三处各写一遍判断,漏一处就会让 discard 从那个口子回来。
    **规则只该有一个实现。** 这和第 15 轮那条教训是同一条:
    同一个"读-改-写要加锁"的规则存在两个实现时,迟早有一处漏掉,
    而漏掉的那处不会报错,只会安静地给出错误的数。

    现在连**报告层**也走同一个判定(`confidence.is_discarded`)。
    之前报告层是自己写的内联判断,语义相同但没复用 ——
    于是"三个消费方口径一致"这句话当时是不成立的。

    判定口径集中在 `core/confidence.status_of`:
    - `discard` → 排除(模型判定这条不算数)
    - `observe` / `report` / 字段缺失 / 脏数据 → 计入
    """
    from .confidence import is_discarded
    rows = storage.query("correlations", limit=10000)
    return [r for r in rows if not is_discarded(r)]


def compute_asset_risks(storage) -> list[AssetRisk]:
    """从 correlations 表计算每个 asset 的风险评分

    ## 置信度与 risk 的关系(第 22 轮补)

    两者**正交**,各管一件事:

    - `confidence`(0-100 + report/observe/discard)决定**这条命中算不算数**
    - `risk`(0-10)决定**算数之后有多严重**

    所以 `discard` 判定的命中**不参与**风险聚合 —— 它已经被第 21 轮
    从报告主表里沉到折叠区了,如果这里还把它算进去,等于给用户看
    「这个资产风险 9 分」却不告诉他为什么报告里看不到对应条目。

    而 `observe` **要参与**:它是「算出来但不足以直接报」,不是
    「不算」。把它排除会低估真实风险 —— 那比高估更危险。

    早先这里只按 `risk` 聚合,`confidence_status` 完全没参与,
    于是上一轮的报告分流可以被这条路径绕过。
    """
    rows = counted_rows(storage)
    if not rows:
        return []

    # 按 target 聚合
    by_target: dict[str, dict] = defaultdict(lambda: {
        "target_type": "",
        "max_risk": 0,
        "rule_count": 0,
        "rule_names": [],
        "headlines": [],
    })
    for r in rows:
        target = r.get("target", "")
        if not target:
            continue
        entry = by_target[target]
        entry["target_type"] = r.get("target_type", "other")
        risk = r.get("risk", 0) or 0
        if risk > entry["max_risk"]:
            entry["max_risk"] = risk
        entry["rule_count"] += 1
        rname = r.get("rule_name", "")
        if rname and rname not in entry["rule_names"]:
            entry["rule_names"].append(rname)
        headline = r.get("headline", "")
        if headline and len(entry["headlines"]) < 5:
            entry["headlines"].append(headline)

    # 计算最终 score
    results: list[AssetRisk] = []
    for target, entry in by_target.items():
        # score = max risk + bonus for multiple rules
        bonus = min(2, (entry["rule_count"] - 1))  # 多规则加 1-2 分
        score = min(10, entry["max_risk"] + bonus)
        results.append(AssetRisk(
            target=target,
            target_type=entry["target_type"],
            risk_score=score,
            risk_level=_score_to_level(score),
            rule_count=entry["rule_count"],
            rule_names=entry["rule_names"][:5],
            headlines=entry["headlines"],
        ))

    # 按 risk_score 降序
    results.sort(key=lambda r: -r.risk_score)
    return results


def risk_summary(storage) -> dict:
    """workspace 整体风险概览

    走 `counted_rows()` —— 和 `compute_asset_risks` 同一个口径。
    早先这里直接 `storage.query("correlations")`,于是 discard 的命中
    照样进 `by_level` 和 `max_risk`:报告主表里已经看不到它了,
    概览却还按它算风险,用户对不上账。
    """
    rows = counted_rows(storage)
    by_level = {"low": 0, "medium": 0, "high": 0, "critical": 0}
    for r in rows:
        score = r.get("risk", 0) or 0
        level = _score_to_level(score)
        by_level[level] += 1
    total_all = len(storage.query("correlations", limit=10000))
    return {
        "total_correlations": len(rows),
        "discarded_correlations": total_all - len(rows),
        "by_level": by_level,
        "max_risk": max((r.get("risk", 0) or 0) for r in rows) if rows else 0,
        "unique_targets": len(set(r.get("target", "") for r in rows if r.get("target"))),
    }


def top_risks(storage, limit: int = 10, min_level: str | None = None) -> list[AssetRisk]:
    """workspace 内 top N 高风险资产

    按 risk_score 降序,可选按 risk_level 过滤(low/medium/high/critical)
    """
    if limit <= 0:
        return []
    risks = compute_asset_risks(storage)
    if min_level:
        # 等级序:low < medium < high < critical
        order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        threshold = order.get(min_level, 0)
        risks = [r for r in risks if order.get(r.risk_level, 0) >= threshold]
    return risks[:limit]
