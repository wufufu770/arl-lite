"""r103 变异测试:验「文案不许和代码矛盾 + 声明过未实现不许静默」在守。

## 主题

两处实测出来的退化:

1. `arl-lite run --preset fast` → rc=0,输出**零字**提及 preset。
   `TaskRunner.run` 的 `preset` 参数函数体零引用,只在 docstring 里
   写着「暂未实现」——而 docstring 是给开发者看的,终端前的用户看不见。
2. `integrations/dirscan.py::_is_interesting` 三处文案全在描述 path:
   docstring 写「哪些 **path** 算 interesting」(实际只看 status)、
   三个参数完整传入但零引用、行内注释写「# 500 算」而下一行是
   `return False`。

## 为什么这轮的覆盖变异占一半

判据是**断言文案与代码一致**的断言。这类判据天生有个致命弱点:
它太容易恒真。一个只会 `assert docstring` 非空的判据,对着一个
彻头彻尾说谎的 docstring 照样绿。所以 C 组专门拆判据,证明那几条
确实是**独占**防线 —— 拆掉之后剩下的仍然全绿,才算真的在守。

## 变异清单

实现变异(期望全被杀):
  M1 删掉 log 里的 preset 警告            → 判据 1 红
  M2 警告改成不含用户值的固定文案          → 判据 1 红
  M3 警告条件写窄成 `preset and modules`   → 判据 1 红(测试走 modules=[])
  M4 删掉 CLI 的提示行                    → 判据 2 红
  M5 CLI 提示退化成固定横幅                → 判据 2 红
  M6 docstring 改回宣称按 path 判         → 判据 4 红
  M7 5xx 注释改回「# 500 算」            → 判据 9 红
  M8 5xx 分支 return True                → 判据 5 红
  M9 整个删掉 5xx 分支                    → 判据 7 红(行为零变化!)
  M10 加一条真用 path 的 soft-404 分支     → 判据 6 + 7 红
  M11 删掉 5xx 分支上方的注释              → 判据 8 红

覆盖变异(期望全存活):
  C1 拆掉判据 1(log 带哨兵值那条)
  C2 拆掉判据 5(状态码表核对那条)
  C3 拆掉判据 9(5xx 注释那条)
  C4 拆掉判据 3(不给 preset 不刷屏那条)

## M9 值得单独说

把 5xx 那条分支**整个删掉**,末尾的 `return False` 照样兜住,
运行时行为**一模一样**。唯一会红的是判据 7(分支计数)。

这是本轮最贵的一条变异:它证明判据 7 不是摆设。判据 5 那张
状态码表抓不到它 —— 表里 500 的期望就是 False,删掉分支后
真值还是 False。**行为表抓不到「多了一条和少了一条」**,
分支计数才抓得到。

## 写判据时当场栽的三次(已修)

**一、修好文案后我顺手在注释里补了一句「原注释写『500 算』」**——
那是**引述反面例子做解释**,不是声称。按字面查的判据把它判成说谎,
红了两条。

两条路:删解释,或者让判据分得清。删解释的代价很实在(r100 刚因为
「文档没写清当初为什么这么改」栽过一次)。所以定约定:**引述一律用
`「」` 包起来**,判据用 `_strip_quoted` 剥掉引述再查断言。

**二、判据 9 原本用裸 `next()` 找 5xx 分支**,M9 一删分支它就抛
StopIteration,harness 报 BAD-SYNTAX —— **看起来像变异器坏了,
其实是被测代码变了**。已加默认值 + 明确断言。

**三、判据 9 里写了 `"500" in ast.unparse(n.test)`,被
`tests/test_source_checks_are_structural.py` 当场拦下**,全量门禁
test_baseline 因此 FAIL(+1 failed)。

讽刺之处:我用的确实是 AST,但 `ast.unparse` 把它**变回文本**再子串
匹配,等于「用了 AST 又退回去」—— 那个门禁管的正是这个。反手一
条它就绿了:分支定位走 `ast.Constant` 节点,注释内容走 `tokenize`
的 `COMMENT` token(字符串里的 `#` 产出的不是 COMMENT,骗不过)。

**教训比修复本身值钱:判据刚写完时是「我以为合规」的状态,不是合规
的状态。仓库里已有的门禁比我更清楚什么算合规 —— 先让它过一遍,
比事后被拦下来便宜。**

## CLAIMS 写反了一次(改 harness,没改判据)

M10 是纯插入(soft-404 分支加在 2xx 前面,原分支原样保留),
我却给它写了 `must_not`;M11 是纯删除,却给它写了 `must_have`。
两条都报 BAD-MUTANT。**变异器工作正常,是我把「纯插入 must_not 留空、
纯删除 must_have 留空」这条约定记岔了。** 判据没毛病,改声明。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
CLAIMS 只写字面量,不写名字引用。
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CRIT = REPO / "tests" / "test_no_silently_ignored_input.py"
RUNNER = REPO / "arl_lite" / "core" / "task_runner.py"
CLI = REPO / "arl_lite" / "cli.py"
DIRSCAN = REPO / "arl_lite" / "integrations" / "dirscan.py"

TARGET = ["tests/test_no_silently_ignored_input.py"]

_RUN_SUMMARY_RE = re.compile(r"^(?:=+\s*)?(.*\bin [\d.]+s.*)$", re.M)
_ERROR_IN_SUMMARY_RE = re.compile(r"\d+\s+errors?\b")


def _run_itself_broke(outp: str) -> bool:
    lines = _RUN_SUMMARY_RE.findall(outp)
    if not lines:
        return True
    tail = lines[-1]
    if "no tests ran" in tail:
        return True
    if _ERROR_IN_SUMMARY_RE.search(tail):
        return "AssertionError" not in outp
    return False


# ── M1/M2/M3:TaskRunner 的 preset 警告 ──
A_WARN = (
    "        if preset:\n"
    '            log.warning(f"preset={preset!r} 未实现(Phase 2),已忽略")\n'
)
M1_GONE = "        # 变异 M1:警告整条删掉,preset 回到静默\n"
M2_FIXED = (
    "        if preset:\n"
    '            log.warning("preset 功能未实现(Phase 2)")  # 变异 M2:不含用户传的值\n'
)
M3_NARROW = (
    "        if preset and modules:  # 变异 M3:条件写窄,有任务才喊\n"
    '            log.warning(f"preset={preset!r} 未实现(Phase 2),已忽略")\n'
)

# ── M4/M5:CLI 的提示行 ──
A_CLI = (
    "    if args.preset:\n"
    '        print(f"[!] --preset {args.preset} 未实现(Phase 2),已忽略")\n'
)
M4_GONE = "    # 变异 M4:提示行删掉,CLI 回到静默\n"
M5_FIXED = (
    "    if args.preset:\n"
    '        print("[!] --preset 未实现(Phase 2)")  # 变异 M5:不回显用户传的值\n'
)

# ── M6:docstring 改回来说谎 ──
A_DOC = (
    '    """启发式:哪些**响应**算 interesting。判定**只看 status**。\n'
    "\n"
    "    `path`/`length`/`body_hash` 调用方完整传入但函数体零引用(soft-404\n"
    "    启发式预留,Phase 2)。r103 前写的是「哪些 path 算 interesting」。\n"
    '    """\n'
)
M6_LIES = (
    '    """启发式:哪些 path 算 interesting"""  # 变异 M6:文档又变回说谎\n'
)

# ── M7:5xx 注释改回反话 ──
A_5XX = (
    "    # 5xx **不算**:通常是 service 挂了(原注释写「500 算」,与下一行打架)\n"
    "    if status in (500, 502, 503):\n"
    "        return False\n"
)
M7_LIES = (
    "    # 500 算(可能存在但 server error)  # 变异 M7:注释又说反话\n"
    "    if status in (500, 502, 503):\n"
    "        return False\n"
)

# ── M8:5xx 真的返回 True ──
M8_TRUE = (
    "    # 5xx **不算**:通常是 service 挂了(原注释写「500 算」,与下一行打架)\n"
    "    if status in (500, 502, 503):\n"
    "        return True  # 变异 M8:行为漂移到「算」\n"
)

# ── M9:整个删掉 5xx 分支(末尾 return False 兜住,行为零变化)──
M9_GONE = "    # 变异 M9:5xx 分支整条删掉,末尾的 return False 照样兜\n"

# ── M10:加一条真用 path 的 soft-404 分支 ──
A_2XX = (
    "    # 2xx 永远 interesting\n"
    "    if 200 <= status < 300:\n"
    "        return True\n"
)
M10_SOFT404 = (
    "    # 变异 M10:soft-404 启发式(同路径不同 body 长度 → 不算)\n"
    "    if status == 200 and path.endswith(\"login\") and length > 0 and body_hash:\n"
    "        return False\n"
    "    # 2xx 永远 interesting\n"
    "    if 200 <= status < 300:\n"
    "        return True\n"
)

# ── M11:删掉 5xx 分支上方的注释 ──
M11_NO_COMMENT = (
    "    if status in (500, 502, 503):\n"
    "        return False\n"
)

# ── 覆盖变异(桩的 def 行整行照抄)──
C1_BODY = "async def test_task_runner_warns_with_the_preset_value_it_ignored(caplog):"
C1_END = "def test_cli_run_echoes_preset_attribute_not_a_fixed_banner():"
C2_BODY = "def test_interesting_branch_table_matches_actual_behavior():"
C2_END = "def test_interesting_ignores_path_length_and_hash_today():"
C3_BODY = "def test_5xx_comment_does_not_claim_5xx_counts():"
C4_BODY = "async def test_preset_warning_only_when_preset_actually_given(caplog):"
C4_END = "# =====================================================================\n# 方向二:文案不许和代码矛盾"

CLAIMS = {
    "M1-警告被删": (
        ["        # 变异 M1:警告整条删掉,preset 回到静默"],
        ["        if preset:\n"
         '            log.warning(f"preset={preset!r} 未实现(Phase 2),已忽略")\n'],
    ),
    "M2-警告不含用户值": (
        ['            log.warning("preset 功能未实现(Phase 2)")  # 变异 M2:不含用户传的值'],
        ['            log.warning(f"preset={preset!r} 未实现(Phase 2),已忽略")'],
    ),
    "M3-警告条件写窄": (
        ["        if preset and modules:  # 变异 M3:条件写窄,有任务才喊"],
        ["        if preset:\n"
         '            log.warning(f"preset={preset!r} 未实现(Phase 2),已忽略")\n'],
    ),
    "M4-CLI提示被删": (
        ["    # 变异 M4:提示行删掉,CLI 回到静默"],
        ["    if args.preset:\n"
         '        print(f"[!] --preset {args.preset} 未实现(Phase 2),已忽略")\n'],
    ),
    "M5-CLI提示是固定横幅": (
        ['        print("[!] --preset 未实现(Phase 2)")  # 变异 M5:不回显用户传的值'],
        ['        print(f"[!] --preset {args.preset} 未实现(Phase 2),已忽略")'],
    ),
    "M6-docstring改回说谎": (
        ['    """启发式:哪些 path 算 interesting"""  # 变异 M6:文档又变回说谎'],
        ["    `path`/`length`/`body_hash` 调用方完整传入但函数体零引用(soft-404"],
    ),
    "M7-5xx注释改回反话": (
        ["    # 500 算(可能存在但 server error)  # 变异 M7:注释又说反话"],
        ["    # 5xx **不算**:通常是 service 挂了"],
    ),
    "M8-5xx真的返回True": (
        ["        return True  # 变异 M8:行为漂移到「算」"],
        ["        return False\n    return False\n"],
    ),
    "M9-5xx分支被删": (
        ["    # 变异 M9:5xx 分支整条删掉,末尾的 return False 照样兜"],
        ["    # 5xx **不算**:通常是 service 挂了"],
    ),
    "M10-加了soft404分支": (
        ["    # 变异 M10:soft-404 启发式(同路径不同 body 长度 → 不算)"],
        [],  # 纯插入:2xx 分支原样还在,must_not 无从指起
    ),
    "M11-5xx分支上方注释被删": (
        [],  # 纯删除:只少了那行注释,must_have 无从指起
        ["    # 5xx **不算**:通常是 service 挂了(原注释写「500 算」,与下一行打架)\n"],
    ),
    "C1-拆掉带哨兵值那条": (["    pass  # 变异 C1:整条判据没了"], []),
    "C2-拆掉状态码表那条": (["    pass  # 变异 C2:整条判据没了"], []),
    "C3-拆掉5xx注释那条": (["    pass  # 变异 C3:整条判据没了"], []),
    "C4-拆掉不刷屏那条": (["    pass  # 变异 C4:整条判据没了"], []),
}

MUTANTS = [
    ("M1-警告被删", lambda p: _apply(p, A_WARN, M1_GONE), False, (RUNNER,)),
    ("M2-警告不含用户值", lambda p: _apply(p, A_WARN, M2_FIXED), False, (RUNNER,)),
    ("M3-警告条件写窄", lambda p: _apply(p, A_WARN, M3_NARROW), False, (RUNNER,)),
    ("M4-CLI提示被删", lambda p: _apply(p, A_CLI, M4_GONE), False, (CLI,)),
    ("M5-CLI提示是固定横幅", lambda p: _apply(p, A_CLI, M5_FIXED), False, (CLI,)),
    ("M6-docstring改回说谎", lambda p: _apply(p, A_DOC, M6_LIES), False, (DIRSCAN,)),
    ("M7-5xx注释改回反话", lambda p: _apply(p, A_5XX, M7_LIES), False, (DIRSCAN,)),
    ("M8-5xx真的返回True", lambda p: _apply(p, A_5XX, M8_TRUE), False, (DIRSCAN,)),
    ("M9-5xx分支被删", lambda p: _apply(p, A_5XX, M9_GONE), False, (DIRSCAN,)),
    ("M10-加了soft404分支", lambda p: _apply(p, A_2XX, M10_SOFT404), False, (DIRSCAN,)),
    ("M11-5xx分支上方注释被删", lambda p: _apply(p, A_5XX, M11_NO_COMMENT), False, (DIRSCAN,)),
]

COVERAGE_MUTANTS = [
    ("C1-拆掉带哨兵值那条",
     lambda p: _replace_fn(p, C1_BODY, C1_BODY + "\n    pass  # 变异 C1:整条判据没了\n",
                           C1_END), True, (CRIT,)),
    ("C2-拆掉状态码表那条",
     lambda p: _replace_fn(p, C2_BODY, C2_BODY + "\n    pass  # 变异 C2:整条判据没了\n",
                           C2_END), True, (CRIT,)),
    ("C3-拆掉5xx注释那条",
     lambda p: _replace_fn(p, C3_BODY, C3_BODY + "\n    pass  # 变异 C3:整条判据没了\n",
                           ""), True, (CRIT,)),
    ("C4-拆掉不刷屏那条",
     lambda p: _replace_fn(p, C4_BODY, C4_BODY + "\n    pass  # 变异 C4:整条判据没了\n",
                           C4_END), True, (CRIT,)),
]


def _check_claim_points_at_one_place(name: str, original: str) -> None:
    if name not in CLAIMS:
        raise AssertionError(f"变异 {name!r} 没有自检声明")
    must_have, must_not = CLAIMS[name]
    if not must_have and not must_not:
        raise AssertionError(f"变异 {name!r} 的声明是空的 —— 等于没声明")
    for piece in must_not:
        n = original.count(piece)
        if n != 1:
            raise AssertionError(
                f"变异 {name!r} 的声明串 {piece!r} 在改前出现 {n} 次 —— 指不准位置")
    for piece in must_have:
        if piece in original:
            raise AssertionError(
                f"变异 {name!r} 的 must_not/must_have 写反了:{piece!r} 改前就存在")


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
    if out == path.read_text(encoding="utf-8"):
        raise AssertionError("变异是空操作 —— 什么都不会变,写了也是白写")
    import ast
    try:
        ast.parse(out)
    except SyntaxError as e:
        raise AssertionError(
            f"变异会让 {path.name} 语法错误({e})。文件未写入。") from None
    path.write_text(out, encoding="utf-8")


def _apply(path: pathlib.Path, old: str, new: str) -> None:
    src = path.read_text(encoding="utf-8")
    n = src.count(old)
    if n != 1:
        raise AssertionError(
            f"变异锚点在 {path.name} 出现 {n} 次(必须恰好 1 次):{old[:70]!r}")
    _write_checked(path, src.replace(old, new, 1))


def _replace_fn(path: pathlib.Path, start: str, stub: str, end: str) -> None:
    src = path.read_text(encoding="utf-8")
    if src.count(start) != 1:
        raise AssertionError(f"变异锚点没唯一命中 {path.name}:{start!r}")
    if end:
        if src.count(end) != 1:
            raise AssertionError(f"变异结束标记没唯一命中 {path.name}:{end!r}")
        j = src.index(end, src.index(start))
    else:
        j = len(src)  # C3 是文件里最后一个函数,后面没有标记
    if j <= src.index(start):
        raise AssertionError(
            f"结束标记 {end!r} 出现在起点**之前** —— 标记写反了,不是判据的问题")
    _write_checked(path, src[:src.index(start)] + stub + src[j:])


def _run_criterion():
    return subprocess.run(
        [sys.executable, "-B", "-m", "pytest", *TARGET, "-q",
         "-p", "no:cacheprovider", "-W", "ignore::DeprecationWarning"],
        capture_output=True, text=True, cwd=REPO, timeout=600,
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
            except Exception as e:
                out.append((name, "BAD-MUTANT",
                            f"变异器自己抛了 {type(e).__name__}: {e}"[:400]))
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
        print(f"\n=== r103 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:24s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:300])
    print(f"\nr103 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
