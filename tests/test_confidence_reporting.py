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


def test_status_field_actually_has_a_consumer():
    """`confidence_status` 必须有读取方,不只是写入方

    这条是本文件最直接的守门:r21 之前它只出现在 `storage.py` 的
    建表语句里 —— 有 schema、有 INSERT、有字段,就是没人读。

    判据是**在渲染路径里**出现,而不是"全仓出现过"。后者在有建表
    语句时就成立,那正是它失效了整个 r1 的原因。
    """
    src = (REPO / "arl_lite" / "cli_report_html.py").read_text(encoding="utf-8")
    # 排除注释/docstring:用可执行代码里是否出现该字段名判断
    code_lines = [
        ln for ln in src.splitlines()
        if not ln.strip().startswith("#")
    ]
    code = "\n".join(code_lines)
    assert "confidence_status" in code, \
        "报告渲染代码里没有 confidence_status —— 模型的处置判断没被消费"
    assert "discard" in code and "observe" in code, \
        "没有按三档处置分流 —— 只是把字段读出来而已"
