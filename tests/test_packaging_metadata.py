"""打包元数据必须对应真实代码

## 为什么需要

`pyproject.toml` 曾经这样写:

```toml
# 核心依赖:全部标准库,零外部 pip 依赖
# 增强依赖(可选):检测到则启用,否则降级
dependencies = []
[project.optional-dependencies]
full = ["typer>=0.9", "rich>=13", "httpx>=0.25", "pyyaml>=6",
        "apscheduler>=3.10", "textual>=0.50", "openpyxl>=3.1"]
```

两处不实(实测见 `docs/DEP_AUDIT.md`):

1. **7 个包一次都没被 import**。`arl_lite/**.py` 里第三方 import 计数全
   是 0,所以 `pip install arl-lite[full]` 装 7 个包,不会让任何一行代码
   走不同分支。
2. **"检测到则启用"描述的机制不存在**。代码里全部的 `except ImportError`
   都服务于跨平台(`fcntl`/`msvcrt`/`termios`),没有一处是"检测到某包就
   启用某功能"。

第 2 条比第 1 条更危险。死元数据只是噪音;**一句描述了不存在机制的注释**
会误导下一个人照着那个"套路"加依赖 —— 而那个套路并不存在。

> 声明的东西必须对应真实的代码。承诺了却没有,比不承诺更糟。

本文件把这条钉住。核心思路和 `test_devloop_loc_metric.py` 里
"红线常量不许被悄悄改"一致:**元数据不能描述一个不存在的世界。**
"""
from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).parents[1]
PYPROJECT = REPO / "pyproject.toml"

# 允许出现在 arl_lite/ 里的顶层 import(标准库 + 项目自身)。
# 与 devloop/gates.py 的 no_thirdparty_import 门禁用同一套判定:
# 判的是"这个包名是不是标准库",不是维护一张白名单 ——
# 白名单会随新标准库版本过时,而 set(sys.stdlib_module_names) 不会。
import sys

_STDLIB = set(sys.stdlib_module_names)


def _declared_deps() -> dict[str, list[str]]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    proj = data.get("project", {})
    out = {"dependencies": list(proj.get("dependencies") or [])}
    for group, items in (proj.get("optional-dependencies") or {}).items():
        out[f"optional:{group}"] = list(items)
    return out


# 每类依赖组该在哪个目录里被用到。运行时依赖必须在 arl_lite/ 里,
# 测试工具在 tests/ 里。**分开是有意的** —— 如果一律扫 tests/,
# 运行时依赖只要在某个测试里 import 一下就算"有使用",那道门形同虚设。
_GROUP_IMPORT_ROOT = {
    "dev": "tests",  # 测试工具:被 tests/ 使用
}

# 靠**配置**激活、因此不会出现在 import 里的包。
# 每一项都必须写明"它靠什么生效",否则这个白名单就成了绕过检查的后门。
_DECLARATIVE = {
    "pytest-asyncio": (
        "pytest 插件,通过 pyproject 的 asyncio_mode=\"auto\" 激活。"
        "它不在任何文件里 import —— 装了它,pytest 就接管 async 测试。"
    ),
}


def _imports_in(dirname: str) -> set[str]:
    """指定目录下所有顶层第三方 import 的包名"""
    found: set[str] = set()
    for py in sorted((REPO / dirname).rglob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in tree.body:  # 只看模块顶层
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module.split(".")[0]]
            for m in mods:
                if m not in _STDLIB and m != "arl_lite":
                    found.add(m)
    return found


def _top_level_imports() -> set[str]:
    return _imports_in("arl_lite")


def _req_name(req: str) -> str:
    """`httpx>=0.25` → `httpx`"""
    return req.split(";")[0].split("[")[0].strip().split("=")[0] \
        .split(">")[0].split("<")[0].split("~")[0].split("!")[0].strip()


# =====================================================================
# 核心:零依赖
# =====================================================================


def test_core_has_no_pip_dependencies():
    assert _declared_deps()["dependencies"] == [], (
        "核心依赖必须为空。arl-lite 的定位是 PyPI 不可达环境也能跑"
    )


def test_no_thirdparty_imports_at_module_top_level():
    """顶层不许有第三方 import

    只查顶层:函数内的 `import` 通常是有意的可选路径(比如跨平台分支)。
    顶层 import 会在包被加载的瞬间执行,一旦失败就是 ImportError 崩在
    启动阶段 —— 那和"可选降级"是两回事。
    """
    found = _top_level_imports()
    assert not found, (
        f"arl_lite/ 顶层出现了第三方 import:{sorted(found)}\n"
        f"  这会破坏零依赖承诺(PyPI 不可达环境直接起不来)。"
    )


# =====================================================================
# 关键:声明的每个依赖都得有对应代码
# =====================================================================


def test_every_optional_dependency_is_actually_imported():
    """**每个**被声明的可选依赖,代码里必须真的 import 它

    这是本文件的核心,直接对应 `full` extras 那 7 个包 import 次数为 0
    的问题。

    死元数据不只是噪音:
    - PyPI 页面显示的依赖面与实际不符
    - Dependabot / pip-audit 会对 7 个与本项目无关的包报 CVE,
      把真正该看的告警淹掉
    - 最坏的一种:注释写着"检测到则启用",而那个分支根本不存在 ——
      下一个人会照着不存在的套路加新依赖
    """
    imported = _top_level_imports()
    test_imported = _imports_in("tests")
    for group, reqs in _declared_deps().items():
        if group == "dependencies":
            continue  # 核心必须为空,上面那条已经断言
        short = group.split(":", 1)[1] if ":" in group else group
        # 测试工具在 tests/ 里被用,运行时依赖在 arl_lite/ 里被用
        where = _GROUP_IMPORT_ROOT.get(short, "arl_lite")
        seen = test_imported if where == "tests" else imported
        for req in reqs:
            name = _req_name(req)
            if name in _DECLARATIVE:
                continue
            assert name in seen, (
                f"{group} 声明了 {req!r},但 {where}/ 里从来没有 import 过它。\n"
                f"  声明了却不用 = 死元数据:装它不改变任何行为,却让依赖面虚高、\n"
                f"  让 CVE 扫描报一堆与本项目无关的告警。\n"
                f"  要么删掉声明,要么真的加上使用它的代码(连同降级分支和测试)。\n"
                f"  如果它靠配置激活(pytest 插件之类),把它加进 _DECLARATIVE "
                f"并写明靠什么生效 —— 白名单必须可 review,不能是万能钥匙。"
            )


def test_optional_dependency_groups_are_intentional():
    """optional-dependencies 里的每一组,都要能说清它是干什么的

    `dev`(测试工具)说得清;`full`(运行时增强)当初说不清 —— 因为没有
    代码会用它。这条把「每一组都得有存在理由」变成可检查的。

    允许的组:只放开发/测试工具。运行时依赖若将来真需要,必须走
    `test_every_optional_dependency_is_actually_imported` 那条。
    """
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    groups = set((data.get("project", {}).get("optional-dependencies") or {}))
    # pytest / pytest-asyncio 是本仓库自己跑测试要用的,不是运行时依赖
    known_test_only = {"dev"}
    assert groups <= known_test_only, (
        f"出现了非测试用途的依赖组:{sorted(groups - known_test_only)}\n"
        f"  运行时可选依赖必须同时有使用它的代码 + 降级分支 + 测试。"
        f"当前组:{sorted(groups)}"
    )


def test_declarative_allowlist_stays_minimal_and_justified():
    """`_DECLARATIVE` 白名单不许变成万能钥匙

    这个白名单存在的唯一目的,是容纳"靠配置激活所以不会 import"的包。
    它天然有被滥用的倾向:把包加进去,检查就绕过了,而加的人可能根本没
    想清楚那个包是怎么生效的。

    所以三条硬规则:
    1. 每一项都要写明理由,而且理由不能空
    2. 白名单里的包必须**真的在声明里** —— 留着不用的条目会让人以为
       那个豁免还生效着
    3. 体积上限。超过 5 个说明依赖形态失控了,该停下来重新想
    """
    assert len(_DECLARATIVE) <= 5, (
        f"_DECLARATIVE 有 {len(_DECLARATIVE)} 项,超过 5 —— "
        f"这么多'声明了但不 import'的包,说明依赖形态本身出了问题,"
        f"不该继续往白名单里加"
    )
    declared = {
        _req_name(r)
        for reqs in _declared_deps().values()
        for r in reqs
    }
    for name, why in _DECLARATIVE.items():
        assert why.strip(), f"{name} 在白名单里但没写理由"
        assert len(why.strip()) >= 10, (
            f"{name} 的理由太短,看不出它到底靠什么生效"
        )
        assert name in declared, (
            f"{name} 在 _DECLARATIVE 里但 pyproject 并没有声明它 —— "
            f"留着一条用不上的豁免,会让人以为它还生效着"
        )


def test_pyproject_is_still_parseable_and_declares_the_entrypoint():
    """改 pyproject 时最容易犯的错是手滑写坏 TOML

    这条很土,但它守的是"改元数据"这个动作本身 —— 前面几条都在读它,
    读不动的话那些测试会以"跳过"或"报错"的形式失效,而不是明确失败。
    """
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert data["project"]["name"] == "arl-lite"
    assert data["project"]["scripts"]["arl-lite"] == "arl_lite.cli:main"
    assert data["project"]["requires-python"], "requires-python 不能丢"
