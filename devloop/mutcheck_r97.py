"""r97 变异测试:验「拼错 `-w` 不得建出垃圾工作区」在守。

## 主题

`Storage(workspace=...)` 会**静默自动创建**工作区。r56 建的
`_ensure_workspace_exists` 守卫只落在 7 条只读命令上,r97 实测发现
monitor 这批**有写副作用**的命令一条都没经过那轮审,于是拼错一个 `-w`
的后果不是「报错」,而是磁盘上凭空多出一个持久垃圾工作区:

    monitor add / remove / enable / prune  +  workspace list

`prune` 还报 rc=0 说成功。r97 给那 4 条补上守卫。

## r97 撤回了它自己的第二个结论 —— M5 是为此写的

r97 初版还认定 `watch list -w prod` 报「没有 watch target」是**一句假话**,
于是给 `cmd_watch_list` 加了守卫。**那个结论是错的**,守卫已撤回:
实测 `watch add a.example.com -w teamA` → rc=0,建出
`~/.arl-lite/watch/teamA/watch.json`,而**工作区目录一个都没建**。
watch 全系的 `-w` 是**清单标签**,不是工作区引用,所以那句「没有
watch target」是**真话**。

守卫还造出一个自相矛盾(`watch add -w teamA` rc=0 但
`watch list -w teamA` rc=1),门禁的 `test_baseline` 当场把 3 条既有
判据打红。

**M5 把那个错的守卫原样加回去。** 判据必须能挡住 r97 自己犯过的错,
否则下一个读到 r97 提交信息的人会照着它再加一次。

## 变异清单

实现变异(期望全被杀):
  M1 `cmd_monitor_add` 的守卫被摘掉        → 建出垃圾 → 判据红
  M2 `cmd_monitor_remove` 的守卫被摘掉     → 同上
  M3 `cmd_monitor_enable` 的守卫被摘掉     → 同上
  M4 `cmd_monitor_prune` 的守卫被摘掉      → 同上(它还报 rc=0 说成功)
  M5 `cmd_watch_list` 的守卫被加回来       → 观感上像修复,实为 r97 撤回的那个错
  M6 守卫恒放行(不存在的名字也 return 0)  → 守卫形同虚设
  M7 守卫恒拒绝(真实存在的也 return 1)    → **正控制**被杀:一刀切拒活
  M8 `monitor prune` 只报错不拦           → 说了 workspace not found 却 rc=0
  M9 `_ws_dirs` 恒返回 []                 → 「没建垃圾」那条恒真

覆盖变异(期望全存活):
  C1 拆掉「watch 加得进去就列得出来」那条   ← 正是挡住 M5 的那条
  C2 拆掉「工作区存在时正常路径不受影响」那条

## 首版 harness 的三处错(全被门禁和既有判据逮出来)

1. **M6/M7 写成了同一个变异**:名字一个叫「恒放行」一个叫「恒拒绝」,
   代码却都是把 `data.db 存在 → return 0` 改成 `return 1`。
   **两条一模一样的变异只能证明一件事**,差点把一个写错的变异记成
   「判据有牙」。
2. **A_GUARD_PASS 是半句锚点**(`return 1` 后面还挂着 `\n\n\ndef
   _needs_total(`),违反 `test_mutcheck_anchors_are_whole_statements`。
   r81 立过这条约定,本轮又犯。
3. **C1 的结束标记把装饰器吃掉了**:`@pytest.mark.parametrize(...)` 落在
   起点和结束标记之间,被一起删掉,于是 pytest 转去找一个叫 `func_name`
   的 fixture。而那个报错在 harness 里长得和「变异被杀」一模一样 ——
   **一条跑坏的覆盖变异差点被当成有效结果记进去。**

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
CLAIMS 只写字面量 —— r86/r88/r89/r93/r94/r96 各栽一次,r97 **没有**栽
(首版就把 must_not 写成了整段源码而不是正则片段)。
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CLI = REPO / "arl_lite" / "cli.py"
CRIT = REPO / "tests" / "test_typo_workspace_must_not_create_garbage.py"

TARGET = ["tests/test_typo_workspace_must_not_create_garbage.py"]

_RUN_SUMMARY_RE = re.compile(r"^(?:=+\s*)?(.*\bin [\d.]+s.*)$", re.M)
_ERROR_IN_SUMMARY_RE = re.compile(r"\d+\s+errors?\b")


def _run_itself_broke(outp: str) -> bool:
    """判据**没跑到断言**时,这条变异作废(不算被杀)

    首版这里是「看到 errors 就作废」。r97 实测栽了:M7 让守卫恒拒绝,
    于是夹具的前置条件断言当场炸了 —— pytest 把它记成 **ERROR** 而不是
    FAILED。但那恰恰是**判据逮住了变异**:夹具里那句 `assert rc == 0`
    就是报警器,不是故障。

    两者得分开:输出里有 AssertionError = 判据在说话,算被杀;
    没有(夹具找不到、模块导不进、收集期炸)才是判据根本没跑起来。
    """
    lines = _RUN_SUMMARY_RE.findall(outp)
    if not lines:
        return True
    tail = lines[-1]
    if "no tests ran" in tail:
        return True
    if _ERROR_IN_SUMMARY_RE.search(tail):
        return "AssertionError" not in outp
    return False


# ── 四个 monitor 命令的守卫块(连 r97 的注释一起,否则指不准) ──
# 守卫块 `if _ensure_workspace_exists(args.workspace): return 1` 在
# cli.py 里出现 14 次(r56 那批 + r97 这批),所以锚点必须带注释。

A_ADD = (
    "    # r97:工作区不存在就停手。`Storage()` 会**静默自动创建**工作区,\n"
    "    # 所以拼错一个 `-w` 的实测后果不是「报错」,而是磁盘上凭空多出一个\n"
    "    # 垃圾工作区,并且它会出现在之后每一次 `workspace list` 里。\n"
    "    if _ensure_workspace_exists(args.workspace):\n"
    "        return 1\n")
A_REMOVE = (
    "    # r97:见 `cmd_monitor_add` 的同一段注释。删除命令去**建出**一个工作区\n"
    "    # 尤其荒唐 —— 实测 `monitor remove nosuch -w TYPO` 会打印\n"
    "    # 「workspace 'TYPO' created」并留下一个持久垃圾目录。\n"
    "    if _ensure_workspace_exists(args.workspace):\n"
    "        return 1\n")
A_ENABLE = (
    "    # r97:见 `cmd_monitor_add` 的同一段注释。实测这一条尤其荒唐 ——\n"
    "    # 它一边打印「monitor #1 not found」,一边把拼错的工作区建了出来。\n"
    "    if _ensure_workspace_exists(args.workspace):\n"
    "        return 1\n")
A_PRUNE = (
    "    # r97:工作区必须已经存在。理由见 docstring 那段 —— 清理命令凭空建出\n"
    "    # 一个工作区,还报 rc=0。\n"
    "    if _ensure_workspace_exists(args.workspace):\n"
    "        return 1\n")
A_PRUNE_NO_RETURN = (
    "    # r97:工作区必须已经存在。理由见 docstring 那段 —— 清理命令凭空建出\n"
    "    # 一个工作区,还报 rc=0。\n"
    "    if _ensure_workspace_exists(args.workspace):\n"
    "        print(\"[!] workspace missing\", file=sys.stderr)  # 变异 M8:只喊不拦\n")

# ── M5:把 r97 撤回的那个错守卫原样加回去 ──
A_WATCH_ANCHOR = ("    而「清单为空」本来就不是错误状态。\n"
                  '    """\n'
                  "    _warn_about_legacy_watch_state(args.workspace)\n")
A_WATCH_GUARDED = (
    "    而「清单为空」本来就不是错误状态。\n"
    '    """\n'
    "    if _ensure_workspace_exists(args.workspace):  # 变异 M5:r97 撤回的那个守卫\n"
    "        return 1\n"
    "    _warn_about_legacy_watch_state(args.workspace)\n")

# ── 守卫本身的两处行为变异 ──
# 首版这两条写成了同一个变异(名字一个叫「恒放行」一个叫「恒拒绝」,
# 代码都是把「data.db 存在 → return 0」改成 `return 1`)。两条一样的
# 变异只能证明一件事。
A_GUARD_PASS = ('        print(f"        arl-lite run -t <target>", file=sys.stderr)\n'
                '        print("    (想换个名字就用 -w <name>;已有工作区可用时上面会列出)",\n'
                "              file=sys.stderr)\n"
                "    return 1\n")
A_GUARD_PASS_M = ('        print(f"        arl-lite run -t <target>", file=sys.stderr)\n'
                  '        print("    (想换个名字就用 -w <name>;已有工作区可用时上面会列出)",\n'
                  "              file=sys.stderr)\n"
                  "    return 0  # 变异 M6:不存在的名字也放行\n")
A_GUARD_FAIL = '    if (ws_root / name / "data.db").exists():\n        return 0\n'
A_GUARD_FAIL_M = ('    if (ws_root / name / "data.db").exists():\n'
                  '        return 1  # 变异 M7:真实存在的也拒\n')

# ── M9:_ws_dirs 恒返回 [] ──
A_WSDIRS = ("    root = _ws_root(home)\n"
            "    if not root.exists():\n"
            "        return []\n"
            "    return sorted(p.name for p in root.iterdir() if p.is_dir())\n")
A_WSDIRS_M = "    return []  # 变异 M9:「没建垃圾」这条恒真\n"

# ── 覆盖变异:def 行整行照抄(削掉 () 和冒号是语法错误,r94 栽过) ──
# 结束标记**必须带上装饰器**:首版 C1 只写到下一个 `def`,把
# `@pytest.mark.parametrize(...)` 一起删掉了,pytest 转去找一个叫
# `func_name` 的 fixture —— 而那个报错和「变异被杀」长得一模一样。
C1_BODY = "def test_watch_add_and_list_agree_on_the_same_name(empty_home):"
C1_END = "def test_watch_list_stays_scoped_to_its_own_label(empty_home):"
C2_BODY = "def test_existing_workspace_normal_paths_still_work(empty_home):"
C2_END = "def test_remove_of_a_real_monitor_still_works(empty_home):"

# 只写字面量,不写名字引用(r86/r88/r89/r93/r94/r96 各栽一次)
CLAIMS = {
    "M1-monitor_add守卫被摘掉": (
        [],
        ["    # r97:工作区不存在就停手。`Storage()` 会**静默自动创建**工作区,"],
    ),
    "M2-monitor_remove守卫被摘掉": (
        [],
        ["    # r97:见 `cmd_monitor_add` 的同一段注释。删除命令去**建出**一个工作区"],
    ),
    "M3-monitor_enable守卫被摘掉": (
        [],
        ["    # r97:见 `cmd_monitor_add` 的同一段注释。实测这一条尤其荒唐 ——"],
    ),
    "M4-monitor_prune守卫被摘掉": (
        [],
        ["    # r97:工作区必须已经存在。理由见 docstring 那段 —— 清理命令凭空建出"],
    ),
    # M5 是**纯插入**:docstring 那行在替换前后都在,所以 must_not 必须留空。
    # 头一版给它写了个 must_not,自检守卫当场报「仍在」—— 那正是它该干的活:
    # 一个变异声称自己删了什么,实际没删,那条变异就是名不副实。
    "M5-把撤回的watch守卫加回来": (
        ["    if _ensure_workspace_exists(args.workspace):  # 变异 M5:r97 撤回的那个守卫"],
        [],
    ),
    "M6-守卫恒放行": (
        ["    return 0  # 变异 M6:不存在的名字也放行"],
        ['        print("    (想换个名字就用 -w <name>;已有工作区可用时上面会列出)",\n'
         "              file=sys.stderr)\n"
         "    return 1\n"],
    ),
    "M7-守卫恒拒绝": (
        ["        return 1  # 变异 M7:真实存在的也拒"],
        ['    if (ws_root / name / "data.db").exists():\n        return 0'],
    ),
    "M8-prune只报错不拦": (
        ["        print(\"[!] workspace missing\", file=sys.stderr)  # 变异 M8:只喊不拦"],
        ["        return 1\n    storage = Storage(workspace=args.workspace)\n    try:\n"
         "        cutoff = _parse_since(args.older_than)"],
    ),
    "M9-没建垃圾恒真": (
        ["    return []  # 变异 M9:「没建垃圾」这条恒真"],
        ["    return sorted(p.name for p in root.iterdir() if p.is_dir())"],
    ),
    "C1-拆掉watch两边一致那条": (
        ["    pass  # 变异 C1:整条判据没了"],
        [],
    ),
    "C2-拆掉正常路径不受影响那条": (
        ["    pass  # 变异 C2:整条判据没了"],
        [],
    ),
}

MUTANTS = [
    ("M1-monitor_add守卫被摘掉", lambda p: _apply(p, A_ADD, ""), False, (CLI,)),
    ("M2-monitor_remove守卫被摘掉", lambda p: _apply(p, A_REMOVE, ""), False, (CLI,)),
    ("M3-monitor_enable守卫被摘掉", lambda p: _apply(p, A_ENABLE, ""), False, (CLI,)),
    ("M4-monitor_prune守卫被摘掉", lambda p: _apply(p, A_PRUNE, ""), False, (CLI,)),
    ("M5-把撤回的watch守卫加回来", lambda p: _apply(p, A_WATCH_ANCHOR, A_WATCH_GUARDED),
     False, (CLI,)),
    ("M6-守卫恒放行", lambda p: _apply(p, A_GUARD_PASS, A_GUARD_PASS_M), False, (CLI,)),
    ("M7-守卫恒拒绝", lambda p: _apply(p, A_GUARD_FAIL, A_GUARD_FAIL_M), False, (CLI,)),
    ("M8-prune只报错不拦", lambda p: _apply(p, A_PRUNE, A_PRUNE_NO_RETURN), False, (CLI,)),
    ("M9-没建垃圾恒真", lambda p: _apply(p, A_WSDIRS, A_WSDIRS_M), False, (CRIT,)),
]

COVERAGE_MUTANTS = [
    ("C1-拆掉watch两边一致那条",
     lambda p: _replace_fn(p, C1_BODY, C1_BODY + "\n    pass  # 变异 C1:整条判据没了\n",
                           C1_END), True, (CRIT,)),
    ("C2-拆掉正常路径不受影响那条",
     lambda p: _replace_fn(p, C2_BODY, C2_BODY + "\n    pass  # 变异 C2:整条判据没了\n",
                           C2_END), True, (CRIT,)),
]


def _check_claim_points_at_one_place(name: str, original: str) -> None:
    if name not in CLAIMS:
        raise AssertionError(f"变异 {name!r} 没有自检声明")
    must_have, must_not = CLAIMS[name]
    if not must_have and not must_not:
        raise AssertionError(f"变异 {name!r} 的声明是空的 —— 等于没声明")
    for piece in must_not:
        n = original.count(piece)
        if n != 1:
            raise AssertionError(
                f"变异 {name!r} 的声明串 {piece!r} 在改前出现 {n} 次 —— 指不准位置")
    for piece in must_have:
        if piece in original:
            raise AssertionError(
                f"变异 {name!r} 的 must_not/must_have 写反了:{piece!r} 改前就存在,"
                f"那它就不是「这次变异带来的」")


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
    if out == path.read_text(encoding="utf-8"):
        raise AssertionError("变异是空操作 —— 什么都不会变,写了也是白写")
    import ast
    try:
        ast.parse(out)
    except SyntaxError as e:
        raise AssertionError(
            f"变异会让 {path.name} 语法错误({e})。文件未写入。") from None
    path.write_text(out, encoding="utf-8")


def _apply(path: pathlib.Path, old: str, new: str) -> None:
    src = path.read_text(encoding="utf-8")
    n = src.count(old)
    if n != 1:
        raise AssertionError(
            f"变异锚点在 {path.name} 出现 {n} 次(必须恰好 1 次):{old[:70]!r}")
    _write_checked(path, src.replace(old, new, 1))


def _replace_fn(path: pathlib.Path, start: str, stub: str, end: str) -> None:
    """把 `start` 到下一个 `end` 标记之间的整段换成一条 pass 桩

    `def` 行**原样照抄** —— 削掉 `()` 和冒号会产出 `def test_x`,那是语法
    错误(r94 栽过)。
    """
    src = path.read_text(encoding="utf-8")
    if src.count(start) != 1:
        raise AssertionError(f"变异锚点没唯一命中 {path.name}:{start!r}")
    if src.count(end) != 1:
        raise AssertionError(f"变异结束标记没唯一命中 {path.name}:{end!r}")
    i = src.index(start)
    j = src.index(end, i)
    if j <= i:
        raise AssertionError(
            f"结束标记 {end!r} 出现在起点**之前** —— 标记写反了,不是判据的问题")
    _write_checked(path, src[:i] + stub + src[j:])


def _run_criterion():
    return subprocess.run(
        [sys.executable, "-B", "-m", "pytest", *TARGET, "-q",
         "-p", "no:cacheprovider", "-W", "ignore::DeprecationWarning"],
        capture_output=True, text=True, cwd=REPO, timeout=1200,
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
            except Exception as e:
                # **harness 里一个没接住的异常,和一条变异没被杀,
                # 是同一种浪费**:都让这一轮白跑(r96 栽过)。
                out.append((name, "BAD-MUTANT",
                            f"变异器自己抛了 {type(e).__name__}: {e}"[:400]))
                continue
            r = _run_criterion()
            outp = r.stdout + r.stderr
            if _run_itself_broke(outp):
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
        print(f"\n=== r97 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:32s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:300])
    print(f"\nr97 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
