"""r98 变异测试:验「源码不许有 U+FFFD」这条守卫自己不是摆设。

## 主题

r97 收尾扫全仓,查出 **8 个文件 22 个 U+FFFD**(多字节汉字在写盘那一刻
被写坏)。它们全在注释/docstring 里,所以**七道门禁一道都没报**。

r98 清掉这 22 处,并加一条仓库级守卫让下次损坏当场红。守卫本身要
能被验:一条从不报警的守卫等于没写,还会占着一个「我们在防这个」的位置。

## 变异清单

实现变异(期望全被杀):
  M1 `_find_replacement_chars` 恒返回 []      → 正控制「扫描器会报」红
  M2 只报第一个就 break(用 count 语义)       → 「连续三个要报三个」红
  M3 `_python_sources` 恒返回 []             → 「扫描范围不能是空的」红
  M4 REPLACEMENT 指到 0xFFFE(错码位)          → 同 M1
  M5 SKIP_PARTS 把仓库根也算进去              → 主判据扫不到任何文件

覆盖变异(期望全存活):
  C1 拆掉「扫描器会报」那条正控制
  C2 拆掉「端到端跑完整收集流程」那条
  C3 拆掉「连续几个都要报」那条

## M5 是从 r97 的教训直接搬过来的

r97 的 C1 把 `@pytest.mark.parametrize` 装饰器一起吃掉了,而那个报错在
harness 里长得和「变异被杀」一模一样 —— 一条**跑坏**的覆盖变异差点被
当成有效结果记进去。所以本 harness 的 `_run_itself_broke` 沿用 r97 的
修法:**输出里有 AssertionError = 判据在说话,算被杀**;夹具找不到、
模块导不进、收集期炸才是判据根本没跑起来。

## 本轮首版自己翻的一次车

判据首版在测试样本里**直接写了 U+FFFD 字符**,一跑就把自己列成 10 处
offender。修法是全部改成 `chr(0xFFFD)` 拼出来 —— **一条扫乱码的守卫,
自己必须干净**,不然它每次跑都在报自己,而人只会觉得「这守卫一直吵」。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
CLAIMS 只写字面量,不写名字引用。
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CRIT = REPO / "tests" / "test_source_files_have_no_replacement_chars.py"

TARGET = ["tests/test_source_files_have_no_replacement_chars.py"]

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


# ── 实现变异 ──
# 锚点都是整条语句(带结尾括号),r81 立的约定:半句锚点会让替换后括号散掉。
A_CODEPOINT = "REPLACEMENT = chr(0xFFFD)\n"
A_CODEPOINT_M = "REPLACEMENT = chr(0xFFFE)  # 变异 M4:错码位\n"

A_LOOP = ("    hits = []\n"
          "    for lineno, line in enumerate(text.splitlines(), 1):\n"
          "        start = 0\n"
          "        while True:\n"
          "            col = line.find(REPLACEMENT, start)\n"
          "            if col < 0:\n"
          "                break\n"
          "            hits.append((lineno, col + 1))\n"
          "            start = col + 1\n"
          "    return hits\n")
A_LOOP_FIRST_ONLY = ("    hits = []\n"
                     "    for lineno, line in enumerate(text.splitlines(), 1):\n"
                     "        col = line.find(REPLACEMENT)  # 变异 M2:只报第一个\n"
                     "        if col >= 0:\n"
                     "            hits.append((lineno, col + 1))\n"
                     "    return hits\n")
A_LOOP_NONE = ("    hits = []\n"
               "    for lineno, line in enumerate(text.splitlines(), 1):\n"
               "        start = 0\n"
               "        while True:\n"
               "            col = line.find(REPLACEMENT, start)\n"
               "            if col < 0:\n"
               "                break\n"
               "            hits.append((lineno, col + 1))\n"
               "            start = col + 1\n"
               "    return []  # 变异 M1:恒报「干净」\n")

# r100 把收集器从 `_python_sources`(只扫 `*.py`)改成 `_text_sources`
# (按 SCANNED_SUFFIXES 扫代码**和**文档),锚点随之重写。
# **变异锚点绑死在源码原文上** —— 改源码就得改锚点,这不是额外负担,
# 锚点本来就是源码的一部分(r99 刚因为「顺手删了个行尾注释」栽过一次,
# 被门禁的 test_mutcheck_claims_match_their_names 逮到)。
A_SOURCES = ('def _text_sources(root: pathlib.Path) -> list[pathlib.Path]:\n'
             '    """root 下所有该扫的文本文件(代码 **和** 文档)\n'
             "\n"
             "    r100 从 `_python_sources`(只扫 `*.py`)改成这个。名字一起改了 ——\n"
             "    留着旧名会让下一个人以为它只管 Python。\n"
             '    """\n'
             '    return sorted(p for p in root.rglob("*")\n'
             "                  if p.suffix in SCANNED_SUFFIXES\n"
             "                  and not SKIP_PARTS & set(p.parts))\n")
A_SOURCES_NONE = ("def _text_sources(root: pathlib.Path) -> list[pathlib.Path]:\n"
                  "    return []  # 变异 M3:恒扫不到文件\n")

# ── r100 新增:文档必须真的在扫描范围内 ──
A_SUFFIXES = ('SCANNED_SUFFIXES = (".py", ".md", ".json", ".toml", ".txt",'
              ' ".yml", ".yaml")\n')
A_SUFFIXES_M = ('SCANNED_SUFFIXES = (".py", ".json", ".toml", ".txt",'
                ' ".yml", ".yaml")  # 变异 M6:文档被踢出扫描范围\n')

A_SKIP = 'SKIP_PARTS = {".git", "__pycache__", ".pytest_cache", "node_modules"}\n'
A_SKIP_M = ('SKIP_PARTS = {".git", "__pycache__", ".pytest_cache", "node_modules",\n'
            '              "arl-lite"}  # 变异 M5:把整个包跳掉\n')

# ── 覆盖变异 ──
C1_BODY = "def test_the_scanner_reports_what_it_was_fed():"
C1_END = "def test_the_scanner_handles_a_replacement_char_at_every_position():"
C2_BODY = "def test_the_scanner_handles_a_replacement_char_at_every_position():"
C2_END = "def test_no_source_file_carries_a_replacement_char():"
C3_BODY = "def test_the_constant_really_is_the_replacement_character():"
C3_END = "def test_the_scanner_handles_a_replacement_char_at_every_position():"
C4_BODY = "def test_the_guard_would_actually_fail_on_a_corrupted_file(tmp_path):"
# 这行原来是 `C3_END = ""`(给「文件最后一条」的 C3 用的)。
# 插入新的 C3 时忘了删它,于是它把新 C3 的 END 覆盖成空串,
# 自检当场报「结束标记没唯一命中: ''」。

CLAIMS = {
    "M1-扫描器恒返回空": (
        ["    return []  # 变异 M1:恒报「干净」"],
        ["    return hits\n"],
    ),
    "M2-只报第一个就停": (
        ["        col = line.find(REPLACEMENT)  # 变异 M2:只报第一个"],
        ["        start = 0\n        while True:\n"],
    ),
    "M3-扫不到文件": (
        ["    return []  # 变异 M3:恒扫不到文件"],
        ["    return sorted(p for p in root.rglob(\"*\")\n"],
    ),
    "M6-文档被踢出扫描范围": (
        ['SCANNED_SUFFIXES = (".py", ".json", ".toml", ".txt", ".yml",'
         ' ".yaml")  # 变异 M6:文档被踢出扫描范围'],
        ['SCANNED_SUFFIXES = (".py", ".md", ".json", ".toml", ".txt",'
         ' ".yml", ".yaml")'],
    ),
    "M4-码位指错": (
        ["REPLACEMENT = chr(0xFFFE)  # 变异 M4:错码位"],
        ["REPLACEMENT = chr(0xFFFD)\n"],
    ),
    "M5-把整个包跳掉": (
        ['              "arl-lite"}  # 变异 M5:把整个包跳掉'],
        ['SKIP_PARTS = {".git", "__pycache__", ".pytest_cache", "node_modules"}\n'],
    ),
    "C1-拆掉扫描器会报那条": (["    pass  # 变异 C1:整条判据没了"], []),
    "C2-拆掉连续几个都要报那条": (["    pass  # 变异 C2:整条判据没了"], []),
    "C3-拆掉码位独立核对那条": (["    pass  # 变异 C3:整条判据没了"], []),
    "C4-拆掉端到端那条": (["    pass  # 变异 C4:整条判据没了"], []),
}

MUTANTS = [
    ("M1-扫描器恒返回空", lambda p: _apply(p, A_LOOP, A_LOOP_NONE), False, (CRIT,)),
    ("M2-只报第一个就停", lambda p: _apply(p, A_LOOP, A_LOOP_FIRST_ONLY), False, (CRIT,)),
    ("M3-扫不到文件", lambda p: _apply(p, A_SOURCES, A_SOURCES_NONE), False, (CRIT,)),
    ("M4-码位指错", lambda p: _apply(p, A_CODEPOINT, A_CODEPOINT_M), False, (CRIT,)),
    ("M5-把整个包跳掉", lambda p: _apply(p, A_SKIP, A_SKIP_M), False, (CRIT,)),
    ("M6-文档被踢出扫描范围", lambda p: _apply(p, A_SUFFIXES, A_SUFFIXES_M),
     False, (CRIT,)),
]

COVERAGE_MUTANTS = [
    ("C1-拆掉扫描器会报那条",
     lambda p: _replace_fn(p, C1_BODY, C1_BODY + "\n    pass  # 变异 C1:整条判据没了\n",
                           C1_END), True, (CRIT,)),
    ("C2-拆掉连续几个都要报那条",
     lambda p: _replace_fn(p, C2_BODY, C2_BODY + "\n    pass  # 变异 C2:整条判据没了\n",
                           C2_END), True, (CRIT,)),
    ("C3-拆掉码位独立核对那条",
     lambda p: _replace_fn(p, C3_BODY, C3_BODY + "\n    pass  # 变异 C3:整条判据没了\n",
                           C3_END), True, (CRIT,)),
    # C4 是文件最后一条判据,所以截到文件末尾(end=None)。
    ("C4-拆掉端到端那条",
     lambda p: _replace_fn(p, C4_BODY, C4_BODY + "\n    pass  # 变异 C4:整条判据没了\n",
                           None), True, (CRIT,)),
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


def _replace_fn(path: pathlib.Path, start: str, stub: str, end: str | None) -> None:
    """把 `start` 到下一个 `end` 标记之间的整段换成一条 pass 桩

    `def` 行**原样照抄** —— 削掉 `()` 和冒号是语法错误(r94 栽过)。
    `end=None` 表示这条在文件最后,截到末尾。
    """
    src = path.read_text(encoding="utf-8")
    if src.count(start) != 1:
        raise AssertionError(f"变异锚点没唯一命中 {path.name}:{start!r}")
    i = src.index(start)
    if end is None:
        _write_checked(path, src[:i] + stub)
        return
    if src.count(end) != 1:
        raise AssertionError(f"变异结束标记没唯一命中 {path.name}:{end!r}")
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
        print(f"\n=== r98 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:28s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:300])
    print(f"\nr98 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
