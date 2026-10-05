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
    """红线量的是**代码行**,且余量不许大到形同虚设

    ## 口径从常量改成「生效值」

    r20 之前这条断言的是 `current < _DEVELOOP_CODE_LOC_LIMIT` —— 拿源码常量
    当唯一真相。r20 之后生效值可能来自 baseline(经 accept 显式提升),
    所以这里改成问生效值。

    底下那条「余量 ≤1.35x」才是这条测试真正的价值:它挡的是"把红线
    抬到一个再也碰不到的高度",也就是把红线取消掉。口径怎么变都行,
    这条不能松。
    """
    assert hasattr(gates, "_DEVELOOP_CODE_LOC_LIMIT")
    assert not hasattr(gates, "_DEFAULT_DEVELOOP_LOC_LIMIT"), \
        "旧的总数口径常量不该还留着,留着会被误用"

    current = gates._count_code_lines(list(DEVLOOP.glob("*.py")))
    g = gates.get_gate("loc_budget")
    prev = gates.load_baseline(REPO).get("loc_budget", {})
    limit, src = g._devloop_limit(REPO, prev)

    assert current < limit, (
        f"devloop 代码行 {current} 已达/超过生效红线 {limit}(来源: {src})"
    )
    # 余量不能大到形同虚设:至少 20% 会被当成没约束
    assert limit <= current * 1.35, (
        f"生效红线 {limit} 相对实测 {current} 太松了(来源: {src})—— "
        f"余量超过 35% 等于没有红线"
    )


def test_devloop_red_line_can_only_move_through_an_audited_path():
    """红线现在可提升,但只能走**留痕机制**——这是 r20 的核心不变式

    ## 为什么改了 r11 的结论

    r11 把它设成**不可提升**,出发点是对的:防止有人为了凑数去改门禁。
    但 r19 发现它有个更根本的问题 —— **没有任何合法更新路径**。

    那不是红线,是一堵没门的墙:面对它只有两个动作,偷偷改常量(正是
    它要防的作弊),或者让门禁永远红着。而门禁永远红着会稀释它的信号 ——
    一个天天红的门禁等于没有门禁。

    所以 r20 把它纳入 `devloop accept` 的留痕机制。**关键不在于
    "能不能提升",而在于"提升必须留下痕迹"**:

    - 门禁必须**正在失败**才能提升(挡住"提前买预算")
    - 理由 ≥10 字符(挡住"随手放宽")
    - 写进 `baselines.json` 进版本库(进 git diff 和 code review)

    早先这两条测试守的是"不可提升"。那个立场现在过期了,但**它们要防的
    东西没有过期** —— 静默放宽。所以改成守新机制,而不是删掉。
    """
    g = gates.get_gate("loc_budget")
    assert g.promotable is True
    # 红线字段可提升,但只在留痕机制下
    assert "devloop_code_loc" in g.promotable_fields
    # 总行数**仍然**不可提升 —— 它只是给人看的参考值,不参与判定
    assert "devloop_total_loc" not in g.promotable_fields, \
        "总行数不参与门禁判定,不该被提升(提升它等于提升一个没用的数)"

    # 提升必须经过 accept 模块的硬规则,而不是绕过它改 baseline
    from arl_lite.devloop.accept import MIN_REASON_LEN, accept_baseline
    assert MIN_REASON_LEN >= 10, "理由门槛被削弱了"

    # 门禁必须正在失败才给提升 —— 用一个绿的仓库状态验这条
    #
    # `(r26 修)` 这里原来调的是 `accept_baseline(REPO, ...)` —— **真实
    # 仓库**,不是临时目录。而 accept_baseline 是会写盘的。
    #
    # 它当时没出事纯属运气:测试自己先 `g.run(REPO)` 拿到绿,才走进
    # `if res.passed:` 分支;而 accept_baseline 内部**又跑一遍**
    # `gate.run(repo)`。两次调用之间只要状态翻转(比如并发的探针文件
    # 增删),内部那次就变成红,于是**提升被真的写进了
    # devloop/baselines.json**。
    #
    # r26 实测到了:baseline 被从 14254 抬到 15014,理由是那句
    # "这条理由够长了用于测试"。而这行代码的本意只是验**拒绝路径**。
    #
    # 换句话说:一个只想验证"系统会说不"的测试,把红线抬了 760 行。
    # 和第 23 轮"测试往用户真实 HOME 写数据"是同一个病,
    # 只是这次写的是协议自己的审计轨迹。
    #
    # 改成临时仓库,和其他所有 accept 测试一致。
    import shutil
    import tempfile
    from pathlib import Path as _P
    tmp_repo = _P(tempfile.mkdtemp())
    try:
        (tmp_repo / "devloop").mkdir(parents=True, exist_ok=True)
        (tmp_repo / "arl_lite").mkdir(parents=True, exist_ok=True)
        (tmp_repo / "arl_lite" / "x.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_repo / "devloop" / "backlog.md").write_text("# backlog\n", encoding="utf-8")
        gates.update_baseline(tmp_repo, "loc_budget", {"total_loc": 1})
        out = accept_baseline(tmp_repo, "loc_budget", reason="这条理由够长了用于测试")
        assert out.ok is False, "门禁是绿的却接受了提升 —— 提前买预算没被挡住"
        assert "green" in out.detail.lower() or "nothing" in out.detail.lower(), out.detail
    finally:
        shutil.rmtree(tmp_repo, ignore_errors=True)


def test_devloop_red_line_value_is_not_silently_changed():
    """红线阈值本身必须来自 baseline 或常量,**不能被就地偷偷改**

    留痕机制成立的前提是"绕过机制的改动看得见"。所以这条守住:
    生效值只能来自两个地方 —— `baselines.json` 的 `devloop_code_loc`
    (经 accept 写入,进版本库)或源码常量(改动进 git diff)。
    """
    g = gates.get_gate("loc_budget")
    prev = gates.load_baseline(REPO).get("loc_budget", {})
    limit, src = g._devloop_limit(REPO, prev)
    assert isinstance(limit, int) and limit > 0
    # 来源说明必须能让人判断这个值是怎么来的
    assert "baseline" in src or "常量" in src, src
    # 没有 baseline 时必须回落到常量,而不是变成 0 或 None
    limit_default, src_default = g._devloop_limit(REPO, {})
    assert limit_default == gates._DEVELOOP_CODE_LOC_LIMIT
    assert "常量" in src_default


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


# =====================================================================
# r27:accept 只该提升真正超标的那一项
# =====================================================================


def _loc_tmp_repo(tmp_path, total_loc_base=15):
    """一个只有 devloop 红线超标、arl_lite 总量"涨了但没涨过容差"的仓库

    `total_loc_base=15` 是关键:实测约 21,**大于** baseline(所以 _plan_changes
    看得见"可以提")但**远小于** baseline+300 的容差上限(所以它没红)。
    这正是 r27 实测到 accept 越界的那副形状:14514 > 14254,可 14514 < 14554。

    第一版这里把 baseline 设成 100(实测 21 < 100),于是那条字段
    **本来就没大到能提**,测试自然过 —— 三个变异全靠它存活。
    """
    (tmp_path / "devloop").mkdir(parents=True)
    (tmp_path / "devloop" / "backlog.md").write_text("# backlog\n", encoding="utf-8")
    dv = tmp_path / "arl_lite" / "devloop"
    dv.mkdir(parents=True)
    (dv / "big.py").write_text("".join(f"x{i} = {i}\n" for i in range(20)), encoding="utf-8")
    (tmp_path / "arl_lite" / "small.py").write_text("y = 1\n", encoding="utf-8")
    # devloop_code_loc 给 5(实测 20 -> 该红);total_loc 见上面的说明
    gates.update_baseline(tmp_path, "loc_budget",
                          {"total_loc": total_loc_base, "devloop_code_loc": 5})
    return tmp_path


def test_accept_only_promotes_the_field_that_actually_failed(tmp_path):
    """实测(r27):只修 devloop 红线,accept 却把 total_loc 也提了

    当时 total_loc 是 14514,上限 14554 —— **它根本没红**,只是比 baseline
    14254 大。规则 1 挡的是"提前买预算",可一旦别的字段真的红了,闸门一过
    就顺带把没红的也买了,等于从侧门绕过了自己。
    """
    from arl_lite.devloop.accept import accept_baseline

    repo = _loc_tmp_repo(tmp_path)
    before = gates.load_baseline(repo)["loc_budget"]
    assert before["total_loc"] == 15

    out = accept_baseline(repo, "loc_budget", reason="这条理由够长了用于测试")
    assert out.ok, f"该接受的没接受: {out.detail}"
    assert "devloop_code_loc" in out.changes, f"真正超标的那项没提: {out.changes}"

    after = gates.load_baseline(repo)["loc_budget"]
    assert after["total_loc"] == 15, (
        f"没红的 total_loc 被顺手提了 {before['total_loc']} -> {after['total_loc']}"
    )
    assert after["devloop_code_loc"] > 5


def test_gate_reports_which_fields_are_over(tmp_path):
    """门禁要能说清"是哪几项超标",否则 accept 无从只提那几项"""
    repo = _loc_tmp_repo(tmp_path)
    r = gates.LocBudgetGate().run(repo)
    assert not r.passed
    assert r.measured["_over"] == ["devloop_code_loc"], r.measured


def test_both_fields_are_promoted_when_both_are_over(tmp_path):
    """两项都超了就都提 —— 过滤不能变成漏网

    对照组:上面那条只测了"只提红的",这条测"该提的都提"。
    只写前者的话,把 `_over` 写成永远空列表也能全绿。
    """
    from arl_lite.devloop.accept import accept_baseline

    repo = _loc_tmp_repo(tmp_path, total_loc_base=1)   # 上限 301,仍不红 total
    gates.update_baseline(repo, "loc_budget",
                          {"total_loc": 1, "devloop_code_loc": 5})
    r = gates.LocBudgetGate().run(repo)
    assert r.measured["_over"] == ["devloop_code_loc"]

    # 真让 total 也红:baseline 调到一个比实测还小的值
    gates.update_baseline(repo, "loc_budget",
                          {"total_loc": -1000, "devloop_code_loc": 5})
    r = gates.LocBudgetGate().run(repo)
    assert set(r.measured["_over"]) == {"total_loc", "devloop_code_loc"}, r.measured

    out = accept_baseline(repo, "loc_budget", reason="这条理由够长了用于测试")
    assert out.ok
    assert set(out.changes) == {"total_loc", "devloop_code_loc"}, out.changes
