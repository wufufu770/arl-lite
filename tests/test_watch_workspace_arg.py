"""watch 全系的 -w 必须真的存在,且真被用上(r70)。

背景:arl_lite/cli.py 的 `cmd_watch_start` 一直在读
`Storage(workspace=getattr(args, "workspace", "default"))` —— 像是有 `-w`
参数似的。但实测 watch 的 5 个子命令(add / list / remove / start / stop)
**全都没注册 `-w`**:`arl-lite watch add example.com -w teamA` 直接
rc=2 `unrecognized arguments: -w teamA`。

于是 `args.workspace` 这个属性**永远不存在**,`getattr` 的默认值恒生效,
`watch start` 永远写进 default 工作区,用户没有任何办法让它写去别处。
这是**静默错路**:不报错、不提示,只是把数据写到用户没指定的地方。
比报错更坏 —— 用户以为自己在 teamA 上跑监控,数据却进了 default。

更糟的一层是**目标清单也是全局的**(`~/.arl-lite/watch/watch.json`,
不带工作区)。两个工作区并存时,`watch list` 把所有目标混在一起列,
而 `watch start` 把它们**全部**写进 default。所以只补 `-w` 不够 ——
那会造出「A 工作区的目标清单 + B 工作区的结果」,比原来更糟。
r70 因此把 `watch.json` 也按工作区切分:`~/.arl-lite/watch/<ws>/watch.json`。

判据守四件事:
1) watch 全系 5 个子命令都接受 `-w`(端到端,不是查源码)
2) 目标清单按工作区隔离(两个工作区各加各的,互不串)
3) `watch start` 真的写进指定工作区(不落 default)
4) 旧格式不自动迁移,只提示(r62:安全默认值是什么都不做)
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
CLI = REPO / "arl_lite" / "cli.py"

WATCH_SUBCMDS = ["add", "remove", "list", "start", "stop"]


def _run(argv, home, timeout=120):
    return subprocess.run(
        [sys.executable, "-B", "-m", "arl_lite.cli", *argv],
        capture_output=True, text=True, cwd=REPO,
        env=dict(os.environ, HOME=str(home), PYTHONDONTWRITEBYTECODE="1"),
        timeout=timeout,
    )


def _home(tmp_path, name="h"):
    h = tmp_path / name
    h.mkdir(parents=True, exist_ok=True)
    return h


def _state(home, workspace) -> pathlib.Path:
    return pathlib.Path(home) / ".arl-lite" / "watch" / workspace / "watch.json"


# ---------------------------------------------------------------- -w 存在


@pytest.mark.parametrize("sub", WATCH_SUBCMDS)
def test_every_watch_subcommand_accepts_w(sub):
    """watch 全系都得认 -w。端到端跑 --help 看 argparse 真注册了它。

    首版查的是源码里有没有 `-w` 字样 —— 那查不到「注册在哪个子命令上」,
    5 个子命令共用一段注册代码时必然误判。直接问 argparse。
    """
    r = subprocess.run(
        [sys.executable, "-B", "-m", "arl_lite.cli", "watch", sub, "--help"],
        capture_output=True, text=True, cwd=REPO, timeout=60)
    assert r.returncode == 0, f"`watch {sub} --help` 跑不通(rc={r.returncode})"
    assert "-w WORKSPACE" in r.stdout or "--workspace" in r.stdout, (
        f"`arl-lite watch {sub}` 没有 -w/--workspace —— "
        "而 cmd_watch_start 一直在读 args.workspace"
    )


def test_watch_add_really_accepts_the_flag(tmp_path):
    """真敲一遍带 -w 的命令,确认不再是 unrecognized arguments。

    原始症状:`arl-lite watch add example.com -w teamA` → rc=2。
    """
    home = _home(tmp_path)
    r = _run(["watch", "add", "a.example.com", "-w", "teamA"], home)
    assert r.returncode == 0, (
        f"`watch add -w teamA` 仍不被接受(rc={r.returncode}):"
        f"{(r.stdout + r.stderr)[-200:]}"
    )


# ---------------------------------------------------------------- 清单隔离


def test_watch_state_is_isolated_per_workspace(tmp_path):
    """两个工作区各加各的目标,互不串。"""
    home = _home(tmp_path)
    assert _run(["watch", "add", "a.example.com", "-w", "teamA"], home).returncode == 0
    assert _run(["watch", "add", "b.example.com", "-w", "teamB"], home).returncode == 0

    a = _run(["watch", "list", "-w", "teamA"], home).stdout
    b = _run(["watch", "list", "-w", "teamB"], home).stdout
    assert "a.example.com" in a and "b.example.com" not in a, f"teamA 看到了别人的:{a[-200:]}"
    assert "b.example.com" in b and "a.example.com" not in b, f"teamB 看到了别人的:{b[-200:]}"


def test_watch_state_path_carries_the_workspace(tmp_path):
    """落盘路径必须带工作区 —— 不带的话两个工作区会共用一份清单。"""
    home = _home(tmp_path)
    _run(["watch", "add", "a.example.com", "-w", "teamA"], home)
    _run(["watch", "add", "b.example.com", "-w", "teamB"], home)
    assert _state(home, "teamA").exists(), f"teamA 清单没落在预期路径:{_state(home, 'teamA')}"
    assert _state(home, "teamB").exists(), f"teamB 清单没落在预期路径:{_state(home, 'teamB')}"


def test_no_code_still_hardcodes_the_global_path(tmp_path):
    """CLI 里不许再有硬编码的全局 watch.json 路径。

    原先 4 个 cmd_watch_* 各写一遍 `~/.arl-lite/watch/watch.json` ——
    同一张契约表两处手抄必然漂。统一到 `_watch_state_file()`。
    """
    src = CLI.read_text(encoding="utf-8")
    hits = [ln for ln in src.splitlines()
            if '".arl-lite" / "watch"' in ln or "'watch.json'" in ln]
    # 允许出现的只有 _watch_state_file / _legacy_watch_state_file 两处定义
    allowed = ("def _watch_state_file", "def _legacy_watch_state_file",
               "return Path.home()")
    bad = [h for h in hits if not any(a in h for a in allowed)]
    assert not bad, (
        f"CLI 里还有硬编码的 watch 路径,应统一走 _watch_state_file():{bad}"
    )


# ---------------------------------------------------------------- 落点


def test_watch_start_writes_into_the_named_workspace(tmp_path):
    """`watch start -w teamA` 必须把工作区 teamA 建出来,而不是 default。

    这是 r70 那个 bug 的核心。验证方式刻意**不依赖它跑完** ——
    `watch start` 是前台同步调度器,会一直跑(r70 调研时实测踩到,
    第一次验证超时了)。
    """
    home = _home(tmp_path)
    _run(["watch", "add", "a.example.com", "-m", "whois", "-w", "teamA"], home)
    try:
        _run(["watch", "start", "-w", "teamA"], home, timeout=25)
    except subprocess.TimeoutExpired:
        pass  # 前台调度器,超时的正是我们要的
    root = home / ".arl-lite" / "workspaces"
    made = sorted(d.name for d in root.iterdir()) if root.exists() else []
    assert "teamA" in made, f"watch start 没写进 teamA,建出的是:{made}"
    assert "default" not in made, f"watch start 仍然写进了 default:{made}"


def test_watch_start_without_w_still_uses_default(tmp_path):
    """不带 -w 时行为不变(向后兼容)。"""
    home = _home(tmp_path)
    _run(["watch", "add", "a.example.com", "-m", "whois"], home)
    try:
        _run(["watch", "start"], home, timeout=25)
    except subprocess.TimeoutExpired:
        pass
    root = home / ".arl-lite" / "workspaces"
    made = sorted(d.name for d in root.iterdir()) if root.exists() else []
    assert made == ["default"], f"不带 -w 时应当落 default,实测 {made}"


# ---------------------------------------------------------------- 迁移


def test_legacy_state_is_reported_but_never_moved(tmp_path):
    """旧格式只提示、**不自动迁移**(r62:安全默认值是什么都不做)。"""
    home = _home(tmp_path)
    legacy = home / ".arl-lite" / "watch" / "watch.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps([{"target": "old.example.com", "modules": ["whois"]}]),
                      encoding="utf-8")

    r = _run(["watch", "list"], home)
    assert r.returncode == 0
    err = r.stderr
    assert "旧格式" in err, f"没提示旧格式:{err[-200:]}"
    assert legacy.exists(), "旧文件被自动搬走了 —— r62 说过默认什么都不做"
    assert not _state(home, "default").exists(), (
        "旧格式被写进了新路径 —— 那不是迁移,是复制,用户会看到两份清单"
    )


def test_no_empty_or_passing_tests_in_this_file():
    """每个 test_ 函数都必须真有断言(空函数体 = 恒真测试,r66 踩过)。"""
    import ast

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
    # 9 个**函数**(parametrize 展开的那几个算同一个函数)
    assert len(seen) == 9, f"本文件应当有 9 个测试函数,实测 {len(seen)}:{sorted(seen)}"
