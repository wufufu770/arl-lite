"""误报率基准本身 —— 反向验证

## 这份基准的定位

它是**回归基线**,不是绝对误报率测量。样本集是跟着修复一起写的,
所以「0%」只说明当前规则能正确处理这 14 个受控场景。
详见 `docs/FP_RATE.md` 的「这个 0% 意味着什么」一节。

**恒真的测试比没有更糟** —— 一个永远绿的基准等于没有。所以本文件的
重点是:把上面那三个已修的 bug 逐个重新植入,验证基准**会红**。

## 全部离线

每个样本在一次性 tmp workspace 里跑,不联网,不碰真实数据。
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from arl_lite.core import fp_bench as fb

REPO = Path(__file__).parents[1]
RULES = REPO / "arl_lite" / "modules" / "analysis" / "rules"


@pytest.fixture(scope="module")
def report():
    return fb.analyze(fb.run_bench())


# =====================================================================
# 基线:当前应当全绿
# =====================================================================


def test_no_false_positives_on_controlled_samples(report):
    assert report.total_fp == 0, (
        f"受控样本上出现误报: "
        f"{[(r.case.name, sorted(r.false_positives)) for r in report.results if r.false_positives]}"
    )


def test_recall_is_complete(report):
    assert report.total_fn == 0, (
        f"规则该响没响: "
        f"{[(r.case.name, sorted(r.false_negatives)) for r in report.results if r.false_negatives]}"
    )


def test_no_case_crashed(report):
    """崩掉的样本被排除在统计外,必须显式可见而不是悄悄消失"""
    broken = [r for r in report.results if r.error]
    assert not broken, f"有样本没跑出结果: {[(r.case.name, r.error) for r in broken]}"


# =====================================================================
# 反向验证:把修好的 bug 逐个植入,基准必须变红
# =====================================================================


def _bench_with_patched_rule(yaml_name: str, transform) -> dict:
    """把某条规则临时改掉,跑一遍基准,再还原"""
    f = RULES / yaml_name
    original = f.read_text(encoding="utf-8")
    try:
        f.write_text(transform(original), encoding="utf-8")
        rep = fb.analyze(fb.run_bench())
        return {
            "total_fp": rep.total_fp,
            "fp": {n for r in rep.results for n in r.false_positives},
        }
    finally:
        f.write_text(original, encoding="utf-8")


def test_benchmark_catches_missing_state_open_check():
    """把 exposed_database 的 state='open' 去掉,基准必须报出误报

    这就是实测抓到的那个 bug:state='closed' 的 6379 端口也报
    「数据库服务暴露公网」,而这条规则的 confidence 标着 high。
    """
    def strip_state(src: str) -> str:
        return src.replace(" AND state = 'open'", "")

    out = _bench_with_patched_rule("exposed_database.yml", strip_state)
    assert "exposed_database" in out["fp"], (
        f"去掉 state='open' 后基准没报出误报,基准测不出这个回归: {out}"
    )


def test_benchmark_catches_missing_aggregation():
    """去掉 db_asset_diversity 的 count_min,基准必须报出误报

    单个 Redis 指纹就触发「workspace 内 2 个数据库」——名字和逻辑对不上。
    """
    def strip_count(src: str) -> str:
        return re.sub(r"^count_min:\s*\d+\s*$", "", src, flags=re.M)

    out = _bench_with_patched_rule("db_asset_diversity.yml", strip_count)
    assert "db_asset_diversity" in out["fp"], (
        f"去掉 count_min 后基准没报出误报: {out}"
    )


def test_benchmark_catches_missing_exclusion():
    """去掉 phpmyadmin_public 的认证层 exclusion,基准必须报出误报

    面板架在 Keycloak 后面照样报「暴露公网」。
    """
    def strip_exclusion(src: str) -> str:
        return re.sub(
            r"exclusion:\n(?:  - table:.*\n(?:    .*\n)+)+", "", src, flags=re.M,
        )

    out = _bench_with_patched_rule("phpmyadmin_public.yml", strip_exclusion)
    assert "phpmyadmin_public" in out["fp"], (
        f"去掉 exclusion 后基准没报出误报: {out}"
    )


def test_benchmark_detects_a_broadly_broken_rule():
    """把任意一条规则改成"什么都命中",基准必须抓得住

    兜底:证明基准不是只对上面三个特定 bug 敏感。
    """
    def match_everything(src: str) -> str:
        return re.sub(r'where:\s*".*?"', 'where: "1=1"', src, count=1)

    out = _bench_with_patched_rule("jenkins_public.yml", match_everything)
    assert out["total_fp"] > 0, f"规则改成 1=1 都没报出误报: {out}"


# =====================================================================
# 规则文件本身的自洽性
# =====================================================================


def test_renamed_rules_no_longer_claim_same_ip():
    """名字里带 same_ip 的规则必须真的做 per-IP 分组,否则改名

    引擎的 count_min 是 workspace 级的,而 findings 的 target 是 URL,
    本来就未必能推出 IP。所以规则不该叫 same_ip。
    """
    stale = [p.stem for p in RULES.glob("*.yml")
             if "same_ip" in p.stem or "same_ip" in p.read_text(encoding="utf-8").split("\n")[0]]
    assert not stale, f"仍有规则声称 per-IP 分组: {stale}"


def test_every_rule_still_loads():
    """改名/改 WHERE 之后所有规则还得能解析"""
    from arl_lite.core.correlation_engine import load_all_rules
    rules = load_all_rules(RULES)
    assert len(rules) == 37, f"规则数变了: {len(rules)}"
    assert {r.name for r in rules} >= {
        "cms_asset_diversity", "db_asset_diversity", "exposed_database",
        "phpmyadmin_public",
    }


def test_rules_have_advice_and_confidence():
    """基准的交叉统计依赖 confidence 字段,缺了就统计不出来"""
    from arl_lite.core.correlation_engine import load_all_rules
    bad = [r.name for r in load_all_rules(RULES)
           if getattr(r, "confidence", None) not in ("high", "medium", "low")]
    assert not bad, f"这些规则缺合法 confidence: {bad}"


# =====================================================================
# 交叉统计:验证第 1 轮的置信度分档
# =====================================================================


def test_confidence_cross_tab_is_not_flat(report):
    """交叉统计必须真的分出档位

    一开始我读的是 `Rule.confidence_level`,而 Rule 只有 `confidence`,
    getattr 默认值让 37 条规则全落进 medium 档,交叉表变成一句废话。
    """
    levels = set(report.by_confidence)
    assert levels == {"high", "medium", "low"}, f"档位没读对: {levels}"
    for lvl, v in report.by_confidence.items():
        assert v["rules"] > 0, f"{lvl} 档一条规则都没有"
    total = sum(v["rules"] for v in report.by_confidence.values())
    assert total == 37, f"三档加起来不是 37: {total}"


def test_high_confidence_tier_has_no_false_positives(report):
    """high 档靠"端口级事实/跨表验证",样本上不该有误报

    这是对第 1 轮那次分档的独立检验。如果 high 档也开始误报,
    说明分档没起作用,这个断言会红。
    """
    high_fp = report.by_confidence.get("high", {}).get("fp", 0)
    assert high_fp == 0, f"high 档出现 {high_fp} 条误报,分档可能失效"


# =====================================================================
# 样本集自身的合理性
# =====================================================================


def test_sample_set_is_not_all_one_kind():
    """样本不能全是负样本或全是正样本,否则指标没有意义"""
    intents = {c.intent for c in fb.ALL_CASES}
    assert intents == {"negative", "positive"}
    assert len(fb.NEGATIVE_CASES) >= 5
    assert len(fb.POSITIVE_CASES) >= 5


def test_case_names_are_unique():
    """重名会让报告里两行看起来一样,读的人分不清"""
    names = [c.name for c in fb.ALL_CASES]
    assert len(names) == len(set(names)), f"样本重名: {names}"


def test_positive_cases_actually_expect_something():
    """正样本必须写明期望命中,否则它证明不了召回"""
    for c in fb.POSITIVE_CASES:
        assert c.expect_fire, f"正样本 {c.name} 没写 expect_fire"
        assert not (c.expect_fire & c.allow_fire), (
            f"{c.name} 的 expect_fire 和 allow_fire 打架: {c.expect_fire & c.allow_fire}"
        )


def test_allow_fire_only_used_on_negative_cases():
    """allow_fire 是"负样本里的例外",用在正样本上会让口径混乱"""
    for c in fb.POSITIVE_CASES:
        assert not c.allow_fire, f"正样本 {c.name} 不该用 allow_fire"


def test_every_case_explains_itself():
    """每个样本都要写清代表什么真实场景 —— 报告直接印 this 字段"""
    for c in fb.ALL_CASES:
        assert c.why and len(c.why) > 10, f"{c.name} 的 why 太简略"
