"""r80 变异测试:验「结构检查不许拿源码文本当子串」这次修复在守。

主题:r79 逮到「一行注释喂饱 `in inspect.getsource(...)`」。r80 把同一类
扩到整个 `tests/`,实测 **7 处 / 3 个文件**(种子写的「13 个文件 / 22 处」
被推翻)。动手前每处都做了注入证明,结论分三种:

  真洞 4 处 —— 删掉真调用、用注释或字符串字面量把同样的串喂回去,
              原判据照样 PASSED:
      test_round_commit_audit.py 206/209  get_source_segment → 返回**原文**,
                                     注释和 docstring 都在里面
      test_watcher_new_count.py   421      unparse(fn) → 字符串字面量原样留着
      test_watcher_new_count.py   426      unparse(n.iter) 同上
  否定结果 1 处:
      test_watcher_new_count.py   458      把喂串放在**别的**语句里时它正确 FAIL
                                     —— 串必须落在它检查的那条赋值内部,
                                     喂不进去。**这处本来就不是洞**,
                                     改它是为了让守卫不必开例外名单。
  非同类 2 处:
      test_cli_limit_honesty.py   258(×2)  查的是 f-string 的**源码拼写**;
                                     注释变不成 JoinedStr 节点,喂不进去。

判定器的设计代价:判别「被摘取的到底是代码节点还是一段文案」在静态
分析里半可判(真实写法是推导式变量 `n`,看不出来),所以守卫改成
**fail-closed** —— 一律命中,逼人改成结构判定。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
所有变异统一走 _write_checked,写盘前 ast.parse。

全部变异:
  M1 删掉真调用(不改注释)            → 杀
  M2 真调用删掉 + 注释里补上那串串     → **杀**。原 bug 的形状,
                                       首版靠 get_source_segment 时它是存活的
  M3 契约表派生换手抄 5 元组 + 字面量喂串 → **杀**。r52 原 bug 的形状,
                                       手抄的那份**行为完全等价**,只有结构判据能逮
  M4 把判据整段退回 r79 的子串写法      → 杀「守卫本身」这条,
                                       证明 test_source_checks_are_structural
                                       不是恒真的
  M5 检测器被改成恒返回空               → 杀。正控制组必须先红
  M6 检测器漏掉 get_source_segment      → 杀。真实历史夹具里正是它
  C1 start 判据放宽成恒真              → 期望存活
  C2 契约表判据放宽成恒真              → 期望存活
  C3 检测器把 read_text 也当文本源       → 期望**被杀**:读 markdown /
                                       运行时输出本来 legit(r80 普查发现
                                       8 处这类),守卫扫进来就是逼人
                                       绕过它
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent

PROTOCOL = REPO / "arl_lite" / "devloop" / "protocol.py"
WATCHER = REPO / "arl_lite" / "core" / "watcher.py"
CRIT_AUDIT = REPO / "tests" / "test_round_commit_audit.py"
CRIT_WATCHER = REPO / "tests" / "test_watcher_new_count.py"
CRIT_GUARD = REPO / "tests" / "test_source_checks_are_structural.py"

# 一次跑全:三处修复所在的文件 + 守卫本身
TARGETS = [
    "tests/test_round_commit_audit.py",
    "tests/test_watcher_new_count.py",
    "tests/test_cli_limit_honesty.py",
    "tests/test_source_checks_are_structural.py",
]

COLLECTION_FAILED = ("error during collection", "ERROR collecting", "Interrupted:")

# ── 实现侧锚点(整句照抄,含结尾括号) ──

REAL_START = '        start = prev.get("started_at")\n'
REAL_TABLES = "        asset_tables = tuple(Monitor._ASSET_TABLES.values())\n"

# ── 判据侧锚点 ──

JUDGE_START = (
    '    key = _get_key(assigns[0].value, "prev")\n'
    '    assert key == "started_at", (\n'
    "        f\"窗口起点取的是 prev 的 {key!r},不是 'started_at' —— \"\n"
    "        f\"实际写的是 {ast.unparse(assigns[0].value)!r}\"\n"
    "    )"
)
JUDGE_CONTRACT = (
    "    assert _contract_refs(fn), (\n"
    '        "_run_target 里没引用契约表 —— 资产清单从别处来了")'
)
GUARD_FUNCS = '_TEXT_FUNCS = {"unparse", "get_source_segment", "getsource"}'
GUARD_RETURN = "    return hits"

# r79 那段子串判据,逐字 —— M4 要把它整段塞回去
R79_AUDIT_JUDGE = (
    '    seg = ast.get_source_segment(src, fn)\n'
    "    assert 'prev.get(\"started_at\")' in seg, (\n"
    '        "窗口起点用的不是上一轮的 started_at"\n'
    "    )\n"
    "    assert 'prev.get(\"finished_at\")' not in seg, (\n"
    '        "窗口起点用了 finished_at —— 会把「跑完之后才提交」判成异常"\n'
    "    )"
)
R80_AUDIT_JUDGE = (
    "    assigns = [n for n in ast.walk(fn)\n"
    "               if isinstance(n, ast.Assign)\n"
    '               and any(getattr(t, "id", None) == "start" for t in n.targets)]\n'
    '    assert len(assigns) == 1, f"start 被赋值 {len(assigns)} 次,窗口起点不唯一"\n'
    "    key = _get_key(assigns[0].value, \"prev\")\n"
    "    assert key == \"started_at\", (\n"
    "        f\"窗口起点取的是 prev 的 {key!r},不是 'started_at' —— \"\n"
    "        f\"实际写的是 {ast.unparse(assigns[0].value)!r}\"\n"
    "    )\n"
    "    # 另一半:整个函数里不许再从 prev 取 finished_at 来当窗口边界\n"
    "    fetched = {k for n in ast.walk(fn)\n"
    "               if (k := _get_key(n, \"prev\")) is not None}\n"
    '    assert "finished_at" not in fetched, (\n'
    '        "窗口起点用了 finished_at —— 会把「跑完之后才提交」判成异常")'
)


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

    这是 r80 动手前实测出的假绿手法。子串判断扛不住它,AST 判定应该能杀。
    """
    src = path.read_text(encoding="utf-8")
    out = src.replace(REAL_START, "        start = 0.0\n", 1)
    if REAL_START in out:
        raise AssertionError("M2 注入后真调用还在,没删干净")
    anchor = '        prev = prev.to_dict() if hasattr(prev, "to_dict") else dict(prev)\n'
    if anchor not in out:
        raise AssertionError("M2 注入点没命中:prev 那行不在了")
    out = out.replace(
        anchor,
        anchor
        + "        # 变异注入: prev.get(\"started_at\") prev.get(\"finished_at\")\n",
        1,
    )
    _write_checked(path, out)


def _m3_handwritten_tables(path: pathlib.Path) -> None:
    """r52 原 bug 的形状:契约表派生换成**手抄清单**。

    抄的是那 5 个真表名,所以计数行为完全正确 —— r80 实测此时只有
    2 条测试会红,而两条子串判据照样 PASSED。只有结构判据能逮住它。
    """
    src = path.read_text(encoding="utf-8")
    out = src.replace(
        REAL_TABLES,
        '        _hint = "Monitor._ASSET_TABLES"  # 变异:喂饱子串判据\n'
        '        asset_tables = ("domains", "hosts", "ports", "sites", "findings")\n',
        1,
    )
    _write_checked(path, out)


MUTANTS = [
    ("M1-删掉真调用", lambda p: _apply(p, REAL_START, "        start = 0.0\n"),
     False, (PROTOCOL,)),
    ("M2-真调用删掉+注释喂饱(原bug形状)", _m2_comment_feeds_the_old_check,
     False, (PROTOCOL,)),
    ("M3-契约表换手抄5元组+字面量喂饱(原bug形状)", _m3_handwritten_tables,
     False, (WATCHER,)),
    ("M4-判据整段退回r79子串写法", lambda p: _apply(
        p, R80_AUDIT_JUDGE, R79_AUDIT_JUDGE), False, (CRIT_AUDIT,)),
    ("M5-检测器恒返回空", lambda p: _apply(
        p, GUARD_RETURN, "    return []  # 变异"), False, (CRIT_GUARD,)),
    ("M6-检测器漏掉get_source_segment", lambda p: _apply(
        p, GUARD_FUNCS, '_TEXT_FUNCS = {"unparse", "getsource"}'), False, (CRIT_GUARD,)),
]

# ---- 覆盖变异:改坏判据自己,或把守卫改得过宽 ----
COVERAGE_MUTANTS = [
    ("C1-start判据放宽成恒真", lambda p: _apply(
        p, JUDGE_START, '    assert True, "  # 变异"'), True, (CRIT_AUDIT,)),
    ("C2-契约表判据放宽成恒真", lambda p: _apply(
        p, JUDGE_CONTRACT, '    assert True, "  # 变异"'), True, (CRIT_WATCHER,)),
    ("C3-检测器把read_text也当文本源", lambda p: _apply(
        p, GUARD_FUNCS,
        '_TEXT_FUNCS = {"unparse", "get_source_segment", "getsource", "read_text"}'),
     False, (CRIT_GUARD,)),
]


def _run_criterion():
    return subprocess.run(
        [sys.executable, "-B", "-m", "pytest", *TARGETS, "-q", "-p", "no:cacheprovider"],
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
        ("覆盖变异", COVERAGE_MUTANTS),
    ):
        print(f"\n=== r80 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:40s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:400])
    print(f"\nr80 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
