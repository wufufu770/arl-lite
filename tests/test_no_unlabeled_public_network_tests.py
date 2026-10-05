"""r105:测试拿公网当 fixture,而且 `@pytest.mark.slow` 用了却从没注册

## 起因是 r104 门禁红的收尾

r104 查清 `test_portscan_integration` 红了是外网波动,顺手往下挖,挖出两件事:

**一、告警。** `tests/test_bench_wont_clobber_baseline.py:172` 用了
`@pytest.mark.slow`,而 `pyproject.toml` 从来没注册过这个 mark。于是
**每一次全量跑**的 summary 里都带着一条:

    PytestUnknownMarkWarning: Unknown pytest.mark.slow

这和 `pyproject.toml` 里那段注释记的 `asyncio_mode` 是**同一个病**:
一条每次都出现的告警等于没有告警 —— 人会脱敏,下一条真告警跟着被忽略。
而这一条更糟:它不在任何一条判据里,连「为什么留着它」都没人记得。

**二、fixture。** `test_httpx_probe_integration` 探测 `example.com` 并且
断言 `title == "Example Domain"` —— 那是**把公网页面的内容当成了契约**。
example.com 的标题哪天改了这条就红,而它红的原因和被测代码一点关系都没有。

## 写判据时自己踩了一次「判不准的检测器」

先写了一条「全仓扫打公网的测试」的判据,跑出来 9 处,其中 3 处是**误报**:

| 测试 | 看起来打公网 | 实际 |
|---|---|---|
| `test_base_module_3state` | `m.run('example.com')` | `m` 是测试自造的 Module |
| `test_3state_discipline` | `runner.run(target='example.com', ...)` | `modules=['crashy']`,crashy 是自造的 |
| `test_crtsh_module_mock` | `mod.run('example.com')` | 网络被 mock 掉了 |

启发式是「函数名在网络函数表里 + 参数含公网域名」,分不清被调用的是生产
对象还是测试自己造的。**判不准的检测器比没有更危险**(r101 的教训):
它会让人照着它去「修」本来没问题的测试,而那些测试是好的。

所以改成**点名制**:已知真打公网的三条逐条点名,不靠启发式扫全仓。
扫出来唯一真漏网的是 `test_phase3::test_e2e_full`(真的跑 crtsh/
rapiddns/hackertarget),已补标记。

## 为什么 network 标记**不**用来跳过

`pytest -m "not network"` 能把它们摘出来,那是给人排查用的。
**门禁不按这个标记跳过** —— 跳过等于不验,而「让门禁看起来是绿的」
正是 r104 花了整整一轮要拆掉的东西(`test_portscan_integration` 原本
那条 `assert etype is None` 在退化实现下恒真)。

剩下那三条的处理方式(接受 flaky / 加重试 / 无网即 skip)是**人的决定**,
已登记待办,本文件不替它表态。

## `_build_targets` 的白名单:本文件只钉正向行为

`httpx_probe._build_targets` 只扫 web 端口白名单(80/443/8080/…)。
所以本机 fixture **必须**起在白名单端口上:首版用随机端口,结果
`_build_targets` 返回 `[]`,一个 target 都不生成 —— 那测的是「压根没扫」
而不是「扫了没响应」,两回事。

白名单本身是个**已知局限**(内网跑在 3000/5000 的服务扫不到),修它要动
`arl_lite/` 而 r105 的 loc_budget 只剩 2 行。所以本文件只钉「白名单里的
端口确实会被扫」这条正向行为,不把局限锁成契约 —— 否则下一个修它的人
会先撞上本文件,还以为改坏了。
"""
from __future__ import annotations

import ast
import subprocess
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"
PYPROJECT = REPO / "pyproject.toml"

# 已知**真的打公网**的测试。点名制,理由见模块 docstring。
# 顺序无所谓,集合比较。
KNOWN_PUBLIC_NETWORK_TESTS = {
    ("tests/test_phase2.py", "test_9_sources_integration"),
    ("tests/test_phase2.py", "test_e2e_chain"),
    ("tests/test_phase3.py", "test_e2e_full"),
}

# r104 改成本机 fixture 的两条,顺带钉住「别改回去」
NOW_LOCAL = [
    ("tests/test_phase2.py", "test_portscan_integration"),
    ("tests/test_phase2.py", "test_httpx_probe_integration"),
]


def _declared_marks() -> set[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    raw = data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("markers", [])
    out = set()
    for entry in raw:
        name = entry.split(":", 1)[0].strip()
        if name:
            out.add(name)
    return out


def _tests_using_mark(mark: str) -> set[tuple[str, str]]:
    """哪些测试**用了** `@pytest.mark.<mark>`(装饰器位置,不是文件里提到它)。"""
    found = set()
    for p in sorted(TESTS.rglob("*.py")):
        if p.name == "conftest.py":
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel = p.relative_to(REPO).as_posix()
        for fn in tree.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for d in fn.decorator_list:
                # @pytest.mark.slow / @pytest.mark.network
                if (isinstance(d, ast.Attribute) and d.attr == mark
                        and isinstance(d.value, ast.Attribute)
                        and d.value.attr == "mark"
                        and isinstance(d.value.value, ast.Name)
                        and d.value.value.id == "pytest"):
                    found.add((rel, fn.name))
    return found


def _labelled(rel: str, name: str) -> bool:
    """这条测试是否带 network 标记。"""
    return (rel, name) in _tests_using_mark("network")


# =====================================================================
# 一、mark 必须注册
# =====================================================================


def test_every_used_pytest_mark_is_registered():
    """AST 扫全 tests/,凡是用了 `@pytest.mark.X` 的,X 都得在 pyproject 里声明。

    这条是 r105 的核心防复发:起因就是 `slow` 用了没注册,而它**不在任何
    一条判据里**,所以每次全量跑都带一条告警却没人拦。
    """
    declared = _declared_marks()
    used: dict[str, list[str]] = {}
    for mark in ("slow", "network", "asyncio", "parametrize", "skipif"):
        for rel, name in sorted(_tests_using_mark(mark)):
            used.setdefault(mark, []).append(f"{rel}::{name}")

    # parametrize/skipif/asyncio 是 pytest 内置 mark,不需要声明
    builtin = {"parametrize", "skipif", "asyncio", "usefixtures", "filterwarnings"}
    undeclared = {
        m: sites for m, sites in used.items()
        if m not in builtin and m not in declared
    }
    assert not undeclared, (
        f"这些 mark 用了却没在 pyproject.toml 的 markers 里声明, "
        f"pytest 每次都会报 Unknown mark warning:{undeclared}\n"
        f"  已声明:{sorted(declared)}"
    )


def test_both_r105_marks_are_actually_declared():
    """点名。`slow` 和 `network` 是 r105 亲手加的,不许悄悄被删。"""
    declared = _declared_marks()
    missing = {"slow", "network"} - declared
    assert not missing, f"pyproject 的 markers 里少了:{sorted(missing)}（实际 {sorted(declared)}）"


def test_collecting_the_suite_emits_no_unknown_mark_warning():
    """行为级核对:真起一次 pytest 收集,输出里不许有 Unknown mark warning。

    上面两条是「配置对不对」,这条是「**真的没有告警冒出来**」。
    判不准的静态检查能漏(比如 mark 写在别处、或者插件自己发的),
    真跑一次才算数。

    ## 收集范围是「用到自定义 mark 的那些文件」,不是全 tests/

    首版这里跑的是 `pytest tests/ --collect-only`,**27 秒**。r105 因此把
    `test_baseline` 从 415.93s 顶到 **600.29s 撞了硬上限**(外面还有约
    156s 的公网 API 波动,但这 27s 是我加的)。

    全量收集的**覆盖面并没有多出任何东西**:C1 已经用 AST 扫过整个 tests/,
    「凡是用了 @pytest.mark.X 的都声明了」这件事它是全覆盖的。这条要验的
    是另一件事 —— *运行时到底冒不冒告警*,而那只需要收集**真正用到自定义
    mark 的那几个文件**。范围缩到 `slow` / `network` 的使用点之后是秒级。
    """
    files = sorted(
        {rel for mark in ("slow", "network")
         for rel, _ in _tests_using_mark(mark)}
    )
    assert files, (
        "全仓一条 @pytest.mark.slow / network 都没用上 —— "
        "这条判据现在什么都验不到,是前置条件不成立"
    )
    r = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", *files, "--collect-only", "-q",
         "-p", "no:cacheprovider", "-W", "ignore::DeprecationWarning"],
        capture_output=True, text=True, cwd=REPO, timeout=300,
    )
    outp = r.stdout + r.stderr
    assert "PytestUnknownMarkWarning" not in outp, (
        f"收集 {files} 时仍然报 Unknown mark warning —— "
        "一条每次都出现的告警等于没有告警,人会脱敏,下一条真告警跟着被忽略\n"
        + outp[-400:]
    )


# =====================================================================
# 二、公网测试必须显式标记
# =====================================================================


def test_every_known_public_network_test_is_labelled():
    """点名制:三条真打公网的测试都必须带 `@pytest.mark.network`。

    **不用启发式扫全仓** —— r105 试过,3 处误报(test 自造 Module / mock
    掉的网络)。判不准的检测器比没有更危险。
    """
    missing = sorted(
        (rel, name) for rel, name in KNOWN_PUBLIC_NETWORK_TESTS
        if not _labelled(rel, name)
    )
    assert not missing, (
        f"这些测试真的打公网,却没打 @pytest.mark.network:{missing}\n"
        f"  打红了没人知道该怀疑网络还是怀疑代码"
    )


def test_the_network_labelled_set_is_not_empty():
    """前置条件:network 标记真的被用上了。

    少了这条,上面那条在「标记被整体删光」时会因为 `missing` 为空而恒绿。
    """
    assert _tests_using_mark("network") == KNOWN_PUBLIC_NETWORK_TESTS, (
        f"network 标记的使用集合变了(多了或少了):"
        f"实际 {sorted(_tests_using_mark('network'))}"
    )


# =====================================================================
# 三、已经本机化的不许改回去
# =====================================================================


def test_the_two_rewritten_fixtures_do_not_call_the_public_internet():
    """r104/r105 把这两条的 fixture 换成本机了,钉住别改回去。

    查的是**这个函数体内有没有公网域名字面量**——不是全文件。
    `test_phase2.py` 整个文件里 `example.com` 到处都是(当字符串数据用),
    所以范围必须收到函数级。
    """
    for rel, name in NOW_LOCAL:
        src = (REPO / rel).read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(
            n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
        )
        hosts = [
            c.value for c in ast.walk(fn)
            if isinstance(c, ast.Constant) and isinstance(c.value, str)
            and c.value in ("example.com", "example.org")
        ]
        assert not hosts, (
            f"{rel}::{name} 里又出现公网域名 {hosts} —— "
            f"r104/r105 刚把它换成本机 fixture,改回去的话它又变成定时炸弹"
        )


# =====================================================================
# 四、白名单的正向行为(不锁局限)
# =====================================================================


def test_a_web_whitelisted_port_is_still_probed():
    """`_build_targets` 对白名单端口生成 target —— 只钉正向。

    局限(内网高端口扫不到)是 r106 的待办;这里**不**把「随机端口返回空」
    锁成契约,否则下一个修它的人会先撞上这条,还以为改坏了。
    """
    from arl_lite.integrations.httpx_probe import _build_targets

    assert _build_targets("127.0.0.1", [8080], ["http"]), (
        "8080 是 web_ports 白名单里的端口,不该被过滤掉"
    )
    assert _build_targets("127.0.0.1", [443], ["https"]), (
        "443 + https 也该生成 target"
    )
