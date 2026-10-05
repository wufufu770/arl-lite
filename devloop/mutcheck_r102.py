"""r102 变异测试:验「不可达 except 检测」在守 —— 重点是两个方向都不能错。

## 主题

3 处 `except PermissionError` 在同一个 `try` 下出现两次,第二个**永远
到不了**(Python 匹配到第一个就跳出)。执行级验证过,删掉不改变行为。

## 为什么这轮的变异重点是「方向」

写这条检测器时我把继承方向**搞反了两次**:

1. 第一次:`except ValueError` 后跟 `except Exception` 报不可达。
   **错** —— 那是正常写法,窄的在前宽的在后,两条都可达。真实代码里
   这种组合到处都是,一共报出 11 处假阳性(真缺陷只有 3 处)。
2. 第二次:方向对了但实现取成「后者在前者的祖先链里」,还是反的。

所以本轮的变异一半专门攻方向。**一个方向写反的检测器比没有检测器更
危险**:它会让人照着它去删正常代码 —— 而且那些代码本来是好的,删了之后
错误处理能力静默下降,不会当场报错。

## 变异清单

实现变异(期望全被杀):
  M1 方向反了(窄在前也报不可达)          → 集成层 7 处正常写法被误报 → 红
  M2 方向反了(只查完全相同的类型)        → 宽在前窄在后漏掉 → 红
  M3 `is_shadowed` 恒 False               → 主判据恒过 → 红
  M4 `shadowed_handlers` 恒返回 []        → 同上,换个位置恒过 → 红
  M5 裸 `except:` 不吃掉后面              → 漏报 → 红
  M6 间接子类不认(只看一层继承)          → HTTPError ⊂ URLError 漏掉 → 红

覆盖变异(期望全存活):
  C1 拆掉「同类型重复」那条
  C2 拆掉「窄在前宽在后不报」那条  ← M1 的唯一防线
  C3 拆掉「宽在前窄在后不报」那条  ← M2 的唯一防线
  C4 拆掉「执行级核对」那条

## C4 单独说一句

首版有个探针**我自己就写错了** —— 把异常**实例**当类传,
`raise exc("boom")` 于是抛的是 TypeError,结果「窄在前」那个反例跑出来
全是 `Exception`,看着像规则错了。

**一个坏探针会让人去改本来正确的规则。** 所以 C4 是真的**跑一遍
CPython** 看它到底进哪个分支,而不是拿我自己的规则去对照我自己。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
CLAIMS 只写字面量,不写名字引用。
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CRIT = REPO / "tests" / "test_no_shadowed_except_handler.py"

TARGET = ["tests/test_no_shadowed_except_handler.py"]

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


# ── M1/M2:方向 ──
A_IS_SHADOWED = ('    if earlier == BARE:\n'
                 '        return True\n'
                 '    return earlier == later or earlier in _ancestors(later)\n')
A_M1_REVERSED = ('    if earlier == BARE:  # 变异 M1:方向反了 —— 窄在前也报不可达\n'
                 '        return True\n'
                 '    return later == earlier or later in _ancestors(earlier)\n')
A_M2_EXACT_ONLY = ('    if earlier == BARE:\n'
                   '        return True\n'
                   '    return earlier == later  # 变异 M2:只认完全相同的类型\n')

# ── M3/M4:恒过 ──
A_M3_FALSE = ('    return False  # 变异 M3:恒判「没有遮蔽」\n')
A_M3_BODY = ('    if earlier == BARE:\n'
             '        return True\n'
             '    return earlier == later or earlier in _ancestors(later)\n')
A_M4_NONE = "    return []  # 变异 M4:恒过\n"
A_M4_TAIL = "            seen.append(t)\n    return found\n"

# ── M5:裸 except ──
A_BARE = ('    if earlier == BARE:\n'
          '        return True\n')
A_BARE_GONE = '    # 变异 M5:裸 except 不再吃掉后面\n'

# ── M6:只看一层继承 ──
A_ANCESTORS = ("def _ancestors(name: str) -> set[str]:\n"
               "    \"\"\"name 的祖先链。`name` 自己不在里面。\"\"\"\n"
               "    out: set[str] = set()\n"
               "    cur = name\n"
               "    while cur in BASES:\n"
               "        cur = BASES[cur]\n"
               "        out.add(cur)\n"
               "    return out\n")
A_ANCESTORS_ONE = ("def _ancestors(name: str) -> set[str]:\n"
                   "    out: set[str] = set()\n"
                   "    if name in BASES:  # 变异 M6:只看一层,间接子类认不出\n"
                   "        out.add(BASES[name])\n"
                   "    return out\n")

# ── 覆盖变异 ──
C1_BODY = "def test_the_same_exception_type_twice_is_shadowed():"
C1_END = "def test_narrow_before_broad_is_not_shadowed():"
C2_BODY = "def test_narrow_before_broad_is_not_shadowed():"
C2_END = "def test_a_narrower_type_after_a_broader_one_is_still_shadowed():"
C3_BODY = "def test_a_narrower_type_after_a_broader_one_is_still_shadowed():"
C3_END = "def test_a_bare_except_shadows_everything_after_it():"
C4_BODY = "def test_the_rules_are_checked_against_real_python_not_just_my_opinion():"
C4_END = "# ── 二、主判据:arl_lite 里不该再有不可达的 handler ──"

CLAIMS = {
    "M1-方向反了": (
        ["    if earlier == BARE:  # 变异 M1:方向反了 —— 窄在前也报不可达"],
        ["    if earlier == BARE:\n        return True\n"
         "    return earlier == later or earlier in _ancestors(later)\n"],
    ),
    "M2-只认相同类型": (
        ["    return earlier == later  # 变异 M2:只认完全相同的类型"],
        ["    return earlier == later or earlier in _ancestors(later)\n"],
    ),
    "M3-恒判没有遮蔽": (
        ["    return False  # 变异 M3:恒判「没有遮蔽」"],
        ["    return earlier == later or earlier in _ancestors(later)\n"],
    ),
    "M4-收集器恒返回空": (
        ["    return []  # 变异 M4:恒过"],
        ["            seen.append(t)\n    return found\n"],
    ),
    "M5-裸except不吃后面": (
        ["    # 变异 M5:裸 except 不再吃掉后面"],
        ["    if earlier == BARE:\n        return True\n"],
    ),
    "M6-只看一层继承": (
        ["    if name in BASES:  # 变异 M6:只看一层,间接子类认不出"],
        ["    while cur in BASES:\n        cur = BASES[cur]\n"
         "        out.add(cur)\n"],
    ),
    "C1-拆掉同类型重复那条": (["    pass  # 变异 C1:整条判据没了"], []),
    "C2-拆掉窄在前不报那条": (["    pass  # 变异 C2:整条判据没了"], []),
    "C3-拆掉宽在前要报那条": (["    pass  # 变异 C3:整条判据没了"], []),
    "C4-拆掉执行级核对那条": (["    pass  # 变异 C4:整条判据没了"], []),
}

MUTANTS = [
    ("M1-方向反了", lambda p: _apply(p, A_IS_SHADOWED, A_M1_REVERSED), False, (CRIT,)),
    ("M2-只认相同类型", lambda p: _apply(p, A_IS_SHADOWED, A_M2_EXACT_ONLY), False, (CRIT,)),
    ("M3-恒判没有遮蔽", lambda p: _apply(p, A_M3_BODY, A_M3_FALSE), False, (CRIT,)),
    ("M4-收集器恒返回空", lambda p: _apply(p, A_M4_TAIL, A_M4_NONE), False, (CRIT,)),
    ("M5-裸except不吃后面", lambda p: _apply(p, A_BARE, A_BARE_GONE), False, (CRIT,)),
    ("M6-只看一层继承", lambda p: _apply(p, A_ANCESTORS, A_ANCESTORS_ONE), False, (CRIT,)),
]

COVERAGE_MUTANTS = [
    ("C1-拆掉同类型重复那条",
     lambda p: _replace_fn(p, C1_BODY, C1_BODY + "\n    pass  # 变异 C1:整条判据没了\n",
                           C1_END), True, (CRIT,)),
    ("C2-拆掉窄在前不报那条",
     lambda p: _replace_fn(p, C2_BODY, C2_BODY + "\n    pass  # 变异 C2:整条判据没了\n",
                           C2_END), True, (CRIT,)),
    ("C3-拆掉宽在前要报那条",
     lambda p: _replace_fn(p, C3_BODY, C3_BODY + "\n    pass  # 变异 C3:整条判据没了\n",
                           C3_END), True, (CRIT,)),
    ("C4-拆掉执行级核对那条",
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
        print(f"\n=== r102 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:24s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:300])
    print(f"\nr102 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
