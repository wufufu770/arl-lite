
"""r85 变异测试:验「--log-level 垃圾值必须被拒、合法值必须真生效」在守。

主题:`--log-level` 原来没有 `choices`,靠 `getattr(logging, 值.upper(),
logging.INFO)` 兜底。r85 实测 `--log-level bogus` 与 `--log-level INFO`
在真会打日志的路径上输出**逐字相同**、rc=0、一个字都没提示。

原 bug 的形状不是「垃圾值没被拒」这么简单,而是**「值被接受了但静默无效」**。
所以判据分两半,变异也分两半验:
  - 垃圾值 → 必须 rc=2 且列出全部合法值(只说 invalid 等于让用户去猜)
  - 合法值 → 必须**真的改变日志**。只验 rc 的话,「永远返回 INFO」那种
    退化也能过,那跟原 bug 是同一个病。

## 探针本身先栽过一次

第一版探针用 `monitor changes`,它在没工作区时只打印一句提示、**根本不
产生日志记录**,于是八种取值输出全同、rc 全 0。看着像完美复现,实际只
证明了「没日志可看」。换成 `workspace create` + 全新 HOME 才测出真差别。
判据里因此把 `LOGGING_CMD` 固定下来,并在 docstring 里写明:换一条不打
日志的命令,这个判据就恒红 —— 那是探针的错,不是实现的错。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
所有变异统一走 _write_checked,写盘前 ast.parse。

全部变异:
  M1 拆掉 choices(退回静默兜底)      → 杀
  M2 拆掉 type=str.upper(弄坏小写)     → 杀
  M3 报错里不列合法值                  → 杀
  M4/M5 「合法值真生效」两条判据分别被拆掉 → 杀。只验 rc 的话,「永远 INFO」
                                       能蒙过去 —— 那跟原 bug 是同一个病
  M6 兜底那句加回来                    → 杀
  C1 「垃圾值必须被拒」判据放宽         → 存活
  C2 「必须列出全部合法值」判据放宽     → 存活
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
IMPL = REPO / "arl_lite" / "cli.py"
CRIT = REPO / "tests" / "test_log_level_garbage_is_rejected.py"

TARGET = ["tests/test_log_level_garbage_is_rejected.py"]

COLLECTION_FAILED = ("error during collection", "ERROR collecting", "Interrupted:")


# ANCHOR_ADDARG —— 整个 add_argument 调用(整块照抄,含结尾括号)
ANCHOR_ADDARG = (
    '    p.add_argument(\n'
    '        "--log-level", default="INFO", type=str.upper,\n'
    '        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],\n'
    '        help="日志级别,大小写均可(默认: INFO)",\n'
    '    )\n'
)

# ── 判据侧锚点 ──

JUDGE_MUST_REJECT = "        if rc != 2:"
JUDGE_MUST_LIST = "        missing = [lv for lv in VALID if lv not in out]"
# 整条断言,不是首行 —— 只锚首行会留下孤儿续行变 SyntaxError(r81 的老规矩,
# r85 又栽了一次)。声明串也用**跨行**的前两行,免得只标了首行。
JUDGE_DEBUG_EFFECT = (
    '    assert "[DEBUG]" in out, (\n'
    '        f"给了 DEBUG 却没有任何 DEBUG 记录 —— 级别没生效,"\n'
    '        f"那正是原 bug 的形状。实际输出:\\n{out}")\n'
)
JUDGE_INFO_NO_DEBUG = (
    '    assert "[DEBUG]" not in out, (\n'
    '        f"给了 INFO 却打出了 DEBUG 记录 —— 级别没生效:\\n{out}")\n'
)

OLD_SILENT_FALLBACK = (
    '        level=getattr(logging, args.log_level.upper(), logging.INFO),'
)
NEW_PLAIN_LEVEL = "        level=args.log_level,"

CLAIMS = {
    "M1-拆掉choices退回静默兜底": (
        [],
        ['        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],'],
    ),
    "M2-拆掉type=str.upper": (
        [],
        ['        "--log-level", default="INFO", type=str.upper,\n'],
    ),
    "M3-报错里不列合法值(存活:该性质由argparse本身保证)": (
        [],
        ["        missing = [lv for lv in VALID if lv not in out]"],
    ),
    "M4-拆掉「DEBUG 真生效」那条判据(存活:另一条还守着反向)": (
        ["assert True  # 变异:DEBUG 真生效那两条被拆了"],
        ['    assert "[DEBUG]" in out, (\n        f"给了 DEBUG 却没有'],
    ),
    "M5-拆掉「INFO 不打 DEBUG」那条判据(存活:另一条还守着正向)": (
        ["assert True  # 变异:反向不变量被拆了"],
        ['    assert "[DEBUG]" not in out, (\n        f"给了 INFO 却打出了'],
    ),
    "M6-兜底那句加回来": (
        ["getattr(logging, args.log_level.upper(), logging.INFO)"],
        ["        level=args.log_level,"],
    ),
    "C1-「垃圾值必须被拒」判据放宽": (
        ["if False:  # 变异:恒真"],
        ["        if rc != 2:"],
    ),
    "C2-「必须列出全部合法值」判据放宽": (
        ["        missing = []  # 变异:恒真"],
        ["        missing = [lv for lv in VALID if lv not in out]"],
    ),
}

MUTANTS = [
    ("M1-拆掉choices退回静默兜底", lambda p: _apply(
        p, ANCHOR_ADDARG, ANCHOR_ADDARG.replace(
            '        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],\n', "")),
     False, (IMPL,)),
    ("M2-拆掉type=str.upper", lambda p: _apply(
        p, ANCHOR_ADDARG, ANCHOR_ADDARG.replace(
            '        "--log-level", default="INFO", type=str.upper,\n',
            '        "--log-level", default="INFO",\n')),
     False, (IMPL,)),
    # M3/M4/M5 实测**存活**,见 docstring「单点覆盖」一节。这里不改成「被杀」——
    # 没有任何东西要求某条判据存在,那是元判据的活,不是变异测试的。
    ("M3-报错里不列合法值(存活:该性质由argparse本身保证)", lambda p: _apply(
        p, JUDGE_MUST_LIST,
        "        missing = []  # 变异:不查合法值了,只看 rc"),
     True, (CRIT,)),
    ("M4-拆掉「DEBUG 真生效」那条判据(存活:另一条还守着反向)", lambda p: _apply(
        p, JUDGE_DEBUG_EFFECT, "    assert True  # 变异:DEBUG 真生效那两条被拆了\n"),
     True, (CRIT,)),
    ("M5-拆掉「INFO 不打 DEBUG」那条判据(存活:另一条还守着正向)", lambda p: _apply(
        p, JUDGE_INFO_NO_DEBUG, "    assert True  # 变异:反向不变量被拆了\n"),
     True, (CRIT,)),
    ("M6-兜底那句加回来", lambda p: _apply(
        p, NEW_PLAIN_LEVEL, OLD_SILENT_FALLBACK + "\n"), False, (IMPL,)),
]

COVERAGE_MUTANTS = [
    ("C1-「垃圾值必须被拒」判据放宽", lambda p: _apply(
        p, JUDGE_MUST_REJECT, "        if False:  # 变异:恒真"), True, (CRIT,)),
    ("C2-「必须列出全部合法值」判据放宽", lambda p: _apply(
        p, JUDGE_MUST_LIST, "        missing = []  # 变异:恒真"), True, (CRIT,)),
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
            _check_claim_points_at_one_place(
                name, "\n".join(t.read_text(encoding="utf-8") for t in targets))
            for t in targets:
                mutate(t)
            _verify_claim(name, "\n".join(
                t.read_text(encoding="utf-8") for t in targets))
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
        print(f"\n=== r85 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:34s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:300])
    print(f"\nr85 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
