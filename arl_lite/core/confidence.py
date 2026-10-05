"""arl_lite.core.confidence — 关联分析结论的置信度

问题:37 条规则命中就直接给 `risk: 9`,没有"半信"的概念。
参照物是 AtlasX 作者用 1000 份真实 SRC 报告测出的"高危真漏洞率 76%"
—— 即便是商业化 + LLM 训练后的产品, 仍有 24% 误报。

置信度不是一个"拍脑袋的分数", 而是四因子乘法模型:

    score = base_prior × cross_evidence × table_complexity × exclusion_bonus

为什么是乘法而不是加法: 任何一项明显偏低, 整条规则就该跟着塌下去。
加法会让"某一项极强"掩盖"某一项很差", 而现实里**跨表印证不成立**
就是致命短板(规则声称要跨表印证, 结果没印证上, 那它本就不该被信)。

`(r22)` 这里原先写的是 `base_prior × signal_factor × cross_evidence
× temporal` —— **`signal_factor` 和 `temporal` 在代码里从不存在**,
而真实存在的 `table_complexity` / `exclusion_bonus` 一个没提。
同一段还写着"任一因子为 0 则总分 0", 而实际上四个因子的取值都 >= 0.6,
根本没有 0 这回事。

一个模块的 docstring 描述着自己没有的机制, 和第 18 轮删掉的死元数据
是同一类病: **说法存在, 不等于机制存在**。真值见 `assess()`,
并由 `tests/test_confidence_truth_table.py` 钉死 —— 那个测试会
从本文件的 `assess()` 实跑结果反查 `docs/CONFIDENCE_VS_RISK.md`。

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
#
# 这**两个**数是工程经验值。定它们的原则是:宁可漏报也不要让高危告警
# 变成噪音——用户对告警麻木比漏报更危险。
#
# `(r22 更正)` 这段原先写的是"这三个数",但上面只摆了两个阈值 ——
# 第三个数是下面的 `_BASE_PRIOR`。同一段还写着"需要用真实误报率数据
# 校准(见 backlog 的误报率实测条目)",而那条**第 14 轮就做完了**
# (离线受控样本 14 个,产出 `docs/FP_RATE.md`,`arl-lite fp-bench` 可复跑),
# 它校准的是**规则本身**的误报,没有回填过这两个阈值。
# 所以准确的说法是:阈值至今仍是经验值,不是从数据推出来的。
#
# 真值表见 `docs/CONFIDENCE_VS_RISK.md`,由
# `tests/test_confidence_truth_table.py` 硬编码钉住 —— 改这两个数
# 必须同步改那份期望值,门禁会红。
THRESHOLD_REPORT = 0.70    # >= 直接报告
THRESHOLD_OBSERVE = 0.45   # >= 只入观察表, 不进主告警; < 直接丢弃

STATUS_REPORT = "report"
STATUS_OBSERVE = "observe"
STATUS_DISCARD = "discard"

# 字段缺失 / 非法值时的兜底档位。
#
# 为什么兜底是 observe 而不是 discard: 升级到 r21 之前落库的关联
# 没有 `confidence_status` 字段。若按 discard 处理,用户升级后报告会
# 突然少掉一批历史命中,而且完全不知道为什么。
# **保守方向是"多算"而不是"少算"** —— 少算会让人误以为资产是安全的。
STATUS_UNKNOWN_FALLBACK = STATUS_OBSERVE


def status_of(row) -> str:
    """读一条关联行的处置档位,统一兜底口径。

    ## 为什么要从这一层开始就只有一个实现

    r21 把报告按处置分流,r22 发现 `compute_asset_risks` 和
    `risk_summary` 是另外两处读 correlations 的地方,三处各写一遍
    判断。三处各写一遍的代价不是"重复",而是**漏改一处不会报错** ——
    它只会安静地给出一个和另外两处对不上的数。

    调用方各写各的还有第二个后果:兜底口径会漂。
    报告层写过 `or "observe"` 也写过 `or ""`,两处判断的**语义**相同
    (都等价于"不是 discard"),但读代码的人得自己推一遍才知道它们一样。

    ## 兜底规则

    - 字段缺失 / None / 空白 → `STATUS_OBSERVE`
    - 取值不在三档之内(脏数据) → `STATUS_OBSERVE`
    - 大小写和首尾空白一律归一(库里存过 "Discard" 这种值)

    脏数据归 observe 而不是 discard,理由同上:**多算优于少算**。
    """
    raw = row.get("confidence_status")
    st = (raw or "").strip().lower()
    if st in (STATUS_REPORT, STATUS_OBSERVE, STATUS_DISCARD):
        return st
    return STATUS_UNKNOWN_FALLBACK


def is_discarded(row) -> bool:
    """这一行是否被模型判定为 discard。

    注意它**只回答是/否**,不回答档位 —— 需要排序或打标签时用
    `status_of()`。把这两个问题混在一处写,正是 r22 之前那三份
    重复实现的由来。
    """
    return status_of(row) == STATUS_DISCARD


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
