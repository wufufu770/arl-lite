"""「注册了却从不被读」的参数必须如实标注 —— 靠源码推导守，不靠手写清单。

背景:本仓已有一条约定 —— 参数确实被忽略时,help 写 `(忽略)`(`workspace list -w`)。
但约定只落地在一处,没人守:
  - `watch stop -w` 的 help 写「工作空间名」**暗示生效**,而 `cmd_watch_stop`
    整个函数只打印「watch 是同步模式,去 Ctrl+C」,不读 args 任何字段。
  - 用户敲 `watch stop -w teamA`,以为停的是 teamA 的调度器,其实这条参数
    从来没被看过。

为什么用推导 + 双向不变量,而不是逐条行为测试(r69 的结论):
  - 「有没有被读」可以从源码**可靠**推出(cmd 函数 + 它调用的同模块辅助函数里
    出现 `args.X` 或 `getattr(args,"X",…)`),42 条命令路径里只落出 2 条,零假阳性;
  - 「该不该被读」**推不出来**(r69 实测 `cmd_watch_add` 明明在写却被判成只读)。
  所以:可靠的部分用推导,不可靠的部分用显式例外清单兜住,并要求每条例外写理由。
  例外清单本身若已不再需要,同样报红(r69 实测当场逮到我一条 stale 条目)。

推导出 0 条不等于这条判据没用 —— 它是让「新增一个被忽略的参数」变成
一次有意的决定,而不是一次没人注意的漂移。所以条数钉死。

## r86:补上反向那一半(上面「双向不变量」此前只实现了一半)

本文件此前只推导了**一个方向**:「注册了却从不被读 → help 必须写忽略」。
另一半没实现:「help 写了忽略 → 必须真的从未被读」。

r86 实测这个缺口是真空的:把 `query -w` 的 help 从「工作空间名」改成
「工作空间名(忽略)」——这个参数**真的**被 `cmd_query` 读——本文件 6 条
判据**全绿**。

这个方向的谎比原 bug 更危险。原 bug 是「参数被忽略、help 暗示生效」,
用户多传一个参数,白传;这一半是「参数生效、help 说忽略」,用户**不传**
,于是静默吃了默认值 —— 连「白传」这个提示都没有。
"""

from __future__ import annotations

import ast
import inspect
import sys

import pytest

import arl_lite.cli as cli

# 判据守的东西:推导出的「注册了却从不被读」的参数**只应有这么多条**。
# 新增一条就意味着有人新加了个被静默忽略的参数,必须是有意的决定。
PINNED_SILENTLY_IGNORED = {
    ("watch stop", "workspace"),
    ("workspace list", "workspace"),
}

# 例外:推导命中、且**有正当理由**不标注为「忽略」的。
# 理由必须写清为什么它被忽略却不该标 (Key Decision 10:例外清单不许只列名字)。
# 留空 = 本轮没有例外。r69 靠这条当场逮到过我自己一条敷衍的 stale 条目。
EXCEPTIONS: dict[tuple[str, str], str] = {}

# argparse 自己注入 / 全局在 main() 里读 / 子命令组用于分派的 dest —— 不算参数
_INFRA_DESTS = {"help", "func", "log_level"}


def _module_funcs(module) -> dict[str, ast.FunctionDef]:
    try:
        tree = ast.parse(inspect.getsource(module))
    except (OSError, TypeError, SyntaxError):  # pragma: no cover - 源码不可得时跳过
        return {}
    return {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}


def _direct_reads(node: ast.AST) -> set[str]:
    got: set[str] = set()
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name)
                and sub.value.id == "args"):
            got.add(sub.attr)
        if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                and sub.func.id == "getattr" and len(sub.args) >= 2
                and isinstance(sub.args[0], ast.Name) and sub.args[0].id == "args"
                and isinstance(sub.args[1], ast.Constant)
                and isinstance(sub.args[1].value, str)):
            got.add(sub.args[1].value)
    return got


def _closure(name: str, module, tables: dict, seen: set | None = None) -> set[str]:
    """函数自身 + 它调用的同模块辅助函数,读到的 args dest 全集。"""
    seen = set() if seen is None else seen
    key = (id(module), name)
    if key in seen:
        return set()
    seen.add(key)
    table = tables.setdefault(id(module), _module_funcs(module))
    fn = table.get(name)
    if fn is None:
        return set()
    got = _direct_reads(fn)
    for sub in ast.walk(fn):
        if not isinstance(sub, ast.Call):
            continue
        callee = (sub.func.id if isinstance(sub.func, ast.Name)
                  else sub.func.attr if isinstance(sub.func, ast.Attribute) else None)
        if callee in table:
            got |= _closure(callee, module, tables, seen)
    return got


def _command_paths() -> dict[str, tuple[object, set[str]]]:
    """{命令路径: (cmd 函数对象, 该路径上可见的 dest 全集)}"""
    out: dict[str, tuple[object, set[str]]] = {}

    def walk(parser, prefix, inherited):
        dests = set(inherited) | {a.dest for a in parser._actions}
        fn = parser.get_default("func")
        if fn is not None:
            out[prefix.strip()] = (fn, dests)
        for action in parser._actions:
            choices = getattr(action, "choices", None)
            if isinstance(choices, dict) and hasattr(action, "_name_parser_map"):
                for name, sub in choices.items():
                    walk(sub, f"{prefix} {name}", dests)

    walk(cli.build_parser(), "", set())
    return out


def _help_text(path: str, dest: str) -> str:
    """取某个参数在 argparse 里登记的 help 文案。

    直接问 argparse 要 `action.help`,**不解析 `--help` 的文本排版** ——
    首版按行匹配 `-W` / `--workspace`,而实际输出是 `-w WORKSPACE, --workspace
    WORKSPACE`,两个匹配都落空,返回空串。结构信息要从结构里取,别去猜它印成什么样。
    """
    target = None
    parser = cli.build_parser()
    parts = path.split()

    def walk(p, i: int):
        nonlocal target
        for action in p._actions:
            choices = getattr(action, "choices", None)
            if isinstance(choices, dict) and hasattr(action, "_name_parser_map"):
                if i < len(parts) and parts[i] in choices:
                    walk(choices[parts[i]], i + 1)
        if target is None and i == len(parts):
            for action in p._actions:
                if action.dest == dest and action.help:
                    target = action.help
                    return

    walk(parser, 0)
    return target or ""


# 本仓约定的「这个参数真的不生效」标记,r75 定的
IGNORE_MARKER = "忽略"


def _all_user_args() -> dict[tuple[str, str], str]:
    """全部用户参数的 help 文案:`{(命令路径, dest): help}`。

    r86 新增。正向那条判据只需要「被忽略的那几个」,所以历来只从 `derived`
    出发;反向那条必须**从全部参数出发**扫「谁标了忽略」—— 漏扫的那些
    正是谎标的那几个。
    """
    out: dict[tuple[str, str], str] = {}
    for path, (_fn, dests) in _command_paths().items():
        for d in sorted(dests - _INFRA_DESTS):
            if d.endswith("_cmd") or d == "command":
                continue  # argparse 用它分派,不是用户参数
            out[(path, d)] = _help_text(path, d)
    return out


def _annotated_as_ignored() -> set[tuple[str, str]]:
    """help 里标了「忽略」的全部参数。"""
    return {k for k, help_text in _all_user_args().items()
            if IGNORE_MARKER in help_text}


@pytest.fixture(scope="module")
def derived() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path, (fn, dests) in _command_paths().items():
        used = _closure(fn.__name__, sys.modules.get(fn.__module__), {})
        for d in dests - used - _INFRA_DESTS:
            if d.endswith("_cmd") or d == "command":
                continue  # argparse 用它分派,不是用户参数
            found.add((path, d))
    return found


def test_the_derived_set_is_pinned(derived):
    """推导结果钉死 —— 新增一条必须是有人有意的决定。"""
    assert derived == PINNED_SILENTLY_IGNORED, (
        f"「注册了却从不被读」的参数集合变了:{sorted(derived)} != "
        f"{sorted(PINNED_SILENTLY_IGNORED)}。"
        f"新增一条 = 有人新加了个被静默忽略的参数,先弄清它是不是真被忽略。"
    )


def test_every_silently_ignored_arg_says_so(derived):
    """每个被静默忽略的参数,help 必须写「忽略」—— 不得让文案暗示它生效。"""
    lying = []
    for path, dest in sorted(derived - set(EXCEPTIONS)):
        help_text = _help_text(path, dest)
        if "忽略" not in help_text:
            lying.append(f"{path} -{dest}: help 写的是 {help_text[:70]!r},没提「忽略」")
    assert not lying, (
        "这些参数从未被命令读取,help 却不说明,用户会以为它生效了:\n  "
        + "\n  ".join(lying)
    )


def test_ignore_annotations_must_be_true(derived):
    """反向不变量:标了「忽略」的参数,必须**真的**从未被读过。

    正向那条只堵一半。这一半的谎更贵:参数生效,help 却说忽略,用户据此
    判定「不用传」,于是静默吃了默认值,连「白传」那点提示都没有。

    r86 实测:把 `query -w` 的 help 改成「工作空间名(忽略)」,本文件
    此前 6 条判据全绿 —— 那个参数是真的被 `cmd_query` 读的。
    """
    lying = sorted(_annotated_as_ignored() - derived)
    assert not lying, (
        "这些参数 help 标了「忽略」,实际却被命令读取 —— 用户会以为不用传:\n  "
        + "\n  ".join(f"{p} -{d}: {_all_user_args()[(p, d)]!r},但它真被读了" for p, d in lying)
        + "\n二选一:要么命令真的别读它,要么把 help 改回描述它实际的作用。"
    )


def test_the_ignore_scan_actually_finds_annotations():
    """反向判据不许空转 —— 扫不到「忽略」标注时,上面那条会恒真。

    判据不能靠「现在恰好一条谎都没有」来证明自己在跑。r85 的教训照搬:
    只验退出码,「永远返回 INFO」那种退化能蒙过去。这里钉的是**扫描器
    看得见东西**这件事本身。
    """
    found = _annotated_as_ignored()
    assert found >= PINNED_SILENTLY_IGNORED, (
        f"反向扫描只找到 {sorted(found)},r75 钉死的 {sorted(PINNED_SILENTLY_IGNORED)} "
        f"至少要能被扫到。扫不到说明 help 文案或 _help_text 变了 —— "
        f"上面那条会变成恒真,不是实现变干净了。"
    )


def test_exceptions_need_a_real_reason():
    """例外必须写清理由,长度下限 10 字符 —— 只列名字等于没写。"""
    thin = [f"{k}: {v!r}" for k, v in EXCEPTIONS.items() if len(v.strip()) < 10]
    assert not thin, f"例外理由太短,只说了「是什么」没说明「为什么」:{thin}"


def test_exceptions_must_still_be_needed(derived):
    """已经不再需要的例外也要报红 —— 否则例外清单只增不减,最后变成黑名单。"""
    stale = sorted(set(EXCEPTIONS) - derived)
    assert not stale, (
        f"EXCEPTIONS 里这些条目已经不再命中推导结果:{stale}。"
        f"删掉它们,否则例外清单会退化成一张没人审的名字黑名单。"
    )


def test_exceptions_are_in_the_derived_set():
    """例外必须是推导真的命中的条目,不能凭空写一条来给自己开路。"""
    bogus = sorted(set(EXCEPTIONS) - set(PINNED_SILENTLY_IGNORED))
    assert not bogus, f"EXCEPTIONS 里的条目不在推导结果里:{bogus}"


def test_watch_stop_w_is_ignored_and_says_so():
    """`watch stop -w` 行为锚点:带上它输出必须一模一样,且 help 说「忽略」。"""
    import subprocess, tempfile, os

    def run(extra: list[str]):
        env = dict(os.environ, HOME=tempfile.mkdtemp(), PYTHONPATH=cli.__file__.rsplit("/arl_lite/", 1)[0],
                   PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run(
            [sys.executable, "-B", "-m", "arl_lite.cli", "watch", "stop", *extra],
            env=env, cwd=tempfile.gettempdir(), capture_output=True, text=True, timeout=60)

    plain, with_w = run([]), run(["-w", "teamA"])
    assert (plain.returncode, plain.stdout) == (with_w.returncode, with_w.stdout), (
        f"watch stop -w teamA 的行为与不带 -w 不同 —— 说明它其实被读了"
    )
    assert "忽略" in _help_text("watch stop", "workspace"), (
        "watch stop -w 的 help 没写「忽略」,却暗示它选工作区"
    )
