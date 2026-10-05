"""r86 变异测试:验「标了忽略的参数必须真的没被读」这条反向不变量在守。

主题:`tests/test_silently_ignored_args_must_say_so.py` 的 docstring 写着
「推导 + **双向**不变量」,但代码历来只推导了**一个方向** ——
「注册了却从不被读 → help 必须写忽略」。另一半从没实现过。

## 缺口是实测出来的,不是推出来的

注入:把 `query -w` 的 help 从「工作空间名」改成「工作空间名(忽略)」。
这个参数**真的**被 `cmd_query` 读(`_command_paths` 推导里它在 used 集合内)。
结果:本文件 6 条判据**全绿**,rc=0。

这个方向的谎比 r69 原 bug 更贵:
  - 原 bug:参数被忽略、help 暗示生效 → 用户多传一个参数,白传(有提示)
  - 这一半:参数生效、help 说忽略   → 用户据此判定「不用传」,静默吃默认值

## 队列那一项本身是 stale 的

r86 是为队列里的 `watch-stop-w-claimed-but-ignored` 开的,结果 r75
(`98bf69a`)早就把 `watch stop -w` 的 help 修成「(忽略)watch stop 不作用于
任何工作区」了。种子被实测推翻,所以这轮不去改实现,改成把「另一半方向」
的缺口补上 —— 那才是同一个主题下真实存在的洞。

## 变异分两类,理由写在名字里

实现变异(IMPL,6 条,期望全被杀):
  M1 谎标:给真被读的 `query -w` 标 (忽略)   → r86 新增的反向判据
  M2 回退 r75:watch stop -w 的 help 改回「工作空间名」
  M3 让 watch stop **真的**读 args,help 不改  → 同一个谎的另一种来法
  M4 撤掉 workspace list -w 的 (忽略)         → 正向判据
  M5 拆掉推导内核:_direct_reads 一个 dest 都读不出来
  M6 拆掉 help 取值:_help_text 恒返回空串

M5/M6 改的是**判据文件自己**,但它们不是「删掉一条判据」,而是「拆掉推导
机制」。这类退化必须被同文件里**别的**判据逮住 —— 那才说明这个文件不是
单点故障。它俩的 CLAIMS 声明同样整体照抄完整语句(r81 的规矩)。

覆盖变异(2 条,期望全存活):
  C1 反向判据放宽成恒真
  C2 正向判据放宽成恒真

C1/C2 实测**存活**,这是如实记的否定结果,不是改成「被杀」把账做平。
按 Key Decision 4:变异测试回答的是「判据集能否逮住实现退化」,
「每条判据能否被单独删掉」是元判据的活,追下去会递归。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
所有变异统一走 _write_checked,写盘前 ast.parse。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
IMPL = REPO / "arl_lite" / "cli.py"
CRIT = REPO / "tests" / "test_silently_ignored_args_must_say_so.py"

TARGET = ["tests/test_silently_ignored_args_must_say_so.py"]

COLLECTION_FAILED = ("error during collection", "ERROR collecting", "Interrupted:")

# ── 实现侧锚点(整体照抄完整语句,含结尾括号)──

PQ_W = '    pq.add_argument("-w", "--workspace", help="工作空间名", default="default")\n'
PQ_W_LYING = '    pq.add_argument("-w", "--workspace", help="工作空间名(忽略)", default="default")\n'

PWL_IGNORED = '    pwl.add_argument("-w", "--workspace", help="(忽略)当前 workspace 名", default="default")\n'
PWL_PLAIN = '    pwl.add_argument("-w", "--workspace", help="当前 workspace 名", default="default")\n'

PWAP_IGNORED = '    pwap.add_argument("-w", "--workspace", help="(忽略)watch stop 不作用于任何工作区", default="default")\n'
PWAP_PLAIN = '    pwap.add_argument("-w", "--workspace", help="工作空间名", default="default")\n'

STOP_PRINT = '    print("[i] watch 是同步模式,直接 Ctrl+C 退出 start 即可")\n'
STOP_PRINT_READS = (
    "    print(f\"[i] watch 是同步模式({getattr(args, 'workspace', 'default')}),"
    '直接 Ctrl+C 退出 start 即可")\n'
)

# ── 判据侧锚点 ──

GOT_INIT = "    got: set[str] = set()\n"
GOT_INIT_DEAD = "    return set()  # 变异:一个 dest 都读不出来\n"

HELP_RET = '    return target or ""\n'
HELP_RET_DEAD = '    return ""  # 变异:help 一律取不到\n'

JUDGE_REVERSE = "    lying = sorted(_annotated_as_ignored() - derived)\n"
JUDGE_FORWARD = "    lying = []\n"

# CLAIMS 里**只许写字面量**,不许写 PQ_W 这种名字引用 ——
# r83 的自检判据用 `ast.literal_eval` 逐条解析,遇到 Name 直接 ValueError。
# 这条约束是好的:声明文本必须自包含,不能靠别处的常量拼出来。
CLAIMS = {
    "M1-给真被读的query-w谎标忽略": (
        ['help="工作空间名(忽略)"'],
        ['    pq.add_argument("-w", "--workspace", help="工作空间名", default="default")\n'],
    ),
    "M2-回退r75:watch-stop-w的help改回工作空间名": (
        ['    pwap.add_argument("-w", "--workspace", help="工作空间名", default="default")\n'],
        ['help="(忽略)watch stop 不作用于任何工作区"'],
    ),
    "M3-让watch-stop真的读args但help不改": (
        ["getattr(args, 'workspace', 'default')"],
        ['    print("[i] watch 是同步模式,直接 Ctrl+C 退出 start 即可")\n'],
    ),
    "M4-撤掉workspace-list-w的忽略标注": (
        ['    pwl.add_argument("-w", "--workspace", help="当前 workspace 名", default="default")\n'],
        ['help="(忽略)当前 workspace 名"'],
    ),
    "M5-拆掉推导内核:_direct_reads读不出任何dest": (
        ["return set()  # 变异:一个 dest 都读不出来"],
        ["    got: set[str] = set()\n"],
    ),
    "M6-拆掉help取值:_help_text恒返回空串": (
        ['return ""  # 变异:help 一律取不到'],
        ['    return target or ""\n'],
    ),
    "C1-反向判据放宽成恒真": (
        ["lying = []  # 变异:反向不变量被拆了"],
        ["    lying = sorted(_annotated_as_ignored() - derived)\n"],
    ),
    "C2-正向判据放宽成恒真": (
        ["lying = []  # 变异:正向判据被拆了"],
        ["    lying = []\n"],
    ),
}

MUTANTS = [
    ("M1-给真被读的query-w谎标忽略", lambda p: _apply(p, PQ_W, PQ_W_LYING), False, (IMPL,)),
    ("M2-回退r75:watch-stop-w的help改回工作空间名",
     lambda p: _apply(p, PWAP_IGNORED, PWAP_PLAIN), False, (IMPL,)),
    ("M3-让watch-stop真的读args但help不改",
     lambda p: _apply(p, STOP_PRINT, STOP_PRINT_READS), False, (IMPL,)),
    ("M4-撤掉workspace-list-w的忽略标注",
     lambda p: _apply(p, PWL_IGNORED, PWL_PLAIN), False, (IMPL,)),
    # M5/M6 改判据文件,但不是「删判据」而是「拆推导机制」—— 必须被同文件
    # 里**别的**判据逮住,否则这个文件就是单点故障。
    ("M5-拆掉推导内核:_direct_reads读不出任何dest",
     lambda p: _apply(p, GOT_INIT, GOT_INIT_DEAD), False, (CRIT,)),
    ("M6-拆掉help取值:_help_text恒返回空串",
     lambda p: _apply(p, HELP_RET, HELP_RET_DEAD), False, (CRIT,)),
]

# C1/C2 实测存活,见 docstring。按 Key Decision 4,如实记,不做平账。
COVERAGE_MUTANTS = [
    ("C1-反向判据放宽成恒真", lambda p: _apply(
        p, JUDGE_REVERSE, "    lying = []  # 变异:反向不变量被拆了\n"), True, (CRIT,)),
    ("C2-正向判据放宽成恒真", lambda p: _apply(
        p, JUDGE_FORWARD, "    lying = []  # 变异:正向判据被拆了\n"), True, (CRIT,)),
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
        print(f"\n=== r86 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:40s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:300])
    print(f"\nr86 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.sandboxed(main))
