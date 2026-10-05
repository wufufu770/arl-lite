"""r103:`--preset` 被静默吞掉,而 `_is_interesting` 的文案两处说谎

## 实测的退化路径(不是推测)

    $ arl-lite run -t example.com --preset fast
    [+] workspace: default
    [+] target: example.com
    [+] modules: ['subfinder', 'crtsh']
    [+] done in 1.2s
    ...全文再无 preset 字样,rc=0

用户给了 `--preset fast`,得到 rc=0 和一次「成功」的扫描,零字提示。
`task_runner.run` 的 `preset` 参数**函数体零引用**——它在签名里,
在 docstring 里写着「暂未实现」,然后就没有然后了。

这和 r97 那一类同源:**声明过要做 ≠ 可以静默**。区别在于这次连
「暂未实现」都只写在开发者看得见的 docstring 里,终端前的用户看不见。
用户能看见的只有 rc=0。

## r103 的修法是**提示**,不是实现,也不是删 flag

- 不删 `--preset`:它已经在 parser 里,删了会破坏已写好的脚本,
  而那些脚本现在至少还知道自己没生效。
- 不「实现」它:凭空造一套 preset 语义比空着更坏。
- **让它说话**:stdout 一行 + log 一条,两边都带用户传进来的值本身。

值本身是关键。写死一句「preset 未实现」是另一种撒谎——用户
分不清自己传的是 `fast` 还是拼错了的 `fas`。

## 第二个发现:同一个文件里,文案和代码当场打架

`integrations/dirscan.py::_is_interesting` 三处文案全在描述
「path」:

1. docstring 写「启发式:哪些 **path** 算 interesting」,函数体
   **只看 status**;
2. `path` / `length` / `body_hash` 三个参数由调用方完整传入
   (dirscan.py:115),函数体**一次都没引用**;
3. 行内注释写「# 500 算(可能存在但 server error)」,紧跟的
   代码是 `return False  # 通常是 service 挂了`。

第 3 条最刺眼:注释说「算」,下一行说不算。就摆在同一个函数里。
读代码的人先读注释,注释是错的。

所以 r103 在 dirscan **不发明行为**——soft-404 启发式(同路径
不同 body 长度比对)是真该做的事,但那是 Phase 2。这轮只把文案
改成和现状一致,并**用判据钉住它不许再漂**。

## 判据为什么这么切

方向一(不许静默)和方向二(文案不许撒谎)是两个独立方向,
各钉各的,别让一条判据同时假装在测两件事:

- 方向一:真跑 `TaskRunner.run(preset=<哨兵>)`,断言 log 里出现
  **哨兵值本身**;再用 AST 确认 `cmd_run` 里有 print 真的引用了
  `args.preset`——不是「文件里有 preset 这个词」。
- 方向二:docstring 用 `ast.get_docstring` 拿(AST 拿得到 docstring,
  拿不到行内注释,注释那条走 tokenize);分支行为用**手写状态码表
  真调函数**核对,而不是复述源码的 if 链。

## 踩过的坑(留给下一个人)

- `_is_interesting` 的三个「死参数」**不是** bug,`storage._query_where`
  的 `table`/`ws` 也一样:签名收下了但函数体不用。判据不因此判它们死,
  因为删签名会改调用点,而这两处删了**行为不变**——那属于整洁问题,
  不属于诚实问题。这轮只管「文案不许和代码矛盾」。
- 查注释必须用 `tokenize` 拿 `COMMENT` token。`ast` 里没有行内注释。
"""
from __future__ import annotations

import ast
import io
import logging
import re
import tempfile
import tokenize
from pathlib import Path

from arl_lite.core.task_runner import TaskRunner
from arl_lite.db.storage import Storage

_REPO = Path(__file__).resolve().parent.parent
_CLI = _REPO / "arl_lite" / "cli.py"
_DIRSCAN = _REPO / "arl_lite" / "integrations" / "dirscan.py"

# 哨兵值:必须是一眼假的真值。断言里出现它才说明「用户传的值被回显了」,
# 而不是「代码里恰好写了句 preset 相关的提示」。
_SENTINEL = "zzz-not-a-real-preset-4711"


# =====================================================================
# 方向一:声明过未实现 ≠ 可以静默
# =====================================================================


async def test_task_runner_warns_with_the_preset_value_it_ignored(caplog):
    """真跑一次,断言 log 里出现**用户传的那个值**。

    写成固定文案就红——「preset 未实现」能让用户分不清自己传的是
    `fast` 还是拼错的 `fas`,那还是假信心。
    """
    with tempfile.TemporaryDirectory() as tmp:
        s = Storage(workspace="preset-warn-r103", workspace_root=tmp)
        runner = TaskRunner(storage=s, workspace_id=s.workspace_id)
        with caplog.at_level(logging.WARNING):
            # modules=[] → 不跑任何模块,不联网,跑得快且确定性高
            await runner.run(target="example.invalid", modules=[], preset=_SENTINEL)

    warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(_SENTINEL in m for m in warned), (
        f"TaskRunner 收了 preset={_SENTINEL!r} 却没有任何 WARNING 带上这个值。"
        f"实际收到的 WARNING:{warned}"
    )


def test_cli_run_echoes_preset_attribute_not_a_fixed_banner():
    """AST 层面:cmd_run 里至少有一个 print 真的引用了 args.preset。

    文本子串匹配挡不住「把提示写成 `print('[!] preset 未实现')`」——
    那样用户在,但依然不知道自己的值去哪儿了。所以查的是
    `Attribute(attr='preset', value=Name('args'))` 出现在 print 的参数里。
    """
    tree = ast.parse(_CLI.read_text(encoding="utf-8"))
    cmd_run = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "cmd_run"
    )
    hit = False
    for node in ast.walk(cmd_run):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"):
            continue
        for arg in node.args:
            for sub in ast.walk(arg):
                if (isinstance(sub, ast.Attribute)
                        and sub.attr == "preset"
                        and isinstance(sub.value, ast.Name)
                        and sub.value.id == "args"):
                    hit = True
    assert hit, (
        "cmd_run 里没有任何 print 引用 args.preset —— 提示行被删了,"
        "或者退化成了不含用户输入的固定横幅"
    )


async def test_preset_warning_only_when_preset_actually_given(caplog):
    """反方向:没给 preset 就别刷屏。

    只钉「该说话时说话」不够——反过来无条件喊「未实现」也吵。
    """
    with tempfile.TemporaryDirectory() as tmp:
        s = Storage(workspace="preset-quiet-r103", workspace_root=tmp)
        runner = TaskRunner(storage=s, workspace_id=s.workspace_id)
        with caplog.at_level(logging.WARNING):
            await runner.run(target="example.invalid", modules=[], preset=None)

    noisy = [r.getMessage() for r in caplog.records
             if r.levelno >= logging.WARNING and "preset" in r.getMessage()]
    assert not noisy, f"没给 preset 也在喊 preset:{noisy}"


# =====================================================================
# 方向二:文案不许和代码矛盾
# =====================================================================


# 「不许说反话」的判据,必须分得清**声称**和**引述**。
#
# r103 写这张表时当场栽了一次:文案改对之后,我顺手在注释里补了
# 一句「原注释写『500 算』,与下一行打架」——那是引用反面例子做
# 解释,不是声称。按字面查的判据把它判成了说谎,红了两条。
#
# 两条路:删掉解释,或者让判据分得清。前者的代价很实在——r100 刚因为
# 「文档没写清当初为什么这么改」栽过一次,注释里的「为什么」正是
# 最该留的东西。所以定约定:**引述一律用 `「」` 包起来**,判据剥掉
# 引述内容再查断言。
_QUOTED = re.compile(r"「[^」]*」")


def _strip_quoted(text: str) -> str:
    """剥掉 `「...」` 包裹的引述,只留作者自己的断言。"""
    return _QUOTED.sub("", text)


def _mentions_int(node: ast.expr, value: int) -> bool:
    """条件表达式里**结构化**地出现了这个整数字面量。

    首版写的是 `str(value) in ast.unparse(node)` —— 被
    `tests/test_source_checks_are_structural.py` 当场拦下。unparse 把 AST
    变回文本,等于「用了 AST 又退回去」,而那个门禁管的正是这种写法:
    注释或字符串字面量都能把它喂饱。走 `Constant` 节点就骗不过。
    """
    return any(isinstance(n, ast.Constant) and n.value == value
               for n in ast.walk(node))


def _comment_on_line(src: str, lineno: int) -> str:
    """取某一行的**注释部分**,词法级。

    不用 `src.splitlines()[lineno - 1]`:整行读进来,那一行要是字符串
    字面量或者别的什么,就等于把非注释内容当成注释断言 —— 同一个病。
    `tokenize` 是词法级的:字符串里的 `#` 产出的不是 `COMMENT` token,
    所以只有真注释收得进来。同一行多个注释用空格拼起。
    """
    out = [
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type == tokenize.COMMENT and tok.start[0] == lineno
    ]
    return " ".join(out)


def _load_is_interesting():
    tree = ast.parse(_DIRSCAN.read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_is_interesting"
    )
    return tree, fn


def test_is_interesting_docstring_does_not_claim_path_filtering():
    """docstring 拿了不算「文案说了什么」——它必须承认只看 status。"""
    _, fn = _load_is_interesting()
    doc = ast.get_docstring(fn) or ""
    assert doc, "_is_interesting 没有 docstring 了 —— 那正是它能开始说谎的原因"
    assert "status" in doc, (
        "docstring 没提 status,但这个函数只按 status 判。"
        f"当前 docstring:{doc!r}"
    )
    # 剥掉 `「...」` 引述再查——见 _strip_quoted 的注释:记「当初错在哪」
    # 是要留的,那句话不能被判成「又在说反话」。
    assert "哪些 path 算" not in _strip_quoted(doc), (
        "docstring 又变回宣称按 path 判了,而函数体零引用 path"
    )


# 手写副本:状态码 → 期望的 interesting。
#
# 这张表**故意不复述源码的 if 链**。改了实现忘了改这张表,就红一下——
# 那正是我们要的:行为漂移必须有人看见。表和实现一起漂,测试就废了。
_EXPECTED = {
    200: True, 201: True, 204: True, 299: True,
    301: True, 302: True, 399: True,
    401: True, 403: True, 405: True,
    500: False, 502: False, 503: False,
    404: False, 418: False, 100: False, 0: False,
}


def test_interesting_branch_table_matches_actual_behavior():
    """执行级核对:把表里的每个状态码真喂进去,核对返回值。"""
    from arl_lite.integrations.dirscan import _is_interesting

    wrong = {
        code: (want, _is_interesting(code, "/probe", 4242, "deadbeef"))
        for code, want in _EXPECTED.items()
        if _is_interesting(code, "/probe", 4242, "deadbeef") is not want
    }
    assert not wrong, f"行为和表对不上(状态码 → (期望, 实际)):{wrong}"


def test_interesting_ignores_path_length_and_hash_today():
    """把「三个参数当前无效」钉成事实,免得下一个人以为它已经生效了。

    这条**不是**说死参数该留 —— 是说留着的代价必须是「文案说清它无效」。
    真去实现 soft-404 时,这条会红,那时该改的是实现和表。
    """
    from arl_lite.integrations.dirscan import _is_interesting

    a = _is_interesting(404, "/admin", 100, "aaaa")
    b = _is_interesting(404, "/wp-login.php", 99999, "bbbb")
    assert a is b, "path/length/body_hash 开始影响判定了 —— soft-404 实现了的话,本文件要重写"


def test_branch_count_matches_registered_table():
    """实现里加了 if 分支,表里没登记 → 红。

    防的是「悄悄加一条判定规则,表和文案都不动」。
    """
    _, fn = _load_is_interesting()
    branches = [n for n in ast.walk(fn) if isinstance(n, ast.If)]
    assert len(branches) == 5, (
        f"_is_interesting 现在有 {len(branches)} 个 if 分支(登记的表按 5 个设计)。"
        f"新增/删除判定规则时,请同步 _EXPECTED 和 docstring"
    )


def test_every_interesting_branch_carries_a_comment():
    """每个分支上方必须有注释 —— 而且注释得跟着分支走。

    r103 前 `# 500 算` 就摆在 `return False` 正上方。那行注释是这个
    bug 的载体:它不是缺注释,是**注释说了反话**。钉住「每分支有注释」
    只是地板,反话由上面两条判据(表 + docstring)兜。
    """
    src = _DIRSCAN.read_text(encoding="utf-8")
    _, fn = _load_is_interesting()
    comment_lines = {
        tok.start[0]
        for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type == tokenize.COMMENT
    }
    missing = [
        n.lineno for n in ast.walk(fn)
        if isinstance(n, ast.If) and (n.lineno - 1) not in comment_lines
    ]
    assert not missing, f"这些判定分支上方没有注释(行号):{missing}"


def test_5xx_comment_does_not_claim_5xx_counts():
    """专门钉 r103 抓到的那处矛盾:注释说「500 算」,代码 return False。

    判据不试图「理解」中文注释——那没有可靠做法。只查一个具体的、
    已经出过一次事的反例短语。抓得住回归,又不假装能做语义审查。
    """
    _, fn = _load_is_interesting()
    # 定位 5xx 分支。
    #
    # 这里**必须**带默认值:写判据时若用裸 `next(...)`,一旦有人把
    # 5xx 那条分支删了(它一删,末尾 `return False` 照样兜住,行为零变化),
    # 探针会抛 StopIteration。harness 看到的是「判据文件收集出错」,
    # 报 BAD-SYNTAX —— **看起来像变异器坏了,其实是被测代码变了**。
    # 一个崩掉的判据会把真发现藏起来,那比判据太松更坏。
    #
    # 条件用 **AST 常量** 判定,不写 `"500" in ast.unparse(n.test)`。
    # 首版就是这么写的,被 tests/test_source_checks_are_structural.py 当场
    # 拦下:那个门禁管的就是「取源码文本再跟字面量比」,unparse 把 AST
    # 变回文本,等于用了 AST 又退回去。`ast.unparse` 只能出现在报错文案里。
    branch = next(
        (n for n in ast.walk(fn)
         if isinstance(n, ast.If) and _mentions_int(n.test, 500)),
        None,
    )
    assert branch is not None, (
        "_is_interesting 里找不到 5xx 分支 —— 判定规则被删改了,"
        "本判据需要跟着重写"
    )
    comment = _strip_quoted(_comment_on_line(_DIRSCAN.read_text(encoding="utf-8"),
                                            branch.lineno - 1))
    assert "500 算" not in comment, (
        f"5xx 分支上方的注释又在说「500 算」,但代码是 return False:{comment!r}"
    )
    assert "不算" in comment, f"5xx 注释没说清它不算:{comment!r}"
