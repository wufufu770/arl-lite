"""r96 变异测试:验「MCP 截断必须说出来」和「hit_identity 是单一来源」在守。

## 主题

`cli.py` 的 `_limit_notice_text` 早就把规矩定死了,r56/r58/r64/r66 一路
建到了 `query`/`search`/`monitor changes`/`correlate`/`export` 上。
**MCP 完全在射程之外**:四个工具全都 `[:limit]` 然后什么都不说。

r96 实测(12 行数据、limit=5):

    query_assets    → 裸数组 5 条,没有任何字段说明还有 7 条
    search_findings → 裸数组 4 条(真实 12),同样什么都不说
    get_risk        → {summary, top},top 是前 N 还是全部?分不清
    run_correlate   → returned 有,但**一共命中几条**没有;
                      new_correlations_saved 是**落库数**,极易被读成「就这么多」

更要紧的是第二条:修截断时造出真实命中,立刻
`AttributeError: 'CorrelationHit' object has no attribute 'target_type'`。
**这个工具从来没成功执行过。** 正确的取法一直内联在 `save_correlations` 里,
MCP 抄了一份,抄错了 —— 仓库决策 #9「两处手抄迟早漂」的已实现实例。

## 变异清单

实现变异(期望全被杀):
  M1 信封里不报 truncated                     → 四个工具的判据红
  M2 total 写成 len(rows)(等于 returned,假装没截断) → 判据红
  M3 total 恒为 0                            → 判据红
  M4 truncated 恒 False                      → 判据红
  M5 total 未知时 truncated 写 False          → 「未知说成没截断」判据红
  M6 MCP 又自己抄一份 target 提取(h.target_type) → run_correlate 崩 + 单一来源判据红
  M7 min_risk 过滤**前**的数当 total          → min_risk 判据红
  M8 save_correlations 又内联一份             → 单一来源判据红

覆盖变异(期望全存活):
  C1 拆掉「四个工具键名一致」那条
  C2 拆掉「没截断也说 False」那条

## M6 是本轮最重要的一条

它同时验证两件事:那个 AttributeError 真的被修掉了(否则 run_correlate
在有命中时直接崩,判据根本跑不到断言),以及单一来源那条判据不是摆设。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
CLAIMS 只写字面量 —— **这是第六次**(r86/r88/r89/r93/r94 各一次,r96 又一次)。
r96 头一版给 M7/M8 的 must_not 写了 `[MATCH_LINE]` / `[SAVE_CALL]`,自检守卫
的 `ast.literal_eval` 当场抛 ValueError,六条判据一起红。上一轮的提交信息里
刚写下「第六次,别再栽」,转头就栽了 —— **把它写进文档不如让守卫每次都逮住。**
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
MCP = REPO / "arl_lite" / "mcp" / "server.py"
ENGINE = REPO / "arl_lite" / "core" / "correlation_engine.py"
CRIT = REPO / "tests" / "test_mcp_truncation_is_always_reported.py"

TARGET = ["tests/test_mcp_truncation_is_always_reported.py"]

_RUN_SUMMARY_RE = re.compile(r"^(?:=+\s*)?(.*\bin [\d.]+s.*)$", re.M)
_ERROR_IN_SUMMARY_RE = re.compile(r"\d+\s+errors?\b")


def _run_itself_broke(outp: str) -> bool:
    lines = _RUN_SUMMARY_RE.findall(outp)
    if not lines:
        return True
    tail = lines[-1]
    if "no tests ran" in tail:
        return True
    return _ERROR_IN_SUMMARY_RE.search(tail) is not None


# ── 信封 ──

ENVELOPE = ('    return {\n'
            '        key: rows,\n'
            '        "returned": len(rows),\n'
            '        "total": total,\n'
            '        "truncated": None if total is None else total > len(rows),\n'
            '    }\n')
ENVELOPE_NO_TRUNC = ('    return {\n'
                     '        key: rows,\n'
                     '        "returned": len(rows),\n'
                     '        "total": total,\n'
                     '    }\n')
ENVELOPE_TOTAL_ROWS = ('    return {\n'
                       '        key: rows,\n'
                       '        "returned": len(rows),\n'
                       '        "total": len(rows),\n'
                       '        "truncated": None if total is None else total > len(rows),\n'
                       '    }\n')
ENVELOPE_TOTAL_ZERO = ('    return {\n'
                       '        key: rows,\n'
                       '        "returned": len(rows),\n'
                       '        "total": 0,\n'
                       '        "truncated": None if total is None else total > len(rows),\n'
                       '    }\n')
ENVELOPE_TRUNC_FALSE = ('    return {\n'
                        '        key: rows,\n'
                        '        "returned": len(rows),\n'
                        '        "total": total,\n'
                        '        "truncated": False,\n'
                        '    }\n')
ENVELOPE_TRUNC_KNOWN_FALSE = ('    return {\n'
                              '        key: rows,\n'
                              '        "returned": len(rows),\n'
                              '        "total": total,\n'
                              '        "truncated": False,  # 变异 M5:未知也说没截断\n'
                              '    }\n')

# ── 三个 handler 里的具体写法 ──

RISK_TOTAL = '            total = sum(summary.get("by_level", {}).values())\n'
RISK_TOTAL_WRONG = '            total = len(top)  # 变异:拿返回条数当总数\n'

MATCH_LINE = '            matched = [h for h in hits if h.risk >= min_risk]\n'
MATCH_ALL = ('            matched = list(hits)  # 变异 M7:不过滤 min_risk 就当总数\n')

HIT_IDENTITY = '                target, target_type = hit_identity(h)\n'
HIT_IDENTITY_FORKED = ('                target, target_type = h.target, h.target_type\n'
                       '                # 变异 M6:又自己抄一份(而且是错的)\n')

SAVE_CALL = '        target, target_type = hit_identity(hit)\n'
SAVE_CALL_GONE = ('        target = hit.target  # 变异 M8:又内联一份\n'
                  '        target_type = "other"\n')

# ── 覆盖变异 ──

C1_BODY = 'def test_all_four_tools_share_the_same_envelope_keys(ws):'
C1_STUB = ('def test_all_four_tools_share_the_same_envelope_keys(ws):\n'
           '    pass  # 变异 C1:整条判据没了\n')
# 结束标记必须是**下一条**判据的头。头一版给 C1 写的是
# `def test_run_correlate_reports`,而它在文件里排在 C1 **前面** ——
# 于是 `src.index(end, src.index(start))` 抛 ValueError,
# 整轮变异跑到这里直接崩掉,前 8 条的结果全被吞了。
# **harness 里一个没接住的异常,和一条变异没被杀,是同一种浪费。**
C1_END = 'def test_not_truncated_is_still_reported_as_false(ws):'
C2_BODY = 'def test_not_truncated_is_still_reported_as_false(ws):'
C2_STUB = ('def test_not_truncated_is_still_reported_as_false(ws):\n'
           '    pass  # 变异 C2:整条判据没了\n')
C2_END = 'def test_unknown_total_is_reported_as_null_not_zero():'

CLAIMS = {
    "M1-信封不报truncated": (
        ['    return {\n        key: rows,\n        "returned": len(rows),\n'
         '        "total": total,\n    }\n'],
        ['"truncated": None if total is None else total > len(rows),'],
    ),
    "M2-total写成len(rows)": (
        ['"total": len(rows),'],
        ['"total": total,'],
    ),
    "M3-total恒为0": (
        ['"total": 0,'],
        ['"total": total,'],
    ),
    "M4-truncated恒False": (
        ['"truncated": False,\n    }\n'],
        ['"truncated": None if total is None else total > len(rows),'],
    ),
    "M5-未知时truncated写False": (
        ['# 变异 M5:未知也说没截断'],
        ['"truncated": None if total is None else total > len(rows),'],
    ),
    "M6-MCP又自己抄一份target提取": (
        ['                target, target_type = h.target, h.target_type\n'],
        ['                target, target_type = hit_identity(h)\n'],
    ),
    "M7-min_risk过滤前的数当total": (
        ['            matched = list(hits)'],
        ['            matched = [h for h in hits if h.risk >= min_risk]\n'],
    ),
    "M8-save_correlations又内联一份": (
        ['        target = hit.target  # 变异 M8:又内联一份'],
        ['        target, target_type = hit_identity(hit)\n'],
    ),
    "C1-拆掉四工具键名一致那条判据": (
        ["    pass  # 变异 C1:整条判据没了"],
        ['    missing = {k: v for k, v in missing.items() if v}\n'],
    ),
    "C2-拆掉没截断也说False那条判据": (
        ["    pass  # 变异 C2:整条判据没了"],
        ['    assert r["truncated"] is False, (\n'],
    ),
}

MUTANTS = [
    ("M1-信封不报truncated",
     lambda p: _apply(p, ENVELOPE, ENVELOPE_NO_TRUNC), False, (MCP,)),
    ("M2-total写成len(rows)",
     lambda p: _apply(p, ENVELOPE, ENVELOPE_TOTAL_ROWS), False, (MCP,)),
    ("M3-total恒为0",
     lambda p: _apply(p, ENVELOPE, ENVELOPE_TOTAL_ZERO), False, (MCP,)),
    ("M4-truncated恒False",
     lambda p: _apply(p, ENVELOPE, ENVELOPE_TRUNC_FALSE), False, (MCP,)),
    ("M5-未知时truncated写False",
     lambda p: _apply(p, ENVELOPE, ENVELOPE_TRUNC_KNOWN_FALSE), False, (MCP,)),
    ("M6-MCP又自己抄一份target提取",
     lambda p: _apply(p, HIT_IDENTITY, HIT_IDENTITY_FORKED), False, (MCP,)),
    ("M7-min_risk过滤前的数当total",
     lambda p: _apply(p, MATCH_LINE, MATCH_ALL), False, (MCP,)),
    ("M8-save_correlations又内联一份",
     lambda p: _apply(p, SAVE_CALL, SAVE_CALL_GONE), False, (ENGINE,)),
]

COVERAGE_MUTANTS = [
    ("C1-拆掉四工具键名一致那条判据",
     lambda p: _replace_fn(p, C1_BODY, C1_STUB, C1_END), True, (CRIT,)),
    ("C2-拆掉没截断也说False那条判据",
     lambda p: _replace_fn(p, C2_BODY, C2_STUB, C2_END), True, (CRIT,)),
]


def _check_claim_points_at_one_place(name: str, original: str) -> None:
    if name not in CLAIMS:
        raise AssertionError(f"变异 {name!r} 没有自检声明")
    for piece in CLAIMS[name][1]:
        n = original.count(piece)
        if n != 1:
            raise AssertionError(
                f"变异 {name!r} 的声明串 {piece!r} 在改前出现 {n} 次 —— 指不准位置")


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
    if old not in src:
        raise AssertionError(f"变异锚点没命中 {path.name}:{old[:70]!r}")
    _write_checked(path, src.replace(old, new, 1))


def _replace_fn(path: pathlib.Path, start: str, stub: str, end: str) -> None:
    """把 `start` 到下一个 `end` 标记之间的整段换成一条 pass 桩

    `def` 行**原样照抄** —— 削掉 `()` 和冒号会产出 `def test_x`,那是语法
    错误(r94 栽过)。r95 的 C1 换成 end=None 截到文件末尾,那是因为它在
    文件最后一条;这里两条都不是,所以必须给下一个标记。
    """
    src = path.read_text(encoding="utf-8")
    if start not in src:
        raise AssertionError(f"变异锚点没命中 {path.name}:{start!r}")
    if end not in src:
        raise AssertionError(f"变异结束标记没命中 {path.name}:{end!r}")
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
                # r96 头一版栽在这:`_replace_fn` 里一个没接住的 ValueError
                # 直接把整轮变异掀了,前 8 条的结果全被吞掉 —— 看着像
                # 「harness 坏了」,其实只是我自己把结束标记写反了。
                # **harness 里一个没接住的异常,和一条变异没被杀,
                # 是同一种浪费**:都让这一轮白跑。
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
        print(f"\n=== r96 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:36s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:300])
    print(f"\nr96 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
