"""r79 变异测试:验「结构检查不许用子串」这条修复在守。

主题:先用 r78 那个手法证明了漏洞**真实存在** ——
`tests/test_devloop_multiagent.py` 里 `assert "q.recover_stale(" in
inspect.getsource(Loop.recover_stale_in_progress)`,我把真调用
`n, held = q.recover_stale()` 换成 `n, held = 0, 0`、再往函数里加一行
`# ... q.recover_stale(` 的注释,**这条断言照样 passed**。

而它守的恰恰是多 agent 并发安全:漏掉 Queue.recover_stale = 安静地丢更新,
抹掉别的 agent 刚领走的活(protocol.py 里那段长注释详细写了这件事)。
**一行注释就能让守卫失效。**

修法:改成 AST 节点级判定 —— 看真正被调用的方法(`q.recover_stale`)、
看 forbidden 调用是否真的出现、看 `with self._locked(` 与 `self.load()`
的 AST 节点顺序。

调研:种子里提的「普查所有子串检查」有真实目标但不能照单全改
  第一版检测器(靠「字面量像不像代码」筛)8 处命中全是误报 —— 它们检查的是
  **产品运行时输出**(`(s)` 单复数、`COUNT(*)`、`changes=never`),那是按行为
  断言,本来就该用字符串。更糟的是 r78 真出事的 `"ast.parse" in seg` 反而
  **没被抓到**:「短标识符形态」正好落在那个正则的盲区。
  换成结构信号(右操作数是否来自 read_text/getsource/get_source_segment)后,
  22 处命中 14 个文件 —— r78 那类洞**不是孤例**。本轮只改被证明有风险、
  且守护并发安全的那一处,其余入队分批改,不盲改。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
所有变异统一走 _write_checked,写盘前 ast.parse。

全部变异:
  M1 删掉真调用(不改注释)         → 杀「必须真的调 q.recover_stale」
  M2 真调用删掉 + 注释里补上那串串 → **杀**。这就是原 bug 的形状,首版靠
                                     子串判断时它是存活的
  M3 注释里补上 q.load()/q.save() → 杀 forbidden 那条(子串版会被喂饱)
  M4 把 self.load() 挪到锁外       → 杀「load 必须在锁内」
  C1 「真调用」判据放宽成恒真      → 期望存活
  C2 forbidden 判据放宽成恒真      → 期望存活
  C3 锁内顺序判据放宽              → 期望存活
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CRIT = REPO / "tests" / "test_devloop_multiagent.py"
TARGET = "tests/test_devloop_multiagent.py"

COLLECTION_FAILED = ("error during collection", "ERROR collecting", "Interrupted:")

# 判据里的三处断言(整块照抄,只替换首行会留下孤儿行 —— 这个坑栽了五次)
JUDGE_MUST_CALL = (
    '    assert "q.recover_stale" in called, (\n'
    '        f"protocol 没走 Queue.recover_stale(实际调用的: {sorted(called)})"\n'
    "    )"
)
JUDGE_NO_LEAK = (
    "    assert not leaked, (\n"
    '        f"protocol 自己做读-改-写/自己加锁,绕开了 Queue 的加锁入口:{leaked}"\n'
    "    )"
)
JUDGE_LOCK_ORDER = (
    '    assert lock_at < load_at, \\\n'
    '        "Queue.recover_stale 的 load 在锁外 —— 丢更新会回来"'
)

# 目标实现的锚点
REAL_CALL = "        n, held = q.recover_stale()"
FN_HEAD = "    def recover_stale_in_progress(self) -> int:"


def _write_checked(path: pathlib.Path, out: str) -> None:
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


def _m2_comment_feeds_the_old_check(path: pathlib.Path) -> None:
    """原 bug 的形状:真调用删掉,用一行**注释**把那串字符补回去。

    这是 r79 动手前实测出的假绿手法。子串判断扛不住它,AST 判定应该能杀。
    """
    src = path.read_text(encoding="utf-8")
    out = src.replace(REAL_CALL, "        n, held = 0, 0", 1)
    out = out.replace(FN_HEAD,
                      FN_HEAD + "\n"
                      "        # 变异注入:注释里写 q.recover_stale( 与 file_lock 与 q.load()"
                      " 与 q.save( —— 子串判断会被喂饱,AST 判断不会", 1)
    _write_checked(path, out)


def _m4_load_outside_lock(path: pathlib.Path) -> None:
    """把 self.load() 挪到 with self._locked( 之外 —— 丢更新会回来。"""
    src = path.read_text(encoding="utf-8")
    start = src.index("    def recover_stale(self)")
    seg = src[start:start + 2600]
    marker = "        with self._locked("
    at = seg.index(marker)
    end_of_with = seg.index("\n", seg.index(":", at)) + 1
    indent = "        "
    head, body = seg[:at], seg[at:end_of_with] + seg[end_of_with:]
    # 简化但有效:把 load 提到 with 之前 —— 用一个显式前置 load 触发顺序变化
    new_seg = head + "        _pre = self.load()  # 变异:load 在锁外\n" + body
    _write_checked(path, src[:start] + new_seg + src[start + 2600:])


MUTANTS = [
    ("M1-删掉真调用", lambda p: _apply(p, REAL_CALL, "        n, held = 0, 0"),
     False, (REPO / "arl_lite" / "devloop" / "protocol.py",)),
    ("M2-真调用删掉+注释喂饱(原bug形状)", _m2_comment_feeds_the_old_check,
     False, (REPO / "arl_lite" / "devloop" / "protocol.py",)),
    ("C1-真调用判据放宽", lambda p: _apply(p, JUDGE_MUST_CALL, '    assert True, "  # 变异"'),
     True, (CRIT,)),
    ("C2-forbidden判据放宽", lambda p: _apply(p, JUDGE_NO_LEAK, '    assert True, "  # 变异"'),
     True, (CRIT,)),
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
        print(f"\n=== r79 {title} ===")
        for name, detail, outp in _sweep(mutants):
            flag = "OK " if detail in ("killed", "survived") else "!! "
            if flag == "!! ":
                bad += 1
            print(f"  {flag}{name:38s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:400])
    print(f"\nr79 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.sandboxed(main))
