"""pytest 的配置和声明的依赖必须是真的 —— 一条每次都出现的告警等于没有告警

## 起因:每一次 pytest 运行都在报一条假告警

r92 用纯 stdlib 的 `pytest_pyfunc_call` 接管了那 9 条 async 测试。但
`pyproject.toml` 还留着两样为 pytest-asyncio 准备的东西:

    [project.optional-dependencies]
    dev = ["pytest>=7", "pytest-asyncio>=0.21"]      ← 本机装不上(PEP 668)

    [tool.pytest.ini_options]
    asyncio_mode = "auto"                              ← 本机根本不被识别

而 `pytest-asyncio` 没装,于是 `asyncio_mode` 这个键 pytest 压根不认识,
**每跑一次就报一次**:

    PytestConfigWarning: Unknown config option: asyncio_mode

实测代价:54 个变异脚本 / 446 条变异,一次全量变异扫描要起 **446 次
pytest**,每一次都带这条;七门禁里的 `test_baseline` 也带。

**一条每次都出现的告警等于没有告警。** 人会对它脱敏,下一条真告警跟着
被一起忽略 —— 这比没有告警更坏,因为它占着「有人在看告警」的位置。

## 更糟的是那段注释两处都说反了

`asyncio_mode` 上面原本写着:

    async def test_xxx() 需要 auto 模式才会被 pytest-asyncio 接管。
    之前没配这一项,9 个 async 测试全部被 skip 成 "async def functions
    are not natively supported",覆盖率比看起来低很多。

两处都错:9 条测试从 r92 起就被接管了(不是 skip),而这一行本机就不被识别。
**把现状说反了的注释,比没有注释更容易误导下一个人** —— 它会让人以为
async 能不能跑取决于这一行。

## 同一份文件自己写着规矩

`pyproject.toml` 里 `dependencies` 上面那段注释:

    只声明不实现,比不声明更糟:它承诺了一个项目没有的能力。

`pytest-asyncio>=0.21` 正好违反它:装不上、用不上、还带崩配置。

## 这份判据守什么

1. **pytest 启动不许报未知配置项** —— 泛化到将来任何拼错的键,不只是 asyncio_mode
2. **dev 里声明的依赖必须真的装着** —— 装不上的声明是负债
3. **不许声明任何 pytest 插件包** —— 项目的测试必须能用裸 pytest 跑
4. **async 那套在关掉全部插件自动加载后照样跑通** —— 这条是**行为**判据,
   不是查名字。r95 实测:带上 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`,
   r92 的 6 条 async 判据全过(0.71s)
5. `dependencies` 恒为空(零 pip 依赖铁律)
6. 判据自己不许退化成 unparse + 子串(r80 立的规矩)
"""
from __future__ import annotations

import ast
import importlib.metadata
import os
import pathlib
import re
import subprocess
import sys
import tomllib

REPO = pathlib.Path(__file__).resolve().parents[1]
PYPROJECT = REPO / "pyproject.toml"

# 判据 3/4 用的探针:最便宜的那个文件。实测 --collect-only 0.62s。
# 另一个候选 tests/test_advice_unchecked_are_classified.py 要 27.16s ——
# 模块级 import 很重。**挑错文件就是每轮白付 27s**,所以这个数是量过的。
PROBE_FILE = "tests/test_swallowed_exceptions_are_accounted_for.py"

# 判据 4 用的:r92 那 6 条 async 判据(含真跑那两条代表)
ASYNC_CRIT = "tests/test_async_tests_run_without_third_party_plugin.py"


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _dev_requirements() -> list[str]:
    return list(_pyproject()["project"]["optional-dependencies"].get("dev", []))


def _dist_name(req: str) -> str:
    """`pytest>=7` → `pytest`(只取包名,版本约束不要)"""
    return re.split(r"[<>=!~\[; ]", req.strip(), maxsplit=1)[0]


def _missing_deps(reqs: list[str]) -> list[str]:
    """声明了但本机没装的包。抽成纯函数是为了能被合成样本检验。"""
    out = []
    for req in reqs:
        name = _dist_name(req)
        try:
            importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            out.append(f"{req}({name})")
    return out


def _plugin_deps(reqs: list[str]) -> list[str]:
    """声明的 pytest 插件包(名字以 `pytest-` 开头,`pytest` 本身不算)"""
    return [r for r in reqs if _dist_name(r).startswith("pytest-")]


# --- 1) 未知配置项 ---

def test_pytest_reports_no_unknown_config_options():
    """**真跑一次 pytest**,不许报任何未知配置项

    这条刻意不点名 `asyncio_mode`:它守的是「配置里的每一个键 pytest 都认识」。
    将来谁再写错一个键(拼错的 `asyncio_mode`、从别的项目抄来的
    `testpaths`/`markers` 私有键),这条都会红。

    用 `--collect-only`:配置解析发生在收集之前,告警照样会打,而代价只有
    0.62s(实测)。
    """
    r = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", PROBE_FILE,
         "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True, timeout=300,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"探针自己没跑通,这条判据就空转了:\n{out[-500:]}"
    unknown = [ln for ln in out.splitlines() if "Unknown config option" in ln]
    assert not unknown, (
        "pytest 报了未知配置项 —— 每跑一次就报一次,等于没有告警:\n  "
        + "\n  ".join(unknown)
    )


# --- 2) 声明的 dev 依赖必须真的装着 ---

def test_every_declared_dev_dependency_is_actually_installed():
    """dev 里写的每个包,本机必须真的装着

    `pytest-asyncio>=0.21` 声明了两年,本机一次也没装上(PEP 668)。一个
    永远装不上的声明不是承诺,是误导:它让人以为装上就能用。

    **合成样本那一段不是保险,是必需的**:真基线里现在每个包都装着,
    「没装」这个分支在真实数据上压根走不到。少了它,把 `except
    PackageNotFoundError` 改掉(接不住「没装」)这条变异会**存活** ——
    判据在真实数据上恒过,和 r94 那份名单判据是同一个坑。
    """
    missing = _missing_deps(_dev_requirements())
    assert not missing, (
        f"dev 里声明了本机装不上的东西:{missing}\n"
        f"装不上的声明比不声明更糟 —— 它承诺了一个项目没有的能力"
    )
    assert _missing_deps(["definitely_not_installed_xyz>=1"]), (
        "「没装」这个分支在合成样本上都没走到 —— 它在真实数据上更走不到")
    assert _missing_deps(["pytest"]) == [], (
        "已装的包被误报成没装 —— 这条检查会变成纯噪音")


# --- 3) 不许声明 pytest 插件 ---

def test_no_pytest_plugin_is_declared_as_a_dev_dependency():
    """项目的测试必须能用**裸 pytest** 跑,所以不许声明任何插件包

    判据 4 用行为证明这件事成立,这里是防它以后被人改回去。
    同样要合成样本:真 dev 里只有 `pytest`,这个检查在真实数据上空过。
    """
    plugins = _plugin_deps(_dev_requirements())
    assert not plugins, (
        f"dev 里声明了 pytest 插件:{plugins}。\n"
        f"本项目的 async 测试由 tests/conftest.py 的纯 stdlib hook 接管"
        f"(r92),不靠任何插件;判据 4 实测关掉插件自动加载照样跑通。"
    )
    assert _plugin_deps(["pytest-cov>=7"]) == ["pytest-cov>=7"], (
        "插件检查认不出 `pytest-cov` —— 前缀写错就会整条失效")
    assert _plugin_deps(["pytest>=7"]) == [], (
        "`pytest` 本身被误报成插件 —— 那会让这条判据永远红")


# --- 4) 行为:关掉全部插件自动加载,async 那套照样跑通 ---

def test_the_async_suite_runs_with_all_plugin_autoload_disabled():
    """**行为判据**:整条 async 管线不许依赖任何 pytest 插件

    判据 3 是查名字(「有没有声明插件」),这条是查行为(「关掉插件还跑不跑得通」)。
    两者互补:名字能改,行为改不了。

    带上 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` 跑 r92 那 6 条判据(其中两条会
    真跑 async 测试代表)。r95 实测 0.71s 全过 —— 这就是「那 9 条 async 测试
    从来不靠 pytest-asyncio」的直接证据,而不是从声明上推的。
    """
    r = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", ASYNC_CRIT, "-q",
         "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True, timeout=300,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
             "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, (
        f"关掉全部插件自动加载后,async 那套就崩了 —— 说明它真的在靠插件:"
        f"\n{out[-700:]}"
    )
    assert "passed" in out, f"探针没跑到判据,这条空转了:\n{out[-300:]}"


# --- 5) 零依赖铁律 ---

def test_runtime_dependencies_stay_empty():
    """`dependencies` 恒为空 —— 零 pip 依赖是项目铁律,判据里的铁律也要被守"""
    assert _pyproject()["project"]["dependencies"] == [], (
        "运行时依赖必须为空:外部工具是外部二进制,不是 pip 依赖"
    )


# --- 6) 一个测试泄漏的 HOME 不许污染下一个(r95 的真教训)---

def test_home_leaked_by_one_test_cannot_break_the_next(tmp_path, real_home):
    """**行为判据**:现场造两个测试文件跑一次真 pytest,验 conftest 那个兜底

    r95 踩的坑:`tests/test_phase4.py` 等 6 处直接 `os.environ["HOME"] = home`
    (没用 monkeypatch,不还原),而 `home` 是 `TemporaryDirectory()`,with 块
    一退出目录就被删。于是后面任何**起子进程**的判据都会拿到一个指向不存在
    路径的 HOME,pytest 装在 `~/.local` 里 → `No module named pytest`。

    实测症状:本文件**单跑 6 passed / 1 skipped,跑全量时其中 2 条红**,
    报错 `/usr/bin/python3: No module named pytest`。(门禁那一轮报的是
    `failed=3` —— 另外那一条是 `test_packaging_metadata.py`,原因不同,
    是 r95 删了声明却忘了删 `_DECLARATIVE` 里的豁免。)

    同一个 HOME 病,`devloop/state.json` 的 round 68 已经吃过一次:
    `test_baseline` 报 `could not parse pytest summary` + 同一句
    `No module named pytest` —— 当时归因成「HOME 被设成 /tmp」,
    机制到这一轮才查清。

    这里不测「兜底函数写对了没有」(那是存在检查),而是**造出污染、跑一遍、
    看下一个测试有没有被搞坏**。兜底不在的话 b 必红。
    """
    pkg = tmp_path / "probe"
    pkg.mkdir()
    (pkg / "test_a_leaks.py").write_text(
        "import os, tempfile\n"
        "def test_leak():\n"
        "    with tempfile.TemporaryDirectory() as home:\n"
        "        os.environ['HOME'] = home\n"
        "    # with 退出:目录被删,HOME 还指着它 —— 这就是泄漏\n",
        encoding="utf-8",
    )
    (pkg / "test_b_checks.py").write_text(
        "import os\n"
        "import os.path\n"        # 模块级:写在函数里会让 os 变成局部名
        "def test_home_is_intact():\n"
        "    home = os.environ.get('HOME', '')\n"
        "    assert os.path.isdir(home), f'HOME 指向不存在的路径: {home}'\n",
        encoding="utf-8",
    )
    conftest = pkg / "conftest.py"
    # 只把**真实 conftest 里那个 fixture 的源码**搬过来,不是手抄一份。
    # 手抄测的是「我抄对了吗」,搬源码测的是「仓库里那个真的管不管用」。
    # 整个 conftest 不能直接复制:它有会话级 autouse 的 `_find_repo_root()`,
    # 在 tmp 目录里会抛「找不到项目根」。
    real_src = (REPO / "tests" / "conftest.py").read_text(encoding="utf-8")
    lines = real_src.splitlines()
    tree = ast.parse(real_src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef)
              and n.name == "_restore_home_between_tests")
    start = min([d.lineno for d in fn.decorator_list] + [fn.lineno]) - 1
    snippet = "\n".join(lines[start:fn.end_lineno]) + "\n"
    conftest.write_text(
        "import os\nimport pytest\n\n\n" + snippet, encoding="utf-8")
    r = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=pkg, capture_output=True, text=True, timeout=300,
        env={**os.environ, "HOME": real_home or os.environ.get("HOME", ""),
             "PYTHONDONTWRITEBYTECODE": "1"},
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, (
        f"一个测试泄漏的 HOME 污染了下一个 —— conftest 那个还原兜底没生效:"
        f"\n{out[-600:]}"
    )


# --- 7) 判据自检 ---

def test_this_file_does_not_use_unparse_plus_substring():
    """r80 立的规矩:结构检查不许退化成文本匹配

    本文件用 AST 查自己有没有用 `ast.unparse`,就得自己也守住这条 ——
    否则下一个来改的人会照着坏样子继续抄(r85/r92 各栽过一次)。
    """
    src = ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))
    offenders = [
        n.lineno for n in ast.walk(src)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "unparse"
    ]
    assert not offenders, (
        f"第 {offenders} 行用了 ast.unparse —— r85/r92 各栽过一次,"
        f"unparse 出来的文本里一个字符串字面量就能把这个检查喂饱"
    )
