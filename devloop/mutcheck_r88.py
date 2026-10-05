"""r88 变异测试:验「每条声明都得有一个真的存在的变异」在守。

## 主题

r83 立的 `CLAIMS` 机制有个方向性的缺口:`test_every_mutant_has_a_claim`
守的是「变异 ⊆ 声明」,**反方向没守** —— 于是「声明里有一条根本不对应
任何变异」畅通无阻。

r87 自己就踩了:那个脚本把实现变异 M2 改名成 C2 挪进覆盖变异,两个列表
都改了,`CLAIMS` 忘了改,留下一条指向不存在变异的声明。当时全部判据全绿。

同一个病(r83 逮到过的「文档描述了代码没有的东西」)出在**专门治这个病
的机制自己身上**。

## 本轮实测(不是推测)

新判据一加上就红,且**只红一条**:

    AssertionError: 这些声明指向的变异不存在于 MUTANTS/COVERAGE_MUTANTS:
    {'mutcheck_r87.py': ['M2-快照目标写死成私有地(r87第一版的错)']}

47 个脚本里就这一处。说明这不是普遍存在的脏数据,而是「改动时漏了一处」
的典型形态 —— 也说明这条判据有判别力:它能指名道姓地说出是哪条、哪个脚本。

## 顺带的否定结果:一个很自然但很糟的守卫

本轮先试过另一条更宽的守卫:**docstring 里提到的每个变异编号都必须存在**。
实测它会误伤 —— 而且误伤的是好东西:

  - `mutcheck_r73.py`「M4 **也删了**:workspaces 表只在 default 库里」
  - `mutcheck_r76.py`「M5 … → 期望存活?**不** —— 那会让真空队列也报错」

这两处是在记录**被否决的变异**,是有价值的工程痕迹,不是漂移。按 r80 的
结论(守卫不能比它守的东西还严,否则有人绕过它),那条守卫**没有写**。
r88 只做 CLAIMS 那一侧 —— 它没有这种歧义:CLAIMS 是结构化字典,
「键指向不存在的列表项」没有「合理的历史叙述」这种解释空间。

## 变异清单

实现变异(期望全被杀,4 条):
  M1 给 r87 的 CLAIMS 加一条指向不存在变异的声明
  M2 从 MUTANTS 删掉一个变异、但留着它的声明(r87 原样)
  M3 从 COVERAGE_MUTANTS 删掉一个变异、但留着它的声明
  M4 把反向判据的推导式改成跟正向那条一模一样,让它不再查任何新东西

## 一个自指障碍:C1/C2 被砍掉了,原因值得记一笔

本来照例该配两条覆盖变异(「反向判据放宽」「正向判据放宽」),
两条实测都是 **killed** —— 但**不是被它们要测的那条判据杀的**,
是被 `test_claims_point_at_exactly_one_place` 杀的。

原因是一条绕不开的自指:r83 那套 `CLAIMS` 机制规定每条声明的 `must_not`
串必须在目标文件里**恰好出现一次**,而 `test_claims_point_at_exactly_one_place`
会扫描**所有** mutcheck 脚本去核这件事 —— 包括本脚本。于是:

    覆盖变异把判据里那行 `claimed = [n for n in claims if not ...]` 换成
    `claimed = []`
        → 本脚本 CLAIMS 里那条 must_not 串在目标文件里出现次数变成 0
        → 元判据报红
        → run 记成「覆盖变异被杀」,而它压根没碰到反向判据

**结论:凡是「声明了 claims、又要改这份 claims 所校验的那个文件」的
变异,都会被元判据先拦下来。** 所以 r88 没有覆盖变异这一栏 ——
不是偷懒,是这一类变异在这套机制下**结构上无法表达**。
如实记,补一条假装能跑的更糟。

## 顺带:这条判据在写完当天就逮到了它自己的作者

r88 加完判据、跑完变异之后,全套自检判据一起跑,红了:

    这些声明指向的变异不存在于 MUTANTS/COVERAGE_MUTANTS:
    {'mutcheck_r88.py': ['C1-反向判据放宽成恒真', 'C2-正向判据放宽']}

原因就是上面那件事 —— 本轮砍掉了覆盖变异,`CLAIMS` 里那两条声明忘了删。
**r87 留了个 M2 声明、r88 留了两个 C 声明,同一个错误连着犯了两次,
两次都是被同一条判据逮住的。** 之前没人守这个方向,所以它一直能犯。

## 写 CLAIMS 时踩的两个坑(都是纯插入/纯删除的必然结果)

- **纯插入**不能写 must_not:新串把原条目原样带上了,原条目改后仍在,
  `_verify_claim` 直接报「仍在」,变异被判成 BAD-MUTANT(M1 上栽了一次)
- **纯删除**不能写 must_have:它不引入任何新串。写成别的变异的串
  就等于给这条变异安了一条跟它无关的声明(M2/M3 上各栽了一次)

## 另一个:锚点必须括号配平

早先写 `CLAIMS_OPEN = "CLAIMS = {\n"`,被 r81 的
`test_modern_mutcheck_scripts_never_use_a_half_anchor` 当场判成半句 ——
判得对,只抄了开括号没抄闭括号。改成照抄一个完整、无注释的字典条目。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
CLAIMS 只写字面量 —— 这一条 r86 写过、r87 抄了、r88 又犯了一次。
所有变异统一走 _write_checked,写盘前 ast.parse。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CRIT = REPO / "tests" / "test_mutcheck_claims_match_their_names.py"
VICTIM = REPO / "devloop" / "mutcheck_r87.py"

TARGET = ["tests/test_mutcheck_claims_match_their_names.py"]

COLLECTION_FAILED = ("error during collection", "ERROR collecting", "Interrupted:")

# ── 判据侧锚点(整体照抄完整语句)──

REVERSE_ASSERT = "        claimed = [n for n in claims if n not in mutant_names(tree)]\n"
REVERSE_ASSERT_NEUTERED = "        claimed = []  # 变异:反向不变量被拆了\n"

# 原判据的那一行,用来把方向改回去
FORWARD_ASSIGN = "        unclaimed = [n for n in mutant_names(tree) if n not in claims]\n"
FORWARD_ASSIGN_NEUTERED = "        unclaimed = []  # 变异:正向判据被拆了\n"

# M1:往 r87 的 CLAIMS 里塞一条指向不存在变异的声明。
# 锚点取**完整且无注释的一个字典条目**(r81 的守卫要求括号配平:
# 早先写 `CLAIMS = {\n` 被判成半句,判得对)。
R87_M5_CLAIM_ENTRY = (
    '    "M5-快照函数恒返回空集": (\n'
    '        ["return set()  # 变异:快照恒空"],\n'
    "        ['p.name.startswith(\"tmp\")'],\n"
    "    ),\n"
)
GHOST_CLAIM = (
    '    "M999-这条变异根本不存在": (\n'
    '        ["改了以后必须出现的那个串"],\n'
    '        ["改前就在的、必须消失的那个串"],\n'
    "    ),\n"
) + R87_M5_CLAIM_ENTRY

# ── 受害者侧锚点:拿 r87 当靶子,复现它犯过的那个错 ──

# M2:把 M3 从 MUTANTS 里摘掉,声明留着 —— r87 原样
R87_M3_ENTRY = (
    '    ("M3-noise不再往真实tmp种噪音", lambda p: _apply(\n'
    "        p, NOISE_MKDTEMP, NOISE_NO_PLANT,\n"
    '        also=(PRIVATE_REF_DECL, PRIVATE_REF_DECL_M3)), False, (CRIT,)),\n'
)
# M3:只删 COVERAGE_MUTANTS 里那一项,CLAIMS 里的 C2 声明留着
R87_C2_ENTRY = (
    '    ("C2-快照直接读TMPDIR而不问子进程", lambda p: _apply(\n'
    "        p, ASK_CHILD, HARDCODED_WHERE), True, (CRIT,)),\n"
)

# M4:判据实现里的**方向**:把「声明 ⊆ 变异」换成「变异 ⊆ 声明」,
# 让反向判据退化成正向那条已覆盖的判断。
REVERSE_COMPREHENSION = (
    "        claimed = [n for n in claims if n not in mutant_names(tree)]\n"
)
REVERSE_SAME_AS_FORWARD = (
    "        claimed = [n for n in mutant_names(tree) if n not in claims]\n"
)

CLAIMS = {
    "M1-给CLAIMS加一条指向不存在变异的声明": (
        # 纯插入,不删任何东西 —— 新串里把原条目**原样带上了**,所以
        # must_not 不能写那个条目(它改后仍然在,早先这么写导致
        # `_verify_claim` 报「仍在」,把这条变异判成 BAD-MUTANT)。
        ['"M999-这条变异根本不存在"'],
        [],
    ),
    "M2-删掉变异却留着声明(r87原样)": (
        # 纯删除,不会引入任何新串 —— must_have 只能是空侧,
        # 全部判别力放在 must_not 上。早先这里错写成了 M1 那条幽灵声明的
        # 字符串,跟本变异毫无关系。
        [],
        ['    ("M3-noise不再往真实tmp种噪音", lambda p: _apply(\n'],
    ),
    "M3-从COVERAGE_MUTANTS删掉变异却留着声明": (
        # 同样是纯删除(C2 只在 COVERAGE_MUTANTS 里,不在 MUTANTS)。
        # 要真做出「挪走」的形状得同时改两个列表,那就跟 M2 重复了。
        [],
        ['    ("C2-快照直接读TMPDIR而不问子进程", lambda p: _apply(\n'],
    ),
    "M4-反向判据退化成正向那条判断": (
        ["        claimed = [n for n in mutant_names(tree) if n not in claims]\n"],
        ["        claimed = [n for n in claims if n not in mutant_names(tree)]\n"],
    ),
    # r88 自己又栽了同一个病一次:C1/C2 两条声明在这里放了没用的,
    # 因为本轮**没有**覆盖变异。写完立刻被本轮新加的
    # `test_every_claim_has_a_mutant` 逮出来:
    #   {'mutcheck_r88.py': ['C1-反向判据放宽成恒真', 'C2-正向判据放宽']}
    # r87 留了个 M2 声明、r88 留了两个 C 声明 —— 同一个错误,连着两轮。
}

MUTANTS = [
    ("M1-给CLAIMS加一条指向不存在变异的声明",
     lambda p: _apply(p, R87_M5_CLAIM_ENTRY, GHOST_CLAIM), False, (VICTIM,)),
    ("M2-删掉变异却留着声明(r87原样)",
     lambda p: _apply(p, R87_M3_ENTRY, ""), False, (VICTIM,)),
    ("M3-从COVERAGE_MUTANTS删掉变异却留着声明",
     lambda p: _apply(p, R87_C2_ENTRY, ""), False, (VICTIM,)),
    # M4:把反向判据的推导式改成跟正向那条一模一样,于是它不再查任何
    # 新东西 —— 这是「方向写反」最典型的退化形态。
    ("M4-反向判据退化成正向那条判断",
     lambda p: _apply(p, REVERSE_COMPREHENSION, REVERSE_SAME_AS_FORWARD), False, (CRIT,)),
]

# 本轮**故意不定义** COVERAGE_MUTANTS。理由见 docstring「一个自指障碍」:
# 覆盖变异要改的那份判据文件,正是本脚本 CLAIMS 声明所校验的对象,
# 改动会让本脚本自己的 must_not 串失效,被元判据先拦下来。
# 留一个空列表假装「有这一栏」比不留更糟。


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


def _apply(path: pathlib.Path, old: str, new: str) -> None:
    src = path.read_text(encoding="utf-8")
    if old not in src:
        raise AssertionError(f"变异锚点没命中 {path.name}:{old[:70]!r}")
    _write_checked(path, src.replace(old, new, 1))


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
    # 本轮**没有**覆盖变异,原因见 docstring「一个自指障碍」一节。
    # 显式说明比留个空循环好 —— 否则下一个人会以为是漏写了。
    print("\n=== r88 实现变异(期望全被杀) ===")
    for name, detail, outp in _sweep(MUTANTS):
        ok = detail in ("killed", "survived")
        if not ok:
            bad += 1
        print(f"  {'OK ' if ok else '!! '}{name:40s} {detail}")
        if outp:
            print("     " + outp.replace("\n", "\n     ")[:400])
    print("\n=== r88 覆盖变异 ===")
    print("  本轮无覆盖变异:改这份判据的变异会被元判据先拦下,")
    print("  记成「被杀」而它压根没碰到要测的东西。详见本文件 docstring。")
    print(f"\nr88 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
