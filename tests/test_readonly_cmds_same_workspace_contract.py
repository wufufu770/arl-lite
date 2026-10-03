"""只读命令的工作区口径必须一致,不许一半拒绝一半静默建库(r68)。

背景(全部实测,每条命令配**独立的干净 HOME**):

零工作区下,10 个只读子命令里有 3 个**静默建库**并返回「全 0」:

    arl-lite risk summary      rc=0  建出 default,打印「全 0 概览」
    arl-lite monitor list      rc=0  建出 default,打印「no monitors」
    arl-lite monitor changes   rc=0  建出 default,打印「no changes」

而同族的另外 7 个(stats / query / search / export / diff / correlate /
risk top)rc=1 报 `workspace not found` 并给出路。

最刺眼的是 `risk top` 与 `risk summary` —— **同一个 risk 的两个子命令**,
一份说「工作区不存在」,一份直接把工作区建出来再告诉你「一切正常」。
用户没法判断自己的数据是真没了,还是被静默初始化了。

反过来也有不该动的:
- `workspace list` / `workspace create` 静默建库是**对的** —— 它们本就该建。
- `version` / `tools check` / `watch list` 压根不碰工作区。

判据守的正是这条分界:**只读命令**要么拒绝并给路,要么压根不碰;
不许静默建库。而判据不靠「我以为哪些是只读」来划线,靠源码:
只读 = 该函数只读 storage / 走只读查询路径,不改数据。
"""
from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
CLI = REPO / "arl_lite" / "cli.py"

# r68 修复后应当统一口径的只读子命令(实测逐条确认)。
# 判据比它守的事窄:这里只列**本轮实测过的**那些,不追求覆盖全部子命令。
MUST_REJECT = [
    ["stats"],
    ["query", "domains"],
    ["search", "domains", "example"],
    ["export"],
    ["diff"],
    ["correlate"],
    ["risk", "top"],
    ["risk", "summary"],
    ["monitor", "list"],
    ["monitor", "changes"],
]

# 这些**必须继续**建库/不校验 —— 判据要钉住「别把好的也改了」。
MUST_NOT_REJECT = [
    ["workspace", "list"],
    ["workspace", "create", "probe-ws"],
    ["version"],
    ["tools", "check"],
]


def _run(argv, home):
    return subprocess.run(
        [sys.executable, "-B", "-m", "arl_lite.cli", *argv],
        capture_output=True, text=True, cwd=REPO,
        env=dict(os.environ, HOME=str(home), PYTHONDONTWRITEBYTECODE="1"),
        timeout=180,
    )


def _workspaces_created(home) -> list:
    root = pathlib.Path(home) / ".arl-lite" / "workspaces"
    return sorted(d.name for d in root.iterdir()) if root.exists() else []


# ---------------------------------------------------------------- 源码头


def test_the_readonly_list_matches_the_source():
    """判据自己列的命令必须是**真实存在的子命令**。

    没有这条,MUST_REJECT 里混进一个已被删/改名的子命令时,
    下面的判据会静默空转(它只是查不到报错而已)。
    这跟 r63 立的那条「类列表被清空后判据静默空转」是同一类坑。

    唯一来源是 build_parser —— 判据不许自己抄一份子命令表
    (抄了就会漂,r 的决策 #8)。首版这里是文本搜索,而且还写了个
    `or f"def cmd_risk" in src`,那个 `or` 让每一项都通过,恒真。
    r63 立过「恒真测试比没有测试更糟」的规矩,这里自己犯了。
    """
    from arl_lite.cli import build_parser

    real = None
    for action in build_parser()._actions:
        if action.dest == "command" and getattr(action, "choices", None):
            real = set(action.choices)
    assert real, "build_parser() 里找不到子命令表"

    for argv in MUST_REJECT + MUST_NOT_REJECT:
        assert argv[0] in real, (
            f"判据里的子命令 `{argv[0]}` 不存在 —— 清单与源码漂了"
        )
    assert len(MUST_REJECT) == 10, (
        f"只读命令清单应当有 10 条,实测 {len(MUST_REJECT)}:{MUST_REJECT}"
    )


@pytest.mark.parametrize("argv", MUST_REJECT, ids=lambda a: " ".join(a))
def test_readonly_cmds_reject_a_missing_workspace(tmp_path, argv):
    """零工作区时,只读命令必须 rc=1 报 workspace not found,且**不许建库**。"""
    home = tmp_path / "reject"
    home.mkdir()
    r = _run(argv, home)
    combined = r.stdout + r.stderr
    assert r.returncode != 0, (
        f"`arl-lite {' '.join(argv)}` 在零工作区下 rc=0 —— 静默建库了"
    )
    assert "workspace not found" in combined, (
        f"`arl-lite {' '.join(argv)}` 拒绝了但没说为什么:{combined[-200:]}"
    )
    assert not _workspaces_created(home), (
        f"`arl-lite {' '.join(argv)}` 拒绝了却把工作区建了出来:"
        f"{_workspaces_created(home)}"
    )


@pytest.mark.parametrize("argv", MUST_NOT_REJECT, ids=lambda a: " ".join(a))
def test_commands_that_should_keep_working_still_work(tmp_path, argv):
    """反向不变量:不该加校验的必须还是原样。

    判据要比它守的事窄 —— 只把该改的改了,把好的也一起禁掉是回归。
    """
    home = tmp_path / "keep"
    home.mkdir()
    r = _run(argv, home)
    assert r.returncode == 0, (
        f"`arl-lite {' '.join(argv)}` 本该照常工作却 rc={r.returncode}:"
        f"{(r.stdout + r.stderr)[-200:]}"
    )


def test_workspace_commands_may_still_create_a_workspace(tmp_path):
    """`workspace list` / `workspace create` 静默建库是对的,不许被禁掉。"""
    home = tmp_path / "wscreate"
    home.mkdir()
    _run(["workspace", "list"], home)
    assert "default" in _workspaces_created(home), (
        f"workspace list 没能建出 default:{_workspaces_created(home)}"
    )


# ---------------------------------------------------------------- 端到端


def test_readonly_cmds_agree_on_an_existing_workspace(tmp_path):
    """有工作区时,MUST_REJECT 里每条都必须真的跑得通。

    上一条只验「零工作区时拒绝」,那一半过了不代表加校验没写坏
    正常路径 —— 这是两件事,必须都验。
    """
    home = tmp_path / "existing"
    home.mkdir()
    assert _run(["workspace", "list"], home).returncode == 0

    failed = []
    for argv in MUST_REJECT:
        r = _run(argv, home)
        if r.returncode != 0:
            failed.append(f"  `arl-lite {' '.join(argv)}` rc={r.returncode}: "
                          f"{(r.stdout + r.stderr).strip()[-150:]}")
    assert not failed, "有工作区时这些只读命令却跑不通:\n" + "\n".join(failed)


def test_risk_top_and_summary_now_agree(tmp_path):
    """`risk top` 与 `risk summary` 必须给出同一个答案。

    r68 的核心矛盾就在这一对:同一个 risk 的两个子命令,一份说
    「工作区不存在」一份说「一切正常」。这条判据直接把那一对钉住,
    免得以后又只改一个。
    """
    home = tmp_path / "riskpair"
    home.mkdir()
    top = _run(["risk", "top"], home)
    summary = _run(["risk", "summary"], home)
    assert (top.returncode == 0) == (summary.returncode == 0), (
        f"risk top rc={top.returncode} 但 risk summary rc={summary.returncode}"
        f" —— 同族两个子命令口径不一致"
    )
    assert _workspaces_created(home) == [], "两个都不该建库"


def test_no_empty_or_passing_tests_in_this_file():
    """每个 test_ 函数都必须真有断言(空函数体 = 恒真测试,r66 踩过)。"""
    tree = ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))
    empty, seen = [], set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name.startswith("test_")):
            continue
        seen.add(node.name)
        body = [s for s in node.body
                if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        if not body:
            empty.append(f"{node.name}:{node.lineno}")
    assert not empty, f"这些测试没有断言:{empty}"
    assert len(seen) == 7, f"本文件应当有 7 个测试函数,实测 {len(seen)}:{sorted(seen)}"
