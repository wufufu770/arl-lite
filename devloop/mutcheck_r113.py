"""r113 变异测试:验「变异脚本必须持锁」和「这把锁真的挡住了竞态」。

## r114 更正:下面这句普查是错的

    会写仓库文件的       46 个(r41–r64 那 24 个是纯分析,一个 write_* 都没有)

**「那 24 个是纯分析、一个 write 调用都没有」是错的。** 判定函数只认
`write_text` 和 `write_bytes` 两个方法名,而 r41–r64 用的是

    open(path, "w", encoding="utf-8").write(s.replace(old, new, 1))
    shutil.copy(bak, path)

一样在改仓库源码(`arl_lite/core/monitor.py`、`arl_lite/cli.py`、
`arl_lite/ai/commands.py`、`tests/test_cli_advice_commandable.py` …)。
后果是那 24 个脚本**确实就地改仓库文件,却被分类器判成「不写文件」而
豁免在锁外面** —— 正是本轮要防的那件事。r114 把它们全补上了锁,
现在 71 个脚本一个不落,判据里的豁免整个删掉。

**教训**:那个结论是用**一个只认两个名字的检测器**量出来的,我把它当成了
全称写进文档、判据注释和 backlog。「测得的东西比声称的窄」是这个项目
反复栽的地方(r101 的检测器误报、r108 的门禁总表虚构引用)。

## 主题

r112 收尾时撞到一件归因不明的事:一轮结束时 `arl_lite/devloop/queue.py` 的
模块 docstring 停在了某条变异的内容上,而那个进程自己报的退出码是 0。

r113 顺着「变异 harness 本身可不可靠」查下去。普查 70 个 `devloop/mutcheck_*.py`:

    会写仓库文件的       46 个(r41–r64 那 24 个是纯分析,一个 write_* 都没有)
    还原点形状           45 个脚本各 1 处,逐字节相同的一行 `t.write_bytes(data)`
    加锁的               0 个
    还原后校验的         0 个

`t.write_bytes(data)` 的语义是「无条件写回本进程读到的备份」。单进程内正确,
跨进程不安全:谁拿到的是「备份」,取决于它读文件那一刻文件长什么样,而不是
取决于谁真正改的。

## 两个后果都实测复现(两个进程,时序固定)

    1. 文件被留在变异态
    2. **更糟**:还原发生在判据跑完之前,生效过的变异被报成 SURVIVED,
       而事后仓库干干净净,没有任何痕迹

第 2 条比第 1 条危险得多 —— 第 1 条会让人去查 git,第 2 条什么都不留,
而变异测试报出来的 `survived` 恰恰是人最不会怀疑的东西。

## 修法与三条判据

新增 `devloop/mutkit.py`,复用 `arl_lite/devloop/lock.py` **已有**的
`file_lock`(不手抄第二份 flock),46 个会写文件的脚本入口统一改成
`mutkit.sandboxed(main)`。三条判据在 `tests/test_mutcheck_sweep_lock.py`:
结构(都取锁)、行为(锁真挡人)、**负控制**(无锁时竞态是真的)。

## 一条被实测推翻的「更温和的修法」

先试的是**条件还原**:写回备份前先确认文件还等于「我写下的变异」,
不是就别写 —— 免得踩掉别人的。**它修不了这个竞态**,因为后启动的进程读到的
「备份」本身就带着前一个进程的变异;A 还原时发现不是自己的(不敢动),B 到点
把带变异的备份写了回去。负控制里发现的:把还原改成条件式之后判据照样绿。

**能修它的只有一件事:让两个进程不要同时改。** 条件还原是在错误发生之后
补救,而错误发生的那一刻备份就已经错了。

## 变异清单

实现变异(期望全被杀):
  M1 摘掉一个脚本的锁        → 判据 1 红
  M2 让 locked 不加锁        → 判据 2 红
  M3 让竞态不发生(备份取自干净副本) → 判据 3 红
  M4 脚本里那个 write_bytes 还原点整体删掉 → 判据 1 的前置条件红

覆盖变异(期望全存活,拆判据):
  C1 拆判据 1
  C2 拆判据 2
  C3 拆判据 3

## 变异与负控制不是一回事

r113 第一次写负控制时**三条全绿**。逐条查下来:**全是控制脚本自己的 bug** ——
一条压根没写删除动作;一条把 12 空格缩进的行匹配成 8 空格,替换后语义完全
不变(**空操作变异**,报出来却像「判据恒真」);第三条改的时序根本不改变胜负。

**假结论比没有结论更贵**(r110)。所以这里把负控制的过程也写下来:三条控制
最终各自精确转红在**预期的那条判据**上。

M4 是给判据 1 的**前置条件**留的出口:`assert total >= 30` 钉住「至少有 30
个脚本会写文件」这个实测值(现在 46)。只删掉某个脚本的 write 调用会让它变
小,判据必须先于「没有脚本取锁」报出来 —— 否则一条范围收缩会被误读成通过。

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
ONE = REPO / "devloop" / "mutcheck_r112.py"

TARGET = ["tests/test_mutcheck_sweep_lock.py"]

_RUN_SUMMARY_RE = re.compile(r"^(?:=+\s*)?(.*\bin [\d.]+s.*)$", re.M)
_ERROR_IN_SUMMARY_RE = re.compile(r"\d+\s+errors?\b")

# ── M1:摘掉一个脚本的锁 ──
A1 = "    raise SystemExit(mutkit.sandboxed(main))\n"
M1 = "    raise SystemExit(main())  # 变异 M1:锁被摘掉\n"

# ── M2:让 locked 不再加锁 ──
A2 = ('        with file_lock(SWEEP_TARGET, timeout=0.0,\n'
      '                       owner=f"mutcheck pid={os.getpid()}"):\n')
M2 = ('        import contextlib  # 变异 M2:换成空上下文,等于没加锁\n'
      '        with contextlib.nullcontext():\n')

# ── M3:让竞态不发生 ──
#
# **r114 重定向锚点**:首版是往复现脚本里塞一份「干净副本」让备份不脏。
# r114 把那段复现脚本从「靠 sleep 抢时序」改成了「靠哨兵文件协调」(它
# 偶发红,而且红的原因和被测代码无关),首版的写法随之失效。
#
# 现在改成同样能消除竞态、但在**新协议**里成立的两处改动:先启动的那个
# 不等对方就立刻还原,后启动的等「已还原」再备份。两处必须一起改,
# 只改一处会死锁。
A3 = ('    until("second-mutated")        # 等后一个也变异完\n'
      '    target.write_bytes(backup)     # 无条件还原:干净内容\n')
M3 = ('    # 变异 M3:不等对方,立刻还原\n'
      '    target.write_bytes(backup)\n')
A3B = '    until("first-mutated")\n'
M3B = '    until("first-restored")  # 变异 M3:等它先还原完\n'


def _no_race(path: pathlib.Path) -> None:
    src = path.read_text(encoding="utf-8")
    if src.count(A3) != 1 or src.count(A3B) != 1:
        raise AssertionError(
            f"M3 的锚点指不准(A3={src.count(A3)} 次,A3B={src.count(A3B)} 次)")
    _write_checked(path, src.replace(A3, M3, 1).replace(A3B, M3B, 1))

# ── M4:掐小判据 1 的遍历范围(打的是它自己那条前置条件) ──
# 首版 M4 是「把某个脚本的还原点整体删掉」,**它存活了**:那个脚本于是
# 不再被 `_writes_repo_files` 认成「会写文件」,判据 1 压根不管它 ——
# 反而是「删掉 finally 里的还原」这种**更危险**的改动,在判据 1 眼里
# 变成了「这个脚本不危险」。方向打反了。
#
# 第二版把属性名改坏一个(`write_bytez`),**也存活了**:`total` 从 46
# 掉到 45,`assert total >= 30` 照样过。**实测出这条前置条件的真实边界:
# 它只挡「遍历范围崩塌」,不挡「掉一个脚本」。** 写死 46 又会变成
# 「写死总数 = 自造一条永远红的门禁」(r110 刚把一个漂移的总数从文件头
# 去掉)。所以这里如实接受那个边界,M4 掐的是它真正声称的那件事:
# 范围整个塌掉时前置条件必须先于其它断言报出来。
A4 = 'MUTCHECKS = sorted(DEV.glob("mutcheck_*.py"))\n'
M4 = ('MUTCHECKS = sorted(DEV.glob("mutcheck_*.py"))\n'
      '# 变异 M4:遍历范围被掐小\n'
      'DEV = DEV / "devloop"  # 多一层,glob 什么都找不到\n')

# ── 覆盖变异的边界锚点(照文件里的**实际顺序**写) ──
C1_BODY = "def test_every_mutcheck_script_takes_the_sweep_lock():"
C2_BODY = "def test_the_locked_entry_point_exists_and_takes_no_arguments():"
C3_BODY = "def test_the_lock_actually_refuses_a_second_holder(monkeypatch, tmp_path):"
C4_BODY = "def test_without_the_lock_the_race_is_real():"
C1_STUB = C1_BODY + "\n    pass  # 变异 C1:整条判据没了\n"
C2_STUB = C2_BODY + "\n    pass  # 变异 C2:整条判据没了\n"

CLAIMS = {
    "M1-摘掉一个脚本的锁": (["# 变异 M1:锁被摘掉"], ["mutkit.sandboxed(main)"]),
        # must_not 写**整块三行**:`with file_lock(SWEEP_TARGET` 这半行在
    # mutkit.py 里出现 **2 次**(r115 给 `sandboxed` 也加了一把),
    # 只写半行会被同名兄弟喂饱 —— 那正是这条判据存在的理由。
    "M2-让locked不加锁": (["变异 M2:换成空上下文,等于没加锁"],
                 ["        with file_lock(SWEEP_TARGET, timeout=0.0,\n                       owner=f\"mutcheck pid={os.getpid()}\"):\n            return fn()\n"]),

    "M3-让竞态不发生": (["# 变异 M3"], []),
    # r114 起这条是**纯插入**(保留原行,追加一行把目录掐深),
    # 所以 must_not 按约定留空。
    "M4-掐小判据遍历范围": (["# 变异 M4:遍历范围被掐小"], []),
    "C1-拆判据1": (["    pass  # 变异 C1:整条判据没了"], []),
    "C2-拆判据2": (["    pass  # 变异 C2:整条判据没了"], []),
    "C3-拆判据3": (["    pass  # 变异 C3:整条判据没了"], []),
}

MUTANTS = [
    ("M1-摘掉一个脚本的锁", lambda p: _apply(p, A1, M1), False, (ONE,)),
    ("M2-让locked不加锁", lambda p: _apply(p, A2, M2), False, (MUTKIT,)),
    ("M3-让竞态不发生", _no_race, False, (CRIT,)),
    ("M4-掐小判据遍历范围", lambda p: _apply(p, A4, M4), False, (CRIT,)),
]

COVERAGE_MUTANTS = [
    # r114 在判据 1 与「锁真挡人」之间插了一条「入口契约」,所以顺序锚点
    # 跟着变了:C1 的**结束**锚点现在是「入口契约」,不是「锁真挡人」。
    # 顺序反了 `_replace_fn` 会当场报错(它本来就查这个)。
    ("C1-拆判据1",
     lambda p: _replace_fn(p, C1_BODY, C1_STUB, C2_BODY), True, (CRIT,)),
    ("C2-拆判据2",
     lambda p: _replace_fn(p, C2_BODY, C2_STUB, C3_BODY), True, (CRIT,)),
    # 「竞态是真的」是文件里**最后一个**判据,后面没有可作边界的 `def`,
    # 所以用 `_stub_tail` 截到文件尾。
    # (r114 首版把结束锚点写成 `\nif __name__`,而那个测试文件里根本没有
    #  `if __name__` —— 一次典型的「结束标记想当然」,harness 当场报 BAD-MUTANT。)
    ("C3-拆判据3", lambda p: _stub_tail(p, C4_BODY), True, (CRIT,)),
]


def _stub_tail(path: pathlib.Path, start: str) -> None:
    """把**文件末尾**那个函数整条换成 `pass`。判据 3 是最后一个函数。"""
    src = path.read_text(encoding="utf-8")
    if src.count(start) != 1:
        raise AssertionError(f"变异锚点没唯一命中 {path.name}:{start!r}")
    i = src.index(start)
    _write_checked(path, src[:i] + start + "\n    pass  # 变异 C3:整条判据没了\n")



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
        import ast
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
        print(f"\n=== r113 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:22s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:300])
    print(f"\nr113 变异总结论: {bad} 个不符合预期")
    return bad


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.sandboxed(_run_all))
