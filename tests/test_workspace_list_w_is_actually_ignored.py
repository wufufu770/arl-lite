"""`workspace list -w` 必须真被忽略 —— 只读列表命令不得有写库副作用，也不得静默给错答案。

背景:EXCEPTIONS 里给 cmd_workspace_list 写明的理由是「列出工作区本就依赖建出
default(它就是入口命令)」—— 边界是精确的,它只授权建 `default`。
但没有任何东西去执行这条声明,实现写的是:

    # 注释:但仍需要一个 Storage 实例来读 workspaces 表,用 "default" 即可
    ws = getattr(args, "workspace", None) or "default"     # ← 用了用户的值

实测两个后果,都出在「声明为被忽略的参数」上:
  1. `workspace list -w teamZ` 在全新环境下**建出 teamZ**,rc=0。只读列表命令
     产生写库副作用。
  2. 更糟:`workspaces` 表只存在于 default 库里。建好 default/alpha/beta 后,
     `workspace list -w alpha` 只列出 alpha(描述变成 auto-created)和 default ——
     **beta 从结果里消失了**。命令返回了一个错误且不完整的答案。

静默给错答案比报错更坏(与 r72 在 MCP 侧同一条教训)。所以 `-w` 既然 help 里
明写「(忽略)」,就必须**真的**被忽略:恒读 default 库,列出全部工作区。

本判据纯行为,不查源码 —— 行为能测出来的别假装测不出。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# 三个工作区故意包含 default / alpha / beta。少一个都测不出「结果被截断」。
ALL_WS = ("default", "alpha", "beta")


def _run(argv: list[str], home: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HOME"] = home
    env["PYTHONPATH"] = str(REPO)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-B", "-m", "arl_lite.cli", *argv],
        env=env, cwd=tempfile.gettempdir(),
        capture_output=True, text=True, timeout=120,
    )


def _ws_dirs(home: str) -> set[str]:
    root = Path(home) / ".arl-lite" / "workspaces"
    return {p.name for p in root.iterdir() if p.is_dir()} if root.is_dir() else set()


def _listed_names(stdout: str) -> set[str]:
    """从表格输出里取工作区名(表格第二列)。"""
    names = set()
    for line in stdout.splitlines():
        parts = line.split()
        if parts and parts[0].isdigit() and len(parts) > 1:
            names.add(parts[1])
    return names


def _seeded_home() -> str:
    """建好 default/alpha/beta 的干净环境。"""
    home = tempfile.mkdtemp(prefix="wslist")
    for w in ALL_WS:
        _run(["workspace", "create", w, "--desc", f"desc-{w}"], home)
    return home


def test_list_without_flag_lists_every_workspace():
    """不带 -w 时三个都在 —— 这是本判据的基线,先确认它本身没坏。"""
    home = _seeded_home()
    try:
        r = _run(["workspace", "list"], home)
        assert r.returncode == 0, f"workspace list 应当成功:rc={r.returncode} {r.stderr[-200:]}"
        assert _listed_names(r.stdout) == set(ALL_WS), (
            f"workspace list 应当列出全部 {sorted(ALL_WS)},实际 {_listed_names(r.stdout)}"
        )
    finally:
        _cleanup(home)


def test_ignored_flag_does_not_create_a_workspace():
    """`workspace list -w teamZ` 不得凭空建出 teamZ。

    只读列表命令带一个声明为被忽略的参数,不产生任何写库副作用。
    """
    home = tempfile.mkdtemp(prefix="wslist-fresh")
    try:
        assert _ws_dirs(home) == set(), "前提错了:环境应当是全新的"
        r = _run(["workspace", "list", "-w", "teamZ"], home)
        assert r.returncode == 0, f"rc={r.returncode} {r.stderr[-200:]}"
        created = _ws_dirs(home)
        assert "teamZ" not in created, (
            f"workspace list -w teamZ 凭空建出了 teamZ(现存 {_ws_dirs(home)})。"
            f"只读列表命令不该有写库副作用;-w 在 help 里本就声明为「忽略」。"
        )
        # default 是入口命令建出来的,允许存在
        assert created <= {"default"}, f"只允许建 default,实际建了 {created}"
    finally:
        _cleanup(home)


def test_ignored_flag_still_lists_every_workspace():
    """带 -w 时结果必须和不带 -w 完全一致 —— 不许静默截断。"""
    home = _seeded_home()
    try:
        plain = _run(["workspace", "list"], home)
        for flag in ("-w", "--workspace"):
            r = _run(["workspace", "list", flag, "alpha"], home)
            assert r.returncode == 0, f"{flag} rc={r.returncode} {r.stderr[-200:]}"
            got = _listed_names(r.stdout)
            assert got == set(ALL_WS), (
                f"workspace list {flag} alpha 只列出了 {sorted(got)},"
                f"beta 等真实工作区从结果里消失了。workspaces 表只存在于 default 库,"
                f"用别的库去读会得到一个错误且不完整的答案。"
            )
            assert _listed_names(plain.stdout) == got, (
                f"{flag} 带与不带结果不一致"
            )
    finally:
        _cleanup(home)


def test_ignored_flag_creates_no_extra_db():
    """带 -w 跑一遍之后,工作区目录集合不能变化。"""
    home = _seeded_home()
    try:
        before = _ws_dirs(home)
        _run(["workspace", "list", "-w", "alpha"], home)
        _run(["workspace", "list", "-w", "brandnew"], home)
        after = _ws_dirs(home)
        assert after == before, (
            f"workspace list -w 让工作区目录从 {sorted(before)} 变成 {sorted(after)}"
        )
    finally:
        _cleanup(home)


def test_explicitly_created_workspaces_are_all_listed():
    """反向不变量:显式建过的工作区必须都能被列出来(防止退化成「什么都不列」)。"""
    home = _seeded_home()
    try:
        r = _run(["workspace", "list"], home)
        got = _listed_names(r.stdout)
        for w in ALL_WS:
            assert w in got, f"{w} 是显式建出来的,必须出现在 workspace list 里:{sorted(got)}"
    finally:
        _cleanup(home)


def _cleanup(home: str) -> None:
    """用可恢复的方式清掉临时 HOME(不用 rm -rf)。"""
    import shutil
    shutil.rmtree(home, ignore_errors=True)
