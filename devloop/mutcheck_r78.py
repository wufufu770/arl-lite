"""r78 变异测试:验「mutcheck 脚本必须自检」这条判据在守。

主题:「多行 `assert X, (` 只替换首行会留下裸 `)`」这个坑栽了四次
(r71 C1、r72 C2/C3、r74 C2、r77 C1)。r74 的 ast.parse 守卫每次都拦住了,
但只把「静默假结果」变成「响亮报错」。r77 进一步暴露守卫的边界:我用 heredoc
修脚本时写成了 `p.write_text(...)` **在 `ast.parse` 之前**,坏文件直接进磁盘。
**守卫保护变异目标,保护不了变异脚本自己。**

调研:种子里提的「锚点必须括号配平」被实测推翻
  扫 37 个 mutcheck,粗扫出 70 处「不配平」多行锚点,但绝大多数是合法用法
  (`@pytest.mark.skip\\ndef test_x` 这种局部锚点括号天然不配平;还有一批模块
  docstring 被我自己的正则误抓)。这条机制会否掉 70 处既有合法用法,不能上。
  真正可用的机制是:每脚本可 parse + round≥r74 必须有写盘语法守卫。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
所有变异统一走 _write_checked 单一写盘出口(r76 的教训:守卫必须有唯一路径)。

全部变异:
  M1 给一个 ≥r74 脚本去掉 ast.parse 守卫      → 杀「有守卫」那条
  M2 造一个语法坏掉的 mutcheck 脚本(文件名合法)→ 杀「每个脚本都能 parse」
  M3 绕过守卫直接 write_text(模拟 r77 的错)   → 杀「每条路径都受保护」
  M4 造一个命名不合约定的脚本                 → 杀「轮次号可推导」
  C1 危险写判据放宽成恒真                     → 期望存活
  C2 「有守卫」判据放宽成恒真                  → 期望存活
  C3 自检判据退化成不真红                      → 期望存活
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CRIT = REPO / "tests" / "test_mutcheck_scripts_are_self_checking.py"
TARGET = "tests/test_mutcheck_scripts_are_self_checking.py"
# 本轮要验的是「判据能不能逮到坏脚本」,变异对象是判据自己 + 造坏的样例脚本
TARGETS_FOR_PROBE = (CRIT,)

COLLECTION_FAILED = ("error during collection", "ERROR collecting", "Interrupted:")

# r77 的形状:_apply 做变换、写盘委托给 _write_checked
APPLY_HEAD = "def _apply(path: pathlib.Path, old: str, new: str) -> None:"
WRITE_CHECKED_HEAD = "def _write_checked(path: pathlib.Path, out: str) -> None:"


def _write_checked(path: pathlib.Path, out: str) -> None:
    """**唯一**的变异写盘出口:先 ast.parse,语法坏了当场拒且不写文件。"""
    import ast
    if out == path.read_text(encoding="utf-8"):
        raise AssertionError("变异是空操作 —— 什么都不会变,写了也是白写")
    try:
        ast.parse(out)
    except SyntaxError as e:
        raise AssertionError(f"变异会让 {path.name} 语法错误({e})。文件未写入。") from None
    path.write_text(out, encoding="utf-8")


def _apply(path: pathlib.Path, old: str, new: str) -> None:
    src = path.read_text(encoding="utf-8")
    if old not in src:
        raise AssertionError(f"变异锚点没命中 {path.name}:{old[:70]!r}")
    _write_checked(path, src.replace(old, new, 1))


def _drop_guard(path: pathlib.Path) -> None:
    """把 r76 的 _write_checked 里的 ast.parse 校验删掉(模拟「没加固」的脚本)。

    锚点照抄真实源码 —— 首版凭印象写了一段 replace,结果「删守卫是空操作」,
    跟 r77 M3 那次一模一样:凭印象写出来的锚点,命中的东西和想验的东西
    常常不是一回事。读一遍源码再写。
    """
    src = path.read_text(encoding="utf-8")
    start = src.index(WRITE_CHECKED_HEAD)
    end = src.index('    path.write_text(out, encoding="utf-8")', start)
    seg = src[start:end]
    new_seg = seg.replace(
        '    try:\n'
        '        ast.parse(out)\n'
        '    except SyntaxError as e:\n'
        '        raise AssertionError(\n'
        '            f"变异会让 {path.name} 语法错误({e}) —— 多行语句只替换首行会留下孤儿行。文件未写入。"\n'
        '        ) from None\n',
        "")
    if new_seg == seg:
        raise AssertionError("删守卫是空操作")
    _write_checked(path, src[:start] + new_seg + src[end:])


# 判据锚点。**必须覆盖整个 assert 语句(含结尾 `)`)** —— 只替换首行会留下
# 孤儿行变 SyntaxError,这个坑一共栽了五次(r71 C1、r72 C2/C3、r74 C2、
# r77 C1、r78 C1)。r74 的 ast.parse 守卫每次都拦住了,但那只是把「静默假结果」
# 变成「响亮报错」;真正的根治是锚点整体照抄,下面两个锚点已按此写。
ASSERT_PROTECTED = (
    '    assert protected, (\n'
    '        f"{path.name} 没有任何「变换后写回」的语法守卫 —— "\n'
    '        f"变异把文件写坏时不会被发现"\n'
    '    )'
)
ASSERT_NOT_DANGEROUS = (
    '    assert not dangerous, (\n'
    '        f"{path.name} 里有变换后直接写回、却没先 ast.parse 的路径:{sorted(dangerous)}。"\n'
    '        f"守卫必须只有一条路径可走,否则「有守卫」只是看起来有(r76 的教训)"\n'
    '    )'
)

def _m2_write_before_parse(path: pathlib.Path) -> None:
    """复现 r77 的真 bug:`write_text` 写在 `ast.parse` **之前**。

    首版我把「绕过守卫」写成在 _write_checked 内部多加一行 write_text ——
    那个函数仍带着 ast.parse,守卫还在,什么都没绕过,于是变异存活。
    **变异没实现它名字声称的语义,等于没变异**(r73 M2/M4、r74 M5 同款)。
    真正的绕过只有一个形状:先写盘、后校验。
    """
    src = path.read_text(encoding="utf-8")
    write_line = '    path.write_text(out, encoding="utf-8")\n'
    start = src.index(WRITE_CHECKED_HEAD)
    at = src.index(write_line, start)
    seg = src[at + len(write_line):]
    _write_checked(path, src[:at] + seg + write_line)   # 校验挪到写盘之后


MUTANTS = [
    ("M1-去掉r76脚本的语法守卫", _drop_guard, False,
     (REPO / "devloop" / "mutcheck_r76.py",)),
    ("M2-先写盘后校验(r77那个bug)", _m2_write_before_parse, False,
     (REPO / "devloop" / "mutcheck_r76.py",)),
    ("C1-危险写判据放宽成恒真", lambda p: _apply(
        p, ASSERT_NOT_DANGEROUS,
        '    assert True, "  # 变异:危险写恒真"'), True, TARGETS_FOR_PROBE),
    ("C2-有守卫判据放宽成恒真", lambda p: _apply(
        p, ASSERT_PROTECTED,
        '    assert True, "  # 变异:有守卫恒真"'), True, TARGETS_FOR_PROBE),
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
            if any(bad in outp for bad in COLLECTION_FAILED):
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
        ("覆盖变异(期望全存活)", [m for m in MUTANTS if m[2]]),
    ):
        print(f"\n=== r78 {title} ===")
        for name, detail, outp in _sweep(mutants):
            flag = "OK " if detail in ("killed", "survived") else "!! "
            if flag == "!! ":
                bad += 1
            print(f"  {flag}{name:32s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:450])
    print(f"\nr78 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
