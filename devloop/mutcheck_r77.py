"""r77 变异测试:验「devloop 退出码必须可判定」这条判据在守。

主题:实测同一条「条目不存在」,`drop` 返 1 而 `unmark` 返 2,两者打的却是
**完全相同**的消息 `no such item: 'ghost-xyz'`。同一命令 `gate` 也分叉:
「门禁红」返 1、「门禁不存在」返 2。约定:rc=1 业务层失败,rc=2 留给 argparse
用法错误(argparse 自己 exit 2,不由 devloop/cli.py 决定)。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明(不靠命名约定)。
所有变异统一走 _write_checked 单一写盘出口(r76 的教训:守卫必须有唯一路径可走,
否则「有守卫」只是看起来有),写盘前 ast.parse,语法坏了当场拒。

全部变异:
  M1 把 unmark 改回 rc=2          → 杀「业务层一律 1」+「同条件同码」两条
  M2 把 gate 未知门禁改回 rc=2     → 杀「同命令两种失败同码」+「业务层一律 1」
  M3 把 drop 改成 rc=2(反向分叉)   → 杀「同条件同码」+「业务层一律 1」
  M4 把 claim 改成 rc=0(失败说成功) → 杀「业务层一律 1」
  M5 release 失败返回 0(说成功)   → 杀「业务层一律 1」
  C1 期望值表条数判据退化成恒真    → 期望存活
  C2 同条件同码判据放宽成恒真       → 期望存活
  C3 用法错误判据放宽成只看 rc!=0  → 期望存活

自踩:M3 的锚点首版照着 _cmd_unmark 写成 `detail`,而 _cmd_drop 用的变量名是
`msg` —— 锚点当场不命中。变异锚点必须照抄真实源码,不能凭印象写
(这跟 r74/r75 的「锚点要落在真正控制行为的那行」是同一族错误的变体:
凭印象写出来的锚点,命中的东西和想验的东西常常不是一回事)。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
DEVLOOP_CLI = REPO / "arl_lite" / "devloop" / "cli.py"
CRIT = REPO / "tests" / "test_devloop_exit_codes_are_decidable.py"
TARGET = "tests/test_devloop_exit_codes_are_decidable.py"

# 守卫只认 pytest **收集失败** —— 裸子串 "SyntaxError" 会被测试失败信息里的
# 同名词骗到(r76 实测:文件语法合法、测试确实被杀,却被报成 BAD-SYNTAX)。
COLLECTION_FAILED = ("error during collection", "ERROR collecting", "Interrupted:")

UNMARK_TAIL = ('        print(f"[!] {detail}", file=sys.stderr)\n'
               '        # 业务层失败一律 1(rc=2 留给 argparse 用法错误)。这里原来返 2,而\n'
               '        # `drop` 对**完全相同**的条件、完全相同的消息 `no such item: \'x\'`\n'
               '        # 返 1 —— 消息都一样、码却不同,连按消息匹配都做不到。\n'
               '        return 1')
GATE_TAIL = ('        print(f"    available: {\', \'.join(gates.all_gate_names())}", file=sys.stderr)\n'
             '        # 约定:rc=1 = 业务层失败(门禁红、门禁不存在、条目不存在…),\n'
             '        # rc=2 留给 argparse 的用法错误(argparse 自己 exit 2,不由本文件决定)。\n'
             '        # 这里原来返 2,于是同一条命令「门禁红」返 1、「门禁不存在」返 2 ——\n'
             '        # 同一命令的两种失败给两种码,脚本没法用统一条件判失败。\n'
             '        return 1')
# _cmd_drop 用的变量名是 msg 不是 detail —— 首版照着 unmark 写成 detail,
# 锚点当场不命中。变异锚点必须照抄真实源码,不能凭印象写。
DROP_TAIL = '    if not ok:\n        print(f"[!] {msg}", file=sys.stderr)\n        return 1'
RELEASE_TAIL = ('    if not ok:\n'
                '        print(f"[!] {args.item_id} is not claimed by anyone", file=sys.stderr)\n'
                '        return 1')
# claim 失败路径的尾巴。首版我写了个嵌套 replace 去改它,结果产出语法错误,
# 守卫当场拒了;改成直接照抄真实源码整段。注意首版锚点里 CLAIM_TAIL 常量的
# 替换压根没生效(heredoc 字符串没匹配上),于是 .replace 成了空操作 ——
# 变异脚本自己坏了却报成「变异无效」,两者要分清。
CLAIM_TAIL = ('        else:\n'
              '            print("[i] nothing to claim (queue has no pending item)")\n'
              '        return 1')

CRIT_ANCHORS = {
    "same_code": "    assert drop.returncode == unmark.returncode, (\n"
                 '        f"同一条不存在的条目,drop rc={drop.returncode} 而 unmark rc={unmark.returncode} —— "\n'
                 '        f"连按消息匹配都做不到"\n'
                 "    )",
    "table": "    assert len(BUSINESS_FAILURES) == 7 and len(USAGE_ERRORS) == 8, (\n"
             '        f"期望表条数变了:业务层 {len(BUSINESS_FAILURES)} 条(应 7)、"\n'
             '        f"用法错误 {len(USAGE_ERRORS)} 条(应 8)。"\n'
             '        f"新增/删除都要先弄清是新增了失败路径还是改错了退出码"\n'
             "    )",
    "usage": '    assert p.returncode == RC_USAGE_ERROR, (\n'
             '        f"{label}: 期望 rc={RC_USAGE_ERROR}(argparse 用法错误),实际 rc={p.returncode}"\n'
             "    )",
}


def _write_checked(path: pathlib.Path, out: str) -> None:
    """**唯一**的变异写盘出口:先 ast.parse,语法坏了当场拒且不写文件。"""
    import ast
    if out == path.read_text(encoding="utf-8"):
        raise AssertionError("变异是空操作 —— 什么都不会变,写了也是白写")
    try:
        ast.parse(out)
    except SyntaxError as e:
        raise AssertionError(
            f"变异会让 {path.name} 语法错误({e}) —— 多行语句只替换首行会留下孤儿行。文件未写入。"
        ) from None
    path.write_text(out, encoding="utf-8")


def _apply(path: pathlib.Path, old: str, new: str) -> None:
    src = path.read_text(encoding="utf-8")
    if old not in src:
        raise AssertionError(f"变异锚点没命中 {path.name}:{old[:70]!r}")
    _write_checked(path, src.replace(old, new, 1))


# 每个变异该改哪些文件 —— 显式写进元组。
MUTANTS = [
    ("M1-unmark改回rc=2", lambda p: _apply(
        p, UNMARK_TAIL,
        UNMARK_TAIL.replace("        return 1", "        return 2  # 变异:改回旧的分叉")),
     False, (DEVLOOP_CLI,)),
    ("M2-gate未知门禁改回rc=2", lambda p: _apply(
        p, GATE_TAIL,
        GATE_TAIL.replace("        return 1", "        return 2  # 变异:同命令两种失败两种码")),
     False, (DEVLOOP_CLI,)),
    ("M3-drop改成rc=2(反向分叉)", lambda p: _apply(
        p, DROP_TAIL,
        DROP_TAIL.replace("        return 1", "        return 2  # 变异:反向分叉")),
     False, (DEVLOOP_CLI,)),
    ("M4-claim失败说成功rc=0", lambda p: _apply(
        p, CLAIM_TAIL,
        CLAIM_TAIL.replace("        return 1", "        return 0  # 变异:失败说成功")),
     False, (DEVLOOP_CLI,)),
    ("M5-release失败说成功rc=0", lambda p: _apply(
        p, RELEASE_TAIL,
        RELEASE_TAIL.replace("        return 1", "        return 0  # 变异:失败说成功")),
     False, (DEVLOOP_CLI,)),
]

# ---- 覆盖变异:改坏判据自己,期望**存活**(真实现已经是对的)
COVERAGE_MUTANTS = [
    ("C1-期望值表条数判据恒真", lambda p: _apply(
        p, CRIT_ANCHORS["table"],
        '    assert True, "  # 变异:条数恒真"'), True, (CRIT,)),
    ("C2-同条件同码判据放宽", lambda p: _apply(
        p, CRIT_ANCHORS["same_code"],
        '    assert True, "  # 变异:同条件同码恒真"'), True, (CRIT,)),
    ("C3-用法错误判据放宽成只看非零", lambda p: _apply(
        p, CRIT_ANCHORS["usage"],
        '    assert p.returncode != 0, "  # 变异:不区分 1 和 2"'), True, (CRIT,)),
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
        ("覆盖变异(期望全存活)", COVERAGE_MUTANTS + [m for m in MUTANTS if m[2]]),
    ):
        print(f"\n=== r77 {title} ===")
        for name, detail, outp in _sweep(mutants):
            flag = "OK " if detail in ("killed", "survived") else "!! "
            if flag == "!! ":
                bad += 1
            print(f"  {flag}{name:34s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:500])
    print(f"\nr77 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
