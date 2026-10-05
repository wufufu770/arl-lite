"""r90 变异测试:验「把异常当没事接住」的登记制判据在守。

## 主题

r89 修掉了 `watch start` 那两处「拿超时当机制」的缺陷。r90 顺势全库扫
这个形状,实测 16 处站点、0 处同类缺陷 —— 是个该被固化的**否定结果**。

所以判据不是「禁止这个形状」(会误伤绝大多数正当用法,r80 的结论),而是
**登记制**:每处都得说明为什么可以这么接,新增的必须登记,过期的必须删。

## 这条判据最容易出的两个错,都在本文件的变异里

1. **同形状站点塌成一处**。第一版用 set 收 (文件, 函数, 异常, 动作),
   而 test_phase5 那个函数里有两处**完全一样**的 `except ValueError: pass`
   —— 塌成一处,于是「少登记一处」根本看不出来。判据不许恒真要防的正是
   这个,`test_same_shape_sites_in_one_function_stay_separate` 钉住它。

2. **检测器自己空转**。`test_the_detector_finds_every_real_site` 拿现场
   16 个站点当正控制组,数不对就报红。

## 变异清单

实现变异(期望全被杀):
  M1 去掉组内出现次序 → 同形状两处塌成一处
  M2 放宽动作类型(把「记了一笔」也算成静默接住)→ 多扫出一批
  M3 放宽「body 只有一条」的限制        → 多扫出一批
  M4 登记表删掉一条                     → 现场有、登记里没有,报红
  M5 登记表加一条不存在的               → 过期登记,报红
  M6 理由下限从 10 字符放宽到 1         → 「没写理由」也能登记
  M7 检测器数错(把正控制组的数写死成 18)→ 现场是 19,报红
     [r105:现场数从 17 变 19,因为 r105 往登记表加了两处 `except: continue`。     **锚点绑死在源码原文上**,改源码就得改这里 —— 这是第三次栽(r99/r100 各一次)]

覆盖变异(期望全存活):
  C1 把「新增站点」那一侧改成忽略

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
CLAIMS 只写字面量。锚点必须**括号配平且唯一**。
所有变异统一走 _write_checked,写盘前 ast.parse。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CRIT = REPO / "tests" / "test_swallowed_exceptions_are_accounted_for.py"

TARGET = ["tests/test_swallowed_exceptions_are_accounted_for.py"]

COLLECTION_FAILED = ("error during collection", "ERROR collecting", "Interrupted:")

# ── 锚点(整体照抄,括号配平)──

SILENT_STMTS = '_SILENT_STMTS = (ast.Pass, ast.Continue, ast.Return)\n'
SILENT_STMTS_LOOSE = '_SILENT_STMTS = (ast.Pass, ast.Continue, ast.Return, ast.Break)\n'

# M3:真的放宽「body 只有一条」。第一版只改了 len 检查,**没改下面那行
# `isinstance(node.body[0], _SILENT_STMTS)`** —— 那才是真正的闸门,于是
# 净效果为零,变异存活。半假变异又一次:名字说的和代码做的不是一回事
# (跟 r87 那条 M3 注释写「落在私有地」而实际落在真实 /tmp 一个毛病)。
BODY_LEN_CHECK = (
    "            if not isinstance(node, ast.ExceptHandler) or len(node.body) != 1:\n"
    "                continue\n"
)
BODY_LEN_LOOSE = (
    "            if not isinstance(node, ast.ExceptHandler):\n"
    "                continue\n"
)
STMT_PICK = (
    "            st = node.body[0]\n"
    "            if not isinstance(st, _SILENT_STMTS):\n"
    "                continue\n"
)
STMT_PICK_LOOSE = (
    "            st = next((s for s in node.body if isinstance(s, _SILENT_STMTS)), None)\n"
    "            if st is None:\n"
    "                continue\n"
)

SEEN_LINE = "    seen: dict[tuple[str, str, str, str], int] = {}\n"
OCCURRENCE = "            seen[base] = seen.get(base, 0) + 1\n            out.add((*base, seen[base]))\n"
NO_OCCURRENCE = "            out.add((*base, 1))\n"

# C1 的锚点必须是**完整配平**的语句。多行 `assert ..., (` 天生不配平,
# r81 的半句锚点守卫会判掉(r90 头一版就是这么栽的)。所以连函数头、
# docstring、赋值一起照抄 —— 配平、且唯一。
PINNED_HEAD = (
    "def test_the_derived_sites_are_pinned():\n"
    '    """现场站点必须与登记表**完全相等** —— 多一处少一处都报红"""\n'
    "    found = swallowed_sites()\n"
)
PINNED_HEAD_COVERED = (
    "def test_the_derived_sites_are_pinned():\n"
    '    """现场站点必须与登记表**完全相等** —— 多一处少一处都报红"""\n'
    "    found = set(ACCOUNTED)  # 变异:只比登记侧,现场多出来的看不见\n"
)

REASON_MIN = "    return [f\"{k}: {v!r}\" for k, v in reasons.items() if len(v.strip()) < 10]\n"
REASON_MIN_LOOSE = "    return [f\"{k}: {v!r}\" for k, v in reasons.items() if len(v.strip()) < 1]\n"

SITE_COUNT = '    assert len(found) == 19, f"现场站点数不对:{len(found)}(逐条对一遍登记项)"\n'
SITE_COUNT_WRONG = '    assert len(found) == 18, f"现场站点数不对:{len(found)}(逐条对一遍登记项)"\n'

# 登记表里删掉 / 加一条
ONE_ENTRY = (
    '    ("test_sql_injection.py", "test_all_attacks_blocked", "ValueError", "pass", 1):\n'
    '        "任何 ValueError 都算拦住了,没抛的进 missed 列表由断言报出来",\n'
)
PHANTOM_ENTRY = (
    '    ("test_arl_lite.py", "cmd_totally_absent", "RuntimeError", "pass", 1):\n'
    '        "这个变异压根不存在,用来证明过期登记会被报出来",\n'
)

CLAIMS = {
    "M1-去掉组内出现次序同形状两处塌成一处": (
        ["out.add((*base, 1))"],
        ["seen[base] = seen.get(base, 0) + 1"],
    ),
    "M2-放宽动作类型把记一笔也算成静默接住": (
        ["ast.Pass, ast.Continue, ast.Return, ast.Break"],
        ["_SILENT_STMTS = (ast.Pass, ast.Continue, ast.Return)\n"],
    ),
    "M3-放宽body只有一条的限制": (
        ["st = next((s for s in node.body if isinstance(s, _SILENT_STMTS)), None)"],
        ["st = node.body[0]"],
    ),
    "M4-登记表删掉一条": (
        [],
        ['("test_sql_injection.py", "test_all_attacks_blocked", "ValueError", "pass", 1)'],
    ),
    "M5-登记表加一条不存在的": (
        ['"这个变异压根不存在,用来证明过期登记会被报出来"'],
        [],
    ),
    # must_have 用带右括号的 `]`,光写 `< 1` 会是 `< 10` 的子串 ——
    # 改前就"存在",声明没有判别力(r87 栽过一次)
    "M6-理由下限从10字符放宽到1": (
        ["len(v.strip()) < 1]"],
        ["len(v.strip()) < 10]"],
    ),
    "M7-检测器数错写死成18": (
        ["assert len(found) == 18,"],
        ["assert len(found) == 19,"],
    ),
    "C1-新增站点那一侧改成忽略": (
        ["found = set(ACCOUNTED)  # 变异:只比登记侧,现场多出来的看不见"],
        # must_not 只能留空:替换块把原来的 docstring 行**原样带上了**,
        # 拿它当 must_not 会报「仍在」。跟 r88 的 M1、r89 的 M1/M2 同一个坑,
        # 第三次栽在同一处 —— 判别力全放 must_have。
        [],
    ),
}

MUTANTS = [
    ("M1-去掉组内出现次序同形状两处塌成一处",
     lambda p: _apply(p, OCCURRENCE, NO_OCCURRENCE), False, (CRIT,)),
    ("M2-放宽动作类型把记一笔也算成静默接住",
     lambda p: _apply(p, SILENT_STMTS, SILENT_STMTS_LOOSE), False, (CRIT,)),
    ("M3-放宽body只有一条的限制",
     lambda p: _apply(p, BODY_LEN_CHECK, BODY_LEN_LOOSE,
                      also=(STMT_PICK, STMT_PICK_LOOSE)), False, (CRIT,)),
    ("M4-登记表删掉一条",
     lambda p: _apply(p, ONE_ENTRY, ""), False, (CRIT,)),
    ("M5-登记表加一条不存在的",
     lambda p: _apply(p, ONE_ENTRY, ONE_ENTRY + PHANTOM_ENTRY), False, (CRIT,)),
    ("M6-理由下限从10字符放宽到1",
     lambda p: _apply(p, REASON_MIN, REASON_MIN_LOOSE), False, (CRIT,)),
    ("M7-检测器数错写死成18",
     lambda p: _apply(p, SITE_COUNT, SITE_COUNT_WRONG), False, (CRIT,)),
]

COVERAGE_MUTANTS = [
    ("C1-新增站点那一侧改成忽略",
     lambda p: _apply(p, PINNED_HEAD, PINNED_HEAD_COVERED), True, (CRIT,)),
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
    import ast
    if out == path.read_text(encoding="utf-8"):
        raise AssertionError("变异是空操作 —— 什么都不会变,写了也是白写")
    try:
        ast.parse(out)
    except SyntaxError as e:
        raise AssertionError(f"变异会让 {path.name} 语法错误({e})。文件未写入。") from None
    path.write_text(out, encoding="utf-8")


def _apply(path: pathlib.Path, old: str, new: str, also: tuple | None = None) -> None:
    src = path.read_text(encoding="utf-8")
    if old not in src:
        raise AssertionError(f"变异锚点没命中 {path.name}:{old[:70]!r}")
    if also is not None and also[0] not in src:
        raise AssertionError(f"变异附加锚点没命中 {path.name}:{also[0][:70]!r}")
    out = src.replace(old, new, 1)
    if also is not None:
        out = out.replace(also[0], also[1], 1)
    _write_checked(path, out)


def _run_criterion():
    return subprocess.run(
        [sys.executable, "-B", "-m", "pytest", *TARGET, "-q", "-p", "no:cacheprovider"],
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
            r = _run_criterion()
            outp = r.stdout + r.stderr
            if any(bad in outp for bad in COLLECTION_FAILED):
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
        print(f"\n=== r90 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:38s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:300])
    print(f"\nr90 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.sandboxed(main))
