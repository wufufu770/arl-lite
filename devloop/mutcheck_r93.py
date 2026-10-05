"""r93 变异测试:验「门禁不许惩罚全绿」这件事在守。

## 主题

r92 把 9 条从没执行过的 async 测试修好之后,全量实测 1234 passed / 0 failed,
`test_baseline` 反而报红,detail 是 `could not parse pytest summary`。

根因:`_PYTEST_SUMMARY_RE` 要求 summary 行出现 `N failed`,而 failed 是 pytest
的**可选**字段——全绿时它根本不打印。这个门禁惩罚的恰好是这套协议要的那个
结果,而且它在成功时刻说「我读不懂」,读起来像环境坏了,像 pytest 崩了。

## 修法的核心风险不是「红」,是「绿」

把 `failed` 改成可选,全绿这一条是修好了。但同一批实测里还有两种**同样没有
`failed`** 的终局:

    收集中断  1 error in 0.16s       import 断了,一条都没跑
    空目录    no tests ran in 0.00s   没收集到任何用例

一并放过去就是**假绿**:套件一条没跑而门禁报通过。假红会被人去查,假绿不会。
所以 M1/M2/M3 三条分别盯这三个方向,它们必须**朝相反方向**判。

## 变异清单

实现变异(期望全被杀):
  M1 把 failed 变回必需(还原原 bug)        → 全绿判据红
  M2 守里去掉 error(收集中断放过去)        → 收集中断判据红
  M3 守里去掉 ran(空跑放过去)              → 空跑判据红
  M4 取第一行而不是最后一行                → 取行判据红
  M5 errors 恒读成 0                       → 混合形态判据红
  M6 拆掉「全绿提示收紧名单」              → 收紧判据红
  M7 门禁自己把名单清空                    → 不许自改名单判据红

覆盖变异(期望全存活):
  C1 拆掉「取最后一行」那条判据
  C2 拆掉「解析器接进了 run()」那条判据

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
CLAIMS 只写字面量。锚点必须**括号配平且唯一**。
所有变异统一走 _write_checked,写盘前 ast.parse。
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
GATES = REPO / "arl_lite" / "devloop" / "gates.py"
CRIT = REPO / "tests" / "test_baseline_gate_does_not_punish_all_green.py"

TARGET = ["tests/test_baseline_gate_does_not_punish_all_green.py"]

COLLECTION_FAILED = ("error during collection", "ERROR collecting", "Interrupted:")

# r93 修正:上面那组**子串**判据在本轮会假阳性。
#
# 本轮判据的输入里就写着 "!!!! Interrupted: 1 error during collection !!!!" 和
# "error during collection" —— 变异被正确杀掉时,pytest 打印的失败信息里会
# 带上判据自己的源码,于是 `any(bad in outp ...)` 命中,**把一个被杀的变异
# 报成 BAD-SYNTAX**。首跑 M2/M5 就这么冤了一回(实测被杀死的判据白纸黑字
# 列在输出里,却记成 BAD-SYNTAX)。
#
# 所以「这一轮自己是不是跑炸了」不能靠子串,得看它**自己那行终局 summary**。
_RUN_SUMMARY_RE = re.compile(r"^(?:=+\s*)?(.*\bin [\d.]+s.*)$", re.M)
_ERROR_IN_SUMMARY_RE = re.compile(r"\d+\s+errors?\b")


def _run_itself_broke(outp: str) -> bool:
    """这一轮 pytest 自己是不是炸了(变异写坏了文件),而不是「判据报红」"""
    lines = _RUN_SUMMARY_RE.findall(outp)
    if not lines:
        return True  # 连终局 summary 都没有,无从判断,保守当成炸了
    tail = lines[-1]
    if "no tests ran" in tail:
        return True
    return _ERROR_IN_SUMMARY_RE.search(tail) is not None

# ── 实现侧锚点(整体照抄,括号配平,改前各出现 1 次)──

COUNTS_LINE = '    counts = {c.group("what"): int(c.group("n")) for c in _OUTCOME_COUNT_RE.finditer(tail)}\n'
# 整段写全而不是 `COUNTS_LINE + ...` 拼出来:拼出来的锚点静态还原不出来,
# 锚点守卫会**静默跳过**它,那这一对就没被检查过(同一个病的又一次)。
COUNTS_PLUS_FAILED_REQUIRED = (
    '    counts = {c.group("what"): int(c.group("n")) for c in _OUTCOME_COUNT_RE.finditer(tail)}\n'
    '    if "failed" not in counts:  # 变异 M1:failed 变回必需字段\n'
    '        return None\n'
)

GUARD = '        if outcome["errors"] or not outcome["ran"]:\n'
GUARD_NO_ERRORS = '        if not outcome["ran"]:\n'
GUARD_NO_RAN = '        if outcome["errors"]:\n'

TAIL_LAST = "    tail = lines[-1]\n"
TAIL_FIRST = "    tail = lines[0]  # 变异 M4:取第一行\n"

ERRORS_SUM = '"errors": counts.get("error", 0) + counts.get("errors", 0), "ran": True}'
ERRORS_ZERO = '"errors": 0, "ran": True}'

SHRINK_BLOCK = (
    '            if known and not failed:\n'
    '                measured["allowed_failures"] = sorted(known)\n'
    "                detail += (\n"
    '                    f"; all {len(known)} baseline failure(s) now pass, "\n'
    "                    f\"shrink allowed_failures: {'; '.join(sorted(known))}\"\n"
    "                )\n"
)

# 锚点只取**一条完整语句**。头一版把 `detail += (` 那个开括号也带上,r81 的
# 锚点守卫判成「半句」当场报红 —— 守卫说得对:半句锚点替换后括号会散。
LIST_ASSIGN = '                measured["allowed_failures"] = sorted(known)\n'
LIST_ASSIGN_EMPTY = '                measured["allowed_failures"] = []  # 变异 M7:门禁自改名单\n'

# ── 判据侧锚点 ──
#
# r93 自己栽的:头一版拿 `def test_xxx():` 那行当 must_not,于是 C1/C2 两条
# 覆盖变异全部 BAD-MUTANT —— 因为替换后的桩**开头就是同一个 def 行**,
# must_not 永远验不过,报的是「替换没有做到它名字声称的事」,其实替换做到了。
# must_not 必须指向**函数体内真正被删掉**的那一行。

C1_BODY = (
    'def test_the_parser_takes_the_last_summary_line_not_the_first():\n'
    '    """测试自己打印的 `generated in 1.00s` 可能先出现\n'
    '\n'
    '    取第一行会把捕获输出里的句子当成判定依据。终局行一定在最后。\n'
    '    """\n'
    '    out = ("captured stdout: build generated in 1.00s\\n"\n'
    '           "warning: retry in 2.00s\\n"\n'
    '           + MEASURED_ALL_GREEN)\n'
    '    got = gates._pytest_outcome(out)\n'
    '    assert got == {"failed": 0, "passed": 1234, "errors": 0, "ran": True}, (\n'
    '        f"取错了行:{got}")\n'
)
C1_GONE = ('def test_the_parser_takes_the_last_summary_line_not_the_first():\n'
           "    pass  # 变异 C1:整条判据没了\n")

C2_BODY = (
    'def test_the_outcome_parser_is_actually_called_by_run():\n'
    '    """用 AST 查 `run()` 体内真的调了它,不是「文件里有这个函数」\n'
    '\n'
    '    r92 栽过这个:`if False:` 的变异让函数还在、就是没人叫它,而只查\n'
    '    `def` 那一行的守卫照样通过。判形状不判文本 —— r80 的守卫会把\n'
    '    unparse + 子串的写法当场逮住,那条说得对。\n'
    '    """\n'
    '    tree = ast.parse(GATES_PY.read_text(encoding="utf-8"))\n'
    '    gate_cls = next(c for c in tree.body\n'
    '                    if isinstance(c, ast.ClassDef)\n'
    '                    and any(isinstance(s, ast.Assign)\n'
    '                            and any(isinstance(t, ast.Name)\n'
    '                                    and t.id == "name" for t in s.targets)\n'
    '                            and isinstance(s.value, ast.Constant)\n'
    '                            and s.value.value == "test_baseline"\n'
    '                            for s in c.body))\n'
    '    run_fn = next(n for n in gate_cls.body\n'
    '                  if isinstance(n, ast.FunctionDef) and n.name == "run")\n'
    '    called = {\n'
    '        c.func.id for c in ast.walk(run_fn)\n'
    '        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)\n'
    '    }\n'
    '    assert "_pytest_outcome" in called, (\n'
    '        "`_pytest_outcome` 写好了却没接进 TestBaselineGate.run() —— "\n'
    '        "解析器是死代码")\n'
)
C2_GONE = ('def test_the_outcome_parser_is_actually_called_by_run():\n'
           "    pass  # 变异 C2:整条判据没了\n")

CLAIMS = {
    # ── 全部写字面量,不写名字引用 ──
    # r86/r88/r89 各栽一次,r93 第四次栽:头一版图省事写了 `[ERRORS_SUM]`、
    # `[GUARD]`,自检守卫的 `ast.literal_eval` 当场抛 ValueError,
    # 七条判据一起红。声明串的意义就是「它确实出现在那个文件里」,
    # 指向一个变量等于把这件事交给别处,那正是 r80 说的「存在检查冒充行为
    # 检查」的远亲。
    #
    # 纯插入 → must_not 只能空(判别力全在 must_have 上)。
    "M1-把failed变回必需还原原bug": (
        ['    if "failed" not in counts:  # 变异 M1:failed 变回必需字段'],
        [],
    ),
    "M2-守里去掉error放行收集中断": (
        ['        if not outcome["ran"]:'],
        ['        if outcome["errors"] or not outcome["ran"]:\n'],
    ),
    "M3-守里去掉ran放行空跑": (
        ['        if outcome["errors"]:'],
        ['        if outcome["errors"] or not outcome["ran"]:\n'],
    ),
    "M4-取第一行而不是最后一行": (
        ["    tail = lines[0]"],
        ["    tail = lines[-1]\n"],
    ),
    "M5-errors恒读成0": (
        ['"errors": 0, "ran": True}'],
        ['"errors": counts.get("error", 0) + counts.get("errors", 0), "ran": True}'],
    ),
    # 纯删除 → must_have 只能空。判别力全在 must_not 上。
    "M6-拆掉全绿提示收紧名单": (
        [],
        ["            if known and not failed:\n"],
    ),
    "M7-门禁自己把名单清空": (
        ['                measured["allowed_failures"] = []'],
        ['                measured["allowed_failures"] = sorted(known)\n'],
    ),
    "C1-拆掉取最后一行那条判据": (
        ["    pass  # 变异 C1:整条判据没了"],
        ["    got = gates._pytest_outcome(out)\n"],
    ),
    "C2-拆掉解析器接线那条判据": (
        ["    pass  # 变异 C2:整条判据没了"],
        ['    assert "_pytest_outcome" in called, (\n'],
    ),
}

MUTANTS = [
    ("M1-把failed变回必需还原原bug",
     lambda p: _apply(p, COUNTS_LINE, COUNTS_PLUS_FAILED_REQUIRED), False, (GATES,)),
    ("M2-守里去掉error放行收集中断",
     lambda p: _apply(p, GUARD, GUARD_NO_ERRORS), False, (GATES,)),
    ("M3-守里去掉ran放行空跑",
     lambda p: _apply(p, GUARD, GUARD_NO_RAN), False, (GATES,)),
    ("M4-取第一行而不是最后一行",
     lambda p: _apply(p, TAIL_LAST, TAIL_FIRST), False, (GATES,)),
    ("M5-errors恒读成0",
     lambda p: _apply(p, ERRORS_SUM, ERRORS_ZERO), False, (GATES,)),
    ("M6-拆掉全绿提示收紧名单",
     lambda p: _apply(p, SHRINK_BLOCK, ""), False, (GATES,)),
    ("M7-门禁自己把名单清空",
     lambda p: _apply(p, LIST_ASSIGN, LIST_ASSIGN_EMPTY), False, (GATES,)),
]

COVERAGE_MUTANTS = [
    ("C1-拆掉取最后一行那条判据", lambda p: _apply(p, C1_BODY, C1_GONE), True, (CRIT,)),
    ("C2-拆掉解析器接线那条判据", lambda p: _apply(p, C2_BODY, C2_GONE), True, (CRIT,)),
]


def _check_claim_points_at_one_place(name: str, original: str) -> None:
    if name not in CLAIMS:
        raise AssertionError(f"变异 {name!r} 没有自检声明")
    for piece in CLAIMS[name][1]:
        n = original.count(piece)
        if n != 1:
            raise AssertionError(
                f"变异 {name!r} 的声明串 {piece!r} 在改前出现 {n} 次 —— 指不准位置")


def _verify_claim(name: str, mutated: str) -> None:
    if name not in CLAIMS:
        raise AssertionError(f"变异 {name!r} 没有自检声明")
    must_have, must_not = CLAIMS[name]
    missing = [s for s in must_have if s not in mutated]
    leftover = [s for s in must_not if s in mutated]
    if missing or leftover:
        raise AssertionError(
            f"变异 {name!r} 的替换没有做到它名字声称的事: 缺 {missing} 仍在 {leftover}")


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
    src = path.read_text(encoding="utf-8")
    if old not in src:
        raise AssertionError(f"变异锚点没命中 {path.name}:{old[:70]!r}")
    _write_checked(path, src.replace(old, new, 1))


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
            try:
                _check_claim_points_at_one_place(
                    name, "\n".join(t.read_text(encoding="utf-8") for t in targets))
                for t in targets:
                    mutate(t)
                _verify_claim(name, "\n".join(
                    t.read_text(encoding="utf-8") for t in targets))
            except AssertionError as e:
                out.append((name, "BAD-MUTANT", str(e)[:400]))
                continue
            r = _run_criterion()
            outp = r.stdout + r.stderr
            if _run_itself_broke(outp):
                out.append((name, "BAD-SYNTAX", outp[-400:]))
                continue
            killed = r.returncode != 0
            detail = "killed" if killed else "survived"
            if killed == expect:
                detail = f"UNEXPECTED-{detail}"
            out.append((name, detail, outp[-500:] if killed == expect else ""))
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
        print(f"\n=== r93 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:34s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:300])
    print(f"\nr93 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.sandboxed(main))
