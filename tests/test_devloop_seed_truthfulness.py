"""播种产出的每一条都必须**能被独立核实**

第 13 轮播种提出 5 条,其中 3 条是假的:

    add-confidence-database_with_public_web
    add-confidence-docker_api_exposed
    add-confidence-elasticsearch_public

这三条规则其实都有 `confidence:` 字段。原因是检查还在找
`low-confidence` **标签** —— 第 1 轮把置信度从标签改成了字段,
标签全没了(0/37),于是 25 条 risk≥7 的规则全部被误判成"缺标注",
取风险最高的 3 条报上来。

**假活比队列空掉更坏**:队列空掉会报错,假活会让人真的去干一遍已经
做完的事,而且干完之后引擎还会把它标 done。

所以本文件的立场是:任何"按现状推导"出来的待办,它的每一条断言都
必须能对着仓库现状独立验证为真。本文件把三类推导全部验一遍:

  (a) 高风险规则缺 confidence 字段
  (b) 数据源数量不足
  (c) 阶段测试缺失

外加一条更一般的:真实队列里不允许存在被自动推导出来、却与仓库
现状矛盾的条目。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from arl_lite.devloop.queue import Queue

REPO = Path(__file__).parents[1]
RULES = REPO / "arl_lite" / "modules" / "analysis" / "rules"


def _derive() -> list:
    return Queue(REPO / "devloop" / "queue.json")._seed_from_project_state(set(), 99)


# =====================================================================
# (a) 高风险规则缺置信度
# =====================================================================


def test_no_rule_is_flagged_that_already_has_confidence():
    """被提名的规则必须真的没有 confidence 字段

    这条直接对应第 13 轮那 3 条假活。
    """
    conf = re.compile(r"^confidence:\s*(\S+)\s*$", re.MULTILINE)
    name = re.compile(r"^name:\s*(\S+)\s*$", re.MULTILINE)

    for item in _derive():
        m = re.match(r"^add-confidence-(.+)$", item.id)
        if not m:
            continue
        rule_name = m.group(1)
        f = RULES / f"{rule_name}.yml"
        assert f.is_file(), f"{item.id} 指向的规则文件不存在: {f}"
        txt = f.read_text(encoding="utf-8")
        assert not conf.search(txt), (
            f"{item.id} 是假的: {rule_name}.yml 已经有 confidence 字段\n"
            f"  {conf.search(txt).group(0) if conf.search(txt) else ''}"
        )
        assert name.search(txt), f"{rule_name}.yml 缺 name 字段"


def test_detection_uses_the_confidence_field_not_a_retired_tag():
    """检查必须读 `confidence:` 字段,而不是已废弃的 `low-confidence` 标签

    置信度模型第 1 轮把标签换成了字段。如果推导还在找标签,
    它对每一条高风险规则都会报"缺标注"。

    这里断言的是**行为**而不是源码文本:早先试过 grep 源码里有没有
    "low-confidence" 字样,但文档字符串在解释"这个标记为什么被废弃"
    时必然会提到它,文本匹配分不出"在用它"和"在说它已经不用了"。
    真正该守的是行为 —— 已经有 confidence 的规则不能被提名。
    """
    conf = re.compile(r"^confidence:\s*(\S+)\s*$", re.MULTILINE)
    risk = re.compile(r"^risk:\s*(\d+)\s*$", re.MULTILINE)

    # 造一条高风险但**有** confidence 的规则,看推导会不会提名它
    high_risk_with_conf = [
        p.stem for p in RULES.glob("*.yml")
        if (m := risk.search(p.read_text(encoding="utf-8")))
        and int(m.group(1)) >= 7
        and conf.search(p.read_text(encoding="utf-8"))
    ]
    assert high_risk_with_conf, (
        "仓库里没有'高风险且有 confidence'的规则,这条用例就验证不了任何东西"
    )

    nominated = {i.id for i in _derive() if i.id.startswith("add-confidence-")}
    for stem in high_risk_with_conf:
        assert f"add-confidence-{stem}" not in nominated, (
            f"{stem}.yml 是 risk>=7 且**已有** confidence 字段,不该被提名"
        )


def test_every_high_risk_rule_actually_has_confidence_now():
    """现状核对:37 条规则全都有 confidence 字段

    记录事实依据。如果哪天真的缺了,这条会提醒重新审视推导逻辑。
    """
    ymls = sorted(RULES.glob("*.yml"))
    assert ymls, "规则目录为空"
    conf = re.compile(r"^confidence:\s*(\S+)\s*$", re.MULTILINE)
    missing = [p.stem for p in ymls if not conf.search(p.read_text(encoding="utf-8"))]
    assert not missing, f"这些规则缺 confidence 字段: {missing}"


# =====================================================================
# (b)(c) 其余推导同样要能核实
# =====================================================================


def test_add_data_source_claim_matches_reality():
    """提出"新增数据源"时,数据源数量必须真的不足"""
    srcs = [
        p for p in (REPO / "arl_lite" / "integrations").glob("*.py")
        if p.stem not in ("__init__", "tool_checker")
    ]
    for item in _derive():
        if item.id != "add-data-source":
            continue
        m = re.search(r"目前 (\d+) 个数据源", item.detail)
        assert m, f"detail 里应当写明当前数据源数量: {item.detail!r}"
        assert int(m.group(1)) == len(srcs), (
            f"detail 说 {m.group(1)} 个,实际 {len(srcs)} 个 —— 假活"
        )
        assert len(srcs) < 12, "数据源已达标,不该再提这条"


def test_missing_phase_tests_claim_matches_reality():
    """提出"补缺失阶段测试"时,列出的阶段必须真的缺"""
    present = {p.stem for p in (REPO / "tests").glob("test_phase*.py")}
    for item in _derive():
        if item.id != "add-missing-phase-tests":
            continue
        m = re.search(r"缺失 phase 测试: ([^。]+)", item.detail)
        assert m, f"detail 里应当列缺失的阶段: {item.detail!r}"
        listed = {int(x) for x in re.findall(r"\d+", m.group(1))}
        real = {n for n in range(1, 8) if f"test_phase{n}" not in present}
        assert listed == real, (
            f"detail 列的缺失阶段 {sorted(listed)} 与实际 {sorted(real)} 不符 —— 假活"
        )


def test_supplement_rules_claim_matches_reality():
    """提出"补充规则覆盖"时,规则数必须真的不足"""
    n = len(list(RULES.glob("*.yml")))
    for item in _derive():
        if item.id == "supplement-rules-coverage":
            assert n < 40, f"规则数 {n} 已达标,不该再提这条"


# =====================================================================
# 通用:真实队列不能有站不住脚的自动推导条目
# =====================================================================


def test_real_queue_auto_derived_items_are_all_justified():
    """真实队列里凡 id 带自动推导特征的,断言都要能被核实"""
    auto_prefixes = ("add-confidence-", "add-data-source",
                     "add-missing-phase-tests", "supplement-rules-coverage")
    q = Queue(REPO / "devloop" / "queue.json")
    active = [i for i in q.load() if i.status in ("pending", "in_progress")]
    flagged = [i for i in active if i.id.startswith(auto_prefixes)]
    # 逐条交给上面那些已验证的检查去核实
    derived = {i.id for i in _derive()}
    unjustified = [i.id for i in flagged if i.id not in derived]
    assert not unjustified, (
        f"队列里有无法核实的自动推导条目: {unjustified}\n"
        f"  当前 _seed_from_project_state 能证实的: {sorted(derived)}"
    )
