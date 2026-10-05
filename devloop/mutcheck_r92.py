"""r92 变异测试:验「async 测试由纯 stdlib hook 接管」这件事在守。

## 主题

`pyproject.toml` 写着 `asyncio_mode = "auto"`,那是给 pytest-asyncio 用的
配置项;而 pytest-asyncio 在本机装不上(PEP 668)。于是 phase1/2/3 里那
9 条 `async def test_*` 一直红在「async def functions are not natively
supported」,被 `test_baseline` 当成「已知基线」一轮轮传下去。

r92 实测:conftest 里一个纯 stdlib 的 `pytest_pyfunc_call` 接管之后,
那三个文件 **21 passed / 0 failed**。**它们从来没坏过,只是一直没人跑。**

## 唯一真正的风险点

`pytest_pyfunc_call` 是**全局**钩子。一旦它对同步测试也返回 True,
pytest 就再也不会正常调用那个函数 —— 表现为「测试莫名其妙什么都不做」,
而且不报错。所以 M1/M2 专门盯这条。

其余变异盯的是「hook 写了但没生效」那几种典型死法。

## 变异清单

实现变异(期望全被杀):
  M1 hook 对同步测试也返回 True(抢 pytest 的活)  → 让步判据红
  M2 删掉 iscoroutinefunction 那一道闸            → 让步判据红
  M3 asyncio.run 换成什么都不做                   → 真跑那条红
  M4 不 return True(交还给 pytest)                → 真跑那条红
  M5 忘了判 pytest-asyncio 在不在场               → 让位判据红
  M6 不从 pyfuncitem 传 fixture 实参              → 真跑那条红

覆盖变异(期望全存活):
  C1 拆掉「那 9 条仍然是 async def」那条

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
CLAIMS 只写字面量。锚点必须**括号配平且唯一**。
所有变异统一走 _write_checked,写盘前 ast.parse。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CONFTEST = REPO / "tests" / "conftest.py"

TARGET = ["tests/test_async_tests_run_without_third_party_plugin.py"]

COLLECTION_FAILED = ("error during collection", "ERROR collecting", "Interrupted:")

# ── 锚点(整体照抄,括号配平)──

DEFER_CHECK = "    if _pytest_asyncio_installed():\n        return None\n"
DEFER_CHECK_GONE = "    if False:  # 变异:不判 pytest-asyncio 在不在场\n        return None\n"

COROUTINE_GUARD = (
    "    func = pyfuncitem.obj\n"
    "    if not inspect.iscoroutinefunction(func):\n"
    "        return None\n"
)
COROUTINE_GUARD_GONE = "    func = pyfuncitem.obj\n"

KWARGS_LINE = (
    "    kwargs = {n: pyfuncitem.funcargs[n]\n"
    "              for n in pyfuncitem._fixtureinfo.argnames}\n"
)
KWARGS_GONE = "    kwargs = {}\n"
RUN_CALL = "    asyncio.run(func(**kwargs))\n    return True\n"
RUN_NOOP = "    return True  # 变异:协程压根没跑\n"

FIND_SPEC = '        return importlib.util.find_spec("pytest_asyncio") is not None\n'
FIND_SPEC_GONE = "        return False  # 变异:永远当成没装\n"

# 判据侧
ASYNC_LIST = (
    '    "tests/test_phase1.py": ["test_base_module_3state", "test_crtsh_module_mock",\n'
    '                             "test_3state_discipline"],\n'
)
ASYNC_LIST_DEAD = '    "tests/test_phase1.py": [],  # 变异:这条被拆了\n'

CLAIMS = {
    # 纯删除,不引入任何新串 —— must_have 只能空(r88/r89/r90 同一个坑,
    # 第四次)。判别力全在 must_not 上。
    "M1-hook对同步测试也返回True抢pytest的活": (
        [],
        ["    if not inspect.iscoroutinefunction(func):\n        return None\n"],
    ),
    "M2-删掉iscoroutinefunction那道闸": (
        ["    if True:  # 变异:M2 专用的标记"],
        ["if not inspect.iscoroutinefunction(func):"],
    ),
    "M3-asyncio_run换成什么都不做": (
        ["return True  # 变异:协程压根没跑"],
        ["asyncio.run(func(**kwargs))"],
    ),
    "M4-不return_True交还给pytest": (
        ["    return None  # 变异:交还给 pytest,等于没接管"],
        ["asyncio.run(func(**kwargs))\n    return True"],
    ),
    "M5-忘了判pytest_asyncio在不在场": (
        ["if False:  # 变异:不判 pytest-asyncio 在不在场"],
        ["if _pytest_asyncio_installed():"],
    ),
    "M6-不从pyfuncitem传fixture实参": (
        ["    kwargs = {}\n"],
        ["              for n in pyfuncitem._fixtureinfo.argnames}\n"],
    ),
    "C1-拆掉那9条仍然是async-def那条": (
        ["    pass  # 变异:这条判据整条没了"],
        ['    assert got == want, f"async 测试名单变了:\\n  实际 {got}\\n  期望 {want}"\n'],
    ),
}

MUTANTS = [
    ("M1-hook对同步测试也返回True抢pytest的活",
     lambda p: _apply(p, COROUTINE_GUARD, COROUTINE_GUARD_GONE), False, (CONFTEST,)),
    ("M2-删掉iscoroutinefunction那道闸",
     lambda p: _apply(p, COROUTINE_GUARD,
                      "    func = pyfuncitem.obj\n"
                      "    if True:  # 变异:M2 专用的标记\n"
                      "        return None\n"), False, (CONFTEST,)),
    ("M3-asyncio_run换成什么都不做",
     lambda p: _apply(p, RUN_CALL, RUN_NOOP), False, (CONFTEST,)),
    ("M4-不return_True交还给pytest",
     lambda p: _apply(p, RUN_CALL, "    asyncio.run(func(**kwargs))\n"
                      "    return None  # 变异:交还给 pytest,等于没接管\n"), False, (CONFTEST,)),
    ("M5-忘了判pytest_asyncio在不在场",
     lambda p: _apply(p, DEFER_CHECK, DEFER_CHECK_GONE), False, (CONFTEST,)),
    ("M6-不从pyfuncitem传fixture实参",
     lambda p: _apply(p, KWARGS_LINE, KWARGS_GONE), False, (CONFTEST,)),
]

CRIT = REPO / "tests" / "test_async_tests_run_without_third_party_plugin.py"

COVERAGE_MUTANTS = [
    ("C1-拆掉那9条仍然是async-def那条", lambda p: _apply(
        p,
        '    assert got == want, f"async 测试名单变了:\\n  实际 {got}\\n  期望 {want}"\n',
        "    pass  # 变异:这条判据整条没了\n"), True, (CRIT,)),
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
        print(f"\n=== r92 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:38s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:300])
    print(f"\nr92 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    import pathlib as _pl, sys as _sy
    _sy.path.insert(0, str(_pl.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.sandboxed(main))
