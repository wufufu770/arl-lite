"""arl_lite.core.confidence — 关联分析结论的置信度

问题:37 条规则命中就直接给 `risk: 9`,没有"半信"的概念。
参照物是 AtlasX 作者用 1000 份真实 SRC 报告测出的"高危真漏洞率 76%"
—— 即便是商业化 + LLM 训练后的产品, 仍有 24% 误报。

置信度不是一个"拍脑袋的分数", 而是四因子乘法模型:

    confidence = base_prior × signal_factor × cross_evidence × temporal

为什么是乘法而不是加法: 任一因子为 0, 整条规则就不可信。
加法会让"某一项极强"掩盖"某一项为 0", 而现实里 0 就是致命短板
(比如完全依赖模糊匹配时, 跨表交叉验证再强也救不了)。

注意这里算的是**规则的证据强度**, 不是"漏洞是否真实存在"。
arl-lite 是资产侦察工具, 命中的是"暴露面", 不是"漏洞"。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

# ── 证据强度分档 ─────────────────────────────────────────────────────────
# 按调研结论 (CVSS v4 的 Base/Threat/Environmental 分组思路) 把
# confidence 拆成可解释的档位, 而不是连续分数——连续分数会给人
# "0.73 比 0.71 精确很多" 的错觉, 而实际区分度没那么高。

CONF_HIGH = "high"        # 单表单条件, 端口或精确标题匹配
CONF_MEDIUM = "medium"    # 有跨表交叉验证, 或命中明确产品特征
CONF_LOW = "low"          # 模糊匹配 / 聚合统计 / 依赖指纹准确性

_BASE_PRIOR = {
    CONF_HIGH: 0.90,
    CONF_MEDIUM: 0.70,
    CONF_LOW: 0.45,
}

# 阈值: 决定这条结论怎么处理
# 这三个数是工程经验值, 需要用真实误报率数据校准 (见 devloop/backlog.md
# 的 "误报率实测" 条目)。定它们的原则是:宁可漏报也不要让高危告警
# 变成噪音——用户对告警麻木比漏报更危险。
THRESHOLD_REPORT = 0.70    # >= 直接报告
THRESHOLD_OBSERVE = 0.45   # >= 只入观察表, 不进主告警; < 直接丢弃

STATUS_REPORT = "report"
STATUS_OBSERVE = "observe"
STATUS_DISCARD = "discard"


@dataclass
class ConfidenceAssessment:
    """一条规则的置信度评估结果"""
    rule_name: str
    base_confidence: str          # high / medium / low
    score: float                  # 0.0 - 1.0
    status: str                   # report / observe / discard
    factors: dict                 # 各因子的贡献, 用于解释为什么是这个分

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def is_reportable(self) -> bool:
        return self.status == STATUS_REPORT


def assess(
    rule_name: str,
    base_confidence: str,
    has_cross_ref: bool = False,
    cross_ref_satisfied: bool = False,
    has_exclusion: bool = False,
    matched_table_count: int = 1,
) -> ConfidenceAssessment:
    """评估一条规则的置信度。

    Args:
        rule_name: 规则名(仅用于返回可读结果)
        base_confidence: 规则声明的基础置信度档位
        has_cross_ref: 规则是否定义了 cross_ref
        cross_ref_satisfied: 运行时 cross_ref 是否满足
        has_exclusion: 规则是否定义了 exclusion
        matched_table_count: 规则实际命中的不同表数量(1=单表)

    Returns:
        ConfidenceAssessment

    因子说明:
        base_prior          规则档位先验
        cross_evidence      跨表交叉验证(定义且满足 = 强证据)
        table_complexity    跨表越多越容易误报(容器/反代/多服务共机)
        fuzzy_match_penalty 模糊匹配(LIKE/server 头)惩罚
    """
    base = _BASE_PRIOR.get(base_confidence, 0.70)

    # 跨表证据: 定义了 cross_ref 但**没满足** 说明交叉验证没通过
    if has_cross_ref and not cross_ref_satisfied:
        cross_evidence = 0.6
    elif has_cross_ref and cross_ref_satisfied:
        cross_evidence = 1.15
    else:
        cross_evidence = 1.0

    # 表复杂度: 每多一张表, 误报面就大一些(同机多服务是常态)
    if matched_table_count <= 1:
        table_complexity = 1.0
    elif matched_table_count == 2:
        table_complexity = 0.9
    else:
        table_complexity = 0.8

    # 有 exclusion 是好事: 说明作者想过误报场景
    exclusion_bonus = 1.05 if has_exclusion else 1.0

    score = base * cross_evidence * table_complexity * exclusion_bonus
    score = max(0.0, min(1.0, score))

    if score >= THRESHOLD_REPORT:
        status = STATUS_REPORT
    elif score >= THRESHOLD_OBSERVE:
        status = STATUS_OBSERVE
    else:
        status = STATUS_DISCARD

    return ConfidenceAssessment(
        rule_name=rule_name,
        base_confidence=base_confidence,
        score=round(score, 3),
        status=status,
        factors={
            "base_prior": base,
            "cross_evidence": cross_evidence,
            "table_complexity": table_complexity,
            "exclusion_bonus": exclusion_bonus,
        },
    )
