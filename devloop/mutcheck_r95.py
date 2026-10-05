"""r95 变异测试:验「pytest 配置和声明的依赖必须是真的」这件事在守。

## 主题

`pyproject.toml` 里留着两样为 pytest-asyncio 准备的东西,而 pytest-asyncio
在本机装不上(PEP 668)、从 r92 起也不再需要:

    dev = [..., "pytest-asyncio>=0.21"]   ← 装不上
    asyncio_mode = "auto"                 ← pytest 压根不认识这个键

于是**每一次 pytest 运行**都报一条 `PytestConfigWarning: Unknown config
option: asyncio_mode`。实测一次全量变异扫描要起 446 次 pytest,每次都带。

一条每次都出现的告警等于没有告警 —— 人会脱敏,下一条真告警跟着被忽略。

## 关键设计:一半的变异动的是**被声明的东西**,不是判据

M1/M2/M3 改 `pyproject.toml`(真实数据会变),所以判据 1/2/3 在真实数据上
就被触发。M4/M5/M6 改的是判据自己的检查逻辑 —— 那些在真实数据上**恒过**
(真 dev 里每个包都装着、没有插件包),所以判据里必须额外喂合成样本,
否则这三条会全部存活。这和 r94 的名单判据是同一个坑,同一个解法。

## 变异清单

实现变异(期望全被杀):
  M1 把 pytest-asyncio 加回 dev 依赖        → 判据 2/3 红
  M2 把 asyncio_mode 加回 ini_options      → 判据 1 红(未知配置项)
  M3 加一个别的 pytest 插件(pytest-cov)   → 只有判据 3 红(它是装着的,
                                            所以判据 2 放行,隔离干净)
  M5 判据 2 接不住「没装」(except 换成别的) → 合成样本红
  M6 判据 3 插件前缀写错                     → 合成样本红

覆盖变异(期望全存活):
  M4 判据 1 不再收集未知配置项
  C1 判据 4 去掉 PYTEST_DISABLE_PLUGIN_AUTOLOAD
  C2 拆掉「dependencies 恒空」那条判据

## M4 的预期写错过一次,如实记下来

头一版把 M4 列进「期望全被杀」,实测存活。查下去不是判据写坏,是我归错类:
拆掉判据 1 的收集逻辑之后「没有任何东西会发现」,因为判据 1 就是唯一能
逮住 M2 的那一条。**一条变异只能被别的判据逮住,不能自己逮住自己。**
于是它是覆盖变异,预期存活。存活是实测结果,不是把账做平。

## C1 也如实存活,而且它值一条负结果

C1 去掉那个环境变量之后判据照样绿。事实是:现在这套东西在「插件全开」
和「插件全关」下都跑得通,所以判据 4 目前只能当**防退化**用,还当不了
**探测器**。记下来,不改成被杀。

## r95 的第二处扩展(同 r94 那处)

目标含 `.toml`,`_write_checked` 按后缀分流(py→ast / json→json.loads /
toml→tomllib)。报「你语法写错了」和报「你的变异工具不支持这个文件类型」
是两回事。

约定:元组第 4 位 = 期望存活(True/False),targets 显式声明。
CLAIMS 只写字面量 —— r86/r88/r89/r93/r94 各栽一次,这是第六次,别再栽。
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import tomllib

REPO = pathlib.Path(__file__).resolve().parent.parent
PYPROJECT = REPO / "pyproject.toml"
CONFTEST = REPO / "tests" / "conftest.py"
CRIT = REPO / "tests" / "test_pytest_config_and_dev_deps_are_real.py"

TARGET = ["tests/test_pytest_config_and_dev_deps_are_real.py"]

_RUN_SUMMARY_RE = re.compile(r"^(?:=+\s*)?(.*\bin [\d.]+s.*)$", re.M)
_ERROR_IN_SUMMARY_RE = re.compile(r"\d+\s+errors?\b")


def _run_itself_broke(outp: str) -> bool:
    lines = _RUN_SUMMARY_RE.findall(outp)
    if not lines:
        return True
    tail = lines[-1]
    if "no tests ran" in tail:
        return True
    return _ERROR_IN_SUMMARY_RE.search(tail) is not None


# ── pyproject 侧锚点 ──

DEV_BLOCK = 'dev = [\n    "pytest>=7",\n]\n'
DEV_WITH_ASYNCIO = ('dev = [\n    "pytest>=7",\n    "pytest-asyncio>=0.21",\n]\n')
DEV_WITH_PLUGIN = 'dev = [\n    "pytest>=7",\n    "pytest-cov>=7",\n]\n'

INI_SECTION = '[tool.pytest.ini_options]\n'
INI_WITH_MODE = '[tool.pytest.ini_options]\nasyncio_mode = "auto"\n'

# ── 判据侧锚点 ──

COLLECT_UNKNOWN = ('    unknown = [ln for ln in out.splitlines() if "Unknown config option" in ln]\n')
COLLECT_UNKNOWN_GONE = '    unknown = []  # 变异 M4:不再收集未知配置项\n'

CATCH_MISSING = '        except importlib.metadata.PackageNotFoundError:\n'
CATCH_WRONG = '        except ZeroDivisionError:  # 变异 M5:接不住「没装」\n'

PLUGIN_PREFIX = '    return [r for r in reqs if _dist_name(r).startswith("pytest-")]\n'
PLUGIN_PREFIX_WRONG = '    return [r for r in reqs if _dist_name(r).startswith("pytest_")]\n'

# M7:conftest 里那个「每个测试后还原 HOME」的兜底(本轮新增)
HOME_RESTORE = ('    saved = os.environ.get("HOME")\n'
                '    yield\n'
                '    if saved is None:\n'
                '        os.environ.pop("HOME", None)\n'
                '    else:\n'
                '        os.environ["HOME"] = saved\n')
HOME_RESTORE_GONE = '    yield  # 变异 M7:故意不还原 HOME\n'

# C1:去掉关插件的环境变量
DISABLE_ENV = '             "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},\n'
DISABLE_ENV_GONE = '             },  # 变异 C1:不关插件了\n'

# C2:拆掉 dependencies 恒空那条
C2_BODY = 'def test_runtime_dependencies_stay_empty():'
C2_STUB = 'def test_runtime_dependencies_stay_empty():\n    pass  # 变异 C2:整条判据没了\n'

CLAIMS = {
    "M1-把pytest-asyncio加回dev依赖": (
        ['"pytest-asyncio>=0.21"'],
        ['dev = [\n    "pytest>=7",\n]\n'],
    ),
    # 纯插入(在 ini_options 段头后面加一行)→ must_not 只能空。
    # 头一版把 must_not 写成段头那行,结果替换后的文本**仍然包含它**
    # —— 替换文本 = 段头 + 新键,于是 must_not 永远验不过,报的是
    # 「替换没有做到它名字声称的事」,其实它做到了。r93 的 C1/C2 栽过同款。
    "M2-把asyncio_mode加回ini_options": (
        ['\nasyncio_mode = "auto"\n'],
        [],
    ),
    "M3-加一个别的pytest插件": (
        ['"pytest-cov>=7"'],
        ['dev = [\n    "pytest>=7",\n]\n'],
    ),
    "M4-判据1不再收集未知配置项": (
        ['    unknown = []  # 变异 M4'],
        ['if "Unknown config option" in ln'],
    ),
    "M5-判据2接不住没装": (
        ['except ZeroDivisionError'],
        ['except importlib.metadata.PackageNotFoundError'],
    ),
    "M6-判据3插件前缀写错": (
        ['startswith("pytest_")'],
        ['startswith("pytest-")'],
    ),
    "M7-不还原HOME的兜底被弄没": (
        ['# 变异 M7:故意不还原 HOME'],
        ['    saved = os.environ.get("HOME")'],
    ),
    "C1-判据4去掉关插件的环境变量": (
        ['# 变异 C1:不关插件了'],
        ['"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"'],
    ),
    "C2-拆掉dependencies恒空那条判据": (
        ['    pass  # 变异 C2:整条判据没了'],
        ['    assert _pyproject()["project"]["dependencies"] == [], (\n'],
    ),
}

MUTANTS = [
    ("M1-把pytest-asyncio加回dev依赖",
     lambda p: _apply(p, DEV_BLOCK, DEV_WITH_ASYNCIO), False, (PYPROJECT,)),
    ("M2-把asyncio_mode加回ini_options",
     lambda p: _apply(p, INI_SECTION, INI_WITH_MODE), False, (PYPROJECT,)),
    ("M3-加一个别的pytest插件",
     lambda p: _apply(p, DEV_BLOCK, DEV_WITH_PLUGIN), False, (PYPROJECT,)),
    ("M5-判据2接不住没装",
     lambda p: _apply(p, CATCH_MISSING, CATCH_WRONG), False, (CRIT,)),
    ("M6-判据3插件前缀写错",
     lambda p: _apply(p, PLUGIN_PREFIX, PLUGIN_PREFIX_WRONG), False, (CRIT,)),
    ("M7-不还原HOME的兜底被弄没",
     lambda p: _apply(p, HOME_RESTORE, HOME_RESTORE_GONE), False, (CONFTEST,)),
]

# M4 从「实现变异」挪到这儿 —— 预期原本写错了。
#
# 头一版把 M4 列为「期望全被杀」,实测**存活**。查下去发现不是判据写坏,
# 是我把它归错了类:拆掉判据 1 的收集逻辑之后,「没有任何东西会发现」——
# 因为判据 1 就是唯一能逮住 M2(把 asyncio_mode 加回去)的那一条。
# 一条变异只能被**别的**判据逮住,不能自己逮住自己。所以它是覆盖变异。
# 如实记成存活,不改成被杀把账做平。
COVERAGE_MUTANTS = [
    ("M4-判据1不再收集未知配置项",
     lambda p: _apply(p, COLLECT_UNKNOWN, COLLECT_UNKNOWN_GONE), True, (CRIT,)),
    ("C1-判据4去掉关插件的环境变量",
     lambda p: _apply(p, DISABLE_ENV, DISABLE_ENV_GONE), True, (CRIT,)),
    ("C2-拆掉dependencies恒空那条判据",
     lambda p: _replace_tail(p, C2_BODY, C2_STUB), True, (CRIT,)),
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
    """写盘前的唯一出口。按后缀分流:py→ast,json→json,toml→tomllib。

    r94 已经为 JSON 做过一次,r95 是同一个道理的第二处。**报「你语法写错了」
    和报「你的变异工具不支持这个文件类型」是两回事**,混起来就是又一次假失败。
    """
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
        import json
        try:
            json.loads(out)
        except ValueError as e:
            raise AssertionError(
                f"变异会让 {path.name} 不是合法 JSON({e})。文件未写入。") from None
    elif path.suffix == ".toml":
        try:
            tomllib.loads(out)
        except tomllib.TOMLDecodeError as e:
            raise AssertionError(
                f"变异会让 {path.name} 不是合法 TOML({e})。文件未写入。") from None
    path.write_text(out, encoding="utf-8")


def _apply(path: pathlib.Path, old: str, new: str) -> None:
    src = path.read_text(encoding="utf-8")
    if old not in src:
        raise AssertionError(f"变异锚点没命中 {path.name}:{old[:70]!r}")
    _write_checked(path, src.replace(old, new, 1))


def _replace_tail(path: pathlib.Path, start_marker: str, stub: str) -> None:
    """从 `start_marker` 到文件末尾,整段换成一条 pass 桩

    桩的 `def` 行**原样保留标记**:削掉 `()` 和冒号会产出 `def test_x`,
    那是语法错误(r94 栽过)。整行照抄最省心。
    """
    src = path.read_text(encoding="utf-8")
    if start_marker not in src:
        raise AssertionError(f"变异锚点没命中 {path.name}:{start_marker!r}")
    i = src.index(start_marker)
    if not start_marker.rstrip().endswith(":"):
        raise AssertionError(f"变异标记不是一条完整的 def 行:{start_marker!r}")
    _write_checked(path, src[:i] + stub)


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
        ("实现变异(动 pyproject / 判据逻辑,期望全被杀)", MUTANTS),
        ("覆盖变异(拆判据,期望全存活)", COVERAGE_MUTANTS),
    ):
        print(f"\n=== r95 {title} ===")
        for name, detail, outp in _sweep(mutants):
            ok = detail in ("killed", "survived")
            if not ok:
                bad += 1
            print(f"  {'OK ' if ok else '!! '}{name:34s} {detail}")
            if outp:
                print("     " + outp.replace("\n", "\n     ")[:300])
    print(f"\nr95 变异总结论: {bad} 个不符合预期")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import mutkit
    raise SystemExit(mutkit.locked(main))
