"""r71 变异测试:验「MCP 错误面必须给出路」这条判据在守。

主题:`arl_lite/mcp/server.py` 原来把所有工具异常压成 `tool error: {type(e).__name__}`,
handlers 里写好的诊断信息(`missing required argument: table`、`limit must be 1..1000`)
全被丢掉。实测 10 种不同的用户错误塌缩成同一句 `tool error: ValueError`,
调用方(AI 客户端)无从分辨自己错在哪 —— 给一条走不通的报错比不给更坏。
`not initialized` 也只说「没初始化」不说下一步做什么,同样零出路。

CLI 那头有 112 条建议逐条验过 rc=0(r60~r70 建起来的),MCP 这整个面此前一条判据都没有。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明(不靠命名约定)。

全部变异**先手工验过会产生差异**才写进来:
  M1 还原原 bug:只回异常类名            → 10 条逐条诊断 + 可分辨 + 裸类名 三组全杀
  M2 `_error_text` 丢掉 detail 分支     → 只杀 10 条 + 裸类名,够窄
  M3 not-initialized 退回旧文案         → 只杀那一条
  M4 两处 not-initialized 各写一份       → 只杀「同源」那一条(守住 Key Decision 7)
  C1 逐条诊断判据退化成只看 isError      → 期望存活
  C2 可分辨判据放宽成允许全部塌缩        → 期望存活
  C3 裸类名判据退化成恒真                → 期望存活(见下,这是自揭的洞)
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SERVER = REPO / "arl_lite" / "mcp" / "server.py"
CRIT = REPO / "tests" / "test_mcp_errors_give_a_way_out.py"
TARGET = "tests/test_mcp_errors_give_a_way_out.py"

BAD_END = (
    "SyntaxError", "IndentationError", "TabError",
    "ERROR collecting", "error during collection", "Interrupted:",
)

ERROR_TAIL = '    detail = str(e).strip()\n    head = f"tool error: {type(e).__name__}"\n    return f"{head}: {detail}" if detail else head'
NI_LINE = '                "error": {"code": -32000, "message": _NOT_INITIALIZED},'
NI_CONST = '_NOT_INITIALIZED = "not initialized: send an \'initialize\' request first"'

# 判据里三处锚点(逐行切,不逐行拼字符串 —— r66/r67/r68 各栽过一次)
CRIT_ANCHORS = {
    "diagnose": '    assert expected in text, (',
    "bare": '    assert not bare, f"这些报错只剩一个裸异常类名,没有任何可执行信息:{bare}"',
    "same": '    assert resp[1]["error"]["message"] == resp[2]["error"]["message"], (',
}


def _apply(path: pathlib.Path, old: str, new: str) -> None:
    src = path.read_text(encoding="utf-8")
    if old not in src:
        raise AssertionError(f"变异锚点没命中 {path.name}:{old[:70]!r}")
    out = src.replace(old, new, 1)
    if out == src:
        raise AssertionError("变异是空操作 —— 什么都不会变,写了也是白写")
    path.write_text(out, encoding="utf-8")


def _m1_revert_to_class_name(path: pathlib.Path) -> None:
    """还原成 r71 之前的样子:只回异常类名,诊断信息全丢。"""
    _apply(path, ERROR_TAIL, '    return f"tool error: {type(e).__name__}"')


def _m2_drop_detail(path: pathlib.Path) -> None:
    """半吊子修法:有 _error_text,但 detail 分支被删掉(仍丢全部诊断)。"""
    _apply(path, ERROR_TAIL, '    return f"tool error: {type(e).__name__}"  # 变异:丢掉 detail')


def _m4_two_copies(path: pathlib.Path) -> None:
    """两处 not-initialized 各写一份文案(违反「同源」)。"""
    src = path.read_text(encoding="utf-8")
    if src.count(NI_LINE) != 2:
        raise AssertionError(f"预期 2 处 not-initialized,实际 {src.count(NI_LINE)}")
    # 只改第一处,第二处保持 _NOT_INITIALIZED —— 两份文案开始漂移
    _apply(path, NI_LINE, '                "error": {"code": -32000, "message": "not initialized"},')


# 每个变异该改哪些文件 —— 显式写进元组,不靠 name.startswith("C") 猜。
MUTANTS = [
    ("M1-还原只回异常类名", _m1_revert_to_class_name, False, (SERVER,)),
    ("M2-丢掉detail分支", _m2_drop_detail, False, (SERVER,)),
    ("M3-not-initialized退回旧文案", lambda p: _apply(p, NI_CONST, '_NOT_INITIALIZED = "not initialized"'), False, (SERVER,)),
    ("M4-两处各写一份文案", _m4_two_copies, False, (SERVER,)),
]

# ---- 覆盖变异:改坏判据自己,期望**存活**(真实现已经是对的)
COVERAGE_MUTANTS = [
    ("C1-逐条诊断判据退化成只看isError", lambda p: _apply(
        p, CRIT_ANCHORS["diagnose"],
        '    assert "tool error" in text, (  # 变异:不看诊断内容'), True, (CRIT,)),
    ("C2-可分辨判据放宽成全塌缩也算过", lambda p: _apply(
        p, "    unexpected = {",
        "    return  # 变异:允许全部塌缩\n    unexpected = {"), True, (CRIT,)),
    ("C3-裸类名判据退化成恒真", lambda p: _apply(
        p, CRIT_ANCHORS["bare"],
        '    assert True, "  # 变异:裸类名判据恒真"'), True, (CRIT,)),
]


def _run_criterion():
    return subprocess.run(
        [sys.executable, "-B", "-m", "pytest", TARGET, "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=REPO, timeout=1200,
    )


def _sweep(mutants, expect_default: bool) -> list:
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
        ("实现变异(期望全被杀)", MUTANTS),
        ("覆盖变异(期望全存活)", COVERAGE_MUTANTS),
    ):
        print(f"\n=== r71 {title} ===")
        for name, detail, outp in _sweep(mutants, expect_default=False):
            flag = "OK " if detail in ("killed", "survived") else "!! "
            if flag == "!! ":
                bad += 1
            print(f"  {flag}{name:30s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:600])
    print(f"\nr71 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
