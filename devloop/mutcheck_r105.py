"""r105 变异测试:验「mark 必须注册 + 公网测试必须显式标记」在守。

## 主题

两件都是**每次都在发生、但没人拦**的事:

1. `tests/test_bench_wont_clobber_baseline.py:172` 用了 `@pytest.mark.slow`
   而 `pyproject.toml` 从没注册过 —— 于是每次全量跑的 summary 都带一条
   `PytestUnknownMarkWarning`。它不在任何一条判据里,连「为什么留着它」
   都没人记得。这和 pyproject 里记的 `asyncio_mode` 是同一个病:
   **一条每次都出现的告警等于没有告警**。

2. `test_httpx_probe_integration` 探测 example.com 并断言
   `title == "Example Domain"` —— 把公网页面的内容当成契约。

## 变异清单

实现/配置变异(期望全被杀):
  M1 删掉 markers 里 network 那行     → 判据 1/2/3/4 红(告警回来)
  M2 删掉 markers 里 slow 那行        → 判据 1/2 红
  M3 整个 markers 块删掉              → 同上
  M4 去掉 phase2 的 network 标记       → 判据 4/5 红
  M5 去掉 phase3 的 network 标记       → 判据 4/5 红
  M6 httpx 测试改回打公网              → 判据 6 红

覆盖变异(期望全存活,拆判据):
  C1 拆掉「所有用到的 mark 都得注册」
  C2 拆掉「真跑一次收集,输出里没有告警」  ← M1/M2/M3 的行为级防线
  C3 拆掉「公网测试必须点名标记」
  C4 拆掉「network 标记的使用集合不许变」  ← 防「整体删光标记」时 C3 恒绿

## C4 为什么必须有

C3 是「已知的三条都必须有 network 标记」。如果有人把**所有**
`@pytest.mark.network` 删光,C3 的 `missing` 变成空集合 → 恒绿。
C4 钉住「用 network 标记的测试**恰好**是那三条」,多一条少一条都红。

**判据自己被改坏的方式,通常就是它检查的那个东西被整体拿掉。**

## C2 是唯一的行为级防线

M1/M2/M3 改的是配置,而 C1/C3 都是静态检查。万一静态检查漏了
(mark 写在别处、或者插件自己发告警),只有 C2 真的起一次 pytest
`--collect-only` 才知道输出里有没有那条告警。
代价是它要 27 秒 —— 但门禁本来就要跑 400 多秒。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
CLAIMS 只写字面量,不写名字引用。纯插入 must_not 留空、纯删除 must_have 留空。
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CRIT = REPO / "tests" / "test_no_unlabeled_public_network_tests.py"
PYPROJECT = REPO / "pyproject.toml"
PHASE2 = REPO / "tests" / "test_phase2.py"
PHASE3 = REPO / "tests" / "test_phase3.py"

TARGET = ["tests/test_no_unlabeled_public_network_tests.py"]

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


# ── M1/M2/M3:pyproject 的 markers ──
A_MARKERS = (
    'markers = [\n'
    '    "slow: 慢测试(基准测试一类),不查功能对错",\n'
    '    "network: 需要外网才能跑 —— 红了先怀疑网络,别急着改代码",\n'
    ']\n'
)
M1_NO_NETWORK = (
    'markers = [\n'
    '    "slow: 慢测试(基准测试一类),不查功能对错",\n'
    ']\n'
)
M2_NO_SLOW = (
    'markers = [\n'
    '    "network: 需要外网才能跑 —— 红了先怀疑网络,别急着改代码",\n'
    ']\n'
)
M3_NO_BLOCK = "markers = [  # 变异 M3:整个块删掉\n]\n"

# ── M4/M5:network 标记 ──
A_P2_E2E = "@pytest.mark.network\nasync def test_e2e_chain():"
M4_GONE = "async def test_e2e_chain():  # 变异 M4:network 标记被去掉"
A_P3_FULL = "@pytest.mark.network\nasync def test_e2e_full():"
M5_GONE = "async def test_e2e_full():  # 变异 M5:network 标记被去掉"

# ── M6:httpx fixture 改回公网 ──
A_LOCAL_PROBE = (
    '        async for site in probe("127.0.0.1", ports=[port], schemes=["http"], timeout=5):\n'
)
M6_PUBLIC = (
    '        async for site in probe("example.com", ports=[80], schemes=["http"], timeout=5):\n'
)

# ── 覆盖变异 ──
C1_BODY = "def test_every_used_pytest_mark_is_registered():"
C1_END = "def test_both_r105_marks_are_actually_declared():"
C2_BODY = "def test_collecting_the_suite_emits_no_unknown_mark_warning():"
C2_END = "# =====================================================================\n# 二、公网测试必须显式标记"
C3_BODY = "def test_every_known_public_network_test_is_labelled():"
C3_END = "def test_the_network_labelled_set_is_not_empty():"
C4_BODY = "def test_the_network_labelled_set_is_not_empty():"
C4_END = "# =====================================================================\n# 三、已经本机化的不许改回去"

CLAIMS = {
    "M1-删掉network声明": (
        [],  # 纯删除
        ['    "network: 需要外网才能跑 —— 红了先怀疑网络,别急着改代码",\n'],
    ),
    "M2-删掉slow声明": (
        [],  # 纯删除
        ['    "slow: 慢测试(基准测试一类),不查功能对错",\n'],
    ),
    "M3-整个markers块删掉": (
        ["markers = [  # 变异 M3:整个块删掉"],
        ['    "slow: 慢测试(基准测试一类),不查功能对错",\n'
         '    "network: 需要外网才能跑 —— 红了先怀疑网络,别急着改代码",\n'],
    ),
    "M4-phase2标记被去掉": (
        ["async def test_e2e_chain():  # 变异 M4:network 标记被去掉"],
        ["@pytest.mark.network\nasync def test_e2e_chain():"],
    ),
    "M5-phase3标记被去掉": (
        ["async def test_e2e_full():  # 变异 M5:network 标记被去掉"],
        ["@pytest.mark.network\nasync def test_e2e_full():"],
    ),
    "M6-httpx改回打公网": (
        ['        async for site in probe("example.com", ports=[80], schemes=["http"], timeout=5):\n'],
        ['        async for site in probe("127.0.0.1", ports=[port], schemes=["http"], timeout=5):\n'],
    ),
    "C1-拆掉mark注册检查": (["    pass  # 变异 C1:整条判据没了"], []),
    "C2-拆掉行为级告警检查": (["    pass  # 变异 C2:整条判据没了"], []),
    "C3-拆掉点名标记检查": (["    pass  # 变异 C3:整条判据没了"], []),
    "C4-拆掉标记集合不变式": (["    pass  # 变异 C4:整条判据没了"], []),
}

MUTANTS = [
    ("M1-删掉network声明", lambda p: _apply(p, A_MARKERS, M1_NO_NETWORK), False, (PYPROJECT,)),
    ("M2-删掉slow声明", lambda p: _apply(p, A_MARKERS, M2_NO_SLOW), False, (PYPROJECT,)),
    ("M3-整个markers块删掉", lambda p: _apply(p, A_MARKERS, M3_NO_BLOCK), False, (PYPROJECT,)),
    ("M4-phase2标记被去掉", lambda p: _apply(p, A_P2_E2E, M4_GONE), False, (PHASE2,)),
    ("M5-phase3标记被去掉", lambda p: _apply(p, A_P3_FULL, M5_GONE), False, (PHASE3,)),
    ("M6-httpx改回打公网", lambda p: _apply(p, A_LOCAL_PROBE, M6_PUBLIC), False, (PHASE2,)),
]

COVERAGE_MUTANTS = [
    ("C1-拆掉mark注册检查",
     lambda p: _replace_fn(p, C1_BODY, C1_BODY + "\n    pass  # 变异 C1:整条判据没了\n",
                           C1_END), True, (CRIT,)),
    ("C2-拆掉行为级告警检查",
     lambda p: _replace_fn(p, C2_BODY, C2_BODY + "\n    pass  # 变异 C2:整条判据没了\n",
                           C2_END), True, (CRIT,)),
    ("C3-拆掉点名标记检查",
     lambda p: _replace_fn(p, C3_BODY, C3_BODY + "\n    pass  # 变异 C3:整条判据没了\n",
                           C3_END), True, (CRIT,)),
    ("C4-拆掉标记集合不变式",
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
    if path.suffix == ".py":
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
        print(f"\n=== r105 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:24s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:300])
    print(f"\nr105 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.sandboxed(main))
