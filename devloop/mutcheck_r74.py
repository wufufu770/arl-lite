"""r74 变异测试:验「run 的 target 边界必须完整」这条判据在守。

主题:`cmd_run` 本来就有一段「防御:target 边界」(空 / 超长 / null byte),
同段注释说明用意是「别让一个手误白跑一次完整扫描」。但它漏了一种:target 含
**绝不可能出现在主机名/IP/URL 里的字符**时(空格、`<>`、引号、反引号)照跑不误,
一路到网络层才失败。

边界刻意取可证明的最小集 —— 反例全是真实用例:IPv6 的冒号、内网单标签
localhost / intranet-host、内网 DNS 的下划线 my_service、通配 *.example.com。
按「像不像域名」写正则这些一个都拦得住,所以只拦可证明的。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明(不靠命名约定)。
判据锚点覆盖**整个 assert 语句**(含结尾 `)`)—— 只替换首行会留下裸 `)`
变 SyntaxError,r71 C1 与 r72 C2/C3 各栽过一次。

自踩:首版 M2 锚在 `bad = sorted(...)` 那一行,但真正决定行为的是后面的
`return 2` 分支;而且 M3 把字符集缩到只剩空格的写法,首版判据因为
`_NEVER_IN_TARGET` 只有一个字符而漏测。变异必须落在**真正改变输出的语句**上
(r73 M2 栽过同一个跟头)。

全部变异:
  M1 撤掉整条检查(还原 r74 之前)  → 杀 9 条逐字符
  M2 检查在但不 return 2(继续跑)  → 杀 9 条逐字符
  M3 字符集缩到只剩空格            → 只杀 8 条(非空格那些)
  M4 拒绝时不点名具体字符          → 杀 9 条里「点名字符」那个断言
  M5 字符集过宽(塞进 `:_*/?#@` 等) → 期望被杀:过宽就是真 bug,判据本来就该杀它
                                     (首版我把它错标成「期望存活」的覆盖变异,
                                      5 个合法 target 被杀说明判据方向是对的,
                                      错的是我的期望)
  C1 合法形态判据放宽成不看 module 报错 → 期望存活
  C2 形态示例判据退化成恒真         → 期望存活
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CLI = REPO / "arl_lite" / "cli.py"
CRIT = REPO / "tests" / "test_run_target_boundary_is_complete.py"
TARGET = "tests/test_run_target_boundary_is_complete.py"

BAD_END = (
    "SyntaxError", "IndentationError", "TabError",
    "ERROR collecting", "error during collection", "Interrupted:",
)

NEVER_CONST = '_NEVER_IN_TARGET = frozenset(" \\t\\r\\n<>\\"\'`")'
GUARD = (
    "    bad = sorted({c for c in args.target if c in _NEVER_IN_TARGET})\n"
    "    if bad:"
)

CRIT_ANCHORS = {
    "names_char": '    assert repr(target[1]) in p.stderr, (\n'
                  '        f"{label}: 报错没点名具体是哪个字符:{p.stderr[-300:]}"\n'
                  "    )",
    "shape": '    for shape in ("example.com", "192.0.2.1"):\n'
             '        assert shape in tail, (\n'
             '            f"报错里没有给出合法 target 的形态示例 {shape!r}:{tail[-300:]}"\n'
             "        )",
}


def _apply(path: pathlib.Path, old: str, new: str) -> None:
    src = path.read_text(encoding="utf-8")
    if old not in src:
        raise AssertionError(f"变异锚点没命中 {path.name}:{old[:70]!r}")
    out = src.replace(old, new, 1)
    if out == src:
        raise AssertionError("变异是空操作 —— 什么都不会变,写了也是白写")
    # 写盘前先验语法:替换多行 assert 的首行会留下裸 `)` 变 SyntaxError,
    # 这个坑我栽过三次(r71 C1、r72 C2/C3、r74 C2),两次写进注释都没挡住。
    # 靠记性防不住,改用机制:语法坏了当场拒,连文件都不写。
    import ast
    try:
        ast.parse(out)
    except SyntaxError as e:
        raise AssertionError(
            f"变异会让 {path.name} 语法错误({e}) —— 多行语句只替换首行会留下孤儿行。"
            f"锚点必须覆盖整个语句(含结尾的 `)`)。文件未写入。"
        ) from None
    path.write_text(out, encoding="utf-8")


def _drop_check(path: pathlib.Path) -> None:
    """撤掉整条检查(还原 r74 之前)。"""
    src = path.read_text(encoding="utf-8")
    start = src.index("    bad = sorted({c for c in args.target if c in _NEVER_IN_TARGET})")
    end = src.index('    storage = Storage(workspace=args.workspace)', start)
    path.write_text(src[:start] + src[end:], encoding="utf-8")


# 每个变异该改哪些文件 —— 显式写进元组。
MUTANTS = [
    ("M1-撤掉整条检查", _drop_check, False, (CLI,)),
    ("M2-检查在但不return2", lambda p: _apply(
        p, "        return 2\n\n    storage = Storage(workspace=args.workspace)",
        "        pass  # 变异:发现了但不拒绝,继续跑\n\n"
        "    storage = Storage(workspace=args.workspace)"), False, (CLI,)),
    ("M3-字符集缩到只剩空格", lambda p: _apply(
        p, NEVER_CONST, '_NEVER_IN_TARGET = frozenset(" ")  # 变异:缩小集合'), False, (CLI,)),
    ("M4-拒绝时不点名具体字符", lambda p: _apply(
        p, '        shown = " ".join(repr(c) for c in bad)',
        '        shown = "?"  # 变异:不点名是哪个字符'), False, (CLI,)),
    ("M5-字符集过宽(误伤合法target)", lambda p: _apply(
        p, NEVER_CONST, '_NEVER_IN_TARGET = frozenset(" \\t\\r\\n<>\\"\'`|:_*/?#@!$%^&=+,")  # 变异:过宽'), False, (CLI,)),
]

# ---- 覆盖变异:改坏判据自己,期望**存活**(真实现已经是对的)
COVERAGE_MUTANTS = [
    ("C1-合法形态判据放宽", lambda p: _apply(
        p, '    assert "unknown module" in p.stderr, (\n'
           '        f"合法 target {target!r} 应当放行到 module 检查,实际:{p.stderr[-300:]}"\n'
           "    )",
        '    assert True, "  # 变异:不看是否放行到 module 检查"'), True, (CRIT,)),
    ("C2-形态示例判据退化成恒真", lambda p: _apply(
        p, CRIT_ANCHORS["shape"],
        '    for shape in ("example.com", "192.0.2.1"):\n'
        '        assert True, "  # 变异:形态示例恒真"\n'
        "    return"), True, (CRIT,)),
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
        print(f"\n=== r74 {title} ===")
        for name, detail, outp in _sweep(mutants):
            flag = "OK " if detail in ("killed", "survived") else "!! "
            if flag == "!! ":
                bad += 1
            print(f"  {flag}{name:30s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:500])
    print(f"\nr74 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
