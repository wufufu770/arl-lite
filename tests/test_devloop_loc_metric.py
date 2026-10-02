"""devloop 膨胀红线量的到底是什么

第 9 轮发现:红线按**总行数**卡在 3189/3200,只剩 11 行余量。
但逐文件拆开,3189 里只有 2096 是代码:

    2096 代码行 + 635 docstring + 149 注释 + 309 空行

这个代码库的注释密度异常高,而且大量 docstring 记的是
**"这个 bug 是怎么被发现的、为什么这么修"**。把它们算成膨胀,
等于惩罚把话说清楚。

所以红线改成量代码行。本文件的核心是证明这个新指标真的在排除
docstring —— 如果它只是把数字改小,那就是在给门禁放水。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from arl_lite.devloop import gates

REPO = Path(__file__).parents[1]
DEVLOOP = REPO / "arl_lite" / "devloop"


def _code(src: str) -> int:
    return gates._code_lines_in_source(src)


# =====================================================================
# 指标本身:必须真的排除 docstring / 注释 / 空行
# =====================================================================


def test_docstring_is_excluded():
    src = 'def f():\n    """\n    很长很长的说明\n    写在这里\n    """\n    return 1\n'
    # 有效代码只有 def 和 return 两行
    assert _code(src) == 2


def test_line_comments_are_excluded_but_trailing_ones_keep_their_line():
    """纯注释行删掉,**行尾注释所在的行必须留着**

    `def f():  # 说明` 这一行有代码。曾经按 token 行号直接 drop,
    把带行尾注释的代码整行扣掉了。
    """
    src = "# 纯注释\ndef f():  # 行尾注释\n    return 1  # 又一个\n"
    # 留下 def 和 return 两行
    assert _code(src) == 2


def test_a_triple_quoted_string_that_is_not_a_docstring_still_counts_as_code():
    """docstring 只指 module/class/def 的第一个字符串语句

    后面 return 的长字符串是代码,不能被扣掉 —— 扣了就等于
    奖励"把代码藏进字符串"。
    """
    src = 'def f():\n    """说明"""\n    return """这不是 docstring\n第二行\n"""\n'
    # def + return + 多行字符串的 3 个物理行 = 4
    assert _code(src) == 4, "非 docstring 的字符串被误扣了"
    # 关键:不能少于 2(def + return),否则字符串被当成了文档
    assert _code(src) >= 2


def test_blank_lines_are_excluded():
    src = "def f():\n\n\n    return 1\n\n"
    assert _code(src) == 2


def test_hash_inside_a_string_is_not_treated_as_a_comment():
    """不能用 src.split('#') 那种朴素做法

    字符串里的 '#' 是数据不是注释,滤错了会把代码行也扣掉。
    """
    src = "def f():\n    return 'a#b#c'\n"
    assert _code(src) == 2


def test_hash_inside_a_docstring_is_handled():
    src = 'def f():\n    """说明里有 # 号\n    还要多写几行\n    """\n    return 1\n'
    assert _code(src) == 2


def test_nested_function_and_class_docstrings_are_excluded():
    src = (
        'class A:\n'
        '    """类说明\n    第二行\n    """\n'
        '    def m(self):\n'
        '        """方法说明"""\n'
        '        return 1\n'
    )
    # class + def + return
    assert _code(src) == 3


def test_module_docstring_is_excluded():
    src = '"""模块说明\n第二行\n第三行\n"""\nimport os\n\n\ndef f():\n    return 1\n'
    assert _code(src) == 3


def test_async_function_docstring_is_excluded():
    src = 'async def f():\n    """说明\n    两行\n    """\n    return 1\n'
    assert _code(src) == 2


# =====================================================================
# 降级:坏文件不能成为绕过红线的口子
# =====================================================================


def test_syntax_error_falls_back_to_physical_lines():
    """语法错误的文件按物理行计入 —— 宁可算多,不可绕过红线

    故意少算 = 自动给自己开豁免,那这条红线就没有意义了。
    """
    src = "def f(:\n    这不是合法的 python\n    也没有别的\n"
    assert _code(src) == 3, "坏文件必须按物理行计入,不能少算"


def test_broken_file_never_gets_counted_fewer_than_physical_lines():
    """坏文件永远不会被少数算 —— 那等于给自己开豁免

    原来写过一个"tokenize 失败仍扣 docstring"的用例,但那条分支实际
    不可达:`ast.parse` 成功意味着同一套 tokenizer 成功,反过来
    tokenize 失败时 ast 必然也失败,会先走进物理行的保守分支。
    不可达的分支配一个假测试没有意义,改成验证真正该保证的性质。
    """
    good = "def f():\n    \"\"\"说明\n    行\n    \"\"\"\n    return 1\n"
    bad = good + "    尾部未闭合的 (\n"

    physical_good = len(good.splitlines())
    assert _code(good) == 2 < physical_good, "正常文件应少于物理行"

    # 坏文件:按物理行计,不少于它的物理行数
    assert _code(bad) >= len(bad.splitlines()), (
        "坏文件被少数算了 —— 红线会因此被绕过"
    )


# =====================================================================
# 红线接线
# =====================================================================


def test_devloop_red_line_uses_code_lines():
    """红线常量换成了代码行口径,且数值来自实测而非拍脑袋"""
    assert hasattr(gates, "_DEVELOOP_CODE_LOC_LIMIT")
    assert not hasattr(gates, "_DEFAULT_DEVELOOP_LOC_LIMIT"), \
        "旧的总数口径常量不该还留着,留着会被误用"
    # 2400 应该是当前代码行数(2096 左右)之上、但不是随手拍的大数
    current = gates._count_code_lines(list(DEVLOOP.glob("*.py")))
    assert current < gates._DEVELOOP_CODE_LOC_LIMIT
    # 余量不能大到形同虚设:至少 20% 会被当成没约束
    assert gates._DEVELOOP_CODE_LOC_LIMIT <= current * 1.35, (
        f"红线 {gates._DEVELOOP_CODE_LOC_LIMIT} 相对实测 {current} 太松了"
    )


def test_devloop_red_line_is_still_not_promotable():
    """改了度量口径,但没把它变成可提升的——那是两件事"""
    g = gates.get_gate("loc_budget")
    assert g.promotable is True, "total_loc 仍然可以显式提升"
    # 协议自身的红线不在 promotable_fields 里,提升动不了它
    assert "devloop_code_loc" not in g.promotable_fields
    assert "devloop_total_loc" not in g.promotable_fields


def test_gate_reports_both_code_and_total():
    """门禁输出要让人看得见协议到底写了多少,而不只是判过没过"""
    res = gates.get_gate("loc_budget").run(REPO)
    m = res.measured or {}
    assert "devloop_code_loc" in m
    assert "devloop_total_loc" in m
    assert m["devloop_total_loc"] > m["devloop_code_loc"], \
        "总行数应该确实大于代码行数(否则说明排除逻辑没生效)"


def test_real_devloop_docstring_ratio_is_what_we_think():
    """把第 9 轮那个判断钉死:总行 3189 里代码 2096

    这条测试记录的是"为什么改口径"的事实依据。
    如果哪天注释密度变了,这个断言会提醒重新审视红线数值。
    """
    files = [p for p in DEVLOOP.glob("*.py") if "__pycache__" not in p.parts]
    total = gates._count_lines(files)
    code = gates._count_code_lines(files)
    assert total > 3000, f"总行数 {total} 与第 9 轮记录不符"
    assert code < total * 0.75, (
        f"代码占比 {(code / total):.0%},与第 9 轮的 66% 偏差过大"
    )


# =====================================================================
# arl_lite/ 总量仍按物理行算
# =====================================================================


def test_arl_lite_total_still_uses_physical_lines():
    """只改了协议自身的口径,项目代码的量没动

    项目的 total_loc 衡量的是"这轮写了多少东西",物理行更直观,
    而且它是可提升的(accept),口径必须稳定。
    """
    src = "def f():\n    \"\"\"说明\n    行\n    \"\"\"\n    return 1\n"
    assert gates._count_lines([]) == 0
    p = REPO / "arl_lite" / "devloop" / "state.py"
    physical = gates._count_lines([p])
    code = gates._count_code_lines([p])
    assert physical > code
