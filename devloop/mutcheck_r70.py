"""r70 变异测试:验「watch 全系的 -w 必须真的存在且真被用上」这条判据在守。

主题:arl_lite/cli.py 的 `cmd_watch_start` 一直在读
`Storage(workspace=getattr(args, "workspace", "default"))` —— 像是有 `-w`。
但实测 watch 的 5 个子命令全都没注册 `-w`:`watch add example.com -w teamA`
直接 rc=2 unrecognized arguments。于是 `args.workspace` 永远不存在,
兜底恒生效,`watch start` 永远写进 default 工作区 —— 静默错路,连错都不报。

更糟的一层:目标清单也全局(`~/.arl-lite/watch/watch.json`)。两个工作区并存时
`watch list` 把所有目标混着列,`watch start` 把它们**全部**写进 default。
所以只补 `-w` 不够,那会造出「A 的目标 + B 的结果」。r70 一并把清单按工作区切分。

约定:元组第 4 位 = 期望存活(True/False)。

全部变异**先手工验过会产生差异**才写进来(r47 老规矩,r63/r65~r69 各踩过):
  M1 撤掉 watch 全系 -w 注册(还原原 bug) → 11 条杀
  M2 半吊子修法:-w 存在但清单仍全局    → 只杀 2 条(隔离与路径),够窄
  M3 换回 getattr 兜底(原 bug 形态)     → **存活**,所以它归到覆盖变异:
     -w 真的注册之后 args.workspace 就存在了,getattr 的兜底**永不触发**,
     两者行为等价。这不是判据的洞,是真实现象,如实记着。
  M4 静默迁移旧文件(违反 r62)          → 只杀那一条
  C1 隔离判据放宽成「两个工作区看到一样也算过」 → 期望存活
  C2 迁移判据只看提示文案、不看文件是否被动 → 期望存活
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CLI = REPO / "arl_lite" / "cli.py"
CRIT = REPO / "tests" / "test_watch_workspace_arg.py"
TARGET = "tests/test_watch_workspace_arg.py"

BAD_END = (
    "SyntaxError", "IndentationError", "TabError",
    "ERROR collecting", "error during collection", "Interrupted:",
)

STATE_PATH_LINE = '    return Path.home() / ".arl-lite" / "watch" / workspace / "watch.json"'
GETATTR_LINE = "    storage = Storage(workspace=args.workspace)\n    w = Watcher(storage, state_path=state_file)"
LEGACY_HEAD = '    legacy = _legacy_watch_state_file()\n    if not legacy.exists():\n        return'


def _apply(path: pathlib.Path, old: str, new: str) -> None:
    src = path.read_text(encoding="utf-8")
    if old not in src:
        raise AssertionError(f"变异锚点没命中 {path.name}:{old[:70]!r}")
    out = src.replace(old, new, 1)
    if out == src:
        raise AssertionError("变异是空操作 —— 什么都不会变,写了也是白写")
    path.write_text(out, encoding="utf-8")


def _strip_watch_w_args(path: pathlib.Path) -> None:
    """撤掉 watch 注册段里全部 -w(还原 r70 之前的样子)。"""
    src = path.read_text(encoding="utf-8")
    start = src.index('pwa = sub.add_parser("watch"')
    end = src.index("pwap.set_defaults(func=cmd_watch_stop)")
    seg = src[start:end]
    seg2 = re.sub(
        r'\n *pwa[a-z]*\.add_argument\("-w", "--workspace", help="工作空间名", default="default"\)',
        "", seg)
    if seg2 == seg:
        raise AssertionError("撤 -w 是空操作")
    path.write_text(src[:start] + seg2 + src[end:], encoding="utf-8")


# 每个变异该改哪些文件 —— **显式写进元组**,不靠名字猜。
# 首版用 `name.startswith("C")` 判读,M3 明明改的是 cli.py 却被当成改判据,
# 锚点当场不命中并中止。r69 刚把同一个坑修过一遍(靠命名约定代替显式声明),
# 这里不重复踩:第 4 位之后多带一个 targets 标记。
MUTANTS = [
    ("M1-撤掉watch全系-w注册", _strip_watch_w_args, False, (CLI,)),
    ("M2-半吊子修法清单仍全局", lambda p: _apply(
        p, STATE_PATH_LINE,
        '    return Path.home() / ".arl-lite" / "watch" / "watch.json"  # 变异:忽略 workspace'),
     False, (CLI,)),
    ("M4-静默迁移旧文件", lambda p: _apply(
        p, LEGACY_HEAD,
        LEGACY_HEAD + "\n"
        "    new = _watch_state_file(workspace)  # 变异:静默迁移\n"
        "    new.parent.mkdir(parents=True, exist_ok=True)\n"
        "    if not new.exists():\n"
        '        new.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")\n'
        "        return"), False, (CLI,)),
]

# ---- 覆盖变异:改坏判据自己,期望**存活**(真实现已经是对的)
#
# M3 换回 getattr 兜底也归到这里 —— 实测存活,理由见模块 docstring。

COVERAGE_MUTANTS = [
    # M3 改的是**实现**(cli.py),但期望存活:
    ("M3-换回getattr兜底", lambda p: _apply(
        p, GETATTR_LINE,
        "    storage = Storage(workspace=getattr(args, 'workspace', 'default'))\n"
        "    w = Watcher(storage, state_path=state_file)"), True, (CLI,)),
    ("C1-隔离判据放宽", lambda p: _apply(
        p, '    assert "a.example.com" in a and "b.example.com" not in a, f"teamA 看到了别人的:{a[-200:]}"',
        "    assert True, '  # 变异:隔离判据放宽'"), True, (CRIT,)),
    ("C2-迁移判据只看文案", lambda p: _apply(
        p, '    assert legacy.exists(), "旧文件被自动搬走了 —— r62 说过默认什么都不做"',
        '    assert True, "  # 变异:不看文件是否被动过"'), True, (CRIT,)),
]


def _run_criterion():
    return subprocess.run(
        [sys.executable, "-B", "-m", "pytest", TARGET, "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=REPO, timeout=1200,
    )


def _sweep(mutants, expect_default: bool) -> list:
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
    for title, mutants, expect_default in (
        ("实现变异(期望全被杀)", MUTANTS, False),
        ("覆盖变异(期望全存活)", COVERAGE_MUTANTS, True),
    ):
        print(f"\n=== r70 {title} ===")
        for name, detail, outp in _sweep(mutants, expect_default):
            flag = "OK " if detail in ("killed", "survived") else "!! "
            if flag == "!! ":
                bad += 1
            print(f"  {flag}{name:30s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:600])
    print(f"\nr70 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
