"""r69 变异测试:验「碰工作区的命令要么有守卫要么登记例外」这条判据在守。

主题:r68 修完三个静默建库的命令后,判据靠**手写** MUST_REJECT 清单守着
「只读命令必须走工作区校验」。漏洞实测过:往 cli.py 注入一个不在清单里的
静默建库只读命令,判据报 19 passed 一声不响 —— 靠猜的清单会漏,
和 r65 建议判据栽的是同一个坑。

推导分两层,第一层可靠、第二层**不可靠**(如实记在判据注释里):
- 「碰不碰工作区 / 有没有守卫」:可靠。看 `_ensure_workspace_exists` 与
  `Storage` 谁先出现 —— 顺序即契约。实测 20 个 cmd_ 干净分成 10 + 10。
- 「该不该有守卫」:不可靠。按 SQL 关键字 / 写方法名 / 只读方法名白名单
  分类,`cmd_watch_add` 这类明明在写的被判成只读+缺守卫,另有 4 个判不准。
  真因是写操作都在更深一层(`Monitor(storage).add(...)`)。

所以守门人是**双向不变量**而不是完整推导:碰工作区的命令要么有守卫,
要么在 EXCEPTIONS 里并写了理由;反过来 EXCEPTIONS 里若有条目已经有守卫
(它不再需要例外)也报红。

约定:元组第 4 位 = 期望存活(True/False)。

全部变异**先手工验过会产生差异**才写进来(r47 老规矩,r63/r65~r68 各踩过):
  M1 推导方向搞反            → 杀
  M2 EXCEPTIONS 清空          → 杀
  M3 推导返回空(静默空转)    → 杀(靠 assert guarded 那句,r63 立的规矩)
  M4 给例外命令加守卫(实现侧) → 4 条杀
  C1 理由长度下限放宽         → 存活
  C2 守门人整个废掉           → 存活,但**必须连带注入真缺口才测**:
     单独废掉时 21 passed(真实实现确实没缺口,理应存活);
     废掉 + 注入静默建库命令时仍 21 passed —— 说明这条守门人是
     **唯一防线**,r68 那 20 条端到端判据一个都没兜住。如实记着。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CLI = REPO / "arl_lite" / "cli.py"
CRIT = REPO / "tests" / "test_readonly_cmds_same_workspace_contract.py"
TARGET = "tests/test_readonly_cmds_same_workspace_contract.py"

# 假杀守卫:写坏语法的变异和打不上的变异**一样无效**,不能算「被杀」。
#
# BAD_END 得列全 —— 首版只有 "ERROR collecting",而 pytest 实际打的是
# "Interrupted: N error(s) during collection",没接住,结果一个把文件
# 改坏的变异冒充成了「变异被杀」。这是本轮实测踩出来的。
BAD_END = (
    "SyntaxError", "IndentationError", "TabError",
    "ERROR collecting", "error during collection",
    "Interrupted:",
)

PARTITION_LINE = (
    '        (guarded if first == "_ensure_workspace_exists" else unguarded).add(node.name)'
)
RETURN_LINE = "    return guarded, unguarded\n"
GUARD_BODY = '''    guarded, unguarded = _workspace_partition()
    assert guarded, "推导不出任何有守卫的命令 —— 推导逻辑坏了"
    unregistered = sorted(unguarded - set(EXCEPTIONS))'''
WS_LIST_BODY = (
    '    ws = getattr(args, "workspace", None) or "default"\n'
    "    storage = Storage(workspace=ws)\n"
    "    rows = storage.list_workspaces()"
)
INJECT = '''def cmd_probe_new_readonly(args) -> int:
    storage = Storage(workspace=args.workspace)
    print(f"[+] probe: {storage.get_stats()}")
    return 0


'''


def _apply(path: pathlib.Path, old: str, new: str) -> None:
    src = path.read_text(encoding="utf-8")
    if old not in src:
        raise AssertionError(f"变异锚点没命中 {path.name}:{old[:70]!r}")
    out = src.replace(old, new, 1)
    if out == src:
        raise AssertionError("变异是空操作 —— 什么都不会变,写了也是白写")
    path.write_text(out, encoding="utf-8")


def _punch(path, start_marker, end_marker, replacement):
    src = path.read_text(encoding="utf-8")
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    out = src[:start] + replacement + src[end:]
    if out == src:
        raise AssertionError("区间变异是空操作")
    path.write_text(out, encoding="utf-8")


def _punch_lines(path, start_line, end_line, replacement):
    """按**整行**切区间。

    `_punch` 切的是子串,锚点一旦落在内容里(比如理由文字中带括号)
    就会切出半截,拼回去直接语法错 —— 而语法错的变异会被假杀守卫
    判成「无效」,那一轮白跑。这里把两端都对齐到行边界,从根上避开。
    """
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    try:
        i = next(k for k, ln in enumerate(lines) if ln.startswith(start_line))
        j = next(k for k, ln in enumerate(lines) if k > i and ln.rstrip() == end_line)
    except StopIteration:
        raise AssertionError(f"区间行锚点没命中:{start_line!r} .. {end_line!r}")
    out = "".join(lines[:i]) + replacement + "".join(lines[j + 1:])
    if out == "".join(lines):
        raise AssertionError("区间变异是空操作")
    path.write_text(out, encoding="utf-8")


# ---- 实现变异:改坏判据的推导/清单,期望全被杀

MUTANTS = [
    ("M1-推导方向搞反", lambda p: _apply(
        p, PARTITION_LINE,
        '        (guarded if first == "Storage" else unguarded).add(node.name)'), False),
    # M2 用区间切。end_marker 必须锚到**行首**的右花括号:
    # 首版写 "}\n",那是字典里第一条理由内部就带括号的位置,切出来是半截;
    # 改 "}\n\n\n" 也不行 —— 理由文字里可能有连续换行。锚 "^}\n"(行首)才准。
    # 这跟 r67 的教训同源:锚点要锚在语义稳定、且不会被内容里的字符撞到的地方。
    ("M2-EXCEPTIONS清空", lambda p: _punch_lines(
        p, "EXCEPTIONS = {", "}", "EXCEPTIONS = {}\n"), False),
    ("M3-推导返回空静默空转", lambda p: _apply(
        p, RETURN_LINE, "    return set(), set()\n"), False),
    ("M4-给例外命令加守卫", lambda p: _apply(
        p, WS_LIST_BODY,
        '    ws = getattr(args, "workspace", None) or "default"\n'
        "    if _ensure_workspace_exists(ws):\n"
        "        return 1\n"
        "    storage = Storage(workspace=ws)\n"
        "    rows = storage.list_workspaces()"), False),
]

# ---- 覆盖变异:改坏判据自己,期望**存活**(真实现已经是对的)

COVERAGE_MUTANTS = [
    ("C1-理由长度下限放宽", lambda p: _apply(
        p, "assert len(why) >= 10,", "assert len(why) >= 1,"), True),
    ("C2-守门人整个废掉", lambda p: _apply(
        p, GUARD_BODY,
        GUARD_BODY.replace(
            "    unregistered =",
            "    if True:  # 变异:整个守门人废掉\n        return\n    unregistered =")), True),
]


def targets_for(name: str) -> list:
    """每个变异该改哪些文件 —— 显式表,不靠名字约定。

    M1/M2/M3 改的是**判据里的推导与清单**,M4 改的是实现,两个 C 改判据。
    首版用 `name.startswith("C")` 判读,r66/r67 栽过同一个坑
    (靠命名约定代替显式声明,约定一变就静默改错文件)。
    """
    if name.startswith("M4"):
        return [CLI]
    return [CRIT]


def _run_criterion():
    return subprocess.run(
        [sys.executable, "-B", "-m", "pytest", TARGET, "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=REPO, timeout=900,
    )


def _sweep(mutants, expect_default: bool) -> list:
    out = []
    for name, mutate, expect in mutants:
        # 目标文件**显式声明**在变异条目里 —— 别靠名字猜。
        # 首版按「名字以 C 开头就是改判据」来分,结果 M1 明明改的是
        # 判据里的推导函数,却被当成改 cli.py,锚点当场不命中并中止
        # (与 r66/r67 同一类事故:靠命名约定代替显式声明)。
        targets = targets_for(name)
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


def _probe_solo_guard() -> list:
    """额外验一件事:C2(废掉守门人)到底还有没有别的防线。

    做法是废掉守门人**并同时注入一个真缺口**(不在任何清单里的静默建库
    只读命令)。如果这样还绿,说明这条守门人是唯一防线 —— 那不是失败,
    是必须如实记下它对判据的依赖关系。
    """
    b1, b2 = CLI.read_bytes(), CRIT.read_bytes()
    try:
        src = CLI.read_text(encoding="utf-8")
        CLI.write_text(src.replace("def cmd_export(args) -> int:",
                                   INJECT + "def cmd_export(args) -> int:", 1),
                       encoding="utf-8")
        s = CRIT.read_text(encoding="utf-8")
        CRIT.write_text(s.replace(
            "    unregistered =",
            "    if True:  # 变异:整个守门人废掉\n        return\n    unregistered =", 1),
            encoding="utf-8")
        r = _run_criterion()
        outp = r.stdout + r.stderr
        killed = r.returncode != 0
        return [("C2b-废守门人+注入真缺口",
                 "still-killed(有别的防线)" if killed else "ALSO-GREEN(唯一防线)",
                 outp[-300:])]
    finally:
        CLI.write_bytes(b1)
        CRIT.write_bytes(b2)


def main() -> int:
    bad = 0
    for title, mutants, expect_default in (
        ("实现变异(期望全被杀)", MUTANTS, False),
        ("覆盖变异(期望全存活)", COVERAGE_MUTANTS, True),
    ):
        print(f"\n=== r69 {title} ===")
        for name, detail, outp in _sweep(mutants, expect_default):
            flag = "OK " if detail in ("killed", "survived") else "!! "
            if flag == "!! ":
                bad += 1
            print(f"  {flag}{name:30s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:700])
    print("\n=== 额外探测:守门人是不是唯一防线 ===")
    for name, detail, outp in _probe_solo_guard():
        print(f"  ·  {name:30s} {detail}")
    print(f"\nr69 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
