"""r75 变异测试:验「被静默忽略的参数必须如实标注」这条判据在守。

主题:本仓已有约定 —— 确实被忽略的参数,help 写 `(忽略)`(`workspace list -w`)。
但约定只落地一处,且没人守:`watch stop -w` 的 help 写「工作空间名」**暗示生效**,
而 `cmd_watch_stop` 整个函数不读 args 任何字段。

判据用 r69 验证过的推导法:可靠地推出「有没有被读」(cmd + 同模块辅助函数闭包),
再用双向不变量 + 显式例外清单兜住「该不该被读」。

**这条判据最大的风险面是它自己的推导被削弱** —— 推导一旦只扫函数体内的直接
属性访问,`fp-bench`/`perf-bench` 经 `_resolve_bench_out(args, ...)` 读取的
`--out`/`--in-place` 就会掉出闭包,被误判成「被静默忽略」。M4 专门验这一条。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明(不靠命名约定)。
_apply 写盘前先 ast.parse,语法坏了当场拒(r74 改的机制,防第三次栽同一坑)。

全部变异:
  M1 撤掉 忽略 标注(watch stop 又变回「工作空间名」) → 杀那条
  M2 给 watch stop 真读上 workspace(它其实该被读)     → 杀 pin 那条
  M3 把 pin 改成只留 1 条                           → 杀 pin 那条
  M4 推导只看函数体内、不走辅助函数(削弱闭包)        → **杀**,证明闭包没白写
  M5 加一个真的被静默忽略的参数且不标注              → 杀 pin + 标注两条
  C1 标注判据放宽成不看 help                        → 期望存活
  C2 理由长度下限判据退化成恒真                       → 期望存活
  C3 stale 例外判据退化成恒真                         → 期望存活
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CLI = REPO / "arl_lite" / "cli.py"
CRIT = REPO / "tests" / "test_silently_ignored_args_must_say_so.py"
TARGET = "tests/test_silently_ignored_args_must_say_so.py"

BAD_END = (
    "SyntaxError", "IndentationError", "TabError",
    "ERROR collecting", "error during collection", "Interrupted:",
)

WATCH_STOP_ARG = '    pwap.add_argument("-w", "--workspace", help="(忽略)watch stop 不作用于任何工作区", default="default")'
CMD_WATCH_STOP = ('def cmd_watch_stop(args) -> int:\n'
                  '    """watch stop(同 start 一样立刻返回,因为是同步)"""\n'
                  '    print("[i] watch 是同步模式,直接 Ctrl+C 退出 start 即可")\n'
                  '    return 0')
PIN_BLOCK = ('PINNED_SILENTLY_IGNORED = {\n'
             '    ("watch stop", "workspace"),\n'
             '    ("workspace list", "workspace"),\n'
             '}')
CLOSURE_CALL = '        if callee in table:\n            got |= _closure(callee, module, tables, seen)'


def _apply(path: pathlib.Path, old: str, new: str) -> None:
    src = path.read_text(encoding="utf-8")
    if old not in src:
        raise AssertionError(f"变异锚点没命中 {path.name}:{old[:70]!r}")
    out = src.replace(old, new, 1)
    if out == src:
        raise AssertionError("变异是空操作 —— 什么都不会变,写了也是白写")
    import ast
    try:
        ast.parse(out)
    except SyntaxError as e:
        raise AssertionError(
            f"变异会让 {path.name} 语法错误({e}) —— 多行语句只替换首行会留下孤儿行。文件未写入。"
        ) from None
    path.write_text(out, encoding="utf-8")


def _m4_weaken_closure(path: pathlib.Path) -> None:
    """推导只看函数体内,不走辅助函数 —— fp-bench 的 --out 就会掉出闭包。"""
    _apply(path, CLOSURE_CALL, "        if False:\n            pass  # 变异:不走辅助函数闭包")


def _m2_make_watch_stop_read(path: pathlib.Path) -> None:
    """让 cmd_watch_stop 真的读上 workspace(它其实本该被读)。"""
    needle = '    print("[i] watch 是同步模式,直接 Ctrl+C 退出 start 即可")'
    _apply(path, CMD_WATCH_STOP, CMD_WATCH_STOP.replace(
        needle,
        '    _ = getattr(args, "workspace", "default")  # 变异:真的读上了\n' + needle,
    ))


# 每个变异该改哪些文件 —— 显式写进元组。
MUTANTS = [
    ("M1-撤掉忽略标注", lambda p: _apply(
        p, WATCH_STOP_ARG,
        '    pwap.add_argument("-w", "--workspace", help="工作空间名", default="default")'), False, (CLI,)),
    ("M2-让watch-stop真读workspace", _m2_make_watch_stop_read, False, (CLI,)),
    ("M3-pin只留1条", lambda p: _apply(
        p, PIN_BLOCK,
        'PINNED_SILENTLY_IGNORED = {\n    ("workspace list", "workspace"),\n}'), False, (CRIT,)),
    ("M4-推导不走辅助函数闭包", _m4_weaken_closure, False, (CRIT,)),
    ("M5-新加一个静默忽略且不标注", lambda p: _apply(
        p, '    pst.set_defaults(func=cmd_stats)',
        '    pst.add_argument("--sort-by", help="排序字段")  # 变异:注册了但 cmd_stats 从不读它\n'
        '    pst.set_defaults(func=cmd_stats)'), False, (CLI,)),
]

# ---- 覆盖变异:改坏判据自己,期望**存活**(真实现已经是对的)
COVERAGE_MUTANTS = [
    ("C1-标注判据放宽成不看help", lambda p: _apply(
        p, "        if \"忽略\" not in help_text:",
        "        if False:  # 变异:不看 help 有没有提「忽略」"), True, (CRIT,)),
    ("C2-理由长度下限退化成恒真", lambda p: _apply(
        p, "    thin = [f\"{k}: {v!r}\" for k, v in EXCEPTIONS.items() if len(v.strip()) < 10]",
        "    thin = []  # 变异:理由长度恒真"), True, (CRIT,)),
    ("C3-stale例外判据退化成恒真", lambda p: _apply(
        p, "    stale = sorted(set(EXCEPTIONS) - derived)",
        "    stale = []  # 变异:stale 检查恒真"), True, (CRIT,)),
]


def _run_criterion():
    return subprocess.run(
        [sys.executable, "-B", "-m", "pytest", TARGET, "-q", "-p", "no:cacheprovider"],
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
            if any(bad in outp for bad in BAD_END):
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
        print(f"\n=== r75 {title} ===")
        for name, detail, outp in _sweep(mutants):
            flag = "OK " if detail in ("killed", "survived") else "!! "
            if flag == "!! ":
                bad += 1
            print(f"  {flag}{name:30s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:500])
    print(f"\nr75 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
