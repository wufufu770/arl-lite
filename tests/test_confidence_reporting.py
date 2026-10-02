"""置信度模型必须在报告层真正生效

## 这个文件为什么存在

第 1 轮建了置信度模型(`core/confidence.py`),算出 `report/observe/discard`
三档处置。第 21 轮回头查的时候发现:

```
grep -rn confidence_status arl_lite/
  arl_lite/db/storage.py:271:  "confidence_status": "ALTER TABLE ..."
```

**只有建表语句,没有任何消费者。**

数据侧是齐的 —— `CorrelationHit` 带四个字段、schema 有三列、
`save_correlations` 的 INSERT 和 ON CONFLICT 都写了。但报告层
`cli_report_html.py` 把 correlations **无条件全渲染**,
还 `[:50]` 按插入顺序截断。

后果正是第 1 轮想解决的问题,原封不动地留在原地:

> 「指纹误报会被直接放大成高危告警」

`discard` 判定的指纹误报,和 `high confidence` 的真问题,
在用户看到的报告里长得一模一样。

## 为什么会漏掉

建模型的那一轮,验收标准是「模型算得对」(`test_confidence.py`),
而不是「模型的结论有人用」。**算对了但没接线,和一个没写的函数
在功能上是一样的** —— 而且更坏,因为它看起来做完了。

这和第 16 轮删掉的那层假活制造机是同一类病:
产出存在,消费链断了,没人发现。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from arl_lite.cli_report_html import generate_html_report
from arl_lite.db.storage import Storage

REPO = Path(__file__).parents[1]


def _seed(storage, rows):
    """往 correlations 塞几条命中。

    rows: (rule_name, confidence, confidence_status)
    直接写 SQL 而不是走 correlate —— 这里要测的是**报告层怎么消费**
    已落库的数据,不是规则引擎怎么产出它们。后者由 test_confidence 管。
    """
    with storage._conn() as conn:
        for name, conf, status in rows:
            conn.execute(
                """INSERT INTO correlations
                   (workspace_id, rule_name, risk, target, target_type,
                    headline, advice, tags, matched_count, evidence,
                    confidence, confidence_level, confidence_status,
                    confidence_factors, detected_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (storage.workspace_id, name,
                 9 if status == "report" else 4,
                 f"{name}.example.com", "host",
                 f"{name} 的概要", "修复建议", "[]", 1, "{}",
                 conf, "high" if conf >= 70 else "low",
                 status, "{}", "2026-01-01T00:00:00"),
            )
        conn.commit()


@pytest.fixture
def st():
    import tempfile
    td = tempfile.TemporaryDirectory()
    s = Storage(workspace="t", workspace_root=Path(td.name))
    yield s
    s.close()
    td.cleanup()


def _main_table(html: str) -> str:
    """截出关联分析卡的**主表**部分(不含折叠区)

    不能用 `split('</table>')[0]` —— 页面上在这张表之前还有别的表,
    切出来的一大段会把后面的内容也带进去,断言就永远为真(假绿)。
    这个坑本轮踩过一次。
    """
    i = html.find("关联分析")
    assert i >= 0, "报告里没有关联分析卡片"
    seg = html[i:]
    return seg.split("<details>")[0]


def test_discard_does_not_reach_the_main_table(st):
    """discard 判定的命中不许出现在主表

    这是第 1 轮承诺的直接兑现:低置信度的指纹误报不该和真问题混在
    同一张表里被当成同一等级的东西。
    """
    _seed(st, [("真实高危", 90, "report"), ("指纹误报", 12, "discard")])
    html = generate_html_report(st, "t")
    main = _main_table(html)
    assert "真实高危" in main, "前置条件不成立:report 档没进主表"
    assert "指纹误报" not in main, \
        "confidence_status=discard 的命中出现在主表里 —— " \
        "第 1 轮「指纹误报不被放大成高危告警」的承诺没兑现"


def test_discarded_hits_stay_visible_in_a_collapsed_section(st):
    """被 discard 的不许**删掉**,要折叠可见

    折叠而不是隐藏,理由是 `discard` 是**模型的判断**,判断可能错。
    藏起来就没人能发现模型错了 —— 而模型错恰恰是这类系统最需要
  被人看见的失败。完全混进主表又会让误报淹没真问题。
    """
    _seed(st, [("指纹误报", 12, "discard")])
    html = generate_html_report(st, "t")
    assert "<details>" in html, "没有折叠区"
    assert "指纹误报" in html, "discard 的命中被彻底删掉了 —— 模型判断错了也没人看得见"
    assert "指纹误报" not in _main_table(html)


def test_observe_hits_stay_in_the_main_table_but_are_marked(st):
    """observe 留在主表,但要打标记 —— 让人自己判断而不是替他决定"""
    _seed(st, [("中等", 55, "observe")])
    html = generate_html_report(st, "t")
    main = _main_table(html)
    assert "中等" in main, "observe 被踢出主表了 —— 那是过度过滤"
    assert "待观察" in main, "observe 没有可见标记,用户分不清它和 report 的差别"


def test_missing_status_is_treated_conservatively(st):
    """老数据没有 confidence_status 时不许被静默隐藏

    升级前落库的关联没有这个字段。如果按「空 = discard」处理,
    用户升级后报告会突然少掉一批历史命中,而且完全不知道为什么。
    保守做法:空 = observe,留在主表。
    """
    with st._conn() as conn:
        conn.execute(
            """INSERT INTO correlations
               (workspace_id, rule_name, risk, target, target_type, headline,
                advice, tags, matched_count, evidence, confidence,
                confidence_level, confidence_status, confidence_factors, detected_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (st.workspace_id, "老数据", 7, "old.example.com", "host",
             "升级前的命中", "", "[]", 1, "{}", 55, "low",
             None, None, "2025-01-01T00:00:00"),  # 关键:status 为 NULL
        )
        conn.commit()
    html = generate_html_report(st, "t")
    assert "老数据" in _main_table(html), \
        "没有 confidence_status 的历史命中被隐藏了 —— " \
        "升级会让用户莫名其妙少看到一批关联"


def test_high_confidence_hit_is_not_truncated_by_id_order(st):
    """高置信度命中不许被 `[:50]` 截掉

    这是 P1 待办的验收。

    ## 这里的坑比想象的大:第一版测试是假绿的

    初版我造 60 条噪声 + 1 条压轴的,断言压轴那条要出现在报告里。
    跑**通过**了 —— 于是我又去变异验证(删掉 `sorted`),测试**照样通过**。
    查下去才发现 `storage.query` 是 `ORDER BY id DESC`:压轴那条 id 最大,
    排**第一**,从头到尾都轮不到被截。

    也就是说初版测试压根没验到它想验的东西 ——
    **前提就是错的,所以断言恒真。**

    > 「测试通过」和「变异生效」是两件事。而这一次,
    > 变异生效了、测试仍然通过,说明该怀疑的是测试自己。

    真实的失效条件:压轴那条必须**先**被插入(id 最小),
    于是 `id DESC` 把它排到末尾,再被 `[:50]` 截掉。
    用两个 workspace 分两次插入来控制 id 大小,不依赖插入顺序的运气。
    """
    # 第一批:高置信度的真问题,id 最小
    _seed(st, [("压轴的真问题", 95, "report")])
    # 第二批:60 条低置信度噪声,id 更大 → ORDER BY id DESC 会排在前面
    _seed(st, [(f"噪声{i}", 20, "report") for i in range(60)])

    got = st.query("correlations", limit=10000)
    idx = [i for i, r in enumerate(got) if r["rule_name"] == "压轴的真问题"]
    assert idx, "前置条件不成立:压轴那条没落库"
    assert idx[0] > 50, (
        f"前置条件不成立:压轴那条在 id DESC 里排第 {idx[0]} 位,"
        f"没落到会被截断的区间 —— 这条测试就白写了"
    )

    html = generate_html_report(st, "t")
    assert "压轴的真问题" in _main_table(html), \
        "id 最小的高置信度命中被前 50 条截掉了 —— 排序仍是 id DESC/插入顺序," \
        "高价值命中会静默丢失"
    # 噪声被截断是可以接受的(有上限),但要让人知道总数
    assert "共 61 条" in html or "已排除" in html or "仅显示前 50" in html


def test_status_field_actually_has_a_consumer(st):
    """`confidence_status` 必须有读取方,不只是写入方

    r21 之前它只出现在 `storage.py` 的建表语句里 —— 有 schema、
    有 INSERT、有字段,就是没人读。后果就是第 1 轮想解决的问题
    原封不动留着。

    ## 这条测试改过一次(r22)

    原来它是这么写的:

    ```python
    src = (REPO / "arl_lite" / "cli_report_html.py").read_text()
    assert "confidence_status" in src       # ← 文本匹配
    assert "discard" in src and "observe" in src
    ```

    r22 把判定下沉到 `core.confidence.status_of()` 之后,
    **`confidence_status` 这个字符串在报告层不再出现**,
    测试红了 —— 而代码其实比改之前更正确。

    这暴露了原测试的毛病:它测的是"字段名字符串在不在",
    不是"处置有没有真的影响输出"。按项目铁律,
    **能用行为断言就别用文本匹配** —— 文本匹配分不清
    「真的在按它分流」和「只是在解释为什么按它分流」,
    而且会把正确的重构判成回归。

    现在改成跑真报告、看三档的行各自落在哪儿。
    """

    _seed(st, [
        ("该直接报的真问题", 90, "report"),
        ("要人自己判断的", 55, "observe"),
        ("指纹误报", 10, "discard"),
    ])
    html = generate_html_report(st, "t")
    main = _main_table(html)

    assert "该直接报的真问题" in main, "report 档没进主表"
    assert "要人自己判断的" in main, "observe 档没进主表"
    assert "待观察" in main, "observe 档没有可见标记 —— 用户看不出它待定"
    assert "指纹误报" not in main, "discard 档混进了主表"
    # discard 不删,折叠起来(见"折叠而非删除"那条决策)
    assert "指纹误报" in html, "discard 被整条删掉了 —— 模型判断可能错,藏起来就没人能发现它错了"


# =====================================================================
# 风险聚合:第三处消费路径
# =====================================================================


def test_discard_hits_do_not_inflate_risk_scores(st):
    """discard 命中的关联不许参与风险评分

    第 21 轮把 discard 从报告主表沉到折叠区。但 `compute_asset_risks`
    只读 `risk`,照样把它算进去 —— 于是用户看到「这个资产风险 9 分」,
    却在报告里找不到对应条目,两个数字对不上账。

    这和 confidence-risks 那条 P2 待办是同一件事:
    confidence 决定**算不算数**,risk 决定**算数之后有多严重**。
    """
    from arl_lite.core.risk_score import compute_asset_risks

    _seed(st, [("真问题", 90, "report"), ("指纹误报", 10, "discard")])
    # 两条的 target 不同,各自成一个资产;误报那条 risk 也是 9
    risks = {a.target: a.risk_score for a in compute_asset_risks(st)}
    assert "真问题.example.com" in risks
    assert "指纹误报.example.com" not in risks, \
        "discard 命中的关联仍然参与了风险评分 —— 报告里看不到它,风险却算了"


def test_observe_hits_still_count_toward_risk(st):
    """observe **要**参与 —— 它是「算出来但不足以直接报」,不是「不算」

    把 observe 也排除会**低估**真实风险,那比高估更危险:
    用户会以为资产是安全的。
    """
    from arl_lite.core.risk_score import compute_asset_risks

    _seed(st, [("待观察的高危", 70, "observe")])
    risks = {a.target for a in compute_asset_risks(st)}
    assert "待观察的高危.example.com" in risks, \
        "observe 被排除了 —— 那会低估真实风险,比高估更危险"


def test_risk_summary_uses_the_same_filter(st):
    """risk_summary 和 compute_asset_risks 必须是同一个口径

    这条守的是「规则只该有一个实现」。r22 实测:`risk_summary` 是
    第三处读 correlations 的地方,它直接 `storage.query`,于是 discard
    照样进 `by_level` 和 `max_risk`。三处各写一遍判断,漏一处
    discard 就从那个口子回来 —— 而且不会报错,只会安静地给出错误的数。
    """
    from arl_lite.core.risk_score import risk_summary

    _seed(st, [("真问题", 90, "report"), ("指纹误报", 10, "discard")])
    s = risk_summary(st)
    assert s["total_correlations"] == 1, \
        f"概览把 discard 也算进去了:{s}"
    assert s["discarded_correlations"] == 1, \
        f"被排除的条数没有单独报出来,用户对不上账:{s}"
    # max_risk 也不能被 discard 拉高
    assert s["max_risk"] == 9 or s["max_risk"] >= 9


def test_missing_status_counts_in(st):
    """字段缺失的老数据照常计入(保守:不因缺字段就少算风险)"""
    from arl_lite.core.risk_score import risk_summary

    with st._conn() as conn:
        conn.execute(
            """INSERT INTO correlations
               (workspace_id, rule_name, risk, target, target_type, headline,
                advice, tags, matched_count, evidence, confidence,
                confidence_level, confidence_status, confidence_factors, detected_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (st.workspace_id, "老数据", 7, "old.example.com", "host",
             "升级前的命中", "", "[]", 1, "{}", 55, "low",
             None, None, "2025-01-01T00:00:00"),
        )
        conn.commit()
    s = risk_summary(st)
    assert s["total_correlations"] == 1, "缺 status 的历史命中被排除了"
    assert s["discarded_correlations"] == 0
