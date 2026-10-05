"""r104 变异测试:验「portscan 不再对没探到的端口静默」在守。

## 主题

`_probe_tcp` 把 `ECONNREFUSED`(端口真关了)、`socket.timeout`
(被防火墙丢包)、`socket.gaierror`(解析失败)全部 catch 成 `None`。
三种事实落进同一个结果,`scan()` 照样返回 `err=None, etype=None`。
**被防火墙挡在后面的服务会被报成「不存在」** —— 对安全工具这是实打实
的漏报,不是显示问题。

r104 在 `scan()` 层面对比 `len(results)` 和 `len(port_list)`,
把「有几个没判定」说出来(`etype="partial"`)。

## 为什么这轮一半变异攻「方向」

判据里最容易写坏的不是「该报的时候报」,是**「不该报的时候也报」**。
把 `if` 删掉让 `scan()` 每次都返回 partial,上面 6 条判据里仍然有 5 条
是绿的 —— 门禁会变绿,而正常扫描从此每次都挂一个假警告。
**和原来的静默一样有害,只是换了个方向。**

所以 M1(删掉整块)和 M6(无条件 partial)是一对镜像,必须都在。

## 变异清单

实现变异(期望全被杀,改 arl_lite/integrations/portscan.py):
  M1 删掉整个 partial 判定块     → 判据 1/2/4/5/6/7 红(r104 之前的退化态)
  M2 `n < tot` 改成 `n == 0`     → 一开一关时不报 → 判据 4 红
  M3 文案去掉「未判定」          → 判据 1/2 红
  M4 文案去掉 `{n}/{tot}` 计数   → 判据 2/4 红
  M5 文案去掉「不等于端口关闭」   → 判据 2 红
  M6 无条件返回 partial          → 全开时也报 → 判据 3 红(M1 的镜像)

覆盖变异(期望全存活,拆判据):
  C1 拆「一个都没探到不许报成功」(r104 的核心那条)
  C2 拆「全开时不刷屏」(M6 的唯一防线)
  C3 拆「不可路由地址算未判定」(信息损失那条)
  C4 拆「文案真能到 errors」(端到端接线那条)

## C2 单独说一句

M6(无条件 partial)只被 `test_all_ports_open_reports_no_problem` 杀掉。
拆掉它,C2 就存活 —— 也就是说 **M6 会从「被杀」变成「存活」而门禁不报
任何异常**,除非有人专门去看变异结果。这就是覆盖变异存在的理由:
「判据存在」不等于「判据承重」。

## 关于锚点

r103 栽过一次「变异锚点绑死在源码原文上」的坑:改源码(哪怕只改注释)
锚点就失效。这里 `A_PARTIAL` 是从当前源码逐行 repr 取的,改源码时
必须连锚点一起改。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
CLAIMS 只写字面量,不写名字引用。纯插入 must_not 留空、纯删除 must_have 留空。
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CRIT = REPO / "tests" / "test_portscan_states.py"
PS = REPO / "arl_lite" / "integrations" / "portscan.py"

TARGET = ["tests/test_portscan_states.py"]

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


# ── 锚点:整块,r103 起从源码逐行 repr 取,改源码必须连它一起改 ──
A_PARTIAL = (
    "        n, tot = len(results), len(port_list)\n"
    "        if n < tot:\n"
    '            return results, f"{tot - n}/{tot} 端口未判定(关闭/被过滤/超时无法区分),不等于端口关闭", "partial"\n'
    "        return results, None, None\n"
)

M1_GONE = "        # 变异 M1:partial 判定整块删掉 —— 回到 r104 之前的静默\n"
M2_ZERO = (
    "        n, tot = len(results), len(port_list)\n"
    "        if n == 0:  # 变异 M2:只有「一个都没探到」才报,一开一关就静默\n"
    '            return results, f"{tot - n}/{tot} 端口未判定(关闭/被过滤/超时无法区分),不等于端口关闭", "partial"\n'
    "        return results, None, None\n"
)
M3_NO_WORD = (
    "        n, tot = len(results), len(port_list)\n"
    "        if n < tot:\n"
    '            return results, f"{tot - n}/{tot} 端口没扫到(关闭/被过滤/超时无法区分),不等于端口关闭", "partial"\n'
    "        return results, None, None\n"
)
M4_NO_COUNT = (
    "        n, tot = len(results), len(port_list)\n"
    "        if n < tot:\n"
    '            return results, f"{tot - n} 个端口未判定(关闭/被过滤/超时无法区分),不等于端口关闭", "partial"\n'
    "        return results, None, None\n"
)
M5_NO_DISCLAIM = (
    "        n, tot = len(results), len(port_list)\n"
    "        if n < tot:\n"
    '            return results, f"{tot - n}/{tot} 端口未判定(关闭/被过滤/超时无法区分)", "partial"\n'
    "        return results, None, None\n"
)
M6_ALWAYS = (
    "        n, tot = len(results), len(port_list)\n"
    '        return results, f"{tot - n}/{tot} 端口未判定(关闭/被过滤/超时无法区分),不等于端口关闭", "partial"\n'
    "        return results, None, None\n"
)

# ── 覆盖变异:桩的 def 行整行照抄 ──
C1_BODY = "async def test_all_ports_closed_is_not_reported_as_a_clean_scan():"
C1_END = "async def test_the_message_says_how_many_and_does_not_claim_they_are_closed():"
C2_BODY = "async def test_all_ports_open_reports_no_problem():"
C2_END = "async def test_partial_scan_reports_exactly_the_missing_count():"
C3_BODY = "async def test_an_unroutable_address_is_counted_as_unknown_not_as_closed():"
C3_END = "async def test_a_filtered_port_is_not_reported_as_open():"
C4_BODY = "async def test_the_partial_note_reaches_module_errors():"
C4_END = "def test_parse_ports_unchanged():"

CLAIMS = {
    "M1-partial判定被删": (
        ["        # 变异 M1:partial 判定整块删掉 —— 回到 r104 之前的静默"],
        ["        n, tot = len(results), len(port_list)\n"
         "        if n < tot:\n"],
    ),
    "M2-条件写成n等于0": (
        ["        if n == 0:  # 变异 M2:只有「一个都没探到」才报,一开一关就静默"],
        ["        if n < tot:\n"],
    ),
    "M3-文案去掉未判定": (
        ['f"{tot - n}/{tot} 端口没扫到(关闭/被过滤/超时无法区分),不等于端口关闭"'],
        ['f"{tot - n}/{tot} 端口未判定(关闭/被过滤/超时无法区分),不等于端口关闭"'],
    ),
    "M4-文案去掉计数": (
        ['f"{tot - n} 个端口未判定(关闭/被过滤/超时无法区分),不等于端口关闭"'],
        ['f"{tot - n}/{tot} 端口未判定(关闭/被过滤/超时无法区分),不等于端口关闭"'],
    ),
    "M5-文案去掉免责声明": (
        ['f"{tot - n}/{tot} 端口未判定(关闭/被过滤/超时无法区分)"'],
        ['f"{tot - n}/{tot} 端口未判定(关闭/被过滤/超时无法区分),不等于端口关闭"'],
    ),
    "M6-无条件报partial": (
        [],  # 纯删除:只少了 `if n < tot:` 那一行(顺带把 return 顶格)
        ["        if n < tot:\n"],
    ),
    "C1-拆掉全关不许报成功": (["    pass  # 变异 C1:整条判据没了"], []),
    "C2-拆掉全开不刷屏": (["    pass  # 变异 C2:整条判据没了"], []),
    "C3-拆掉不可路由那条": (["    pass  # 变异 C3:整条判据没了"], []),
    "C4-拆掉端到端接线": (["    pass  # 变异 C4:整条判据没了"], []),
}

MUTANTS = [
    ("M1-partial判定被删", lambda p: _apply(p, A_PARTIAL, M1_GONE), False, (PS,)),
    ("M2-条件写成n等于0", lambda p: _apply(p, A_PARTIAL, M2_ZERO), False, (PS,)),
    ("M3-文案去掉未判定", lambda p: _apply(p, A_PARTIAL, M3_NO_WORD), False, (PS,)),
    ("M4-文案去掉计数", lambda p: _apply(p, A_PARTIAL, M4_NO_COUNT), False, (PS,)),
    ("M5-文案去掉免责声明", lambda p: _apply(p, A_PARTIAL, M5_NO_DISCLAIM), False, (PS,)),
    ("M6-无条件报partial", lambda p: _apply(p, A_PARTIAL, M6_ALWAYS), False, (PS,)),
]

COVERAGE_MUTANTS = [
    ("C1-拆掉全关不许报成功",
     lambda p: _replace_fn(p, C1_BODY, C1_BODY + "\n    pass  # 变异 C1:整条判据没了\n",
                           C1_END), True, (CRIT,)),
    ("C2-拆掉全开不刷屏",
     lambda p: _replace_fn(p, C2_BODY, C2_BODY + "\n    pass  # 变异 C2:整条判据没了\n",
                           C2_END), True, (CRIT,)),
    ("C3-拆掉不可路由那条",
     lambda p: _replace_fn(p, C3_BODY, C3_BODY + "\n    pass  # 变异 C3:整条判据没了\n",
                           C3_END), True, (CRIT,)),
    ("C4-拆掉端到端接线",
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
        print(f"\n=== r104 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:26s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:300])
    print(f"\nr104 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.sandboxed(main))
