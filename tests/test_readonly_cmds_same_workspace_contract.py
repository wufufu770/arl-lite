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

# ---- r69:从源码推导,而不是靠手写清单覆盖
#
# r68 的判据靠上面那份手写 MUST_REJECT 守住「只读命令必须走工作区校验」。
# 漏洞实测过:往 cli.py 注入一个**不在清单里**的静默建库只读命令
# (cmd_probe_new_readonly),判据报 19 passed 一声不响 ——
# 靠猜的清单会漏,和 r65 建议判据栽的是同一个坑。
#
# 推导分两层,第一层实测可靠、第二层实测**不可靠**,如实记着:
#
# 1) 「这个命令碰不碰工作区 / 有没有守卫」——可靠。看 cmd_ 函数体里
#    `_ensure_workspace_exists` 与 `Storage` 谁**先**出现:先守卫的是只读
#    契约,先 Storage 的是写/建库。实测 20 个碰 Storage 的 cmd_ 被这个
#    信号干净地分成 10 + 10,零交叉零遗漏。
#
# 2) 「它该不该有守卫」——**不可靠**。试过按 SQL 关键字 / 写方法名 /
#    只读方法名白名单分类,结果 `cmd_watch_add`、`cmd_watch_start` 这类
#    明明在写的命令被判成「只读+缺守卫」,另有 4 个「判不准」。
#    真因:写操作都在更深一层(`Monitor(storage).add(...)`、
#    `Watcher(...)`),只看 cmd_ 函数体看不出来。
#    按错信号建判据 = 判据比它守的事宽,误报一片,比没有更糟。
#
# 所以守门人不是「推导该有哪些」,而是**双向不变量**:
# 碰工作区的命令,要么有守卫(只读契约),要么出现在下面的例外清单里
# 且清单里写了理由。新加命令忘了守卫又没登记例外 → 报红。
EXCEPTIONS = {
    # 每个例外都必须写清「为什么静默建库是对的」,不许只列名字。
    # 理由长度下限 10 字符这条判据当场逮到了我自己的敷衍:
    # 「删除监控是写操作」只有 8 个字,说清了「是什么」却没说清
    # 「为什么建库是对的」—— 那正是例外清单最容易退化成名字黑名单的地方。
    #
    # r70 移除了 cmd_watch_add:它原来有一行 `storage = Storage(...)`,
    # 但那个变量**创建了根本没用**(后面全走 watch.json 文件路径)。
    # 删掉之后它压根不碰工作区,自然也不再是例外。
    # 是本文件那条 stale 检测(`EXCEPTIONS 里已不需要例外了就报红`)
    # 把它逮住的 —— 少一行死代码,判据自动跟上。
    "cmd_run": "用户明确要求写入(run 就是建工作区的正路),静默建库合理",
    "cmd_watch_start": "启动 watch 调度器是写操作,状态落在 watch 目录",
    "cmd_workspace_list": "列出工作区本就依赖建出 default(它就是入口命令)",
    "cmd_workspace_create": "建工作区就是它该干的事,建库是本职",
    "cmd_workspace_delete": "删工作区是写操作,而且它删的就是工作区本身",
}
# r97 移除了 cmd_monitor_add / remove / enable / prune 四条:它们原来靠
# `Storage()` 静默建库,实测这会**凭空造出持久垃圾工作区** ——
# `monitor prune -w TYPO` 报 rc=0 说成功,却在磁盘上留下一个 TYPO,
# 而且它会出现在之后每一次 `workspace list` 里。
# 四条现在都走 `_ensure_workspace_exists`,由本文件下面那条 stale 检测
# 认出来并要求从例外清单里删掉 —— 少四条死豁免,判据自动跟上。
#
# r97 的初版**没有**改这张表,而是自己另写了一份登记表。那是决策 #9
# 的又一处实例(两处手抄同一段逻辑迟早漂),门禁的 test_baseline
# 当场把两份的冲突逮了出来:一份说「已豁免」,另一份说「未豁免」。


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


def _workspace_partition() -> tuple:
    """从源码推导:每个碰工作区的 cmd_ 属于「有守卫」还是「无守卫」。

    判据看的是**调用顺序**而不是调用集合 —— 顺序即契约:
    先 `_ensure_workspace_exists` 再 `Storage` 的是只读命令
    (先确认工作区存在,再读它);先 `Storage` 的是写/建库命令。

    为什么用顺序而不是「调没调」:两者都可能只调一次,顺序能区分
    「先问再读」和「直接写」。实测这个信号把 20 个碰 Storage 的 cmd_
    干净地分成 10 + 10,零交叉零遗漏。
    """
    src = CLI.read_text(encoding="utf-8")
    tree = ast.parse(src)
    guarded, unguarded = set(), set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name.startswith("cmd_")):
            continue
        first = None
        for stmt in node.body:          # 只看顶层语句的顺序
            for c in ast.walk(stmt):
                if not isinstance(c, ast.Call):
                    continue
                nm = (c.func.id if isinstance(c.func, ast.Name)
                      else getattr(c.func, "attr", None))
                if nm in ("_ensure_workspace_exists", "Storage"):
                    first = nm
                    break
            if first:
                break
        if first is None:
            continue
        (guarded if first == "_ensure_workspace_exists" else unguarded).add(node.name)
    return guarded, unguarded


# ---------------------------------------------------------------- 源码头


def test_every_workspace_command_is_guarded_or_registered():
    """r69 的守门人:碰工作区的命令,要么有守卫,要么在例外清单里并写了理由。

    这条替代了 r68 那份手写 MUST_REJECT 的守门作用 ——
    注入一个不在清单里的新只读命令时,这条会报红(实测注入时它确实红了,
    而 r68 的 19 条全绿)。
    """
    guarded, unguarded = _workspace_partition()
    assert guarded, "推导不出任何有守卫的命令 —— 推导逻辑坏了"
    unregistered = sorted(unguarded - set(EXCEPTIONS))
    assert not unregistered, (
        "这些命令碰工作区却既没有 _ensure_workspace_exists 守卫、"
        f"也没登记为例外:{unregistered}。"
        "只读命令必须走守卫(否则零工作区下静默建库,用户分不清"
        "数据是真没了还是被初始化了);写操作可以静默建库,但要在 "
        "EXCEPTIONS 里写清理由。"
    )
    stale = sorted(set(EXCEPTIONS) - unguarded)
    assert not stale, (
        f"EXCEPTIONS 里这些条目已经不需要例外了(它们有守卫了):{stale}。"
        "留着会让例外清单慢慢变成藏污纳垢的地方"
    )


def test_every_exception_states_why():
    """例外清单的每一条都必须写清理由,不许只列名字。

    没有这条,EXCEPTIONS 会退化成一张「名字黑名单」——
    而 r63 立过「禁用词表这种写法本身就是洞」:没人知道为什么,
    下一个进来的人只会照抄。判据要盯不变量,别盯名字列表。
    """
    for name, why in EXCEPTIONS.items():
        assert len(why) >= 10, f"{name} 的例外理由太短({why!r}),看不出为什么"


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
    assert len(seen) == 9, f"本文件应当有 9 个测试函数,实测 {len(seen)}:{sorted(seen)}"
