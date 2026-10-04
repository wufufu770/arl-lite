"""那 9 条 async 测试必须真的跑起来 —— 不许再以「已知基线」躺着

## 这 9 条红了几十轮,不是它们坏了

`pyproject.toml` 里写着 `asyncio_mode = "auto"`,那是**给 pytest-asyncio
用的配置项**。但 pytest-asyncio 在本机装不上(PEP 668 外部管理环境),
于是 phase1/2/3 里那 9 条 `async def test_*` 一直以
「async def functions are not natively supported」红着,被 `test_baseline`
门禁当成「已知基线」记了下来,一轮轮传下去。

r92 实测:在 `tests/conftest.py` 里放一个纯 stdlib 的 `pytest_pyfunc_call`
接管之后,那三个文件 **21 passed / 0 failed**。

**它们从来没坏过,只是一直没人跑它们。** 「已知基线」这个状态最阴险的
地方就在这儿:它让「没人跑过」和「跑过但坏了」在账面上长得一模一样。

## 这条判据守什么

1. 那 9 条测试仍然是 `async def`(有人把它们改成同步的了,说明 hook 白写)
2. hook 真的接上了,而不是「conftest 里有个函数就算」
3. hook **不抢**非协程的测试 —— 这是它唯一的风险点:
   `pytest_pyfunc_call` 是全局钩子,一旦对同步测试也返回 True,
   pytest 就再也不会去正常调用那个函数了
4. pytest-asyncio 装上了就必须让位给它,别跟它抢 loop 语义
5. **真跑一遍**,确认那 9 条从「failed」变成「passed」—— 前四条都是静态
   检查,静态检查证明不了 asyncio.run 真的能把它们跑完
"""
from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
TESTS = REPO / "tests"
CONFTEST = TESTS / "conftest.py"

# r92 实测:这 9 条此前长期红在「async def functions are not natively supported」
KNOWN_ASYNC_TESTS = {
    "tests/test_phase1.py": ["test_base_module_3state", "test_crtsh_module_mock",
                             "test_3state_discipline"],
    "tests/test_phase2.py": ["test_9_sources_integration", "test_portscan_integration",
                             "test_httpx_probe_integration", "test_e2e_chain",
                             "test_concurrent_isolation"],
    "tests/test_phase3.py": ["test_e2e_full"],
}


def _async_test_names(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.name for n in tree.body
            if isinstance(n, ast.AsyncFunctionDef) and n.name.startswith("test_")}


def test_the_nine_are_still_async_def():
    """它们必须还是协程 —— 有人改成同步的就说明 hook 白写了"""
    got = {rel: sorted(_async_test_names(REPO / rel)) for rel in KNOWN_ASYNC_TESTS}
    want = {rel: sorted(names) for rel, names in KNOWN_ASYNC_TESTS.items()}
    assert got == want, f"async 测试名单变了:\n  实际 {got}\n  期望 {want}"


def test_the_hook_is_actually_wired_into_conftest():
    """conftest 里必须有 `pytest_pyfunc_call` —— 写个函数不算接线"""
    src = CONFTEST.read_text(encoding="utf-8")
    assert "def pytest_pyfunc_call(pyfuncitem):" in src, (
        "conftest 里没有 pytest_pyfunc_call —— hook 没接上")
    assert "asyncio.run(func(**kwargs))" in src, "hook 没有真的跑那个协程"
    assert "return True" in src, "hook 必须返回 True 才算接管,return None 是交还给 pytest"


def _hook_fn() -> ast.FunctionDef:
    tree = ast.parse(CONFTEST.read_text(encoding="utf-8"))
    return next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "pytest_pyfunc_call")


def _guards_returning_none(fn: ast.FunctionDef) -> list[ast.If]:
    """hook 里所有「条件成立就 return None」的 if 语句"""
    out = []
    for n in ast.walk(fn):
        if (isinstance(n, ast.If) and len(n.body) == 1
                and isinstance(n.body[0], ast.Return)
                and isinstance(n.body[0].value, ast.Constant)
                and n.body[0].value.value is None):
            out.append(n)
    return out


def _is_coroutine_guard(node: ast.expr) -> bool:
    """是不是 `not inspect.iscoroutinefunction(...)` —— 判 **AST 形状**

    早先这里写的是 `ast.unparse(fn)` 再拿字符串找,被 r80 那条
    `test_source_checks_are_structural` 当场逮住 —— 那条守卫说得对:
    unparse 出来的文本里,一个字符串字面量或一句注释就能把这个检查喂饱。
    r85 栽过一次,这轮又栽了一次。
    """
    return (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not)
            and isinstance(node.operand, ast.Call)
            and isinstance(node.operand.func, ast.Attribute)
            and node.operand.func.attr == "iscoroutinefunction"
            and isinstance(node.operand.func.value, ast.Name)
            and node.operand.func.value.id == "inspect")


def _is_asyncio_defer_guard(node: ast.expr) -> bool:
    """是不是 `_pytest_asyncio_installed()` 本身作为条件"""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "_pytest_asyncio_installed")


def test_the_hook_yields_to_sync_tests():
    """**安全性来源**:非协程必须 `return None` 交回给 pytest

    `pytest_pyfunc_call` 是全局钩子。一旦对同步测试也返回 True,pytest 就
    再也不会正常调用那个函数 —— 表现为「测试莫名其妙什么都不做」。
    这是本 hook 唯一可能伤到别人的地方。
    """
    fn = _hook_fn()
    assert any(_is_coroutine_guard(n.test) for n in _guards_returning_none(fn)), (
        "hook 里没有 `if not inspect.iscoroutinefunction(func): return None` "
        "这道闸 —— 同步测试会被它吞掉")


def test_the_hook_defers_to_pytest_asyncio_when_installed():
    """装了 pytest-asyncio 就让位 —— 它的 loop 语义比我们这版全

    查的是**hook 体内真的有一道以它为条件的闸**,不是「这个函数在文件里
    存在」。r92 头一版只查了 `def` 那一行,于是把条件换成 `if False:`
    的变异照样通过 —— 函数还在,只是没人叫它。
    """
    fn = _hook_fn()
    assert any(_is_asyncio_defer_guard(n.test) for n in _guards_returning_none(fn)), (
        "hook 体内没有真的以 _pytest_asyncio_installed() 为条件的闸 —— "
        "pytest-asyncio 装上了也会跟它抢")
    src = CONFTEST.read_text(encoding="utf-8")
    assert 'importlib.util.find_spec("pytest_asyncio")' in src, (
        "没有真的去查 pytest-asyncio 装没装")


async def test_an_async_test_can_take_a_fixture(tmp_path):
    """async 测试**带 fixture** 必须也能跑 —— kwargs 那条管线不是摆设

    r92 头一版没有这条:那 9 条 async 测试**一个 fixture 都不用**,
    于是「不传 kwargs」的变异完全观测不到(实测存活)。加一条真的用
    `tmp_path` 的协程测试,管线才被证明是通的。
    """
    probe = tmp_path / "probe.txt"
    probe.write_text("ok", encoding="utf-8")
    assert probe.read_text(encoding="utf-8") == "ok"


def test_the_nine_actually_pass_now():
    """真跑一遍 —— 前面几条都是静态检查,证明不了 asyncio.run 能跑完它们

    ## 只跑两条最便宜的代表,而且这**不是**省事,是量过的

    r92 实测这 9 条各自的耗时(同机、单独跑):

        test_e2e_chain               30.56s
        test_9_sources_integration   27.77s
        test_e2e_full                17.38s
        test_httpx_probe_integration  2.26s
        test_portscan_integration    2.05s
        test_concurrent_isolation    0.13s
        test_3state_discipline       0.07s
        test_crtsh_module_mock       0.04s
        test_base_module_3state      0.02s

    头一版挑了每个文件的**第一条**,恰好全挑中最重的三条,判据自己花掉
    56.64s;再头一版干脆重跑整个 phase1+2+3(73s),把全量从 ~490s 顶到
    707s,**直接冲破 `test_baseline` 的 600s 挂死上限**。

    hook 要回答的问题只有一个:「协程能不能被跑起来」。答案是与协程本身
    无关的,所以挑两条最便宜的分属两个文件的(0.02s + 0.13s)就够。
    九条的完整覆盖归主套件 —— 它跑得更全,还不额外花钱。
    """
    probes = ["tests/test_phase1.py::test_base_module_3state",
              "tests/test_phase2.py::test_concurrent_isolation"]
    r = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", *probes,
         "-q", "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    out = r.stdout + r.stderr
    assert "async def functions are not natively supported" not in out, (
        f"仍然有 async 测试没被接管 —— hook 没生效:\n{out[-800:]}")
    assert r.returncode == 0, f"这两条 async 代表测试没绿:\n{out[-800:]}"
    assert f"{len(probes)} passed" in out, f"只跑通了部分:{out[-300:]}"
