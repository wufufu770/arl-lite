"""r84 变异测试:验「截断只能用在决策之后」这条判据在守。

主题:r68 和 r74 我都把种子前提搞错了 —— 探针用 `[:7]` 之类截断输出,
把 stderr 日志行和 stdout 混读,得出「source status 段没打印」「没有任何
提示」这类**根本不存在的结论**:

  - r68 丢了一条真种子(risk summary 与 risk top 的口径矛盾是真的)
  - r84 差点把一个已有出路的问题当成死路(那是 r74)

## r84 在仓库代码上的结论是**否定结果**

扫 tests/ + devloop/ + arl_lite/ 全部 .py,「对 stdout/stderr/readouterr
取有界切片」共 5 处。逐个读过,五处的**决策依据**都是 returncode 或文件
是否存在,截断只出现在决策**之后**拼给人看的文案里 —— 正确用法,不修。

反模式不存在于版本控制里,它发生在会话中的一次性 shell heredoc 探针上,
那些没留档,事后无法审计。所以这轮能做的是把判据钉住,别让它长进来。

## 判据不能写成「不许出现切片」

那会误伤上面 5 处正确用法,守卫一旦比它守的东西还严就会被人绕过。
真正的判据是:**那个截断的值,有没有流进一个决定**。两层结构判定:
  1. 切片落在 if/while/assert 的 test 里
  2. 切片赋给变量,而那个变量之后被某个 test 引用

已知漏网(不假装没有):跨函数的流判不了。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
所有变异统一走 _write_checked,写盘前 ast.parse。

全部变异:
  M1 直接用在判定里的那层被删掉      → 杀。正控制组「截断直接进判定」必须红
  M2 先赋值后判定那层被删掉          → 杀。正控制组「赋给变量后进判定」必须红
  M3 找到的记录被丢掉                → 杀
  C1 负控制组被整个删掉              → **存活**。这是个否定结果,也是这轮
                                       找到的真实缺口:没有任何判据要求
                                       控制组**存在**,所以它可以被删掉而
                                       无人察觉 —— 守卫从此只抓漏报、不再
                                       防误伤,如实记着不当成失败
  C2 扫描范围缩小到一个目录          → 杀(数量对不上,结论不能再采信)
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CRIT = REPO / "tests" / "test_probe_truncation_only_after_deciding.py"
TARGET = ["tests/test_probe_truncation_only_after_deciding.py"]

COLLECTION_FAILED = ("error during collection", "ERROR collecting", "Interrupted:")

# ── 锚点:全部从真实文件里逐字抠出来 ──

# DIRECT
ANCHOR_DIRECT = (
    '            if n is slice_node:\n'
    '                return "直接用在 if/while/assert 的判定里"\n'
)


# VIAVAR
ANCHOR_VIAVAR = (
    '                if any(isinstance(x, ast.Name) and x.id == var for x in ast.walk(root)):\n'
    '                    return f"赋给 {var!r} 之后被判定用到"\n'
)


# COLLECT
ANCHOR_COLLECT = (
    '            if why:\n'
    '                out.append(f"{path.name}:{n.lineno}  {why}")\n'
)


# 负控制组整块(r84 用来验「删掉它没人会发现」这个否定结果)
ANCHOR_NEGATIVE = (
    '_NEGATIVE = {\n'
    '    "决策看 returncode,截断只拼文案": \'\'\'\n'
    'def probe(r):\n'
    '    if r.returncode != 0:\n'
    '        print(f"exit={r.returncode} | {r.stderr[:100]}")\n'
    '    return r.returncode\n'
    "''',\n"
    '    "决策看文件存在,截断只拼文案": \'\'\'\n'
    'def probe(r):\n'
    '    if r.returncode == 0 and Path("/tmp/out.html").exists():\n'
    '        return "ok"\n'
    '    return f"export html: {r.stderr[:100]}"\n'
    "''',\n"
    '    "决策看另一个字段的完整值": \'\'\'\n'
    'def probe(r):\n'
    '    return r.stderr.count("ERROR") > 2\n'
    "''',\n"
    '    "截断结果只 return 出去,不参与判定": \'\'\'\n'
    'def probe(r):\n'
    '    return r.stderr[:200]\n'
    "''',\n"
    '}\n'
)

CLAIMS = {
    "M1-删掉「直接用在判定里」那层": (
        ["# 变异:直接判定那层被删了"],
        ["直接用在 if/while/assert 的判定里"],
    ),
    "M2-删掉「先赋值后判定」那层": (
        ["# 变异:变量流那层被删了"],
        ["之后被判定用到"],
    ),
    "M3-找到的记录被丢掉": (
        [],
        ["out.append(f\"{path.name}:{n.lineno}  {why}\")"],
    ),
    "C1-负控制组被整个删掉": (
        ["_NEGATIVE: dict[str, str] = {}"],
        # 必须指在**被删的那一块内部**。第一版我写的是那条测试的
        # docstring,而它在 _NEGATIVE 字典外面 —— 删字典根本不会动它,
        # 于是声明永远不成立。声明机制当场把这条抓出来报给了你。
        ["决策看 returncode,截断只拼文案"],
    ),
    "C2-扫描范围缩到一个目录": (
        ["# 变异:只看 tests"],
        ['SCAN_DIRS = ("tests", "devloop", "arl_lite")'],
    ),
}


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


MUTANTS = [
    ("M1-删掉「直接用在判定里」那层", lambda p: _apply(
        p, ANCHOR_DIRECT,
        "            if False:  # 变异:直接判定那层被删了\n"
        "                return \"这一形态不再报\"\n"),
     False, (CRIT,)),
    ("M2-删掉「先赋值后判定」那层", lambda p: _apply(
        p, ANCHOR_VIAVAR,
        "                if False:  # 变异:变量流那层被删了\n"
        "                    return \"这一形态不再报\"\n"),
     False, (CRIT,)),
    ("M3-找到的记录被丢掉", lambda p: _apply(
        p, ANCHOR_COLLECT, "            pass  # 变异:找到的记录被丢掉\n"),
     False, (CRIT,)),
]

# ---- 覆盖变异:C2 期望被杀,C1 是**实测出来的否定结果** ----
COVERAGE_MUTANTS = [
    ("C1-负控制组被整个删掉", lambda p: _apply(
        p, ANCHOR_NEGATIVE, "_NEGATIVE: dict[str, str] = {}\n"),
     True, (CRIT,)),
    ("C2-扫描范围缩到一个目录", lambda p: _apply(
        p, 'SCAN_DIRS = ("tests", "devloop", "arl_lite")',
        'SCAN_DIRS = ("tests",)  # 变异:只看 tests'),
     False, (CRIT,)),
]


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
        ("覆盖变异(期望值按实测,不按想象)", COVERAGE_MUTANTS),
    ):
        print(f"\n=== r84 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:36s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:300])
    print(f"\nr84 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.sandboxed(main))
