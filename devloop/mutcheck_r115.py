"""r115 变异测试:验「变异作用在副本上」这件事真的接上了。

## 主题

r113 收尾记下过一条已知失效模式,本轮处理它:

    `finally` 对 SIGKILL 不生效,**锁也拦不住进程被杀**。
    本轮又主动复现了一次 —— 用 `timeout` 打断一个跑到一半的 mutcheck,
    仓库就停在变异态,而那个进程报的退出码还是 0。

锁管的是「两个进程同时改」,管不了「一个进程被杀、来不及还原」。所以修法
不是把还原写得更仔细,而是**根本不改真仓库**。

## 三个关键实测(都是量出来的,不是估的)

1. **复制整个仓库(含 `.git`):1.34–2.03 秒。** 一个 mutcheck 本来就要跑
   几分钟,这 2 秒可以忽略。
2. **副本必须带 `.git`。** 不带的话,`test_round_index_stays_current.py`
   与 `test_round_commit_audit.py` 一共 **7 条**判据在副本里直接红
   (它们查 `git log`)。任何跑到它们的变异都会被**误报成「被杀」**——
   变异测试最坏的输出就是这个。
3. **72 个脚本一行都不用改。** 每个脚本开头都是
   `REPO = pathlib.Path(__file__).resolve().parent.parent`;脚本在副本里
   被执行时 `__file__` 就在副本里,`REPO` 于是自动指向副本。入口只需要从
   `mutkit.locked(` 换成 `mutkit.sandboxed(`(一个词)。

## 代价(如实写下来)

- 每次 mutcheck 多 1.3–2 秒建副本。
- **被 SIGKILL 时副本留在磁盘上**(`/tmp/mutcheck-sandbox-*`)。清理代码
  不跑。这是有意的取舍:宁可留一个一眼能看出该删哪个的临时目录,也不要留
  一个被改坏的仓库。
- 端到端判据要多花 1 秒左右(起一个真进程、等沙箱提示、杀掉、比对指纹)。

## 变异清单

实现变异(期望全被杀):
  M1 把一个脚本的入口退回 `locked`   → 判据 1 红
  M2 `_IGNORE` 里加上 `.git`          → 判据 1 红(副本不带 git 会误报)
  M3 `_locked_call` 里删掉 `file_lock` → 判据 1 红(链条断:沙箱不再互斥)
  M4 `sandboxed` 不再重执行子进程      → 判据 5 红(看不到沙箱提示)
  M5 让 `locked` 自己也不加锁           → 判据 3 红
     (与 M3 区分:M3 拆的是**沙箱**那条链上的锁,M5 拆的是 `locked` 自己。
      两条都在 mutkit 里,锚点不同 —— 首版 M5 和 M3 写成了同一个变异。)
  M6 让竞态不发生                      → 判据 4 红

覆盖变异(期望全存活,拆判据):
  C1 拆判据 1
  C2 拆判据 2
  C3 拆判据 3
  C4 拆判据 4
  C5 拆判据 5

M2/M3/M4 都是**拆链条**的变异:入口换成 `sandboxed` 之后,「都走沙箱」和
「沙箱真的加了锁 / 真的建了副本」是两件事。少任何一环,保护就没了,而
表面上看不出区别。

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
# 同 r114:靶子只是任意一个带 `mutkit.sandboxed(main)` 的脚本。原来是 r41,
# 已随 r41–r73 那批未验证脚本一起删掉,改指 r77。
ONE = REPO / "devloop" / "mutcheck_r77.py"

TARGET = ["tests/test_mutcheck_sweep_lock.py"]

_RUN_SUMMARY_RE = re.compile(r"^(?:=+\s*)?(.*\bin [\d.]+s.*)$", re.M)
_ERROR_IN_SUMMARY_RE = re.compile(r"\d+\s+errors?\b")

A1 = "    raise SystemExit(mutkit.sandboxed(main))\n"
M1 = "    raise SystemExit(mutkit.locked(main))  # 变异 M1:退回只加锁\n"

A2 = ('_IGNORE = shutil.ignore_patterns(\n'
      '    "__pycache__", "*.pyc", "*.db", ".pytest_cache", "*.egg-info")')
M2 = ('_IGNORE = shutil.ignore_patterns(\n'
      '    ".git",  # 变异 M2:副本不带 git\n'
      '    "__pycache__", "*.pyc", "*.db", ".pytest_cache", "*.egg-info")')

A3 = ('    with file_lock(SWEEP_TARGET, timeout=0.0,\n'
      '                   owner=f"mutcheck pid={os.getpid()}"):')
M3 = "    import contextlib  # 变异 M3:沙箱不再加锁\n    with contextlib.nullcontext():"

A4 = ('            return subprocess.run(\n'
      '                [sys.executable, "-B", str(script.relative_to(REPO))],\n'
      '                cwd=copy, env=env,\n'
      '            ).returncode')
M4 = '            return fn()  # 变异 M4:不重执行子进程,就在原地跑'

A5LOCK = '        with file_lock(SWEEP_TARGET, timeout=0.0,\n                       owner=f"mutcheck pid={os.getpid()}"):\n            return fn()\n'
M5LOCK = '        return fn()  # 变异 M5:locked 根本不加锁\n'
A5LOCK = '        with file_lock(SWEEP_TARGET, timeout=0.0,\n                       owner=f"mutcheck pid={os.getpid()}"):\n            return fn()\n'
M5LOCK = '        return fn()  # 变异 M5:locked 根本不加锁\n'

A5 = ('    until("second-mutated")        # 等后一个也变异完\n'
      '    target.write_bytes(backup)     # 无条件还原:干净内容\n')
M5 = ('    # 变异 M5:不等对方,立刻还原\n'
      '    target.write_bytes(backup)\n')
A5B = '    until("first-mutated")\n'
M5B = '    until("first-restored")  # 变异 M5:等它先还原完\n'


def _no_race(path: pathlib.Path) -> None:
    src = path.read_text(encoding="utf-8")
    if src.count(A5) != 1 or src.count(A5B) != 1:
        raise AssertionError(
            f"M6 的锚点指不准(A5={src.count(A5)} 次,A5B={src.count(A5B)} 次)")
    _write_checked(path, src.replace(A5, M5, 1).replace(A5B, M5B, 1))


C1 = "def test_every_mutcheck_script_takes_the_sweep_lock():"
C2 = "def test_the_locked_entry_point_exists_and_takes_no_arguments():"
C3 = "def test_the_lock_actually_refuses_a_second_holder(monkeypatch, tmp_path):"
C4 = "def test_without_the_lock_the_race_is_real():"
C5 = "def test_a_killed_sweep_never_dirties_the_real_repo():"

CLAIMS = {
    "M1-入口退回locked": (["# 变异 M1:退回只加锁"], ["mutkit.sandboxed(main)"]),
    "M2-副本不带git": (["# 变异 M2:副本不带 git"], []),
    # must_not 写**整块两行**,不能只写 `with file_lock(SWEEP_TARGET` ——
    # 那串在 mutkit.py 里出现 **2 次**(`locked` 一次、`_locked_call` 一次),
    # 声明会被同名兄弟喂饱(M3 和 M5 各删一处,光看那半行分不出是哪一处)。
    "M3-沙箱不再加锁": (["# 变异 M3:沙箱不再加锁"],
                        ["    with file_lock(SWEEP_TARGET, timeout=0.0,\n                   owner=f\"mutcheck pid={os.getpid()}\"):\n"]),

    "M4-不重执行子进程": (["# 变异 M4:不重执行子进程"], ["str(script.relative_to(REPO))"]),
    "M5-locked不加锁": (["# 变异 M5:locked 根本不加锁"],
                        ["        with file_lock(SWEEP_TARGET, timeout=0.0,\n                       owner=f\"mutcheck pid={os.getpid()}\"):\n            return fn()\n"]),

    "M6-让竞态不发生": (["# 变异 M5"], []),
    "C1-拆判据1": (["    pass  # 变异 C1:整条判据没了"], []),
    "C2-拆判据2": (["    pass  # 变异 C2:整条判据没了"], []),
    "C3-拆判据3": (["    pass  # 变异 C3:整条判据没了"], []),
    "C4-拆判据4": (["    pass  # 变异 C4:整条判据没了"], []),
    "C5-拆判据5": (["    pass  # 变异 C5:整条判据没了"], []),
}

MUTANTS = [
    ("M1-入口退回locked", lambda p: _apply(p, A1, M1), False, (ONE,)),
    ("M2-副本不带git", lambda p: _apply(p, A2, M2), False, (MUTKIT,)),
    ("M3-沙箱不再加锁", lambda p: _apply(p, A3, M3), False, (MUTKIT,)),
    ("M4-不重执行子进程", lambda p: _apply(p, A4, M4), False, (MUTKIT,)),
    ("M5-locked不加锁", lambda p: _apply(p, A5LOCK, M5LOCK), False, (MUTKIT,)),
    ("M6-让竞态不发生", _no_race, False, (CRIT,)),
]


def _stub(fn_body):
    def run(p):
        _replace_fn(p, fn_body, fn_body + "\n    pass  # 占位\n", _end_after(fn_body, p))
    return run


def _end_after(body: str, path: pathlib.Path) -> str:
    """取这个判据后面那一条的 `def` 行当结束锚点"""
    src = path.read_text(encoding="utf-8")
    names = re.findall(r"^def (\w+)\(", src, re.M)
    i = names.index(re.match(r"def (\w+)\(", body).group(1))
    assert i + 1 < len(names), f"{body} 已经是最后一个判据了,要用 _stub_tail"
    return f"def {names[i + 1]}("


def _stub_tail(p):
    src = p.read_text(encoding="utf-8")
    assert src.count(C5) == 1
    _write_checked(p, src[:src.index(C5)] + C5 + "\n    pass  # 变异 C5:整条判据没了\n")


COVERAGE_MUTANTS = [
    ("C1-拆判据1", lambda p: _replace_fn(p, C1, C1 + "\n    pass  # 变异 C1:整条判据没了\n", C2), True, (CRIT,)),
    ("C2-拆判据2", lambda p: _replace_fn(p, C2, C2 + "\n    pass  # 变异 C2:整条判据没了\n", C3), True, (CRIT,)),
    ("C3-拆判据3", lambda p: _replace_fn(p, C3, C3 + "\n    pass  # 变异 C3:整条判据没了\n", C4), True, (CRIT,)),
    ("C4-拆判据4", lambda p: _replace_fn(p, C4, C4 + "\n    pass  # 变异 C4:整条判据没了\n", C5), True, (CRIT,)),
    ("C5-拆判据5", _stub_tail, True, (CRIT,)),
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
        print(f"\n=== r115 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:22s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:300])
    print(f"\nr115 变异总结论: {bad} 个不符合预期")
    return bad


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.sandboxed(_run_all))
