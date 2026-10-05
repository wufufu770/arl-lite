"""r67 变异测试:验「工作区报错必须给出路」这条判据真的在守。

主题:arl_lite/cli.py 的 `_ensure_workspace_exists` 被 7 个只读子命令共用
(cmd_query / cmd_search / cmd_export / cmd_stats / cmd_diff /
cmd_correlate / cmd_risk_top)。它有两条分支:有工作区但名字拼错时打印
`available: [...]`(一直是对的);**一个工作区都没有**时(全新安装,
正是新用户的默认处境)原来只有一行 `workspace not found: 'default'`
然后 rc=1 —— 零出路。

修法里两个反直觉的点都是实测踩出来的:
- 出路指向 `arl-lite run -t <target>`,不是 `workspace list` ——
  后者确实 rc=0 但那是绕路,run 才是用户本来就想做的事;
- 刻意不指向 `workspace create default`:实测空环境下它报
  「already exists」rc=1(Storage 先静默建库),最像样的出路本身是错路。

约定:元组第 4 位 = 期望存活(True/False)。
sweep(MUTANTS, expect_default=False)      # 实现变异,期望全被杀
sweep(COVERAGE_MUTANTS, expect_default=True)  # 覆盖变异,期望全存活

全部变异**先手工验过会产生差异**才写进来(r47 老规矩,r63/r65/r66 各踩过):
  M1 首版锚点漏了 `else:` 行,写出了 IndentationError —— 假杀守卫
     本来就该拦下它。补上锚点后实测:2 条判据报红。
  M2 出路改成指向 workspace create → 端到端 + 负向判据两条报红
  M3 拿掉 arl-lite 前缀 → 退回隐形建议,2 条报红
  M4 静默建库(取消存在性校验) → 4 条报红
  C1 端到端放宽 rc → 被负向判据「不许指向 workspace create」兜住
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CLI = REPO / "arl_lite" / "cli.py"
CRIT = REPO / "tests" / "test_workspace_missing_gives_a_way_out.py"
TARGET = "tests/test_workspace_missing_gives_a_way_out.py"

BAD_END = ("SyntaxError", "IndentationError", "TabError", "ERROR collecting")

# 出路那几行的起止标记 —— 按标记切,不按行号切(行号会随注释漂)
WAYOUT_START = "    else:\n        # 没有任何工作区时"
WAYOUT_END = "    return 1"
WAYOUT_CMD = '        print(f"        arl-lite run -t <target>", file=sys.stderr)'

# 端到端判据那段的起止标记(按标记切,不按行号切)
# r68 期间判据重构过一次(「出路 rc=0」这个错指标被换成
# 「出路达成目的」,那套网络 skip 机制一起删了),锚点随之失效。
# 教训又是一次:锚点要锚在**语义稳定**的地方,别锚在会被重构掉的中间步骤上。
C2_FROM = "    for cmd in cmds:\n"
C2_TO = "def test_the_way_out_does_not_point_at_workspace_create"


def _punch(path: pathlib.Path, start_marker: str, end_marker: str) -> None:
    """把 [start, end) 之间整段替换掉。"""
    src = path.read_text(encoding="utf-8")
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    out = src[:start] + "    # 变异:端到端两半全废\n\n\n" + src[end:]
    if out == src:
        raise AssertionError("端到端变异是空操作")
    path.write_text(out, encoding="utf-8")


def _apply(path: pathlib.Path, old: str, new: str) -> None:
    src = path.read_text(encoding="utf-8")
    if old not in src:
        raise AssertionError(f"变异锚点没命中 {path.name}:{old[:70]!r}")
    out = src.replace(old, new, 1)
    if out == src:
        raise AssertionError("变异是空操作 —— 什么都不会变,写了也是白写")
    path.write_text(out, encoding="utf-8")


def _drop_way_out(path: pathlib.Path) -> None:
    """整段拿掉出路(含 `else:` 行,否则会写出 IndentationError)。"""
    src = path.read_text(encoding="utf-8")
    start = src.index(WAYOUT_START)
    end = src.index(WAYOUT_END, start)
    out = src[:start] + src[end:]
    if out == src:
        raise AssertionError("拿掉出路是空操作")
    path.write_text(out, encoding="utf-8")


MUTANTS = [
    # M1: 回到「只有一行报错、零出路」的原样
    ("M1-拿掉出路", _drop_way_out, False),

    # M2: 出路指向 workspace create —— 实测那是条错路(rc=1「已存在」)
    ("M2-出路指向workspace_create", lambda p: _apply(
        p, WAYOUT_CMD,
        '        print("        arl-lite workspace create default", file=sys.stderr)'), False),

    # M3: 拿掉 arl-lite 前缀 —— 退回隐形建议,r60-r65 判据看不见
    ("M3-拿掉arl-lite前缀", lambda p: _apply(
        p, WAYOUT_CMD,
        '        print(f"        run -t <target>", file=sys.stderr)'), False),

    # M4: 静默建库 —— 违反 _ensure_workspace_exists 明写的不变式
    ("M4-静默建库", lambda p: _apply(
        p, '    if (ws_root / name / "data.db").exists():\n        return 0',
        '    Storage(workspace=name)  # 变异:静默建库\n    return 0'), False),
]

# ---- 覆盖变异:改坏判据自己,期望**存活**(真实现已经是对的)

COVERAGE_MUTANTS = [
    # C1: 放宽端到端唯一那个断言 —— 期望存活(真实实现本来就通过),
    #     而「出路指向 workspace create」那类真问题由负向判据独立逮住。
    # 锚点随 r68 的判据重构改过一次:首版锚的是循环里那句
    # `if rr.returncode != 0:`,重构把它整个删掉了,脚本当场报
    # 「锚点没命中」并中止(幸而 finally 复原了文件,判据没被改坏 ——
    # 但这已经是 r66/r67 各一次的同类事故:锚点锚在了会被重构掉的中间步骤上)。
    # 现在锚的是**契约本身**,它在重构后仍然存在,而且语义稳定。
    ("C1-端到端接受rc1", lambda p: _apply(
        p, "    assert after.returncode == 0, (",
        "    assert after.returncode in (0, 1), ("), True),

    # C2 前两版都**被杀**,两次原因不同,如实记着:
    #  v1「把抠出路的正则退回跨行版」→ 跨行抠出不存在的东西,
    #     端到端真跑 rc=2 就报红 —— 压根没削弱到,只是把同一个洞又戳一遍。
    #  v2「跳过执行出路那一步」→ 同一条判据里还有「照敲完 stats 必须
    #     rc=0」那半截,工作区压根没被建出来,照样报红。
    # 也就是说这条判据有两半,互相兜底,削弱一半没用 —— 那是这条
    # 判据设计得对,但也意味着要测出「别处兜不兜得住」得两半一起废。
    # v3 两半一起废。
    # 锚点**按标记切**而不是逐行拼字符串:r67 判据中途改过一次
    # (`_run` 加了 timeout 参数、前面插了网络判定),逐行拼的锚点当场断裂
    # —— r66 刚吃过这个亏,这里不重复踩。
    ("C2-废掉端到端两半", lambda p: _punch(p, C2_FROM, C2_TO), True),
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
        print(f"\n=== r67 {title} ===")
        for name, detail, outp in _sweep(mutants, expect_default):
            flag = "OK " if detail in ("killed", "survived") else "!! "
            if flag == "!! ":
                bad += 1
            print(f"  {flag}{name:34s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:700])
    print(f"\nr67 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
