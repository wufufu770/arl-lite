"""r68 变异测试:验「只读命令的工作区口径一致」这条判据真的在守。

主题:零工作区下,10 个只读子命令里有 3 个**静默建库**并返回「全 0」:
risk summary / monitor list / monitor changes。而同族的另外 7 个
(stats / query / search / export / diff / correlate / risk top)
rc=1 报 workspace not found 并给出路。

最刺眼的是 risk top 与 risk summary —— 同一个 risk 的两个子命令,
一份说「工作区不存在」,一份直接把工作区建出来告诉你「一切正常」。

反过来也有不该动的:workspace list / workspace create 静默建库是对的
(它们本就该建),version / tools check / watch list 压根不碰工作区。
所以判据是双向的:既钉住「该拒的必须拒」,也钉住「不该拒的必须还能跑」。

约定:元组第 4 位 = 期望存活(True/False)。
sweep(MUTANTS, expect_default=False)      # 实现变异,期望全被杀
sweep(COVERAGE_MUTANTS, expect_default=True)  # 覆盖变异,期望全存活

全部变异**先手工验过会产生差异**才写进来(r47 老规矩,r63/r65/r66/r67 各踩过):
  M1 撤掉 risk summary 的守卫   → 2 条报红(零工作区拒绝 + risk 那一对)
  M2 撤掉 monitor list 的守卫   → 只杀那一条,够窄
  M3 撤掉 monitor changes 守卫  → 只杀那一条,够窄
  M4 过度修正:给 workspace list 也加校验 → 3 条报红,
     证明反向不变量真的在守(这是本轮特意加的一类变异:
     「把该禁的一起禁掉」是这类修复最容易犯的错)
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CLI = REPO / "arl_lite" / "cli.py"
CRIT = REPO / "tests" / "test_readonly_cmds_same_workspace_contract.py"
TARGET = "tests/test_readonly_cmds_same_workspace_contract.py"

BAD_END = ("SyntaxError", "IndentationError", "TabError", "ERROR collecting")

RISK_GUARD = (
    "    # 与同族的 cmd_risk_top 保持一致:只读命令不静默建工作区。\n"
    "    # 实测零工作区下 `risk top` rc=1 报 workspace not found,\n"
    "    # 而 `risk summary` 却 rc=0、静默建出 default、并打印一份\n"
    "    # 「全 0 概览」—— 同一份数据、同一个 workspace 名下两条路\n"
    "    # 给出互相矛盾的答案,用户没法判断自己的数据是不是真没了。\n"
    "    if _ensure_workspace_exists(args.workspace):\n"
    "        return 1\n"
)
MON_LIST_GUARD = (
    "    from .core.monitor import Monitor\n"
    "    if _ensure_workspace_exists(args.workspace):\n"
    "        return 1\n"
)
MON_CHANGES_GUARD = (
    "        return 2\n"
    "    if _ensure_workspace_exists(args.workspace):\n"
    "        return 1\n"
)
WS_LIST_BODY = (
    '    ws = getattr(args, "workspace", None) or "default"\n'
    "    storage = Storage(workspace=ws)\n"
    "    rows = storage.list_workspaces()"
)


def _apply(path: pathlib.Path, old: str, new: str) -> None:
    src = path.read_text(encoding="utf-8")
    if old not in src:
        raise AssertionError(f"变异锚点没命中 {path.name}:{old[:70]!r}")
    out = src.replace(old, new, 1)
    if out == src:
        raise AssertionError("变异是空操作 —— 什么都不会变,写了也是白写")
    path.write_text(out, encoding="utf-8")


MUTANTS = [
    ("M1-撤掉risk-summary守卫", lambda p: _apply(p, RISK_GUARD, ""), False),
    ("M2-撤掉monitor-list守卫", lambda p: _apply(p, MON_LIST_GUARD, MON_LIST_GUARD.split("\n")[0] + "\n"), False),
    ("M3-撤掉monitor-changes守卫", lambda p: _apply(p, MON_CHANGES_GUARD, "        return 2\n"), False),
    # M4 过度修正:把「该建库的命令」也禁掉
    ("M4-过度修正workspace-list", lambda p: _apply(
        p, WS_LIST_BODY,
        '    ws = getattr(args, "workspace", None) or "default"\n'
        "    if _ensure_workspace_exists(ws):\n"
        "        return 1\n"
        "    storage = Storage(workspace=ws)\n"
        "    rows = storage.list_workspaces()"), False),
]

# ---- 覆盖变异:改坏判据自己,期望**存活**(真实现已经是对的)

COVERAGE_MUTANTS = [
    # C1: 放宽「零工作区必须拒绝」—— 真实实现本来就拒绝,应当存活
    ("C1-拒绝判据放宽rc", lambda p: _apply(
        p, "    assert r.returncode != 0, (",
        "    assert r.returncode in (0, 1), ("), True),
    # C2: 把「不许建库」这条不变量整条废掉 —— 手工验过存活(19 passed)。
    #     锚点按**整块**替换:首版只把 `assert not _workspaces_created(home), (`
    #     换成 `if False:`,括号里的续行就变成了语法错误 —— 那种变异
    #     会被假杀守卫挡下,等于白写(r66 记过「写坏语法的变异和打不上的
    #     变异一样无效」)。
    ("C2-废掉不许建库检查", lambda p: _apply(
        p,
        '    assert not _workspaces_created(home), (\n'
        "        f\"`arl-lite {' '.join(argv)}` 拒绝了却把工作区建了出来:\"\n"
        '        f"{_workspaces_created(home)}"\n'
        "    )",
        "    # 变异:不检查建库"), True),
]


def _run_criterion():
    return subprocess.run(
        [sys.executable, "-B", "-m", "pytest", TARGET, "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=REPO, timeout=900,
    )


def _sweep(mutants, expect_default: bool) -> list:
    out = []
    for name, mutate, expect in mutants:
        targets = [CRIT] if name.startswith("C") else [CLI]
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
    for title, mutants, expect_default in (
        ("实现变异(期望全被杀)", MUTANTS, False),
        ("覆盖变异(期望全存活)", COVERAGE_MUTANTS, True),
    ):
        print(f"\n=== r68 {title} ===")
        for name, detail, outp in _sweep(mutants, expect_default):
            flag = "OK " if detail in ("killed", "survived") else "!! "
            if flag == "!! ":
                bad += 1
            print(f"  {flag}{name:32s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:700])
    print(f"\nr68 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
