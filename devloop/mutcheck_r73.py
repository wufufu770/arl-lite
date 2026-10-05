"""r73 变异测试:验「`workspace list -w` 必须真被忽略」这条判据在守。

主题:EXCEPTIONS 给 cmd_workspace_list 写明的理由边界是精确的 —— 「列出工作区本就
依赖建出 default」,它只授权建 default。但没有任何东西执行这条声明,实现写的是
`ws = getattr(args, "workspace", None) or "default"`,用了用户的值。

实测两个后果:
  1. `workspace list -w teamZ` 凭空建出 teamZ —— 只读列表命令有写库副作用
  2. workspaces 表只存在于 default 库,所以 `-w alpha` 只列出 alpha+default,
     **beta 从结果里消失** —— 命令返回了一个错误且不完整的答案

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明(不靠命名约定)。
判据锚点覆盖**整个 assert 语句**(含结尾 `)`)—— 只替换首行会留下裸 `)` 变
SyntaxError,r71 C1 与 r72 C2/C3 各栽过一次。

全部变异**先手工验过会产生差异**才写进来:
  M1 还原原 bug(用 args.workspace)      → 杀建库副作用 + 静默截断两条
  M2 只修建库副作用(恒建 default 但仍按 -w 过滤输出) → 只杀静默截断那一条
  M3 什么都不列                          → 杀反向不变量,够窄
  C1 建库副作用判据放宽成只查 teamZ 之外 → 期望存活
  C2 静默截断判据放宽成子集包含          → 期望存活
  C3 反向不变量判据退化成恒真            → 期望存活

自踩:M2 前后栽了两次,两次都是**假变异** ——
  第一版写了 `_ = storage`,rows 压根没变,等于没变异;
  第二版把过滤插在 `storage = Storage(...)` 那行,结果**下一行原始的
  `rows = storage.list_workspaces()` 又把过滤结果覆盖回去**,仍然等于没变异,
  于是 M2 存活。必须锚在真正产出 rows 的那一行上。
M4 也删了:workspaces 表只在 default 库里,根本不存在「遍历所有库」这种正确
替代实现,拿它当覆盖变异是自欺(它实际就是 `rows = []`,一个该被杀的真变异)。
削弱不到让真问题逃掉的变异不算变异(Key Decision 14)。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CLI = REPO / "arl_lite" / "cli.py"
CRIT = REPO / "tests" / "test_workspace_list_w_is_actually_ignored.py"
TARGET = "tests/test_workspace_list_w_is_actually_ignored.py"

BAD_END = (
    "SyntaxError", "IndentationError", "TabError",
    "ERROR collecting", "error during collection", "Interrupted:",
)

ORIGINAL = (
    "    # workspace list 不依赖某个具体 workspace,显示所有。\n"
    "    # 但仍需要一个 Storage 实例来读 workspaces 表 —— 而 workspaces 表**只存在于\n"
    "    # default 库**里,其他工作区的库里没有这张表。\n"
    "    # 所以这里必须恒用 \"default\":用 args.workspace 会既凭空建出一个用户命名\n"
    "    # 的工作区(只读列表命令产生写库副作用),又只列出那个新库里的两条,\n"
    "    # 把 beta 之类的真实工作区从结果里弄丢。-w 在 help 里本就声明为「忽略」。\n"
    "    storage = Storage(workspace=\"default\")"
)

# 判据锚点(整块)
CRIT_ANCHORS = {
    "side_effect": '        assert "teamZ" not in created, (\n'
                   '            f"workspace list -w teamZ 凭空建出了 teamZ(现存 {_ws_dirs(home)})。"\n'
                   '            f"只读列表命令不该有写库副作用;-w 在 help 里本就声明为「忽略」。"\n'
                   "        )",
    "truncation": '            assert got == set(ALL_WS), (\n'
                  '                f"workspace list {flag} alpha 只列出了 {sorted(got)},"',
    "reverse": "        for w in ALL_WS:\n"
               "            assert w in got, f\"{w} 是显式建出来的,必须出现在 workspace list 里:{sorted(got)}\"",
}


def _apply(path: pathlib.Path, old: str, new: str) -> None:
    src = path.read_text(encoding="utf-8")
    if old not in src:
        raise AssertionError(f"变异锚点没命中 {path.name}:{old[:70]!r}")
    out = src.replace(old, new, 1)
    if out == src:
        raise AssertionError("变异是空操作 —— 什么都不会变,写了也是白写")
    path.write_text(out, encoding="utf-8")


# 每个变异该改哪些文件 —— 显式写进元组,不靠 name.startswith("C") 猜。
MUTANTS = [
    ("M1-还原原bug用args.workspace", lambda p: _apply(
        p, ORIGINAL,
        '    ws = getattr(args, "workspace", None) or "default"\n'
        "    storage = Storage(workspace=ws)"), False, (CLI,)),
    ("M2-只修副作用仍按-w过滤", lambda p: _apply(
        p, "    rows = storage.list_workspaces()",
        "    # 变异:建库副作用修好了(恒读 default 库),但仍然按 -w 过滤输出\n"
        "    rows = storage.list_workspaces()\n"
        "    _w = getattr(args, \"workspace\", None)\n"
        "    if _w:\n"
        "        rows = [r for r in rows if r[1] == _w]"), False, (CLI,)),
    ("M3-什么都不列", lambda p: _apply(
        p, "    rows = storage.list_workspaces()",
        "    rows = []  # 变异:什么都不列"), False, (CLI,)),
]

# ---- 覆盖变异:改坏判据自己,期望**存活**(真实现已经是对的)
COVERAGE_MUTANTS = [
    ("C1-副作用判据放宽", lambda p: _apply(
        p, CRIT_ANCHORS["side_effect"],
        '        assert True, "  # 变异:不看是否建了工作区"'), True, (CRIT,)),
    ("C2-静默截断判据放宽成子集", lambda p: _apply(
        p, CRIT_ANCHORS["truncation"],
        "            assert set(ALL_WS) <= got, (  # 变异:放宽成子集包含"), True, (CRIT,)),
    ("C3-反向不变量判据退化成恒真", lambda p: _apply(
        p, CRIT_ANCHORS["reverse"],
        "        assert True, \"  # 变异:反向不变量恒真\""), True, (CRIT,)),
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
        ("实现变异(期望全被杀)", [m for m in MUTANTS if not m[2]]),
        ("覆盖变异(期望全存活)", COVERAGE_MUTANTS + [m for m in MUTANTS if m[2]]),
    ):
        print(f"\n=== r73 {title} ===")
        for name, detail, outp in _sweep(mutants):
            flag = "OK " if detail in ("killed", "survived") else "!! "
            if flag == "!! ":
                bad += 1
            print(f"  {flag}{name:32s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:600])
    print(f"\nr73 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
