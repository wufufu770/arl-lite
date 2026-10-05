"""r114 变异测试:验「锁的覆盖面」和「入口对得上 `fn()` 这个契约」。

## 主题

r113 把 46 个脚本的入口改成 `mutkit.locked(main)`,并且在判据里给
「不写文件的脚本」留了豁免。两条都在 r114 被实测推翻:

**一、那个普查是错的。** r113 的判定函数只认 `write_text` 和 `write_bytes`
两个方法名,于是判「70 个脚本里 46 个会写文件,r41–r64 那 24 个是纯分析,
一个 write 调用都没有」。而那 24 个用的是

    open(path, "w", encoding="utf-8").write(s.replace(old, new, 1))
    shutil.copy(bak, path)

一样在改仓库源码(`arl_lite/core/monitor.py`、`arl_lite/cli.py`、
`arl_lite/ai/commands.py`、`tests/test_cli_advice_commandable.py` …)。
**后果:24 个确实就地改仓库文件的脚本,被分类器判成不写文件而豁免在锁外面** ——
正是 r113 那个文件要防的那件事。

修宽检测面之后实测:**71 个脚本全部会写,一个只读的都没有** —— 豁免成了
死代码,而死代码在这类判据里会被人当先例,所以整个删掉。

**二、入口的名字可以错,而 r113 的改造脚本查不出来。** r114 批量统一入口
形状时,脚本把 47 个文件的锁统一成了 `mutkit.locked(_run_all)` —— 那些文件里
根本没有 `_run_all`(只有 r113 自己有)。改造脚本的复核查了「能 parse」
「写调用数没变」「`main` 还在」,**没查「锁的那个名字是不是就是那个
`main`」**,于是 47 个脚本一起改成了运行时才 `NameError` 的样子,当场没发现。

顺带一个检测器陷阱:把 `Path.replace` 算进「会写」之后,**71 个全部命中** ——
静态分不清 `s.replace(old, new, 1)` 和 `Path.replace(...)`。分不清的检测器
会匹配一切,**判不准的检测器比没有更危险**(r101)。

## 三条判据 + 一条新增的入口契约

1. **每一个** mutcheck 脚本都必须持锁(无豁免,r113 的豁免已删)
2. 被锁的那个函数必须**真实存在**、**零必填参数**、模块能 import(新增)
3. 锁真的挡住并发;4. 无锁时竞态是真的(r113 那两条,本轮继续守着)

## 变异清单

实现变异(期望全被杀):
  M1 摘掉一个脚本的锁            → 判据 1 红
  M2 把锁指向一个不存在的函数    → 判据 2 红
  M3 让 locked 里的名字进 docstring(把真调用删掉) → 判据 1 红
  M4 让 locked 不加锁            → 判据 3 红
  M5 让竞态不发生                → 判据 4 红

覆盖变异(期望全存活,拆判据):
  C1 拆判据 1
  C2 拆判据 2
  C3 拆判据 3
  C4 拆判据 4

## M3 为什么必须有

首版判据用源码子串 `if "mutkit.locked(" in src` 找入口。它当场出过一次错:
`mutcheck_r113.py` 的 **docstring 里写着 `mutkit.locked(main)`**,于是被算成
「锁了 main」,而它实际锁的是 `_run_all`。「文档里提到一个调用」和「代码里
真的调了它」是两件事。M3 把真调用删掉、只在 docstring 里留一句,判据必须红。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
CLAIMS 只写字面量。old 侧必须**整句**配平。纯插入 must_not 留空、
纯删除 must_have 留空。
"""
from __future__ import annotations

import ast
import json
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
MUTKIT = REPO / "devloop" / "mutkit.py"
CRIT = REPO / "tests" / "test_mutcheck_sweep_lock.py"
ONE = REPO / "devloop" / "mutcheck_r65.py"
DECOY = REPO / "devloop" / "mutcheck_r66.py"

TARGET = ["tests/test_mutcheck_sweep_lock.py"]

_RUN_SUMMARY_RE = re.compile(r"^(?:=+\s*)?(.*\bin [\d.]+s.*)$", re.M)
_ERROR_IN_SUMMARY_RE = re.compile(r"\d+\s+errors?\b")

# ── M1:摘掉一个脚本的锁 ──
A1 = "    raise SystemExit(mutkit.locked(main))\n"
M1 = "    raise SystemExit(main())  # 变异 M1:锁被摘掉\n"

# ── M2:锁一个不存在的函数 ──
A2 = "    raise SystemExit(mutkit.locked(main))\n"
M2 = "    raise SystemExit(mutkit.locked(_run_all))\n"

# ── M3:真调用删掉 + 在 docstring 里放一个同形诱饵 ──
#
# 这条验的是判据**用 AST 而不是源码子串**。诱饵放在 docstring 里而不是
# 注释里是有讲究的:注释在 AST 里根本不存在,放那儿等于没放;docstring
# 是一个真实的字符串常量,子串匹配会**看得见它**,AST 匹配看不见。
#
# 所以这条的形状是「删掉真调用 + 加一个长得一模一样的字符串」:
# 若判据是子串版,诱饵会把它骗绿;是 AST 版,它必须红。
A3 = "    raise SystemExit(mutkit.locked(main))\n"
M3 = "    raise SystemExit(main())\n"
DECOY_LINE = ("入口写法是 `raise SystemExit(mutkit.locked(main))`"
              " —— 变异 M3:这一句是诱饵,上面那行才是真调用\n")


def _decoy(path: pathlib.Path) -> None:
    """删掉真调用,再往模块 docstring 里塞一句同形的诱饵。"""
    src = path.read_text(encoding="utf-8")
    assert src.startswith('"""'), "这个脚本没有模块 docstring,诱饵没处放"
    i = src.index('"""', 3)
    out = src[:i] + "\n" + DECOY_LINE + src[i:]
    out = out.replace(A3, M3, 1)
    _write_checked(path, out)

# ── M4:让 locked 不再加锁 ──
A4 = ("        with file_lock(SWEEP_TARGET, timeout=0.0,\n"
      '                       owner=f"mutcheck pid={os.getpid()}"):\n')
M4 = ("        import contextlib  # 变异 M4:换成空上下文,等于没加锁\n"
      "        with contextlib.nullcontext():\n")

# ── M5:让竞态不发生 ──
#
# 首版打的是 r113 那版基于 `sleep` 的复现脚本,而 r114 把那段脚本改成了
# 哨兵协调(见 `test_mutcheck_sweep_lock.py` 里的说明),锚点随之消失。
#
# 新写法:让先启动的那个**不等对方**就立刻还原,后启动的那个改成等
# 「已还原」再备份。两处必须一起改 —— 只改一处会死锁(先一个等
# `second-mutated`,后一个等 `first-restored`,而 `first-restored` 只在
# `second-mutated` 之后才发)。于是后一个的备份拿到的是干净内容,竞态排不出来。
A5 = ('    until("second-mutated")        # 等后一个也变异完\n'
      '    target.write_bytes(backup)     # 无条件还原:干净内容\n')
M5 = ('    # 变异 M5:不等对方,立刻还原\n'
      '    target.write_bytes(backup)\n')
A5B = '    until("first-mutated")\n'
M5B = '    until("first-restored")  # 变异 M5:等它先还原完\n'


def _no_race(path: pathlib.Path) -> None:
    src = path.read_text(encoding="utf-8")
    assert src.count(A5) == 1 and src.count(A5B) == 1
    _write_checked(path, src.replace(A5, M5, 1).replace(A5B, M5B, 1))

# ── 覆盖变异的边界锚点(照文件里的**实际顺序**写) ──
C1_BODY = "def test_every_mutcheck_script_takes_the_sweep_lock():"
C1B_BODY = "def test_the_locked_entry_point_exists_and_takes_no_arguments():"
C2_BODY = "def test_the_lock_actually_refuses_a_second_holder(monkeypatch, tmp_path):"
C3_BODY = "def test_without_the_lock_the_race_is_real():"

CLAIMS = {
    "M1-摘掉一个脚本的锁": (["# 变异 M1:锁被摘掉"], ["mutkit.locked(main)"]),
    "M2-锁一个不存在的函数": (["mutkit.locked(_run_all)"],
                           ["    raise SystemExit(mutkit.locked(main))\n"]),
    "M3-子串诱饵": (["变异 M3:这一句是诱饵"],
                 ["    raise SystemExit(mutkit.locked(main))\n"]),
    "M4-让locked不加锁": (["# 变异 M4:换成空上下文,等于没加锁"],
                        ["with file_lock(SWEEP_TARGET"]),
    "M5-让竞态不发生": (["# 变异 M5"], []),
    "C1-拆判据1": (["    pass  # 变异 C1:整条判据没了"], []),
    "C2-拆判据2": (["    pass  # 变异 C2:整条判据没了"], []),
    "C3-拆判据3": (["    pass  # 变异 C3:整条判据没了"], []),
    "C4-拆判据4": (["    pass  # 变异 C4:整条判据没了"], []),
}

MUTANTS = [
    ("M1-摘掉一个脚本的锁", lambda p: _apply(p, A1, M1), False, (ONE,)),
    ("M2-锁一个不存在的函数", lambda p: _apply(p, A2, M2), False, (ONE,)),
    ("M3-子串诱饵", _decoy, False, (DECOY,)),
    ("M4-让locked不加锁", lambda p: _apply(p, A4, M4), False, (MUTKIT,)),
    ("M5-让竞态不发生", _no_race, False, (CRIT,)),
]

def _stub_tail(path: pathlib.Path, start: str) -> None:
    """把**文件末尾**那个函数整条换成 `pass`。判据 4 是最后一个。"""
    src = path.read_text(encoding="utf-8")
    if src.count(start) != 1:
        raise AssertionError(f"变异锚点没唯一命中 {path.name}:{start!r}")
    i = src.index(start)
    _write_checked(path, src[:i] + start + "\n    pass  # 变异 C4:整条判据没了\n")


COVERAGE_MUTANTS = [
    ("C1-拆判据1",
     lambda p: _replace_fn(p, C1_BODY, C1_BODY + "\n    pass  # 变异 C1:整条判据没了\n",
                           C1B_BODY), True, (CRIT,)),
    ("C2-拆判据2",
     lambda p: _replace_fn(p, C1B_BODY,
                           C1B_BODY + "\n    pass  # 变异 C2:整条判据没了\n",
                           C2_BODY), True, (CRIT,)),
    ("C3-拆判据3",
     lambda p: _replace_fn(p, C2_BODY,
                           C2_BODY + "\n    pass  # 变异 C3:整条判据没了\n",
                           C3_BODY), True, (CRIT,)),
    # 这条**首版又是用 `COVERAGE_MUTANTS.append(...)` 挂上去的**,结果
    # `test_mutcheck_claims_match_their_names.py` 第二次当场报「声明指向的变异
    # 不存在:C4-拆判据4」—— r113 我刚为同一个错写了一段说明,下一轮又犯。
    # 那条判据用 AST 读**字面量列表**,`.append` 出来的条目它看不见。
    # **被逮住两次还犯第三次,就说明「知道」不等于「会做到」**,所以这一次
    # 直接写进字面量,让结构本身不可能再犯。
    ("C4-拆判据4", lambda p: _stub_tail(p, C3_BODY), True, (CRIT,)),
]




def _assert_claims_are_all_literals() -> None:
    """CLAIMS 里只许有字面量 —— 方法调用和名字引用都不行

    r109 写了 `[A_PRED.strip()]`,r110 写了 `[A_TAG]`,两次都是
    `ast.literal_eval` 抛 `ValueError`,那些判据于是**全在报错而不是在校验**。

    ## 它自己第一版是**恒真**的

    首版写的是 `getattr(node.value, "elts", [])` —— 而 `ast.Dict` 根本没有
    `.elts`(在 `.keys` / `.values` 上),`getattr` 返回 `[]`,循环一次没跑,
    `offenders` 永远空。**「取不到就当空」和 r83 那次
    `getattr(node, "blocking", None)` 是同一个错**。

    所以除了正着查,下面还**反着自查遍历器本身**。
    """
    import ast as _ast
    tree = _ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))
    dicts = [n for n in tree.body
             if (isinstance(n, _ast.Assign) and len(n.targets) == 1
                 and isinstance(n.targets[0], _ast.Name)
                 and n.targets[0].id == "CLAIMS"
                 and isinstance(n.value, _ast.Dict))]
    if not dicts:
        raise AssertionError("这个脚本里根本没有 CLAIMS —— 声明被删了?")
    if len(dicts) > 1:
        raise AssertionError(f"CLAIMS 被赋值了 {len(dicts)} 次 —— 读到哪一份是运气")
    node = dicts[0]
    pairs = list(zip(node.value.keys, node.value.values))
    offenders = []
    for k, v in pairs:
        name = k.value if isinstance(k, _ast.Constant) else "<非字面量键>"
        if not isinstance(v, (_ast.Tuple, _ast.List)):
            offenders.append(f"{name}: 声明本身是 {type(v).__name__}")
            continue
        for side in v.elts:
            if not isinstance(side, (_ast.Tuple, _ast.List)):
                offenders.append(f"{name}: 一侧是 {type(side).__name__}")
                continue
            for piece in side.elts:
                if not isinstance(piece, _ast.Constant):
                    offenders.append(f"{name}: {type(piece).__name__}")
    assert pairs, "CLAIMS 是空字典 —— 遍历器一次都没跑,上面那些检查是恒真的"
    assert any(isinstance(p, _ast.Constant)
               for _k, v in pairs for s in v.elts for p in s.elts), (
        "CLAIMS 里连一个字符串字面量都没有 —— 遍历器坏了,别信上面那些检查")
    assert not offenders, (
        f"CLAIMS 里出现了非字面量:{offenders}\n"
        f"  `ast.literal_eval` 遇到它们会抛 `ValueError: malformed node`,"
        f"那些判据会**在报错**而不是在校验 —— 而门禁把它算成回归。")


def _write_checked(path: pathlib.Path, out: str) -> None:
    if out == path.read_text(encoding="utf-8"):
        raise AssertionError("变异是空操作 —— 什么都不会变,写了也是白写")
    if path.suffix == ".py":
        try:
            ast.parse(out)
        except SyntaxError as e:
            raise AssertionError(
                f"变异会让 {path.name} 语法错误({e})。文件未写入。") from None
    elif path.suffix == ".json":
        try:
            json.loads(out)
        except json.JSONDecodeError as e:
            raise AssertionError(
                f"变异会让 {path.name} 不是合法 JSON({e})。文件未写入。") from None
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


def _run_criterion():
    return subprocess.run(
        [sys.executable, "-B", "-m", "pytest", *TARGET, "-q",
         "-p", "no:cacheprovider", "-W", "ignore::DeprecationWarning"],
        capture_output=True, text=True, cwd=REPO, timeout=300,
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


def _run_all() -> int:
    _assert_claims_are_all_literals()
    bad = 0
    for title, mutants in (
        ("实现变异(期望全被杀)", MUTANTS),
        ("覆盖变异(期望全存活)", COVERAGE_MUTANTS),
    ):
        print(f"\n=== r114 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:22s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:300])
    print(f"\nr114 变异总结论: {bad} 个不符合预期")
    return bad


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(_run_all))
