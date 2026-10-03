"""r60:面向用户的建议里不能混着跑不通的 arl-lite 命令。

背景(实测,不是推演):`arl-lite correlate first`、`arl-lite export json`、
README 里 `ai explain --finding-id` / `ai suggest --target` / `ai fix --rule-id`
共 5 条死路。跑不通的报错和真正原因毫无关系——用户看到「没关联分析」,照着提示
敲 `correlate first`,撞上的是 argparse 的「unrecognized arguments」。
给用户一条走不通的路,比不给更坏(r59 之后立的规矩)。

## 为什么不是「按建议动词扫」

种子的设想是只认 `run ...` / `试试 ...` 这类句式。实测下来那样更差:全仓库 101 处
`arl-lite` 出现里,`arl-lite` 前 24 字内带动词的只有 **15** 处,其余 **86** 处没有
——README 的命令清单、模块 docstring、AI prompt 模板全是裸命令行。按动词扫等于
漏掉 85%,而死路恰恰长在裸命令行里(本轮 README 那 3 条就全是裸命令)。

真正把噪声压下去的是**命令形状**本身:`arl-lite <token>` 的第一个 token 必须是
真子命令,最终提取到 87 条建议,只有 2 处形状撞车,而这 2 处各有明确理由排除:

| 撞车处 | 形状 | 为什么不是建议 | 排除规则 |
|---|---|---|---|
| `arl_lite/__init__.py` `__author__` | `arl-lite contributors` | 包元数据 | 模块级 dunder 赋值整个跳过 |
| `arl_lite/devloop/gates.py` 横幅 | `== arl-lite gates ==` | 标题行 | `arl-lite` 左边紧邻**成对**装饰符 |

成对而非单个:markdown 表格里的 `| arl-lite ... |` 和 `# arl-lite ...` 标题只有单个
符号,里面装的是真命令,实测没有一条真命令被这条规则吃掉。
`arl-lite v1.2.3` / `arl-lite v{__version__}` 是版本号不是命令,单独一条规则;
这条规则必须写成 `v` 后接数字/`{`/空白,写成 `arl-lite v` 会把真子命令
`arl-lite version` 一起吃掉——第一版探针正是这么写的,靠「`arl-lite version`
在候选列表里凭空消失」这件事发现的,`test_extractor_ignores_version_banners_but_keeps_the_version_subcommand`
把它钉住。

## 扫多大

扫 `arl_lite/**/*.py`(AST)+ `README.md` + `docs/**/*.md`(文本)。
不扫 `tests/`(里面故意有畸形输入)、不扫顶层 `devloop/`(变异脚本的字符串里全是
故意的坏命令)、不扫 `devloop/backlog.md`(任务队列,引的都是片段)。

## 查不动的就放过,但不许悄悄不查

占位符(`<provider>`、`{table}`)和被截断的建议会让 argparse 直接 SystemExit,
这种一律放过(保守方向:宁可漏报不可误报)。但放过不等于不查——
`test_criterion_actually_checks_enough_commands` 钉住「真验的条数」下限,
防止提取器哪天退化成什么都不产出,判据变成恒真。
"""
from __future__ import annotations

import ast
import contextlib
import io
import pathlib
import re
import shlex

import pytest

from arl_lite.cli import build_parser

REPO = pathlib.Path(__file__).resolve().parent.parent
PKG = REPO / "arl_lite"
MARKDOWN = [REPO / "README.md", *sorted((REPO / "docs").rglob("*.md"))]

# `arl-lite` 左边紧邻(允许空格)是**成对**装饰符 → 横幅/标题,不是命令。
# 只认成对的:`== arl-lite gates ==` 是横幅,而 markdown 表格里的 `| arl-lite ... |`
# 和 `# arl-lite ...` 标题都只有单个符号,里面装的是真命令,不能一起吃掉。
BANNER_LEFT = re.compile(r"([-=#*~^])\1\s*$")
# 版本号(arl-lite v1.2.3 / arl-lite v{__version__})不是命令;
# 注意不能写成 `arl-lite v`,那会把真子命令 `arl-lite version` 一起吃掉。
VERSION_BANNER = re.compile(r"arl-lite\s+v(?=[\d{.]|\s|$)")
# 命令 token 允许的字符(ASCII);中文正文、管道、重定向一律在此终止命令
TOKEN = re.compile(r"[\w.:/@=<>{},%-]+")


# ---------------------------------------------------------------- 提取


def _py_strings(path: pathlib.Path):
    """AST 遍历。产出 (行号, 文本, 是否 f-string)。

    跳过模块级 dunder 赋值(`__author__` 之类)——那是包元数据,不是建议。
    f-string 的取值槽用 \\x00 占位,免得把表达式源码当成命令的一部分。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))

    dunder = set()
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id.startswith("__") and t.id.endswith("__")
            for t in stmt.targets
        ):
            continue
        for node in ast.walk(stmt.value):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                dunder.add(id(node))

    in_fstring = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for part in node.values:
                in_fstring.add(id(part))
            yield node.lineno, "".join(
                p.value if isinstance(p, ast.Constant) and isinstance(p.value, str) else "\x00"
                for p in node.values
            ), True
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in in_fstring
            and id(node) not in dunder
        ):
            yield node.lineno, node.value, False


def _commands_in(text: str):
    """从一段文本里切出所有 `arl-lite <子命令> ...` 形状的命令。"""
    for m in re.finditer(r"arl-lite", text):
        seg = text[m.start() : text.find("\n", m.start()) if text.find("\n", m.start()) != -1 else len(text)]
        if VERSION_BANNER.match(seg) or BANNER_LEFT.search(text[: m.start()]):
            continue
        try:
            toks = shlex.split(seg, comments=False)
        except ValueError:
            toks = seg.split()
        if not toks or toks[0] != "arl-lite":
            continue
        keep = []
        for t in toks[1:]:
            t = t.rstrip(")]}>,;:.!")
            if t.isascii() and TOKEN.fullmatch(t):
                keep.append(t)
            else:
                break
        if keep:
            yield " ".join(["arl-lite", *keep])


def _all_advice():
    """全量建议。产出 (来源标签, 命令),同一条命令只出现一次。"""
    seen = set()
    for path in sorted(PKG.rglob("*.py")):
        for lineno, text, _ in _py_strings(path):
            for cmd in _commands_in(text):
                key = (str(path.relative_to(REPO)), cmd)
                if key not in seen:
                    seen.add(key)
                    yield f"{key[0]}:{lineno}", cmd
    for path in MARKDOWN:
        if not path.exists():
            continue
        rel = str(path.relative_to(REPO))
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for cmd in _commands_in(line):
                key = (rel, cmd)
                if key not in seen:
                    seen.add(key)
                    yield f"{rel}:{lineno}", cmd


# ---------------------------------------------------------------- 校验


def _real_subcommands() -> set:
    """真子命令表——唯一来源是 build_parser,判据不许自己抄一份。"""
    for action in build_parser()._actions:
        if action.dest == "command" and getattr(action, "choices", None):
            return set(action.choices)
    raise AssertionError("build_parser() 里找不到 command 子命令表")


def _leftovers(cmd: str):
    """跑一遍真 parser。返回 [] = 干净,None = 查不动(保守放过),否则是多余参数。"""
    parser = build_parser()
    sink = io.StringIO()
    try:
        with contextlib.redirect_stderr(sink), contextlib.redirect_stdout(io.StringIO()):
            _, extra = parser.parse_known_args(cmd.split()[1:])
    except SystemExit:
        return None
    return extra


ADVICE = list(_all_advice())
CHECKED = [(src, cmd) for src, cmd in ADVICE if _leftovers(cmd) is not None]


# ---------------------------------------------------------------- 判据


def test_every_advice_command_starts_with_a_real_subcommand():
    """A1:`arl-lite` 后面的第一个 token 必须是真子命令。"""
    real = _real_subcommands()
    bad = [(src, cmd) for src, cmd in ADVICE if cmd.split()[1] not in real]
    assert not bad, "建议里出现了不存在的子命令:\n" + "\n".join(f"  {s}: {c}" for s, c in bad)


def test_advice_command_leaves_no_unparseable_arguments():
    """A2:整条建议要能过 argparse,且不剩任何多余参数/未知开关。

    这条专治 r57/r58/r59/r60 同一个病:子命令对,但后面挂的东西跑不通
    (`correlate first`、`export json`、`--finding-id`)。
    """
    bad = [(src, cmd, _leftovers(cmd)) for src, cmd in CHECKED if _leftovers(cmd)]
    assert not bad, "建议里的命令跑不通(argparse 收不下多余部分):\n" + "\n".join(
        f"  {s}: {c}   多余={e}" for s, c, e in bad
    )


def test_criterion_actually_checks_enough_commands():
    """防恒真:提取器哪天退化成什么都不产出,判据不能跟着变成空转。

    实测基线:提到 87 条建议,其中 60 条能真验(其余因占位符/截断被保守放过)。
    下限取 75 / 55,给未来新增的 `<占位符>` 写法留余量,但不给退化的空间。
    对照过旧版提取器:新提取器多抓到 2 条(`arl-lite version`、`arl-lite devloop drop`),
    恰好排掉 2 处非建议(包元数据、gates 横幅),零真丢。
    """
    assert len(ADVICE) >= 75, f"只提到 {len(ADVICE)} 条建议,提取器多半退化了"
    assert len(CHECKED) >= 55, f"只有 {len(CHECKED)} 条真验,判据基本在空转"


def test_the_unchecked_ones_stay_unchecked():
    """「查不动就放过」不许被悄悄改成「查不动就当通过」。

    变异测试 M14 把 `except SystemExit: return None` 改成 `return []`,
    结果 27 条占位符型建议从「查不动」变成「已验通过」,一条测试都没红——
    判据的覆盖面少了一截,却没有任何声音。这是 r57~r59 一路吃过的暗亏:
    静默降级比降级本身更坏。

    所以这里把「查不动的那一批」钉成一个**有下界**的集合。实测 27 条
    (`<provider>`、`{table}`、devloop docstring 里被截断的清单)。
    """
    unchecked = [c for _, c in ADVICE if _leftovers(c) is None]
    assert len(unchecked) >= 10, (
        f"只剩 {len(unchecked)} 条查不动,其余 70+ 条全被当成「已验通过」了 —— "
        f"检查覆盖面被静默削掉了吗"
    )


# ---------------------------------------------------------------- 提取器自身


def test_extractor_finds_advice_in_python_and_markdown():
    """提取器要同时吃 .py 和 .md——死路在两边都真实发生过。"""
    py = {src for src, _ in ADVICE if src.endswith(".py") or ":1" in src or ".py:" in src}
    md = {src for src, _ in ADVICE if ".md:" in src}
    assert len(md) >= 10, f"markdown 只提到 {len(md)} 条建议"
    assert any(s.startswith("arl_lite/") for s in py), "python 侧一条都没提到"


def test_extractor_ignores_package_metadata():
    """`__author__ = "arl-lite contributors"` 是包元数据,不是建议。"""
    for src, cmd in ADVICE:
        assert "contributors" not in cmd, f"{src}: 把包元数据当成了建议"


def test_extractor_ignores_decorated_title_lines():
    """`== arl-lite gates ==` 是横幅,`gates` 压根不是子命令。"""
    for src, cmd in ADVICE:
        assert not cmd.startswith("arl-lite gates"), f"{src}: 把标题行当成了建议"


def test_extractor_ignores_version_banners_but_keeps_the_version_subcommand():
    """`arl-lite v{__version__}` 是版本号;`arl-lite version` 是真命令,不能一起吃掉。"""
    assert any(c == "arl-lite version" for _, c in ADVICE), "arl-lite version 被版本号规则误伤了"
    for src, cmd in ADVICE:
        assert not re.match(r"arl-lite v[\d{]", cmd), f"{src}: 把版本号当成了命令"


def test_extractor_stops_at_non_ascii_prose():
    """`arl-lite query correlations 看)` 里的「看」是正文,不是参数。"""
    for _, cmd in ADVICE:
        assert all(t.isascii() for t in cmd.split()), f"把中文正文吃进了命令: {cmd!r}"


# ---------------------------------------------------------------- 本轮 5 处死路的回归


@pytest.mark.parametrize(
    "src, cmd",
    [
        ("arl_lite/ai/commands.py", "arl-lite correlate"),
        ("arl_lite/cli.py", "arl-lite export --format json --workspace example.com"),
    ],
)
def test_fixed_advice_is_present_and_clean(src, cmd):
    """修完不能顺手把建议删干净了——删建议不叫修,得是能跑的建议。"""
    found = [c for s, c in ADVICE if s.startswith(src)]
    assert cmd in found, f"{src} 里找不到修好的建议 {cmd!r}"
    assert not _leftovers(cmd), f"{cmd!r} 还是跑不通"


def test_readme_ai_commands_have_no_invented_flags():
    """README 那 3 条曾经凭空发明了 --finding-id / --target / --rule-id。"""
    readme = [c for s, c in ADVICE if s.startswith("README.md")]
    for bogus in ("--finding-id", "--target", "--rule-id"):
        assert not any(bogus in c for c in readme), f"README 又写回了 {bogus}"


def test_ai_explain_and_fix_take_positional_ids_not_flags():
    """钉住语义:`ai explain` 要 corr_id、`ai fix` 要 finding_id,都是位置参数。

    原来 README 那两条即便改对 flag 名也是错的——`--finding-id 123` 传给
    `ai explain` 依然是「关联 ID」,而 123 是 finding id,修好了也报错。
    """
    parser = build_parser()
    for path, sub in ((["ai", "explain"], "corr_id"), (["ai", "fix"], "finding_id")):
        cur, found = parser, False
        for seg in path:
            for action in cur._actions:
                choices = getattr(action, "choices", None)
                if isinstance(choices, dict) and seg in choices:
                    cur, found = choices[seg], True
                    break
            assert found, f"arl-lite {' '.join(path)} 不存在"
        dests = [a.dest for a in cur._actions if not a.option_strings and a.dest != "help"]
        assert sub in dests, f"arl-lite {' '.join(path)} 收的位置参数是 {dests},不含 {sub}"
