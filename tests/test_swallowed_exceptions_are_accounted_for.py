"""tests/ 里每一个「把异常当没事接住」的处数 —— 要登记,而且要说明白为什么

## 为什么要做这条

r89 在 `test_watch_workspace_arg.py` 逮到两处真缺陷:测试拿
`subprocess.run(timeout=25)` + `except TimeoutExpired: pass` 当**机制**用,
白等满 25 秒。`watch start` 按设计跑到 Ctrl+C 才停,而那两条测试的
docstring 自己写着「验证方式刻意**不依赖它跑完**」—— 意图和机制正好相反。

r90 的问题本来是「这个形状是不是不止那两处」。实测结论:**不是**。

## 实测:16 处站点 / 15 组,0 处是 r89 那种缺陷

扫法是 AST(不是文本子串,r80 的规矩):`ExceptHandler` 的 body **只有一条**
语句,且那条是 `pass` / `continue` / `return`。

    2 处  r89 形状的缺陷 —— 已修,这就是本条的由来
    1 处  test_phase1.py 的 **mock 在模拟** TimeoutError,是「代码在演坏掉」
          不是「测试在吞错」。结构撞上了,性质不同
    1 处  test_devloop_seed_truthfulness.py 把超时**变成失败**
          (return False),正确方向
    1 处  test_workspace_missing_gives_a_way_out.py 的 300s 超时是
          **从未触发的安全网** —— 实测该测试 3.70s 跑完。跟 r89 那种
          「靠超时当机制」不是一回事
    6 处  期望它抛、抛了就过的标准写法(try 里先 fail() 再 except)
    4 处  扫源码的测试跳过解析不了的 .py
    1 处  辅助函数把异常当**返回值**(docstring 写明了)

## 所以这条**不禁止**这个形状

按 r80 的结论(守卫不能比它守的东西还严,否则有人绕过它):一刀切禁止
`except ...: pass` 会误伤上面绝大多数正当用法,而其中「把超时变成失败」
和「mock 模拟超时」那两处,禁掉反而是错的。

改成**登记制**:每处都必须出现在 `ACCOUNTED` 里并写清理由;新增一处就
必须有人有意识地登记,过期的一处必须删掉。理由太短等于没写。

## 身份为什么带「第几次出现」

同一个函数里两处形状完全一样的站点(test_phase5 有两处
`except ValueError: pass`),只按 (文件, 函数, 异常, 动作) 收进 set 会
**塌成一处** —— 那就意味着「少登记一处」根本看不出来,正是判据不许恒真
要防的事。所以键里加一个组内出现次序。

不用行号做身份:行号会被上面任何一次编辑挪动,登记表会立刻失效。
函数名 + 组内次序在编辑下是稳定的。

判别力由四样东西保证,缺一样它就退化成「数了一下」:
  - 正控制组:16 个真实站点必须全被认出来(拿现场文件当样本)
  - 同组两处必须**各算一处**(塌成一处就报红)
  - 反向合成样本:body 不是单条 pass/continue/return 的不该被算进来
  - 过期登记必须报红 —— 否则登记表只增不减,最后变成一张黑名单
"""
from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]
TESTS = REPO / "tests"

# 异常体只有一条、且那条是这三种之一 → 「接住了就当没事」
_SILENT_STMTS = (ast.Pass, ast.Continue, ast.Return)

Site = tuple[str, str, str, str, int]   # 文件, 函数, 异常, 动作, 组内第几次


def _enclosing_name(tree: ast.AST, target: ast.ExceptHandler) -> str:
    """往回找最近的外层函数名;模块级返回 `<module>`。"""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(sub is target for sub in ast.walk(node)):
                return node.name
    return "<module>"


def swallowed_sites(root: pathlib.Path | None = None) -> set[Site]:
    """{(文件, 所在函数, 异常类型, 接住之后做的事, 组内第几次出现)}

    `root` 可传,是为了让**合成样本**能走同一条代码路径 —— 检测器自己
    得能被检验,不然「放宽某条规则」这种改动在真实数据上根本看不出来
    (r90 头一版就栽在这儿:M2/M3/M6 三条变异全都存活,因为现场没有任何
    一个 handler 命中它们削弱的那条路)。
    """
    base_dir = TESTS if root is None else root
    seen: dict[tuple[str, str, str, str], int] = {}
    out: set[Site] = set()
    for f in sorted(base_dir.glob("*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):  # pragma: no cover - 源码不可得
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler) or len(node.body) != 1:
                continue
            st = node.body[0]
            if not isinstance(st, _SILENT_STMTS):
                continue
            exc = ast.unparse(node.type) if node.type else "<bare>"
            base = (f.name, _enclosing_name(tree, node), exc,
                    type(st).__name__.lower())
            seen[base] = seen.get(base, 0) + 1
            out.add((*base, seen[base]))
    return out


def too_thin_reasons(reasons: dict) -> list:
    """理由太短(= 只说了「是什么」没说明「为什么」)的条目。

    抽成函数才能被合成样本检验,理由跟检测器一样。
    """
    return [f"{k}: {v!r}" for k, v in reasons.items() if len(v.strip()) < 10]


# ── 登记表:每处都必须写清为什么可以这么接 ──
#
# 理由下限 10 字符,跟 r80 的 EXCEPTIONS 一个规矩:只说「是什么」不说
# 「为什么」,等于没登记。

ACCOUNTED: dict[Site, str] = {
    ("test_cli_advice_commandable.py", "_leftovers", "SystemExit", "return", 1):
        "argparse 遇到不认识的参数会 sys.exit;这里返回 None 表示「没有多余参数」,"
        "正是这条测试要判定的东西",
    ("test_confidence_truth_table.py", "_modules_reading_the_field", "SyntaxError", "continue", 1):
        "扫源码算置信度;解析不了的 .py 不是本条要判的对象,跳过而不是崩",
    ("test_db_errors.py", "_capture", "sqlite3.IntegrityError", "return", 1):
        "辅助函数的 docstring 就写着「返回抛出的 IntegrityError;没抛就返回 None」"
        "—— 返回异常对象是它的正常返回值,不是把失败吞掉",
    ("test_devloop_multiagent.py", "test_production_modules_do_not_carry_their_own_self_tests",
     "SyntaxError", "continue", 1):
        "统计哪些脚本有 __main__ 入口;解析不了的文件不参与统计",
    ("test_devloop_seed_truthfulness.py", "_run_verify", "subprocess.TimeoutExpired", "return", 1):
        "一条写坏的 verify 不能挂死套件;超时被**变成失败** return False,"
        "跟 r89 那种「超时即正常结局」正好相反",
    ("test_gates_verify_effects.py", "test_absolute_imports_are_actually_in_the_graph",
     "SyntaxError", "continue", 1):
        "扫规则文件;坏文件由「每个文件都能 parse」那类判据单独兜,这里不重复炸",
    ("test_mutcheck_anchors_are_whole_statements.py", "balance",
     "(tokenize.TokenError, IndentationError, SyntaxError)", "pass", 1):
        "源码取不到时返回 None 让守卫扫描器停手;崩了就变成「所有判据集体消失」,"
        "那比放过一处更糟",
    ("test_no_real_home_writes.py", "_tmp_dirs", "OSError", "return", 1):
        "快照目录读不到时返回空集。判据主体是「新增了哪些 tmp* 目录」;"
        "r87 实测这正是它要的 fail-soft 口径",
    ("test_packaging_metadata.py", "_imports_in", "SyntaxError", "continue", 1):
        "查模块顶层有没有相对导入;解析不了的文件跳过,不影响这条要查的东西",
    ("test_phase1.py", "test_base_module_3state", "TimeoutError", "return", 1):
        "这是 **mock 模块在模拟**模块超时,返回失败结果是它在演示的行为 —— "
        "它是「代码在演坏掉」,不是「测试在吞错」。禁掉它等于把测试演的东西删了",
    ("test_phase5.py", "test_bug_regression", "ValueError", "pass", 1):
        "期望它抛的写法:try 里先 fail() 区分「没抛」,except ValueError 才 pass",
    ("test_phase5.py", "test_bug_regression", "ValueError", "pass", 2):
        "同上,第二个 Bug(端口越界)的同类写法。两处形状完全一样,所以键里"
        "带了「组内第几次出现」,否则它们会塌成一处、少登记一处也看不出来",
    ("test_silently_ignored_args_must_say_so.py", "_module_funcs",
     "(OSError, TypeError, SyntaxError)", "return", 1):
        "inspect.getsource 在动态安装/冻结时可能取不到,返回空字典让推导退化为 0 条;"
        "条数被钉死,退化成 0 条时 test_the_derived_set_is_pinned 会报红",
    ("test_sql_injection.py", "test_all_attacks_blocked", "ValueError", "pass", 1):
        "任何 ValueError 都算拦住了,没抛的进 missed 列表由断言报出来",
    ("test_swallowed_exceptions_are_accounted_for.py", "swallowed_sites",
     "(OSError, SyntaxError)", "continue", 1):
        "本文件自己的一处:源码读不到就跳过这个文件,不崩。登记表因此会把自己"
        "也算进去 —— 诚实的代价,总比开个后门强",
    ("test_workspace_missing_gives_a_way_out.py", "test_the_way_out_actually_works",
     "subprocess.TimeoutExpired", "pass", 1):
        "出路命令里可能有常驻的,300s 是**从未触发过的安全网**,不是机制 —— "
        "该测试实测 3.70s 跑完。断言主体是「工作区有没有建出来」,不是跑没跑完",
}


def test_the_derived_sites_are_pinned():
    """现场站点必须与登记表**完全相等** —— 多一处少一处都报红"""
    found = swallowed_sites()
    assert found == set(ACCOUNTED), (
        f"「把异常当没事接住」的站点变了。\n"
        f"  新增(没登记):{sorted(found - set(ACCOUNTED))}\n"
        f"  过期(登记了但代码里没了):{sorted(set(ACCOUNTED) - found)}\n"
        f"新增一处 = 有人新写了个「接住了就当没事」,先弄清它是不是像 r89 那两处。\n"
        f"过期一条 = 那个写法已经改掉了,登记表要跟着删。"
    )


def test_every_site_has_a_real_reason():
    """理由下限 10 字符 —— 只说「是什么」不说「为什么」,等于没登记"""
    assert not too_thin_reasons(ACCOUNTED), f"这些登记理由太短:{too_thin_reasons(ACCOUNTED)}"


# ── 合成样本:让检测器自己可被检验 ──
#
# 现场数据里有 16 个站点,但**没有一个**能命中「放宽动作类型」「放宽单条
# 限制」「放宽理由下限」这三条 —— r90 头一版把这三条当实现变异写,三条
# 全存活。存活不是因为判据没用,是因为**没人能证明它有用**:削弱了规则,
# 现场数据却照样满足。
#
# 下面这段合成样本把三种形状都造出来,让那三条变得可观测。

_SYNTHETIC = '''
def one_silent_pass():
    try:
        risky()
    except ValueError:
        pass


def one_silent_break():
    for item in items:
        try:
            risky()
        except ValueError:
            break


def one_silent_continue():
    for item in items:
        try:
            risky()
        except KeyError:
            continue


def one_two_stmt_body_ending_in_pass():
    try:
        risky()
    except ValueError:
        seen.append("hit")
        pass


def one_twice_same_shape():
    for a, b in pairs:
        try:
            first(a)
        except ValueError:
            pass
        try:
            second(b)
        except ValueError:
            pass


def one_handler_that_records():
    try:
        risky()
    except ValueError:
        seen.append("skipped")
'''


def test_the_detector_on_a_synthetic_sample(tmp_path):
    """拿合成样本走同一条代码路径,三种形状一个都不许漏

    这条是 r90 头一版的补课:三条「放宽规则」的变异当时全都存活,因为
    现场没有任何一个 handler 命中它们。检测器不能只在真实数据上被检验。
    """
    (tmp_path / "synth.py").write_text(_SYNTHETIC, encoding="utf-8")
    found = swallowed_sites(tmp_path)

    assert ("synth.py", "one_silent_pass", "ValueError", "pass", 1) in found
    assert ("synth.py", "one_silent_continue", "KeyError", "continue", 1) in found
    # 同形状两处必须各算一处
    assert ("synth.py", "one_twice_same_shape", "ValueError", "pass", 1) in found
    assert ("synth.py", "one_twice_same_shape", "ValueError", "pass", 2) in found

    # ↓ 这两条现在**不该**被算进来。正因为它们现在不算,把规则放宽的变异
    #   (M2 把 ast.Break 加进类型表、M3 放宽「body 只有一条」)会让它们
    #   突然被算出来,从而让下面两条断言翻红 —— 那就是这两条变异可观测的原因。
    assert ("synth.py", "one_silent_break", "ValueError", "break", 1) not in found, (
        "break 不在静默接住的类型表里,现在就不该被算出来")
    assert ("synth.py", "one_two_stmt_body_ending_in_pass",
            "ValueError", "pass", 1) not in found, (
        "body 超过一条就不算「静默接住」,现在不该被算出来")

    # 记了一笔的 handler 不算「静默接住」
    assert not [k for k in found if k[1] == "one_handler_that_records"], (
        "handler 里记了东西,不是把失败当没事,不该被算进来")
    assert len(found) == 4, f"合成样本现在应扫出 4 处,实得 {len(found)}:{sorted(found)}"


def test_a_thin_reason_is_rejected():
    """理由下限这条也必须能被合成样本检验(M6 削弱的就是它)"""
    thin = too_thin_reasons({("k",): "太短"})
    assert thin, "5 个字符的理由居然被放过了"
    assert not too_thin_reasons({("k",): "够长的理由:它说清了为什么可以这么接"}), \
        "正经理由被误伤了"


def test_the_detector_finds_every_real_site():
    """检测器不许空转 —— 拿**现场**文件当正控制组,一处都不能漏"""
    found = swallowed_sites()
    assert len(found) == 16, f"现场站点数不对:{len(found)}(逐条对一遍登记项)"
    by_file: dict[str, int] = {}
    for f, *_rest in found:
        by_file[f] = by_file.get(f, 0) + 1
    assert by_file.get("test_phase5.py") == 2, (
        f"test_phase5.py 有两处同形状,实得 {by_file.get('test_phase5.py')}"
        " —— 塌成一处就意味着「少登记一处」看不出来,正是判据不许恒真要防的")
    assert all(f.startswith("test_") for f, *_rest in found), "扫到了非测试文件"


def test_same_shape_sites_in_one_function_stay_separate():
    """同函数内形状相同的两处必须各算一处(组内次序不同)"""
    found = swallowed_sites()
    dupes = [k for k in found if k[4] > 1]
    assert dupes == [("test_phase5.py", "test_bug_regression", "ValueError", "pass", 2)], (
        f"预期只有 test_phase5 那一组有第二次出现,实得 {sorted(dupes)}")
