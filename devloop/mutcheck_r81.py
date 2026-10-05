"""r81 变异测试:验「变异锚点必须整体照抄语句」这条检查在守。

主题:变异锚点只照抄多行语句的**首行**(`assert X, (`),替换后文件里
剩下的消息行变成孤儿,直接 SyntaxError。历史上栽了四次
(r71 C1、r72 C2/C3、r74 C2、r77 C1),r74 加的 `ast.parse` 守卫把它从
「静默假结果」变成「响亮报错」,但没根治。

r81 把全库 40 个脚本(r41–r80)扫了一遍 `_apply` 的锚点,实测:

    静态可还原的 old/new 对   84
    还原不出的 4 对            r65/r68/r70/r75 —— 锚子是运行时算出来的
                               (`CONST.split("\\n")[0] + "\\n"`、
                               `CONST + ...`、`CONST.replace(变量, ...)`)
    old 侧是半句的 4 处        r66:91 / r67:116 / r68:94 / r71:93
    **两侧配平状态不一致的     0 处**

最后那条是这轮最值钱的发现:`new` 侧另有 7 处半句,**但它们不是 bug** ——
old 侧也是半句,替换后故意让文件里原有的尾巴接上,拼出来正好是完整语句。
所以判据不是「每侧都得整句」,而是**两侧状态必须一致**。

探测方法本身也栽了三次(r81 的教训,已写进判据 docstring):
  1. `textwrap.dedent` + `ast.parse` 判「能否独立解析」→ r74 那个合法锚点被误报
  2. `tokenize` 遇 EOF 未闭合括号直接抛异常 → **把要抓的 4 处当「非 Python」跳过**
  3. 解析器只认模块级字符串常量名 → `CRIT_ANCHORS["key"]` 全被静默跳过,
     却报了个漂亮的「0 处不一致」
同一个病:探针静默漏报,我拿它的输出下结论。所以判据里有一条专门守
「扫描器真的扫到了东西」。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
所有变异统一走 _write_checked,写盘前 ast.parse。

全部变异:
  M1 把 r80 的整句锚点截成半句           → **杀**。r66–r71 栽过的原样形状
  M2 检测器恒返回「整句」                → 杀。正控制组必须先红
  M3 两侧一致性判据只看 old              → 杀
  M4 「现代脚本」判定改成轮次号 >= 99     → 杀。能力派生的阈值不能被静默架空
  C1 「现代脚本不许半句锚点」放宽成恒真   → 期望存活
  C2 「两侧一致」判据放宽成恒真           → 期望存活
"""
from __future__ import annotations

import io
import pathlib
import subprocess
import sys
import tokenize

REPO = pathlib.Path(__file__).resolve().parent.parent

CRIT = REPO / "tests" / "test_mutcheck_anchors_are_whole_statements.py"
R80 = REPO / "devloop" / "mutcheck_r80.py"

TARGET = ["tests/test_mutcheck_anchors_are_whole_statements.py"]

COLLECTION_FAILED = ("error during collection", "ERROR collecting", "Interrupted:")

_OPEN, _CLOSE = "([{", ")]}"


def _bracket_balance(text: str) -> str:
    """锚点是整句还是半句 —— 本脚本自己也用这条,不是只在判据里写一遍"""
    stack: list[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type != tokenize.OP:
                continue
            if tok.string in _OPEN:
                stack.append(tok.string)
            elif tok.string in _CLOSE and stack:
                stack.pop()
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return "整句" if not stack else "半句"


def _write_checked(path: pathlib.Path, out: str) -> None:
    import ast
    if out == path.read_text(encoding="utf-8"):
        raise AssertionError("变异是空操作 —— 什么都不会变,写了也是白写")
    try:
        ast.parse(out)
    except SyntaxError as e:
        raise AssertionError(f"变异会让 {path.name} 语法错误({e})。文件未写入。") from None
    path.write_text(out, encoding="utf-8")


def _apply(path: pathlib.Path, old: str, new: str) -> None:
    """变异锚点必须**整体照抄语句**

    r81 把这条检查直接放进变异框架:只锚首行会留下孤儿行,
    写出去的文件语法就坏了(r66/r67/r68/r71 各栽过一次)。
    """
    if _bracket_balance(old) != "整句":
        raise AssertionError(
            f"锚点只照抄了半句(括号没配平),替换后会留下孤儿行:\n{old!r}\n"
            "锚点要整体照抄完整语句,含结尾括号"
        )
    src = path.read_text(encoding="utf-8")
    if old not in src:
        raise AssertionError(f"变异锚点没命中 {path.name}:{old[:70]!r}")
    _write_checked(path, src.replace(old, new, 1))


# ── 判据侧锚点(整块照抄,含结尾括号) ──

JUDGE_BALANCE = '    return "整句" if not stack else "半句"'
JUDGE_MODERN = (
    "    bad = []\n"
    "    for s in scripts():\n"
    "        if not has_write_guard(s):\n"
    "            continue\n"
    "        for lineno, old, new, expr in anchor_pairs(s):\n"
    '            if balance(old) != "整句":\n'
    '                bad.append(f"{s.name}:{lineno}  {expr[:60]}")'
)
JUDGE_PAIRING = (
    "            if balance(old) != balance(new):\n"
    "                mismatched.append(\n"
    '                    f"{s.name}:{lineno}  old={balance(old)} new={balance(new)}  {expr[:50]}")'
)
JUDGE_WITHHUARD = '    with_guard = [s.name for s in found if has_write_guard(s)]'

# ── r80 侧的锚点:把整句截成半句 ──

R80_CONTRACT = (
    'JUDGE_CONTRACT = (\n'
    '    "    assert _contract_refs(fn), (\\n"\n'
    '    \'        "_run_target 里没引用契约表 —— 资产清单从别处来了")\'\n'
    ')'
)
R80_CONTRACT_HALF = (
    'JUDGE_CONTRACT = (\n'
    '    "    assert _contract_refs(fn), (\\n"\n'
    ')'
)


def _m4_threshold_neutered(path: pathlib.Path) -> None:
    """把「现代脚本」判定从能力派生改成硬编码轮次号 —— 阈值被架空

    守护这条的是 `test_the_sweep_actually_reads_every_script`:它要求
    至少扫到一个带写盘守卫的脚本,否则整条主判据变成空转。
    """
    _apply(
        path,
        "def has_write_guard(path: pathlib.Path) -> bool:",
        "def has_write_guard(path: pathlib.Path) -> bool:\n"
        "    if int(path.stem[len('mutcheck_r'):]) >= 99:\n"
        "        return _has_write_guard_real(path)\n"
        "    return False\n"
        "\n"
        "\n"
        "def _has_write_guard_real(path: pathlib.Path) -> bool:",
    )


MUTANTS = [
    ("M1-把r80整句锚点截成半句", lambda p: _apply(
        p, R80_CONTRACT, R80_CONTRACT_HALF), False, (R80,)),
    ("M2-检测器恒返回整句", lambda p: _apply(
        p, JUDGE_BALANCE, '    return "整句"  # 变异'), False, (CRIT,)),
    ("M3-两侧一致判据只看old", lambda p: _apply(
        p, JUDGE_PAIRING,
        "            if balance(old) != \"整句\":\n"
        "                mismatched.append(\n"
        '                    f"{s.name}:{lineno}  old={balance(old)}  {expr[:50]}")'),
     False, (CRIT,)),
    ("M4-现代脚本判定改成轮次号>=99", _m4_threshold_neutered, False, (CRIT,)),
]

# ---- 覆盖变异:改坏判据自己,期望**存活**(真实现已经是对的) ----
COVERAGE_MUTANTS = [
    ("C1-现代脚本半句判据放宽成恒真", lambda p: _apply(
        p, JUDGE_MODERN, "    bad = []  # 变异:恒真"), True, (CRIT,)),
    ("C2-两侧一致判据放宽成恒真", lambda p: _apply(
        p, JUDGE_PAIRING,
        "            if False:  # 变异:恒真\n"
        "                pass"), True, (CRIT,)),
]


def _run_criterion():
    return subprocess.run(
        [sys.executable, "-B", "-m", "pytest", *TARGET, "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=REPO, timeout=1200,
    )


def _sweep(mutants) -> list:
    out = []
    for name, mutate, expect, targets in mutants:
        backups = {t: t.read_bytes() for t in targets}
        try:
            for t in targets:
                mutate(t)
            r = _run_criterion()
            outp = r.stdout + r.stderr
            if any(bad in outp for bad in COLLECTION_FAILED):
                out.append((name, "BAD-SYNTAX", outp[-400:]))
                continue
            killed = r.returncode != 0
            detail = "killed" if killed else "survived"
            if killed == expect:
                detail = f"UNEXPECTED-{detail}"
            out.append((name, detail, outp[-400:] if killed == expect else ""))
        finally:
            for t, data in backups.items():
                t.write_bytes(data)
    return out


def main() -> int:
    bad = 0
    for title, mutants in (
        ("实现变异(期望全被杀)", MUTANTS),
        ("覆盖变异(期望全存活)", COVERAGE_MUTANTS),
    ):
        print(f"\n=== r81 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:36s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:400])
    print(f"\nr81 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
