"""r65 变异测试:验「TUI 兜底建议逐条能跑」这条判据真的在守。

主题:第 10 处死路 —— arl_lite/tui/app.py 在 TUI 不可用时打印的替代路径,
原文案写的是裸 `query`,而 `arl-lite query` 缺必填的 table(rc=2)。
这是用户在困境中看到的唯一指引。

约定:元组第 4 位 = 期望存活(True/False)。
sweep(MUTANTS, expect_default=False)      # 实现变异,期望全被杀
sweep(COVERAGE_MUTANTS, expect_default=True)  # 覆盖变异,期望全存活

全部变异**先手工验过会产生差异**才写进来(r47 老规矩,r63/r64 各踩一次):
  M1 手工验过 → 杀掉 test_every_named_command_actually_runs[arl-lite query]
  M2 手工验过 → 首版判据**存活**(软门槛 + 提取器要求前缀,两个洞),
              修判据后才被杀(前缀钉 + 端到端两条同时报红)
  M3 手工验过 → 杀掉 test_the_advice_constant_exists_and_is_shared
  M4 手工验过 → 杀掉 test_every_named_command_actually_runs[arl-lite exportt]
  C1 手工验过 → 端到端被削弱(rc=2 也放过)后,**只有**
              test_query_is_never_named_without_a_table 兜住(见下方如实记录)
  C2 手工验过 → 钉死条数被改回软门槛后,前缀钉 + 端到端两条兜住
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
APP = REPO / "arl_lite" / "tui" / "app.py"
CRIT = REPO / "tests" / "test_tui_fallback_advice_runnable.py"
TARGET = "tests/test_tui_fallback_advice_runnable.py"

BAD_END = ("SyntaxError", "IndentationError", "TabError", "ERROR collecting")


def _apply(path: pathlib.Path, old: str, new: str) -> bool:
    """改文件;返回是否真的产生了差异(空操作变异不证明任何事)。"""
    src = path.read_text(encoding="utf-8")
    if old not in src:
        raise AssertionError(f"变异锚点没命中 {path.name}:{old[:70]!r}")
    if src == src.replace(old, new, 1):
        raise AssertionError("变异是空操作 —— 什么都不会变,写了也是白写")
    path.write_text(src.replace(old, new, 1), encoding="utf-8")
    return True


# ---- 实现变异:改坏 arl_lite/tui/app.py,期望判据被杀

MUTANTS = [
    ("M1-裸query死路", APP,
     "arl-lite query domains               # 域名列表",
     "arl-lite query                      # 域名列表", False),

    ("M2-拿掉arl-lite前缀", APP,
     '"  arl-lite stats                       # 资产概览\\n"',
     '"  stats                                # 资产概览\\n"', False),

    ("M3-拆掉单一来源改手抄", APP,
     'print("[!] TUI 需要交互终端(cron/管道下不可用)。" + _NON_TUI_ALTERNATIVES,',
     'print("[!] TUI 需要交互终端(cron/管道下不可用)。请用 arl-lite stats。",', False),

    ("M4-指向不存在的子命令", APP,
     "arl-lite export                      # 导出现状",
     "arl-lite exportt                     # 导出现状", False),

    # M5 是全量测试时才逮到的第二个坑:全新 HOME 下没有 default 工作区,
    # `arl-lite stats` rc=1 报 "workspace not found: 'default'"。
    # 拿掉建工作区那条,后面 8 条就全撞墙 —— 而「刚装完还没跑过任务」
    # 正是新用户的默认处境。删掉建议是掩盖能力缺失(r63 的规矩)。
    ("M5-拿掉建工作区那条", APP,
     '    "  arl-lite workspace list              # 首次使用先跑这条,会自动建出 default 工作区\\n"\n',
     "", False),
]

# ---- 覆盖变异:改坏判据自己,期望**存活**(真实现已经是对的)

COVERAGE_MUTANTS = [
    # C1 首版写成 `r.returncode not in (0, 1)`,手工实测**被杀**了:
    # 死路 rc=2 仍不在 {0,1} 里,端到端照样报红 —— 削弱得不够狠,
    # 压根没削弱到。真正把端到端废掉是「完全不检查退出码」。
    # 首版那条不算覆盖变异,只算我自己的手滑,如实记着。
    ("C1-端到端完全不检查退出码", CRIT,
     "            if r.returncode != 0:",
     "            if False:", True),

    ("C2-钉死条数改软门槛", CRIT,
     "assert len(cmds) == EXPECTED_COMMAND_COUNT, (",
     "assert len(cmds) >= 3, (", True),
]


def _run_criterion():
    """在**真仓库**里跑判据。

    别把判据连同源文件一起拷进 tmp 目录跑:判据用
    `REPO = pathlib.Path(__file__).resolve().parent.parent` 定位仓库,
    拷到别处它就指向那个空目录,测的不是真东西 —— 而空目录里
    pytest 连收集都做不到,那样的「变异被杀」毫无意义。
    """
    return subprocess.run(
        [sys.executable, "-B", "-m", "pytest", TARGET, "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=REPO, timeout=900,
    )


def _sweep(mutants, expect_default: bool) -> list:
    out = []
    for name, path, old, new, expect in mutants:
        # 复原一律 shutil.copy —— 变异脚本不许用 git 还原
        backup = path.read_bytes()
        try:
            _apply(path, old, new)
            r = _run_criterion()
            outp = r.stdout + r.stderr
            # 假杀守卫:写坏语法的变异和打不上的变异一样无效
            if any(bad in outp for bad in BAD_END):
                out.append((name, "BAD-SYNTAX", outp[-300:]))
                continue
            killed = r.returncode != 0
            ok = (killed != expect)
            detail = "killed" if killed else "survived"
            if not ok:
                detail = f"UNEXPECTED-{detail}"
            out.append((name, detail, outp[-300:] if not ok else ""))
        finally:
            path.write_bytes(backup)
    return out


def main() -> int:
    bad = 0
    for expect_default, mutants, title in (
        (False, MUTANTS, "实现变异(期望全被杀)"),
        (True, COVERAGE_MUTANTS, "覆盖变异(期望全存活)"),
    ):
        print(f"\n=== r65 {title} ===")
        for name, detail, outp in _sweep(mutants, expect_default):
            flag = "OK " if detail in ("killed", "survived") else "!! "
            if flag == "!! ":
                bad += 1
            print(f"  {flag}{name:26s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:600])
    print(f"\nr65 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
