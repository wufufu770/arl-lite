"""r107 变异测试:验「轮次索引不会烂在那儿」在守。

## 主题

`devloop/ROUND_INDEX.md` 是 r107 新建的索引(从 `git log` 生成,每轮一行)。
**索引如果没有东西在守,它的失效方式和不存在一模一样。** r103 那句
「r53–r102 的来龙去脉只在不入库的队列文件里」能存在整整一轮,就是因为
没人查过「到底还剩多少索引」。

## 变异清单

索引变异(期望全被杀,改 devloop/ROUND_INDEX.md):
  M1 删掉中间一行(r90)        → 判据 5(覆盖率)红
  M2 把一个 hash 改成不存在的   → 判据 3(hash 真实)红
  M3 造一个重复轮次号          → 判据 2(唯一且递增)红
  M4 加一行不存在的 r999       → 判据 6(反向:不许收录不存在的轮次)红
  M5 整个索引清空              → 判据 1(非空前置)红

覆盖变异(期望全存活,拆判据):
  C1 拆掉「除最新一轮外都得收录」  ← M1 的唯一防线
  C2 拆掉「索引非空」              ← M5 的唯一防线

## M4 单独说一句

M1/M2/M3/M5 都是「少了什么」或「多了错的东西」。M4 是**凭空多一行
指向空气的记录** —— 只查「漏」不查「多」的话它过得去,而那比缺一行更难
发现:缺一行会让人去补,多一行指向 r999 会让人以为 r999 真做过。

## 判据里那条「放过最新一轮」不是漏洞

索引从 git log 生成,每一轮 commit 都让它当场过期。判据显式放过 HEAD
那一轮,代价是最多一轮的索引滞后,换来的是门禁不会每轮必红。
**一条永远红的门禁等于没有门禁** —— r104 刚为这件事付过代价。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
CLAIMS 只写字面量,不写名字引用。纯插入 must_not 留空、纯删除 must_have 留空。
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CRIT = REPO / "tests" / "test_round_index_stays_current.py"
INDEX = REPO / "devloop" / "ROUND_INDEX.md"

TARGET = ["tests/test_round_index_stays_current.py"]

_RUN_SUMMARY_RE = re.compile(r"^(?:=+\s*)?(.*\bin [\d.]+s.*)$", re.M)
_ERROR_IN_SUMMARY_RE = re.compile(r"\d+\s+errors?\b")


def _run_itself_broke(outp: str) -> bool:
    lines = _RUN_SUMMARY_RE.findall(outp)
    if not lines:
        return True
    tail = lines[-1]
    if "no tests ran" in tail:
        return True
    if _ERROR_IN_SUMMARY_RE.search(tail):
        return "AssertionError" not in outp
    return False


# ── 锚点:从索引文件逐行 repr 取 ──
A_R90 = ('| r90 | `00ccf80` | 「把异常当没事接住」全库普查 —— 16 处,0 处同类缺陷,'
         '改成登记制 | r89 修掉了 watch start 那两处「拿 subprocess.run(timeout=25) '
         '+ except TimeoutExpired: pass 当机制用」的缺陷。 |\n')
M1_GONE = "# 变异 M1:r90 那一行被删掉了\n"

A_R100 = ('| r100 | `09576d6` | r98 的守卫只扫 .py —— 而文档里坏掉的那两个字,'
          '正是「当初为什么这么改」 | devloop/backlog.md    4 个 U+FFFD '
          'devloop/queue.json    2 个 U+FFFD (r98 的守卫:一个都没算进去 —— '
          '它的范围是… |\n')
M2_FAKE = A_R100.replace("09576d6", "deadbee")

A_R106 = ('| r106 | `7fb5ff5` | r103 写下「记录只在不入库的队列里」—— 方向反了,'
          '记录在 commit message 里 | r103 收尾时我只查了 backlog.md 和 '
          'queue.json 就下了结论: |\n')
M3_DUP = A_R106 + ('| r106 | `7fb5ff5` | 变异 M3:同一个轮次号出现第二次 | '
                   '判据 2 要求唯一且严格递增 |\n')
M4_PHANTOM = A_R106 + ("| r999 | `0000000` | 变异 M4:凭空多一行指向空气的记录 | "
                       "git 里根本没有 r999 |\n")
M5_EMPTY = "# 变异 M5:索引被整个清空了\n"

# ── 覆盖变异 ──
C1_BODY = "def test_every_round_commit_except_the_newest_is_indexed():"
C1_END = "def test_the_index_does_not_claim_to_cover_rounds_that_do_not_exist():"
C2_BODY = "def test_the_index_is_not_empty():"
C2_END = "def test_round_numbers_are_unique_and_strictly_increasing():"

CLAIMS = {
    "M1-中间一行被删": (
        ["# 变异 M1:r90 那一行被删掉了"],
        ['| r90 | `00ccf80` |'],
    ),
    "M2-hash改成不存在的": (
        ["| r100 | `deadbee` |"],
        ["| r100 | `09576d6` |"],
    ),
    "M3-造重复轮次": (
        ["| r106 | `7fb5ff5` | 变异 M3:同一个轮次号出现第二次 |"],
        [],  # 纯插入
    ),
    "M4-凭空多一行": (
        ["| r999 | `0000000` | 变异 M4:凭空多一行指向空气的记录 |"],
        [],  # 纯插入
    ),
    "M5-索引被清空": (
        ["# 变异 M5:索引被整个清空了"],
        ["| r106 | `7fb5ff5` | r103 写下"],
    ),
    "C1-拆掉覆盖率那条": (["    pass  # 变异 C1:整条判据没了"], []),
    "C2-拆掉非空前置那条": (["    pass  # 变异 C2:整条判据没了"], []),
}

MUTANTS = [
    ("M1-中间一行被删", lambda p: _apply(p, A_R90, M1_GONE), False, (INDEX,)),
    ("M2-hash改成不存在的", lambda p: _apply(p, A_R100, M2_FAKE), False, (INDEX,)),
    ("M3-造重复轮次", lambda p: _apply(p, A_R106, M3_DUP), False, (INDEX,)),
    ("M4-凭空多一行", lambda p: _apply(p, A_R106, M4_PHANTOM), False, (INDEX,)),
    ("M5-索引被清空", lambda p: _write_all_gone(p), False, (INDEX,)),
]

COVERAGE_MUTANTS = [
    ("C1-拆掉覆盖率那条",
     lambda p: _replace_fn(p, C1_BODY, C1_BODY + "\n    pass  # 变异 C1:整条判据没了\n",
                           C1_END), True, (CRIT,)),
    ("C2-拆掉非空前置那条",
     lambda p: _replace_fn(p, C2_BODY, C2_BODY + "\n    pass  # 变异 C2:整条判据没了\n",
                           C2_END), True, (CRIT,)),
]


def _write_all_gone(path: pathlib.Path) -> None:
    """把索引整个清空(保留表头,免得文件变成语法错误似的东西)。"""
    src = path.read_text(encoding="utf-8")
    keep = [ln for ln in src.splitlines() if ln.startswith("# ") or ln.startswith("|---")]
    _write_checked(path, "\n".join(keep) + "\n# 变异 M5:索引被整个清空了\n")


def _check_claim_points_at_one_place(name: str, original: str) -> None:
    if name not in CLAIMS:
        raise AssertionError(f"变异 {name!r} 没有自检声明")
    must_have, must_not = CLAIMS[name]
    if not must_have and not must_not:
        raise AssertionError(f"变异 {name!r} 的声明是空的 —— 等于没声明")
    for piece in must_not:
        n = original.count(piece)
        if n != 1:
            raise AssertionError(
                f"变异 {name!r} 的声明串 {piece!r} 在改前出现 {n} 次 —— 指不准位置")
    for piece in must_have:
        if piece in original:
            raise AssertionError(
                f"变异 {name!r} 的 must_not/must_have 写反了:{piece!r} 改前就存在")


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
    if out == path.read_text(encoding="utf-8"):
        raise AssertionError("变异是空操作 —— 什么都不会变,写了也是白写")
    # 覆盖变异 C1/C2 改的是 `tests/test_round_index_stays_current.py`,
    # **是 .py** —— 语法守卫在这里是真需要的,不是形式。
    #
    # 首版我按「r107 只写 .md」把整段 ast.parse 删了,结果
    # `tests/test_mutcheck_scripts_are_self_checking.py` 报
    # 「没有任何『变换后写回』的语法守卫」。那条判据是对的而我是错的:
    # 我只看了实现变异(M1–M5 写 .md),漏了覆盖变异也走同一个写盘出口。
    # **同一个 `_write_checked` 承接两类目标时,守卫得按后缀分支,不能整段删。**
    if path.suffix == ".py":
        import ast
        try:
            ast.parse(out)
        except SyntaxError as e:
            raise AssertionError(
                f"变异会让 {path.name} 语法错误({e})。文件未写入。") from None
    path.write_text(out, encoding="utf-8")


def _apply(path: pathlib.Path, old: str, new: str) -> None:
    src = path.read_text(encoding="utf-8")
    n = src.count(old)
    if n != 1:
        raise AssertionError(
            f"变异锚点在 {path.name} 出现 {n} 次(必须恰好 1 次):{old[:70]!r}")
    _write_checked(path, src.replace(old, new, 1))


def _replace_fn(path: pathlib.Path, start: str, stub: str, end: str) -> None:
    src = path.read_text(encoding="utf-8")
    if src.count(start) != 1:
        raise AssertionError(f"变异锚点没唯一命中 {path.name}:{start!r}")
    if src.count(end) != 1:
        raise AssertionError(f"变异结束标记没唯一命中 {path.name}:{end!r}")
    i = src.index(start)
    j = src.index(end, i)
    if j <= i:
        raise AssertionError(
            f"结束标记 {end!r} 出现在起点**之前** —— 标记写反了,不是判据的问题")
    _write_checked(path, src[:i] + stub + src[j:])


def _run_criterion():
    return subprocess.run(
        [sys.executable, "-B", "-m", "pytest", *TARGET, "-q",
         "-p", "no:cacheprovider", "-W", "ignore::DeprecationWarning"],
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
            except Exception as e:
                out.append((name, "BAD-MUTANT",
                            f"变异器自己抛了 {type(e).__name__}: {e}"[:400]))
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
        print(f"\n=== r107 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:22s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:300])
    print(f"\nr107 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
