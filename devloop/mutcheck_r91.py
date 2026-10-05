"""r91 变异测试:验「超时文案必须说清是挂了还是变慢」这条在守。

## 主题

`gates.py` 里那个 `timeout=600` 的注释写着「兜底,防止 pytest 卡死」——
它是**挂死检测器**,不是性能预算。但它原来的报错只有一句
`pytest timed out after 600s`,真挂死和「机器今天慢」读起来完全一样。

r88 实测吃过这个亏:全量 476.37s / 531.52s / 564.61s 三次都对得上 600s,
首跑超时、单独重跑就过了。那一轮的 6%~11% 余量**压根没被记录**,想查也
没得查。

r91 只做诊断能力:把上次成功用时写进基线,超时时带出来并给倾向性判断。
**没有把 600s 调大** —— 提额要走 `devloop accept`。

## 变异清单

实现变异(期望全被杀):
  M1 两种情形给出同一句话(不分类了)        → 判据红
  M2 判据反了:差 127s 说卡住、差 520s 说变慢 → 判据红
  M3 没有用时数据时编一个秒数出来           → 判据红
  M4 阈值卡死成 180s(分类边界被挪掉)       → 判据红
  M5 超时分支不调用 _timeout_detail(白写)   → 接线判据红
  M6 measured 里不记用时                     → 接线判据红
  M7 不从 baseline 读上一次用时             → 接线判据红

覆盖变异(期望全存活):
  C1 把「两种倾向必须不同」那条拆掉

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
CLAIMS 只写字面量。锚点必须**括号配平且唯一**。
所有变异统一走 _write_checked,写盘前 ast.parse。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
IMPL = REPO / "arl_lite" / "devloop" / "gates.py"
CRIT = REPO / "tests" / "test_timeout_says_whether_it_hung_or_ran_slow.py"

TARGET = ["tests/test_timeout_says_whether_it_hung_or_ran_slow.py"]

COLLECTION_FAILED = ("error during collection", "ERROR collecting", "Interrupted:")

# ── 实现侧锚点 ──

VERDICT_PICK = (
    "    gap = 600 - prev_seconds\n"
    '    verdict = ("只差 %.0fs,多半是机器变慢而不是卡死 —— 先看有没有别的进程在抢 CPU"\n'
    "               % gap) if gap < 180 else (\n"
    '        "比上次多出 %.0fs 以上,更像真的卡住了,去看最后卡在哪条测试上" % gap)\n'
)
VERDICT_SAME = (
    "    gap = 600 - prev_seconds\n"
    '    verdict = "多半是机器变慢而不是卡死 —— 先看有没有别的进程在抢 CPU"\n'
)
VERDICT_INVERTED = (
    "    gap = 600 - prev_seconds\n"
    '    verdict = ("比上次多出 %.0fs 以上,更像真的卡住了" % gap) if gap < 180 else (\n'
    '        "只差 %.0fs,多半是机器变慢" % gap)\n'
)

# M4:分类边界被挪掉。锚点用**整块**而不是 `... if gap < 180 else (` 那一行 ——
# 那是个不配平的三元表达式片段,r81 的半句锚点守卫会判掉(r91 头一版栽过)。
VERDICT_180 = VERDICT_PICK
VERDICT_30 = VERDICT_PICK.replace("gap < 180", "gap < 30")

MISSING_BRANCH = (
    '    if not isinstance(prev_seconds, (int, float)) or prev_seconds <= 0:\n'
    '        return ("pytest timed out after 600s(基线里没有上一次用时,"\n'
    '                "所以分不清是真卡死还是机器变慢)")\n'
)
MISSING_BRANCH_INVENTS = (
    '    if not isinstance(prev_seconds, (int, float)) or prev_seconds <= 0:\n'
    '        return "pytest timed out after 600s;上次成功用时 123s,多半是机器变慢"\n'
)

WIRE_CALL = "                detail=_timeout_detail(prev_seconds),\n"
WIRE_DEAD = "                detail=\"pytest timed out after 600s\",\n"

MEASURED_LINE = (
    '        measured: dict = {"failed": failed, "passed": passed,\n'
    '                          "seconds": round(time.monotonic() - started, 1)}\n'
)
MEASURED_NO_SECONDS = (
    '        measured: dict = {"failed": failed, "passed": passed}\n'
)

PREV_SECONDS = '        prev_seconds = base.get("seconds")\n'
PREV_SECONDS_GONE = '        prev_seconds = None\n'

CLAIMS = {
    "M1-两种情形给出同一句话": (
        ['verdict = "多半是机器变慢而不是卡死 —— 先看有没有别的进程在抢 CPU"'],
        ['% gap) if gap < 180 else ('],
    ),
    "M2-判据反了差得少说卡住差得多说变慢": (
        ['("比上次多出 %.0fs 以上,更像真的卡住了" % gap) if gap < 180 else ('],
        ['("只差 %.0fs,多半是机器变慢而不是卡死 —— 先看有没有别的进程在抢 CPU"'],
    ),
    "M3-没有用时数据时编一个秒数出来": (
        ["上次成功用时 123s,多半是机器变慢"],
        ["所以分不清是真卡死还是机器变慢"],
    ),
    "M4-分类边界被挪到30s": (
        ["gap < 30"],
        ["gap < 180"],
    ),
    "M5-超时分支不调用_helper白写": (
        ['detail="pytest timed out after 600s",'],
        ["detail=_timeout_detail(prev_seconds),"],
    ),
    "M6-measured里不记用时": (
        ['measured: dict = {"failed": failed, "passed": passed}'],
        ['"seconds": round(time.monotonic() - started, 1)'],
    ),
    "M7-不从baseline读上一次用时": (
        ["prev_seconds = None"],
        ['prev_seconds = base.get("seconds")'],
    ),
    # 替换必须是合法 Python:早先写 `assert slow != hung,  # 变异…`,
    # 逗号后跟注释是 **SyntaxError**(逗号要求后随表达式,注释不算)。
    # `_write_checked` 在写盘前拦住了它 —— 正是它该干的事。
    "C1-拆掉两种倾向必须不同那条": (
        ["    pass  # 变异:这条判据整条没了"],
        ['    assert slow != hung, "两种情形给出了同一句话"\n'],
    ),
}

MUTANTS = [
    ("M1-两种情形给出同一句话",
     lambda p: _apply(p, VERDICT_PICK, VERDICT_SAME), False, (IMPL,)),
    ("M2-判据反了差得少说卡住差得多说变慢",
     lambda p: _apply(p, VERDICT_PICK, VERDICT_INVERTED), False, (IMPL,)),
    ("M3-没有用时数据时编一个秒数出来",
     lambda p: _apply(p, MISSING_BRANCH, MISSING_BRANCH_INVENTS), False, (IMPL,)),
    ("M4-分类边界被挪到30s",
     lambda p: _apply(p, VERDICT_180, VERDICT_30), False, (IMPL,)),
    ("M5-超时分支不调用_helper白写",
     lambda p: _apply(p, WIRE_CALL, WIRE_DEAD), False, (IMPL,)),
    ("M6-measured里不记用时",
     lambda p: _apply(p, MEASURED_LINE, MEASURED_NO_SECONDS), False, (IMPL,)),
    ("M7-不从baseline读上一次用时",
     lambda p: _apply(p, PREV_SECONDS, PREV_SECONDS_GONE), False, (IMPL,)),
]

COVERAGE_MUTANTS = [
    ("C1-拆掉两种倾向必须不同那条", lambda p: _apply(
        p, '    assert slow != hung, "两种情形给出了同一句话"\n',
        "    pass  # 变异:这条判据整条没了\n"), True, (CRIT,)),
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
            r = _run_criterion()
            outp = r.stdout + r.stderr
            if any(bad in outp for bad in COLLECTION_FAILED):
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
        print(f"\n=== r91 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:36s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:300])
    print(f"\nr91 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
