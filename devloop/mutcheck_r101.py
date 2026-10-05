"""r101 变异测试:验「死代码检测器」自己不是一个恒过的过滤器。

## 主题

本轮真正的交付物是 `tests/test_no_unreferenced_module_level_defs.py` 里
那个检测器 —— 不是它删掉的那两行代码。

起因是我想找死代码来松开 `loc_budget`,用「静态引用计数 == 0」去找,
跑出 **115 个候选**,逐个查下来几乎全是误报。改两处之后剩 2 个(真的):

1. **阈值错了**:`def` 的名字在 AST 里不是 `ast.Name`,所以「被调用一次」
   正好等于计数 1。首版用 `<= 1` 当阈值 → 所有只被调用一次的函数全被判死。
   这一处就贡献了 93 个误报。
2. **漏了动态发现面**:`registry.discover_modules()` 用 `pkgutil.walk_packages`
   + `inspect.getmembers` 扫 `arl_lite.modules` 下每个模块,那些
   `XxxModule` **按设计没有静态引用**。

判不准的检测器比没有更危险 —— 它会让人去删能跑的东西。所以这轮的变异
重点不是「代码对不对」,而是**「检测器出错时会不会被逮住」**。

## 变异清单

实现变异(期望全被杀):
  M1 阈值改回 `<= 1`                       → r101 的真实 bug 复活 → 红
  M2 阈值改成 `<= 2`(更宽的错)             → 更多误报 → 红
  M3 去掉动态发现面的排除                   → 24 个 XxxModule 全被判死 → 红
  M4 `find_unreferenced` 恒返回 []          → 恒过的过滤器 → 红
  M5 `MainScreen` 改回不继承 `Screen`       → 又要抄一份 loop → 红
  M6 `move_to` 加回来                      → 红

覆盖变异(期望全存活):
  C1 拆掉「只被调用一次不是死代码」那条   ← M1/M2 的唯一防线
  C2 拆掉「真的零引用要被列出来」那条     ← M4 的唯一防线
  C3 拆掉「动态发现不算死代码」那条       ← M3 的唯一防线
  C4 拆掉「loop 只有一份实现」那条

## C1/C2/C3 三条是这一轮的命门

它们是**纯合成样本**判据,不碰真实代码 —— 也就是说它们在任何真实代码
变化下都恒绿。看起来「没用」,其实相反:正因为它们锁的是**检测器自身**
的阈值与排除项,它们才是不受代码演进影响的那层。

一个只会随代码变红、从不随自身缺陷变红的检测器,和没有检测器等价。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
CLAIMS 只写字面量,不写名字引用。
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
TUI = REPO / "arl_lite" / "tui" / "app.py"
CRIT = REPO / "tests" / "test_no_unreferenced_module_level_defs.py"

TARGET = ["tests/test_no_unreferenced_module_level_defs.py"]

_RUN_SUMMARY_RE = re.compile(r"^(?:=+\s*)?(.*\bin [\d.]+s.*)$", re.M)
_ERROR_IN_SUMMARY_RE = re.compile(r"\d+\s+errors?\b")


def _run_itself_broke(outp: str) -> bool:
    lines = _RUN_SUMMARY_RE.findall(outp)
    if not lines:
        return True
    tail = lines[-1]
    if "no tests ran" in tail:
        return True
    if _ERROR_IN_SUMMARY_RE.search(tail):
        return "AssertionError" not in outp
    return False


# ── M1/M2:阈值 ──
A_THRESHOLD = "            if uses[name] == 0:\n                out.append((p, lineno, name))\n"
A_THRESHOLD_LE1 = ("            if uses[name] <= 1:  # 变异 M1:阈值错了(调用一次也判死)\n"
                   "                out.append((p, lineno, name))\n")
A_THRESHOLD_LE2 = ("            if uses[name] <= 2:  # 变异 M2:更宽的错\n"
                   "                out.append((p, lineno, name))\n")

# ── M3:去掉动态发现面的排除 ──
A_SKIP_DOC = ("    `skip` 里的文件整份跳过 —— 那就是动态发现面。\n"
              "    \"\"\"\n"
              "    out = []\n"
              "    for p in sources:\n"
              "        if p in skip:\n"
              "            continue\n")
A_SKIP_GONE = ("    变异 M3:跳过逻辑没了 —— 动态发现目录会被当成普通源码\n"
               "    \"\"\"\n"
               "    out = []\n"
               "    for p in sources:\n")

# ── M4:恒过的过滤器 ──
A_RETURN = "    return out\n"
A_RETURN_NONE = "    return []  # 变异 M4:恒过的过滤器\n"

# ── M5/M6/MainScreen 继承 ──
A_INHERIT = 'class MainScreen(Screen):\n    name = "arl-lite 主菜单"\n'
A_INHERIT_GONE = 'class MainScreen:  # 变异 M5:又没继承,loop 得再抄一份\n    name = "arl-lite 主菜单"\n'

# ── M6:move_to 复活 ──
A_TERM_WIDTH = "def _term_width() -> int:\n"
A_MOVE_TO = ("def move_to(row: int, col: int) -> None:  # 变异 M6:死代码回来了\n"
             '    sys.stdout.write(f"\\033[{row};{col}H")\n'
             "    sys.stdout.flush()\n"
             "\n"
             "\n"
             "def _term_width() -> int:\n")

# ── 覆盖变异 ──
C1_BODY = "def test_a_name_called_exactly_once_is_not_dead(tmp_path):"
C1_END = "def test_a_genuinely_unreferenced_name_is_still_reported(tmp_path):"
C2_BODY = "def test_a_genuinely_unreferenced_name_is_still_reported(tmp_path):"
C2_END = "def test_dynamically_discovered_definitions_are_not_dead(tmp_path):"
C3_BODY = "def test_dynamically_discovered_definitions_are_not_dead(tmp_path):"
C3_END = "def test_the_real_dynamic_root_exists_and_is_not_empty():"
C4_BODY = "def test_the_shared_loop_lives_in_exactly_one_place():"
C4_END = "def test_move_to_stays_deleted():"

CLAIMS = {
    "M1-阈值改回le1": (
        ["            if uses[name] <= 1:  # 变异 M1:阈值错了(调用一次也判死)"],
        ["            if uses[name] == 0:"],
    ),
    "M2-阈值改宽": (
        ["            if uses[name] <= 2:  # 变异 M2:更宽的错"],
        ["            if uses[name] == 0:"],
    ),
    "M3-去掉动态面排除": (
        ["    变异 M3:跳过逻辑没了 —— 动态发现目录会被当成普通源码"],
        ["    `skip` 里的文件整份跳过 —— 那就是动态发现面。"],
    ),
    "M4-恒过的过滤器": (
        ["    return []  # 变异 M4:恒过的过滤器"],
        ["    return out\n"],
    ),
    "M5-MainScreen不继承": (
        ['class MainScreen:  # 变异 M5:又没继承,loop 得再抄一份'],
        ["class MainScreen(Screen):"],
    ),
    # M6 是**纯插入**:`def _term_width` 在替换前后都在,所以 must_not 必须留空。
    # 头一版给它写了 must_not,自检当场报「仍在」—— 那个守卫每轮都在逮人。
    "M6-move_to复活": (
        ["def move_to(row: int, col: int) -> None:  # 变异 M6:死代码回来了"],
        [],
    ),
    "C1-拆掉调用一次不是死那条": (["    pass  # 变异 C1:整条判据没了"], []),
    "C2-拆掉零引用要列出来那条": (["    pass  # 变异 C2:整条判据没了"], []),
    "C3-拆掉动态面不算死那条": (["    pass  # 变异 C3:整条判据没了"], []),
    "C4-拆掉loop只有一份那条": (["    pass  # 变异 C4:整条判据没了"], []),
}

MUTANTS = [
    ("M1-阈值改回le1", lambda p: _apply(p, A_THRESHOLD, A_THRESHOLD_LE1), False, (CRIT,)),
    ("M2-阈值改宽", lambda p: _apply(p, A_THRESHOLD, A_THRESHOLD_LE2), False, (CRIT,)),
    ("M3-去掉动态面排除", lambda p: _apply(p, A_SKIP_DOC, A_SKIP_GONE), False, (CRIT,)),
    ("M4-恒过的过滤器", lambda p: _apply(p, A_RETURN, A_RETURN_NONE), False, (CRIT,)),
    ("M5-MainScreen不继承", lambda p: _apply(p, A_INHERIT, A_INHERIT_GONE), False, (TUI,)),
    ("M6-move_to复活", lambda p: _apply(p, A_TERM_WIDTH, A_MOVE_TO), False, (TUI,)),
]

COVERAGE_MUTANTS = [
    ("C1-拆掉调用一次不是死那条",
     lambda p: _replace_fn(p, C1_BODY, C1_BODY + "\n    pass  # 变异 C1:整条判据没了\n",
                           C1_END), True, (CRIT,)),
    ("C2-拆掉零引用要列出来那条",
     lambda p: _replace_fn(p, C2_BODY, C2_BODY + "\n    pass  # 变异 C2:整条判据没了\n",
                           C2_END), True, (CRIT,)),
    ("C3-拆掉动态面不算死那条",
     lambda p: _replace_fn(p, C3_BODY, C3_BODY + "\n    pass  # 变异 C3:整条判据没了\n",
                           C3_END), True, (CRIT,)),
    ("C4-拆掉loop只有一份那条",
     lambda p: _replace_fn(p, C4_BODY, C4_BODY + "\n    pass  # 变异 C4:整条判据没了\n",
                           C4_END), True, (CRIT,)),
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
                f"变异 {name!r} 的 must_not/must_have 写反了:{piece!r} 改前就存在")


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
        capture_output=True, text=True, cwd=REPO, timeout=600,
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
                # 是同一种浪费**(r96 栽过)。
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
        print(f"\n=== r101 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:26s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:300])
    print(f"\nr101 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.sandboxed(main))
