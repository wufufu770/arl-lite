"""r83 变异测试:验「变异自检声明」这套机制本身在守。

主题:变异**存活**时,答案永远是「名字和实现不一致」,不是「判据错了」。
可我每次都要重新推一遍才能确认是哪一边错了 —— 已栽五次(r73 M2/M4、
r74 M5、r78 M2、r82 M3)。这套机制要消掉的就是那次重推。

## 机制

每条变异在 `CLAIMS` 里带一份声明 `(必须出现, 必须消失)`。跑判据**之前**:

    _check_claim_points_at_one_place(名字, 改前的文件)   # 声明指得准吗
    mutate(目标)
    _verify_claim(名字, 改后的文件)                       # 替换真做到了吗

## 这轮顺手逮到的第二个缺陷:r78 判据的 docstring 说了代码没有的规则

r78 那条自检判据的 docstring 声称区分「危险写」和「还原写」,说还原写
(`finally: write_bytes(backup)`)不可能引入语法错误、不该报红。

**代码里从来没有这条豁免。** `_sweep` 一直没被误报,只是因为它当时恰好
没调 replace/split/join。r83 给它加了一行 `"\n".join(...)` 去读被改动的
文件(校验声明要用),`transforms()` 立刻成立,缺口当场顶出来。

跟 r80 那条是同一个病:注释/文档描述了代码没有的行为,于是「有这条规则」
和「这条规则真的在跑」被当成了一回事。这次把豁免**实现**出来,并且配
正/负控制组 —— 豁免是放宽,放宽最容易被写成新的假绿,所以两个方向都得钉:
真的还原写放行,伪装成还原写的危险写(`data.encode()`、`t.read_text().replace(...)`)
必须仍然被逮住。实现取 fail-closed:认不出实参是什么就不豁免。

## 这轮第三次栽在「凭印象写」上

写判据时用了 `q.add(item_id, "标题", ...)` 与 `q.claim(item_id, owner=...)`
(r82 就栽过一次),还有 `_root_name` 只认 Name 不认 `backup[t]` 下标 ——
控制组当场把它照出来。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
所有变异统一走 _write_checked,写盘前 ast.parse。

全部变异:
  M1 声明放宽成两边都空            → 杀
  M2 声明改成改前也成立(不区分)     → 杀
  M3 声明串指不准(原文件里出现多次)  → 杀
  M4 删掉某条变异的声明             → 杀
  M5 机制没接进 _sweep              → 杀
  M6 还原写豁免改成「认不出也放行」   → 杀(负控制组逮住)
  C1/C2 放宽判据自己的两条断言        → **被杀**,但死在声明记账上,
                                       不是死在它们想放宽的那条 ——
                                       按 r73 的老规矩记成否定结果
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent

R82 = REPO / "devloop" / "mutcheck_r82.py"
SELFCHK = REPO / "tests" / "test_mutcheck_scripts_are_self_checking.py"
CLAIMS_CRIT = REPO / "tests" / "test_mutcheck_claims_match_their_names.py"

TARGET = [
    "tests/test_mutcheck_claims_match_their_names.py",
    "tests/test_mutcheck_scripts_are_self_checking.py",
]

COLLECTION_FAILED = ("error during collection", "ERROR collecting", "Interrupted:")

# ── 锚点:全部从真实文件里逐字抠出来 ──

# CLAIM_M3
ANCHOR_CLAIM_M3 = (
    '    "M3-判据换回owner进程已退出(恒红那版)": (\n'
    '        # 带换行:光写 `if by_id and by_id.owner:` 会被\n'
    '        # `if by_id and by_id.owner_alive_dead_flag_placeholder:` 喂饱 ——\n'
    '        # 那正是 r82 那版假实现。声明串必须指到「换行处」才算指得准。\n'
    '        ["if by_id and by_id.owner:\\n", "owner_alive(by_id.owner)"],\n'
    '        ["claimed_at < last_finished", "owner 仍在运行", "owner 探测不到"],\n'
    '    ),\n'
)


# CLAIM_M1
ANCHOR_CLAIM_M1 = (
    '    "M1-跨轮判据放宽成恒真": (\n'
    '        [],\n'
    '        ["claimed_at < last_finished", "owner 探测不到,确认下是不是忘了收尾"],\n'
    '    ),\n'
)


# WIRED
ANCHOR_WIRED = (
    '            _verify_claim(name, "\\n".join(\n'
    '                t.read_text(encoding="utf-8") for t in targets))\n'
)


# EXEMPT_FAILCLOSED
ANCHOR_EXEMPT_FAILCLOSED = (
    '            if root is None:\n'
    '                return False\n'
)


# DISCRIM
ANCHOR_DISCRIM = (
    '            if not new_bits and not gone_bits:\n'
    '                flat.setdefault(s.name, []).append(name)\n'
)


# ── 本脚本自己的声明 ──
#
# 写这轮判据时它立刻报了我的新脚本一条红:「每个变异都得有声明」——
# 机制对**新写的脚本**同样适用,不管写它的人是不是刚写完这条规则。
# 声明照 r82 的办法从每个变异的**真实 diff** 导出,不凭印象写。
CLAIMS = {
    # must_not 一律用**被删掉的那一整行**:短片段(比如
    # `claimed_at < last_finished`)在同一个文件里出现多次,指不准位置,
    # 会被同名兄弟喂饱 —— 这正是 r83 第一版 4 条声明全栽在这。
    "M1-声明放宽成两边都空": (
        [],
        ['        ["claimed_at < last_finished", "owner 探测不到,确认下是不是忘了收尾"],'],
    ),
    "M2-声明改成改前也成立": (
        [],
        ['        ["claimed_at < last_finished", "owner 仍在运行", "owner 探测不到"],'],
    ),
    "M3-声明串指不准(出现多次)": (
        [],
        ["        # 带换行:光写 `if by_id and by_id.owner:` 会被"],
    ),
    "M4-删掉某条变异的声明": (
        ["# 变异:M2 的声明被删了"],
        ['        ["# 变异:不点名了"],'],
    ),
    "M5-机制没接进sweep": (
        ["# 变异:不验声明了"],
        ['                t.read_text(encoding="utf-8") for t in targets))'],
    ),
    "M6-还原写豁免改成认不出也放行": (
        ["# 变异:一律放行"],
        ["            if root is None:"],
    ),
    "C1-放宽「声明能区分改前改后」(被声明记账杀)": (
        ["# 变异:恒真"],
        ["            if not new_bits and not gone_bits:"],
    ),
    "C2-放宽「声明不能两边都空」(被声明记账杀)": (
        ["bad = []  # 变异:恒真"],
        ["        bad = [n for n, pair in claims.items() if not any(pair)]"],
    ),
}


def _check_claim_points_at_one_place(name: str, original: str) -> None:
    if name not in CLAIMS:
        raise AssertionError(f"变异 {name!r} 没有自检声明")
    _, must_not = CLAIMS[name]
    for s in must_not:
        n = original.count(s)
        if n != 1:
            raise AssertionError(
                f"变异 {name!r} 的声明串 {s!r} 在改前出现 {n} 次 —— "
                "指不准被替换的那一处,会被同名兄弟喂饱")


def _verify_claim(name: str, mutated: str) -> None:
    if name not in CLAIMS:
        raise AssertionError(f"变异 {name!r} 没有自检声明")
    must_have, must_not = CLAIMS[name]
    missing = [s for s in must_have if s not in mutated]
    leftover = [s for s in must_not if s in mutated]
    if missing or leftover:
        raise AssertionError(
            f"变异 {name!r} 的替换没有做到它名字声称的事:"
            f" 缺 {missing} 仍在 {leftover}")


MUTANTS = [
    # M1: 声明两边都空 —— 判据里的「声明不能两边都空」必须报红
    ("M1-声明放宽成两边都空", lambda p: _apply(
        p, ANCHOR_CLAIM_M1,
        '    "M1-跨轮判据放宽成恒真": (\n        [],\n        [],\n    ),\n'),
     False, (R82,)),
    # M2: 声明改成改前也成立(只写一条本来就有的)—— 「能区分改前改后」必须报红
    ("M2-声明改成改前也成立", lambda p: _apply(
        p, ANCHOR_CLAIM_M3,
        '    "M3-判据换回owner进程已退出(恒红那版)": (\n'
        '        ["import pathlib"],\n'
        '        ["import pathlib"],\n'
        '    ),\n'),
     False, (R82,)),
    # M3: 声明串指不准 —— 在原文件里出现三次,拆一条另外两条还在
    ("M3-声明串指不准(出现多次)", lambda p: _apply(
        p, ANCHOR_CLAIM_M3,
        '    "M3-判据换回owner进程已退出(恒红那版)": (\n'
        '        ["if by_id and by_id.owner:' + chr(92) + 'n", "owner_alive(by_id.owner)"],\n'
        '        ["LEFTOVER_HINT in out"],\n'
        '    ),\n'),
     False, (R82,)),
    # M4: 直接删掉 M2 那条声明 —— 「每个变异都得有声明」必须报红
    ("M4-删掉某条变异的声明", lambda p: _apply(
        p, '    "M2-收尾只报计数不点名": (\n'
           '        ["# 变异:不点名了"],\n'
           '        ["{iid} ← {owner or \'(无主)\'}{age}{why}"],\n'
           '    ),\n',
        "    # 变异:M2 的声明被删了\n"),
     False, (R82,)),
    # M5: 机制定义出来了却没接进 _sweep —— 最容易骗过自己的一种写法
    ("M5-机制没接进sweep", lambda p: _apply(
        p, ANCHOR_WIRED, "            pass  # 变异:不验声明了\n"), False, (R82,)),
    # M6: 还原写豁免改成「认不出来也放行」—— 负控制组必须逮住
    ("M6-还原写豁免改成认不出也放行", lambda p: _apply(
        p, ANCHOR_EXEMPT_FAILCLOSED, "            if False:  # 变异:一律放行\n"
                                    "                return False\n"),
     False, (SELFCHK,)),
]

# ---- 覆盖变异:改坏判据自己,期望**存活**(真实现本来就是对的) ----
JUDGE_DISCRIM_BODY = (
    "            if not new_bits and not gone_bits:"
)
JUDGE_DISCRIM_REPL = (
    "            if False:  # 变异:恒真"
)
JUDGE_NONEMPTY = (
    "        bad = [n for n, pair in claims.items() if not any(pair)]"
)
JUDGE_NONEMPTY_REPL = (
    "        bad = []  # 变异:恒真"
)

# ── 覆盖变异:本以为「放宽判据自己」会存活,实测**被别的判据杀了** ──
#
# 这是本轮第二个有价值的发现,而且方向跟预期相反:
#
#   C1 放宽「声明能区分改前改后」 → 实测被 test_claims_point_at_exactly_one_place 杀
#   C2 放宽「声明不能两边都空」   → 实测被上面那条 + test_claims_can_tell_before_from_after 杀
#
# 原因不是「判据不够严」,而是**声明机制自指地兜住了它**:C1/C2 要替换的
# 那一行,正好是 r83 自己 C1/C2 声明里的 `must_not` 串。改坏判据的同时也
# 破坏了声明记账,于是声明检查当场报红。
#
# 按 r73 立的老规矩,「覆盖变异被另一道闸兜住」是**有价值的否定结果**,
# 如实记着,不改成「期望存活」把账做平。所以它们留在这一组,但期望值
# 改成被杀,组标题也照实写。
COVERAGE_MUTANTS = [
    ("C1-放宽「声明能区分改前改后」(被声明记账杀)", lambda p: _apply(
        p, JUDGE_DISCRIM_BODY, JUDGE_DISCRIM_REPL), False, (CLAIMS_CRIT,)),
    ("C2-放宽「声明不能两边都空」(被声明记账杀)", lambda p: _apply(
        p, JUDGE_NONEMPTY, JUDGE_NONEMPTY_REPL), False, (CLAIMS_CRIT,)),
]


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
            _check_claim_points_at_one_place(
                name, "\n".join(t.read_text(encoding="utf-8") for t in targets))
            for t in targets:
                mutate(t)
            _verify_claim(name, "\n".join(
                t.read_text(encoding="utf-8") for t in targets))
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
        ("覆盖变异(本以为存活,实测被别的判据杀了)", COVERAGE_MUTANTS),
    ):
        print(f"\n=== r83 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:36s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:400])
    print(f"\nr83 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
