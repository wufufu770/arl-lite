"""r72 变异测试:验「MCP 工具不得静默忽略未声明参数」这条判据在守。

主题:4 个 MCP 工具只读启动时的 `self.workspace`,从不读 per-call 的 `workspace` key。
实测传 `workspace: teamA` 拿到的是**启动工作区**的数据,`isError` 还是 False。
静默忽略比报错危险 —— 它把错误答案包装成成功答案,AI 客户端会拿它当 teamA 的
资产清单去汇报。

修法:未声明的 key 一律报错,合法 key 列表**从工具自己的 inputSchema 推导**。
判据里最关键的一条是 `test_accepted_list_matches_advertised_schema`:判据先
`tools/list` 问服务器自己声明了什么,再逐字比报错里列的名单。手抄名单当场对不上。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明(不靠命名约定)。

全部变异**先手工验过会产生差异**才写进来:
  M1 撤掉整个拒绝(还原 r71 的静默忽略)   → 杀 workspace/拼写/名单三条
  M2 合法名单改成手抄一份               → 只杀「名单与 schema 一致」那一条
  M3 去掉 workspace 的出路提示           → 只杀 -w 那一条
  M4 连 schema 声明过的 key 也拒(过度修正) → 只杀「声明过的 key 不能被拒」那一条
  C1 名单比对放宽成子集包含               → 期望存活
  C2 拼写错误判据放宽成只看 isError       → 期望存活
  C3 出路判据放宽成不看 -w                → 期望存活
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SERVER = REPO / "arl_lite" / "mcp" / "server.py"
CRIT = REPO / "tests" / "test_mcp_unknown_args_are_rejected.py"
TARGET = "tests/test_mcp_unknown_args_are_rejected.py"

BAD_END = (
    "SyntaxError", "IndentationError", "TabError",
    "ERROR collecting", "error during collection", "Interrupted:",
)

# 实现锚点(整行切,不逐行拼字符串 —— r66/r67/r68 各栽过一次)
ALLOWED_LINE = "    allowed = set(_declared_args(tool))\n    return sorted(k for k in args if k not in allowed)"
UNKNOWN_LINE = '    return sorted(k for k in args if k not in allowed)'
WS_HINT_LINE = '    if "workspace" in bad:\n        text += f". {_WS_HINT}"'
REJECT_BLOCK = '        bad = _unknown_args(self.tools[name], args)'

# 判据锚点。**必须覆盖整个 assert 语句(含结尾的 `)`)**,不能只替换首行 ——
# 替换 `assert X, (` 的第一行会把后面的消息字符串行留成孤儿,当场 SyntaxError。
# r71 的 C1 和 r72 的 C2/C3 各栽过一次,假杀守卫把它们挡下来了但变异本身是废的。
CRIT_ANCHORS = {
    "listed": "        assert listed == schemas[name], (",
    "misspelled": '        assert bad_key in _text(resp[70 + i]), (\n'
                  '            f"{name} 的报错没点名是哪个 key 不认识:{_text(resp[70 + i])}"\n'
                  "        )",
    "ws_out": '    assert "-w" in text, (\n'
              '        f"workspace 的报错没指出真正的入口(启动时的 -w):{text!r}"\n'
              "    )",
}


def _apply(path: pathlib.Path, old: str, new: str) -> None:
    src = path.read_text(encoding="utf-8")
    if old not in src:
        raise AssertionError(f"变异锚点没命中 {path.name}:{old[:70]!r}")
    out = src.replace(old, new, 1)
    if out == src:
        raise AssertionError("变异是空操作 —— 什么都不会变,写了也是白写")
    path.write_text(out, encoding="utf-8")


def _m1_remove_rejection(path: pathlib.Path) -> None:
    """撤掉整个拒绝(还原 r71 之前:静默忽略未声明的 key)。"""
    _apply(path, REJECT_BLOCK, "        bad = []  # 变异:静默忽略未声明的 key")


# 每个变异该改哪些文件 —— 显式写进元组,不靠 name.startswith("C") 猜。
# r70 的 M3 明明改的是 cli.py 却被当成改判据,锚点当场不命中并中止。
MUTANTS = [
    ("M1-撤掉整个拒绝", _m1_remove_rejection, False, (SERVER,)),
    ("M2-合法名单改手抄一份", lambda p: _apply(
        p, ALLOWED_LINE,
        '    allowed = {"table", "limit"}  # 变异:手抄名单,不再从 schema 推导\n'
        "    return sorted(k for k in args if k not in allowed)"), False, (SERVER,)),
    ("M3-去掉workspace出路", lambda p: _apply(
        p, WS_HINT_LINE, "    if False:  # 变异:不给出路"), False, (SERVER,)),
    ("M4-连声明过的key也拒", lambda p: _apply(
        p, UNKNOWN_LINE,
        "    return sorted(args)  # 变异:过度修正,一律拒绝"), False, (SERVER,)),
]

# ---- 覆盖变异:改坏判据自己,期望**存活**(真实现已经是对的)
COVERAGE_MUTANTS = [
    ("C1-名单比对放宽成子集", lambda p: _apply(
        p, CRIT_ANCHORS["listed"],
        "        assert set(schemas[name]) <= set(listed), (  # 变异:放宽成子集包含"), True, (CRIT,)),
    ("C2-拼写判据放宽成只看isError", lambda p: _apply(
        p, CRIT_ANCHORS["misspelled"],
        '        assert r["isError"] is True, "  # 变异:不点名哪个 key"'), True, (CRIT,)),
    ("C3-出路判据放宽成不看-w", lambda p: _apply(
        p, CRIT_ANCHORS["ws_out"],
        '    assert text, "  # 变异:不看是否给出路"'), True, (CRIT,)),
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
        ("实现变异(期望全被杀)", MUTANTS),
        ("覆盖变异(期望全存活)", COVERAGE_MUTANTS),
    ):
        print(f"\n=== r72 {title} ===")
        for name, detail, outp in _sweep(mutants):
            flag = "OK " if detail in ("killed", "survived") else "!! "
            if flag == "!! ":
                bad += 1
            print(f"  {flag}{name:30s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:600])
    print(f"\nr72 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
