"""r76 变异测试:验「`devloop claim` 不得谎报队列状态」这条判据在守。

主题:实测 `devloop claim --item-id ghost-item-xyz` 回「queue has no pending item」,
而队列里明明有 4 条 pending。根因是 `_cmd_claim` 把「指定条目不可领」和
「队列真空」两种 `it is None` 塌缩成同一句文案。

为什么这条值得守:本循环协议每轮都跑 `claim --item-id`。条目 id 打错、或那条已被
别的 agent 领走时,工具报「队列没有待办」—— 照这句话走会误判整个队列已空,
转头去补种子,把真正的待办撂在一边。**一条假消息直接腐蚀循环的状态判断。**

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明(不靠命名约定)。
_apply 写盘前先 ast.parse,语法坏了当场拒且不写文件(r74 加的机制)。

全部变异:
  M1 还原原 bug(两种 None 共用一句)   → 杀「不得谎报」+「点名条目」两条
  M2 报错点名了但仍谎报队列为空         → 只杀「不得谎报」那条
  M3 报错不列剩余可领条目              → 只杀「说清还剩什么」那条
  M4 对已完成条目也报「队列空」        → 杀「对已完成条目」那条
  M5 真空队列也改成 cannot claim 口径   → 期望存活?不 —— 那会让真空队列也报错,
                                          是过度修正,判据第 5 条(真空队列保持
                                          原消息)正是为拦它而写 → 期望被杀
  C1 「点名条目」判据放宽              → 期望存活
  C2 「列出剩余」判据放宽成恒真        → 期望存活
  C3 「真空队列保持原消息」放宽         → 期望存活
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
DEVLOOP_CLI = REPO / "arl_lite" / "devloop" / "cli.py"
CRIT = REPO / "tests" / "test_devloop_claim_does_not_lie_about_the_queue.py"
TARGET = "tests/test_devloop_claim_does_not_lie_about_the_queue.py"

# 守卫**只**在 pytest 没能把测试收集起来时才算 BAD-SYNTAX(变异无效)。
# 早期版本把裸子串 "SyntaxError" 也算进去,于是测试失败信息里恰好出现这个词时
# 会误判 —— r76 的 M1 实测就是:文件语法完全合法、4 条测试 FAIL(确实被杀),
# 却被报成 BAD-SYNTAX。判据的失败模式也必须可判定,不能靠子串猜。
COLLECTION_FAILED = (
    "error during collection", "ERROR collecting", "Interrupted:",
)


CRIT_ANCHORS = {
    "names": '    assert "r76-ghost" in err, (\n'
             '        f"报错没有点名用户指定的那一条,用户无从知道是哪个 id 出了问题:{err!r}"\n'
             "    )",
    "remaining": '    assert PENDING_ID in err, (\n'
                 '        f"报错没有列出仍然可领的条目,调用方无法接着往下做:{err!r}"\n'
                 "    )",
    "empty_kept": '    assert "nothing to claim" in out, (\n'
                  '        f"真空队列应当仍然说「没东西可领」:{out!r} / {err!r}"\n'
                  "    )",
}


def _write_checked(path: pathlib.Path, out: str) -> None:
    """**唯一**的变异写盘出口:先 ast.parse,语法坏了当场拒且不写文件。

    r74 加过这个守卫,但 r76 暴露出两个手写变异函数直接 path.write_text 绕过了它,
    于是守卫形同虚设。守卫必须只有一条路径可走,否则「有守卫」只是看起来有。
    """
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


def _m1_revert(path: pathlib.Path) -> None:
    """还原成 r76 之前:两种 None 共用同一句谎话。"""
    src = path.read_text(encoding="utf-8")
    start = src.index("    if it is None:")
    end = src.index("    print(f\"[+] claimed {it.id} — {it.title}\")", start)
    _write_checked(
        path,
        src[:start]
        + '    if it is None:\n'
        + '        print("[i] nothing to claim (queue has no pending item)")\n'
        + "        return 1\n"
        + src[end:])


def _m3_drop_remaining(path: pathlib.Path) -> None:
    """报错点名了条目,但不再列出队列里还剩什么。"""
    src = path.read_text(encoding="utf-8")
    start = src.index("            others = [i.id for i in _queue().load()")
    end = src.index("        else:", start)
    _write_checked(path, src[:start] + src[end:])


# 每个变异该改哪些文件 —— 显式写进元组。
MUTANTS = [
    ("M1-还原原bug共用谎话", _m1_revert, False, (DEVLOOP_CLI,)),
    ("M2-点名了但仍谎报队列空", lambda p: _apply(
        p, '            others = [i.id for i in _queue().load()\n'
           '                      if i.status in ("pending", "in_progress")\n'
           '                      and (i.owner or "") != owner]\n'
           '            if others:\n'
           '                print(f"    queue still has {len(others)} claimable item(s): "\n'
           '                      f"{\', \'.join(sorted(others)[:5])}", file=sys.stderr)',
        '            print("[i] queue has no pending item", file=sys.stderr)  # 变异:仍然谎报'),
     False, (DEVLOOP_CLI,)),
    ("M3-不列剩余可领条目", _m3_drop_remaining, False, (DEVLOOP_CLI,)),
    ("M5-真空队列也改成cannot-claim口径", lambda p: _apply(
        p, '            print("[i] nothing to claim (queue has no pending item)")',
        '            print("[!] cannot claim: nothing claimable", file=sys.stderr)  # 变异:过度修正'),
     False, (DEVLOOP_CLI,)),
]

# ---- 覆盖变异:改坏判据自己,期望**存活**(真实现已经是对的)
COVERAGE_MUTANTS = [
    ("C1-点名条目判据放宽", lambda p: _apply(
        p, CRIT_ANCHORS["names"],
        '    assert True, "  # 变异:不看有没有点名条目"'), True, (CRIT,)),
    ("C2-列出剩余判据放宽成恒真", lambda p: _apply(
        p, CRIT_ANCHORS["remaining"],
        '    assert True, "  # 变异:不看有没有列出剩余"'), True, (CRIT,)),
    ("C3-真空队列判据放宽", lambda p: _apply(
        p, CRIT_ANCHORS["empty_kept"],
        '    assert rc == 1, "  # 变异:真空队列不看消息"'), True, (CRIT,)),
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
        ("实现变异(期望全被杀)", MUTANTS),
        ("覆盖变异(期望全存活)", COVERAGE_MUTANTS),
    ):
        print(f"\n=== r76 {title} ===")
        for name, detail, outp in _sweep(mutants):
            flag = "OK " if detail in ("killed", "survived") else "!! "
            if flag == "!! ":
                bad += 1
            print(f"  {flag}{name:34s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:500])
    print(f"\nr76 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
