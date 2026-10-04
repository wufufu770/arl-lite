"""r81:变异锚点必须整体照抄语句,只锚首行会留下孤儿行

## 这轮量到的(不是推测)

全库 40 个 mutcheck 脚本(r41–r80)逐个扫了一遍 `_apply` 的锚点:

    静态可解析的 old/new 对: 84
    解析不出的 4 对           : r65/r68/r70/r75 —— 锚子是**运行时算出来的**
                              (`CONST.split("\\n")[0] + "\\n"`、`CONST + ...`、
                              `CONST.replace(变量, ...)`),不是字面量
    old 侧是「半句」的 4 处    : r66:91 / r67:116 / r68:94 / r71:93
                              形状全是 `assert X, (` 或 `unexpected = {`

**4 处全是 r71 及更早的历史脚本**,r74 之后的全是整句锚点。

## 一个差点被我漏掉的发现

`new` 侧另有 7 处「半句」,第一反应是它们也是 bug。**不是。**
它们是**成对**的:old 也是半句,替换后故意复用文件里原有的尾巴 ——
`assert X, (` 把首行换掉,后面那几行消息原样接着,拼出来正好是完整语句。
所以真正的判据不是「每侧都得整句」,而是**两侧状态必须一致**:

    整句 ↔ 整句   安全
    半句 ↔ 半句   合法(靠文件原有尾巴接上)
    整句 ↔ 半句   **危险**:替换后会多出/少掉括号,直接 SyntaxError

实测 80 对可解析的里 **0 处不一致**。

## 判定方法:边词法分析边收集,EOF 也算数

一开始我用 `textwrap.dedent` + `ast.parse` 判「能不能独立解析」,错得很离谱:
  - r74 那个锚点在原文上下文里完全合法,dedent 之后反而缩进错乱 → 误报
  - `tokenize` 遇到 EOF 处未闭合括号直接抛异常,我把那批**真 bug 当成
    「不是 Python」跳过了** —— 恰恰把要抓的漏掉
  - 解析器只认「模块级字符串常量名」,结果 `CRIT_ANCHORS["key"]`(字典下标)
    和 `CONST.replace(a, b)` 全被静默跳过,却报了个漂亮的「0 处不一致」

最后这三条都是**同一个病**:探针静默漏报,然后我拿它的输出下结论。
所以判据里专门有一条守「扫描器真的扫到了东西」。

判据本身只看**括号配平**:把锚点喂给 `tokenize`,边吐 token 边压栈,
EOF 处的异常也接着算(未闭合的括号就留在栈里)。shell 锚点(栈恒空)和
字典片段锚点(`"error": {...},` 配平)都自然放行,零误伤。
"""
from __future__ import annotations

import ast
import io
import pathlib
import tokenize

REPO = pathlib.Path(__file__).resolve().parent.parent
MUTCHECK_DIR = REPO / "devloop"

_OPEN, _CLOSE = "([{", ")]}"


def balance(text: str) -> str:
    """锚点是「整句」还是「半句」——只看括号配不配平

    字符串和注释里的括号不算(tokenize 只吐 OP token)。
    EOF 处抛异常是「括号没收尾」的典型表现,压栈结果照样算数。
    """
    stack: list[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type != tokenize.OP:
                continue
            if tok.string in _OPEN:
                stack.append(tok.string)
            elif tok.string in _CLOSE:
                if stack:
                    stack.pop()
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return "整句" if not stack else "半句"


def _module_anchors(tree: ast.Module) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """收齐模块级字符串常量与字符串字典(锚点的四种写法之一)"""
    consts: dict[str, str] = {}
    dicts: dict[str, dict[str, str]] = {}
    for n in tree.body:
        if not (isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)):
            continue
        name = n.targets[0].id
        if isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
            consts[name] = n.value.value
        elif isinstance(n.value, ast.Dict):
            d = {k.value: v.value for k, v in zip(n.value.keys, n.value.values)
                 if isinstance(k, ast.Constant) and isinstance(k.value, str)
                 and isinstance(v, ast.Constant) and isinstance(v.value, str)}
            if d:
                dicts[name] = d
    return consts, dicts


def resolve(node, consts, dicts):
    """把锚点表达式还原成字符串;认不出来就返回 None(如实记账,不猜)"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in consts:
        return consts[node.id]
    if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
            and node.value.id in dicts and isinstance(node.slice, ast.Constant)
            and node.slice.value in dicts[node.value.id]):
        return dicts[node.value.id][node.slice.value]
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "replace"):
        base = resolve(node.func.value, consts, dicts)
        args = [a.value for a in node.args
                if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        if base is not None and len(args) == 2:
            return base.replace(*args)
    return None


def anchor_pairs(path: pathlib.Path) -> list[tuple[int, str, str, str]]:
    """`(行号, old, new, 还原出的 old 表达式)`,只收能静态还原的"""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    consts, dicts = _module_anchors(tree)
    out = []
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == "_apply" and len(call.args) >= 3):
            continue
        old = resolve(call.args[1], consts, dicts)
        new = resolve(call.args[2], consts, dicts)
        if old is None or new is None:
            continue
        out.append((call.lineno, old, new, ast.unparse(call.args[1])))
    return out


def has_write_guard(path: pathlib.Path) -> bool:
    """脚本有没有 r76 之后那套 `_write_checked` —— 用脚本自己的能力判定归属

    刻意**不用轮次号当阈值**:轮次号是历史,能力是现状。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(isinstance(n, ast.FunctionDef) and n.name == "_write_checked"
               for n in ast.walk(tree))


def scripts() -> list[pathlib.Path]:
    return sorted(MUTCHECK_DIR.glob("mutcheck_*.py"))


# ── 正控制组:r81 实测出的 4 处真实半句锚点(逐字) ──

_HALF_ANCHORS = {
    "r66 名单判据只锚首行": "    assert r.returncode != 0, (",
    "r67 端到端判据只锚首行": "    assert after.returncode == 0, (",
    "r68 判据只锚首行": "    assert r.returncode != 0, (",
    "r71 预期字典只锚首行": "    unexpected = {",
}

# 配平但**不是**整句的:shell 文本、字典片段。
# 它们不是 Python 语句,但也不该被判成「半句 bug」—— 判据要放行。
_MUST_NOT_FLAG = {
    "shell 锚点(r66 README 那条)": "git clone <repo-url> arl-lite",
    "字典片段锚点(r71 那条)": '"error": {"code": -32000, "message": _NOT_INITIALIZED},',
    "整句多行 assert(r74 的形状)": (
        '    assert "-w" in text, (\n'
        '        f"workspace 的报错没指出真正的入口:{text!r}"\n'
        "    )"
    ),
    "两条并列语句(r74 的形状)": (
        "        return 2\n\n    storage = Storage(workspace=args.workspace)"
    ),
}


# ── 判据 ──

def test_detector_calls_every_real_half_anchor_half():
    """正控制组:那 4 处真实半句必须被判成半句,否则检测器坏了"""
    wrong = {n: balance(s) for n, s in _HALF_ANCHORS.items() if balance(s) != "半句"}
    assert not wrong, f"检测器没认出这些半句锚点:{wrong}"


def test_detector_does_not_invent_half_anchors():
    """负控制组:shell / 字典片段 / 多行整句都不该被判成半句"""
    wrong = {n: balance(s) for n, s in _MUST_NOT_FLAG.items() if balance(s) != "整句"}
    assert not wrong, (
        f"检测器误伤了 {len(wrong)} 种合法锚点:{wrong}\n"
        "守卫比它守的东西还严,就会有人绕过它"
    )


def test_modern_mutcheck_scripts_never_use_a_half_anchor():
    """带 `_write_checked` 的脚本(现代约定)不许再用半句锚点

    r81 实测:r74 之后的脚本全是整句锚点,最早的半句出现在 r66–r71。
    """
    bad = []
    for s in scripts():
        if not has_write_guard(s):
            continue
        for lineno, old, new, expr in anchor_pairs(s):
            if balance(old) != "整句":
                bad.append(f"{s.name}:{lineno}  {expr[:60]}")
    assert not bad, (
        "现代 mutcheck 脚本里出现了半句锚点(只照抄了首行):\n  "
        + "\n  ".join(bad)
        + "\n锚点要整体照抄完整语句,含结尾括号"
    )


def test_half_anchors_are_always_paired_with_half_replacements():
    """真正的判据是**两侧一致**,不是「每侧都得整句」

    `assert X, (` 故意只换首行、让文件里原有的消息行接上,这本身是合法的。
    危险的是整句换半句(或反过来)—— 替换完括号就散了。
    r81 实测 80 对可静态还原的锚点里 0 处不一致。
    """
    mismatched = []
    total = 0
    for s in scripts():
        for lineno, old, new, expr in anchor_pairs(s):
            total += 1
            if balance(old) != balance(new):
                mismatched.append(
                    f"{s.name}:{lineno}  old={balance(old)} new={balance(new)}  {expr[:50]}")
    assert not mismatched, (
        f"锚点两侧的配平状态不一致,替换后括号会散:\n  " + "\n  ".join(mismatched))
    assert total >= 80, (
        f"只还原出 {total} 对锚点,比 r81 实测的 80 还少 —— "
        "解析器退化了(它曾经漏掉 CRIT_ANCHORS[...] 这类字典下标),"
        "这样的扫描结果不能拿来下结论"
    )


def test_the_sweep_actually_reads_every_script():
    """守住「扫描器真的扫到了东西」——扫不到文件的守卫等于没写"""
    found = scripts()
    assert len(found) >= 40, f"只扫到 {len(found)} 个 mutcheck 脚本,扫描范围不对"
    for s in found:
        tree = ast.parse(s.read_text(encoding="utf-8"))
        assert tree.body, f"{s.name} 解析出来是空的"
    with_guard = [s.name for s in found if has_write_guard(s)]
    assert with_guard, "一个带写盘守卫的脚本都没扫到 —— 现代约定判定失效了"
