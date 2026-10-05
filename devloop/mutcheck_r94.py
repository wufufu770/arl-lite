"""r94 变异测试:验「基线不许记假账」这件事在守。

## 主题

r93 修好 9 条失败之后,门禁提示 `all 9 baseline failure(s) now pass, shrink
allowed_failures`,但没人执行。名单就一直挂着。

r94 实测它值多少钱:把**已经修好的** `test_e2e_chain` 再次弄坏,门禁报

    e2e_chain 再次被弄坏   passed=True | all 1 failure(s) are known baseline ones

**9 张免费通行证。** 门禁守的是「别引入名单外的新失败」,而这 9 条曾经
就是名单内的失败。更阴险的是账面上看不出来:基线记 `failed: 9`,运行确实
是红的,两边都「对得上」。

这一轮的判据是拿合成样本喂的 —— 名单收紧后它是空的,真基线上「自洽吗」
「有幽灵吗」压根不会被触发,判据恒过,削弱它的变异会全部存活(r90 同款教训)。

## 变异清单

实现变异(期望全被杀):
  M1 把 9 条陈旧名单塞回基线(还原那 9 张通行证) → 真跑那条红
  M2 身份收集器退回只认 FunctionDef(正控制逮到的真 bug) → async 那条红
  M3 自洽检查恒返回空                              → 合成假账红
  M4 幽灵检查恒返回空                              → 合成幽灵红
  M5 假账检查恒返回空                              → 合成样本红
  M6 假账检查不认参数化后缀                        → 参数化那条红
  M7 loc_budget 被 --update-baseline 顺手改掉        → 红线那条红

覆盖变异(期望全存活):
  C1 拆掉「身份收集器认得 async def」那条判据
  C2 拆掉「红线没被顺手改」那条判据

## r94 的两处扩展(都不是随便加的)

1. **目标含 .json**:`_write_checked` 原来只做 `ast.parse`,遇到 JSON 会直接
   判成语法错误。M1 是本轮最重要的一条变异,目标恰恰是 baselines.json ——
   工具不支持就等于最重要那条验不了。所以按后缀分流:py 走 ast,其余走
   `json.loads`。**报「你语法写错了」和报「你的变异工具不支持这个文件类型」
   是两回事,混起来就是又一次假失败。**

2. **判据文件本身也是变异目标**:C1/C2 拆的是判据,不是实现。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
CLAIMS 只写字面量。锚点必须**括号配平且唯一**(JSON 片段天然配平)。
所有变异统一走 _write_checked,写盘前做语法/JSON 校验。
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
BASELINES = REPO / "devloop" / "baselines.json"
CRIT = REPO / "tests" / "test_baseline_allowlist_has_no_stale_entries.py"

TARGET = ["tests/test_baseline_allowlist_has_no_stale_entries.py"]

# r94:本轮判据的输入里没有 "Interrupted:" 这类会撞上子串启发式的字面量,
# 但仍然按 summary 行判这一轮自己跑没跑炸 —— 子串启发式在 r93 假阳性过一次,
# 不该指望换一个判据文件就自动变可靠。
import re  # noqa: E402

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


# ── 锚点 ──

# M1:把收紧后的基线换回 9 条陈旧名单(本轮修掉的正是这个状态)
TB_TIGHT = ('  "test_baseline": {\n'
            '    "allowed_failures": [],\n'
            '    "failed": 0,\n'
            '    "passed": 1249\n'
            '  }\n')
TB_STALE = ('  "test_baseline": {\n'
            '    "allowed_failures": [\n'
            '      "tests/test_phase1.py::test_3state_discipline",\n'
            '      "tests/test_phase1.py::test_base_module_3state",\n'
            '      "tests/test_phase1.py::test_crtsh_module_mock",\n'
            '      "tests/test_phase2.py::test_9_sources_integration",\n'
            '      "tests/test_phase2.py::test_concurrent_isolation",\n'
            '      "tests/test_phase2.py::test_e2e_chain",\n'
            '      "tests/test_phase2.py::test_httpx_probe_integration",\n'
            '      "tests/test_phase2.py::test_portscan_integration",\n'
            '      "tests/test_phase3.py::test_e2e_full"\n'
            '    ],\n'
            '    "failed": 9,\n'
            '    "passed": 433\n'
            '  }\n')

# M7:`--update-baseline` 会把 loc_budget 也一起写成当前实测值(r91 差点这么干)
LOC_LIMIT_OLD = '    "devloop_code_loc": 2900,\n'
LOC_LIMIT_NEW = '    "devloop_code_loc": 2897,\n'

# 判据侧
IS_TEST_FN = ('    return (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))\n'
              '            and node.name.startswith("test_"))\n')
IS_TEST_FN_SYNC = ('    return (isinstance(node, ast.FunctionDef)\n'
                   '            and node.name.startswith("test_"))\n')

CONSISTENCY = ('    return ([] if failed == len(allowed)\n'
               '            else [f"failed={failed} 但名单有 {len(allowed)} 条"])\n')
CONSISTENCY_NONE = '    return []\n'

GHOST = '    return [i for i in (b.get("allowed_failures") or []) if i not in real]\n'
GHOST_NONE = '    return []\n'

STALE = ('    return [i for i in listed\n'
         '            if not any(f == i or f.startswith(i + "[") for f in failed_ids)]\n')
STALE_NONE = '    return []\n'
STALE_NO_PARAM = '    return [i for i in listed if i not in failed_ids]\n'

C1_BODY = 'def test_the_identity_collector_understands_async_tests(tmp_path):'
C2_BODY = 'def test_the_baseline_was_not_written_by_a_gate():'

CLAIMS = {
    # ── 全部写字面量,不写名字引用 ──
    # r86/r88/r89/r93 各栽一次,这是**第五次**。自检守卫的 `ast.literal_eval`
    # 当场抛 ValueError,六条判据一起红。这条规矩本身早该长进肌肉记忆:
    # 声明串的意义是「它确实出现在那个文件里」,指向一个变量等于把这件事
    # 交给别处 —— 而「别处」正是 r80 说的「存在检查冒充行为检查」的近亲。
    "M1-把9条陈旧名单塞回基线": (
        ['"allowed_failures": [\n      "tests/test_phase1.py::test_3state_discipline"'],
        ['  "test_baseline": {\n    "allowed_failures": [],\n'
         '    "failed": 0,\n    "passed": 1249\n  }\n'],
    ),
    "M2-身份收集器退回只认FunctionDef": (
        ['isinstance(node, ast.FunctionDef)'],
        ['isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))'],
    ),
    "M3-自洽检查恒返回空": (
        ['    return []\n'],
        ['    return ([] if failed == len(allowed)\n'
         '            else [f"failed={failed} 但名单有 {len(allowed)} 条"])\n'],
    ),
    "M4-幽灵检查恒返回空": (
        ['    return []\n'],
        ['    return [i for i in (b.get("allowed_failures") or []) if i not in real]\n'],
    ),
    "M5-假账检查恒返回空": (
        ['    return []\n'],
        ['    return [i for i in listed\n'
         '            if not any(f == i or f.startswith(i + "[") for f in failed_ids)]\n'],
    ),
    "M6-假账检查不认参数化后缀": (
        ['    return [i for i in listed if i not in failed_ids]\n'],
        ['f.startswith(i + "[")'],
    ),
    "M7-红线被update-baseline顺手改掉": (
        ['"devloop_code_loc": 2897'],
        ['    "devloop_code_loc": 2900,\n'],
    ),
    "C1-拆掉身份收集器认async那条判据": (
        ["    pass  # 变异 C1:整条判据没了"],
        ['    assert got == want, (\n'],
    ),
    "C2-拆掉红线没被顺手改那条判据": (
        ["    pass  # 变异 C2:整条判据没了"],
        ['    assert loc["total_loc"] == 16269, (\n'],
    ),
}

MUTANTS = [
    ("M1-把9条陈旧名单塞回基线",
     lambda p: _apply(p, TB_TIGHT, TB_STALE), False, (BASELINES,)),
    ("M2-身份收集器退回只认FunctionDef",
     lambda p: _apply(p, IS_TEST_FN, IS_TEST_FN_SYNC), False, (CRIT,)),
    ("M3-自洽检查恒返回空",
     lambda p: _apply(p, CONSISTENCY, CONSISTENCY_NONE), False, (CRIT,)),
    ("M4-幽灵检查恒返回空",
     lambda p: _apply(p, GHOST, GHOST_NONE), False, (CRIT,)),
    ("M5-假账检查恒返回空",
     lambda p: _apply(p, STALE, STALE_NONE), False, (CRIT,)),
    ("M6-假账检查不认参数化后缀",
     lambda p: _apply(p, STALE, STALE_NO_PARAM), False, (CRIT,)),
    ("M7-红线被update-baseline顺手改掉",
     lambda p: _apply(p, LOC_LIMIT_OLD, LOC_LIMIT_NEW), False, (BASELINES,)),
]

C1_END_MARK = "# ── 判据 4"
C2_END_MARK = None  # 判据 5 是文件最后一条,用「到文件末尾」
C1_STUB = "    pass  # 变异 C1:整条判据没了\n"
C2_STUB = "    pass  # 变异 C2:整条判据没了\n"

COVERAGE_MUTANTS = [
    ("C1-拆掉身份收集器认async那条判据",
     lambda p: _replace_span(p, C1_BODY, C1_END_MARK, C1_STUB), True, (CRIT,)),
    ("C2-拆掉红线没被顺手改那条判据",
     lambda p: _replace_span(p, C2_BODY, C2_END_MARK, C2_STUB), True, (CRIT,)),
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
    """写盘前的唯一出口。

    r94 扩展:按后缀分流。`.py` 走 `ast.parse`,`.json` 走 `json.loads`。
    原来只有 ast.parse,于是 JSON 目标会直接被判成「语法错误」——
    **报「你语法写错了」和报「你的变异工具不支持这个文件类型」是两回事**,
    混起来就是又一次假失败。本轮最重要的一条变异(M1)目标恰恰是 JSON。
    """
    if out == path.read_text(encoding="utf-8"):
        raise AssertionError("变异是空操作 —— 什么都不会变,写了也是白写")
    if path.suffix == ".py":
        import ast
        try:
            ast.parse(out)
        except SyntaxError as e:
            raise AssertionError(
                f"变异会让 {path.name} 语法错误({e})。文件未写入。") from None
    elif path.suffix == ".json":
        try:
            json.loads(out)
        except ValueError as e:
            raise AssertionError(
                f"变异会让 {path.name} 不是合法 JSON({e})。文件未写入。") from None
    path.write_text(out, encoding="utf-8")


def _apply(path: pathlib.Path, old: str, new: str) -> None:
    src = path.read_text(encoding="utf-8")
    if old not in src:
        raise AssertionError(f"变异锚点没命中 {path.name}:{old[:70]!r}")
    _write_checked(path, src.replace(old, new, 1))


def _replace_span(path: pathlib.Path, start_marker: str, end_marker: str | None,
                  stub_body: str) -> None:
    """把 `start_marker` 到下一个顶层标记(或文件末尾)整段换成一条 pass 桩

    `stub_body` 由调用方给 —— 桩里那行注释必须和 `CLAIMS` 的 must_have
    **逐字一致**,否则这条覆盖变异会被自己的自检判成 BAD-MUTANT。
    (头一版桩是硬编码的 `# 变异:整条判据没了`,声明里写的是
    `# 变异 C1:整条判据没了`,于是两条覆盖变异全部 BAD-MUTANT。
    **变异脚本自己踩了 r92 那条坑:替换做到了,声明却说没做到。**)

    桩的 `def` 行**原样保留标记**:头一版想「去掉参数」,结果连 `()` 和
    冒号一起去掉了,产出 `def test_x` —— 缺了参数表和冒号,那是语法错误。
    写变异锚点最省心的做法就是**别动它**,整行照抄。
    """
    src = path.read_text(encoding="utf-8")
    if start_marker not in src:
        raise AssertionError(f"变异锚点没命中 {path.name}:{start_marker!r}")
    i = src.index(start_marker)
    j = src.index(end_marker, i) if end_marker else len(src)
    if not start_marker.rstrip().endswith(":"):
        raise AssertionError(
            f"变异标记不是一条完整的 def 行(缺结尾冒号):{start_marker!r}")
    _write_checked(path, src[:i] + f"{start_marker}\n{stub_body}" + src[j:])


def _run_criterion():
    return subprocess.run(
        [sys.executable, "-B", "-m", "pytest", *TARGET, "-q", "-p", "no:cacheprovider"],
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
        print(f"\n=== r94 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:36s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:300])
    print(f"\nr94 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
