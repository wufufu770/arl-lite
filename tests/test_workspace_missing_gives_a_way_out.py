"""工作区不存在时的报错必须给出**能走通**的路(r67)。

背景:arl_lite/cli.py 的 `_ensure_workspace_exists` 被 7 个只读子命令共用
(cmd_query / cmd_search / cmd_export / cmd_stats / cmd_diff /
cmd_correlate / cmd_risk_top)。它有两条分支:

- **有工作区、名字拼错** → 打印 `available: [...]`。这条一直是对的。
- **一个工作区都没有**(全新安装)→ 原来只有一行
  `workspace not found: 'default'` 然后 rc=1,**零出路**。
  实测 7 个命令全走这条,每条都只有那一行。

而「装完还没跑过任何任务」正是新用户的默认处境,于是他看到这条
完全不知道下一步该干什么。

修法里有两个反直觉的点,都是实测踩出来的:
1. 出路指向 `arl-lite run -t <target>`(用户本来就想做的事),
   不是 `workspace list` —— 后者确实 rc=0 但那是绕路。
2. 刻意**不**指向 `workspace create default`:实测在空环境下它报
   「workspace 'default' already exists」rc=1(Storage 先静默建了库),
   最像样的那条出路本身是条错路。

判据守:零工作区时报错必须带出路,那条出路**照着敲真能跑通**
(端到端,不是文本匹配);有工作区时不多嘴。

一条实测踩出来的、别的判据也用得上的纪律 —— **端到端判据自己
不许有爆炸半径**:

出路是 `arl-lite run -t <target>`,而 `run` 必然出网。首版里另一条
判据为了造「有工作区」的场景,顺手也跑了一次 `run -m whois`。
结果 test_baseline 门禁报 rc=1 —— 全量下这条判据红了而单跑绿,
门禁指名 `test_the_way_out_actually_works` 是第 10 条(非基线)失败。
两个修法:造场景改用**零网络**的 `workspace list`(实测 rc=0,0.3s);
验出路时把网络类症状(超时/连不上/解析失败)识别出来,确认工作区确实
被建出来后如实 skip —— 但**先断言工作区建出来了**,免得把
「出路根本没用」这个真问题一起 skip 掉。

判据不把自己的偶发失败算成实现的缺陷,但也不拿它当遮羞布。
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

# 走 _ensure_workspace_exists 的全部只读子命令 —— 实测逐条确认过。
#
# **每条判据都必须配自己的干净 HOME**,这不只是卫生习惯:
# 调研阶段我一度在一串命令里共用一个 HOME,结果列表里排在
# `workspace list` 后面的 `risk top` / `diff` 测出 rc=0,
# 于是「一半命令拒绝一半正常」成了假数据。
# 而 cmd_risk_top 确实调了 _ensure_workspace_exists(arl_lite/cli.py),
# 源码与实测对不上,才暴露是顺序污染。
# 探针里任何一条命令建了工作区,后面所有条的结论都会偏。
READONLY_CMDS = [
    ["stats"],
    ["query", "domains"],
    ["search", "domains", "example"],
    ["export"],
    ["diff"],
    ["correlate"],
    ["risk", "top"],
]


def _run(argv, home, timeout=180):
    return subprocess.run(
        [sys.executable, "-B", "-m", "arl_lite.cli", *argv],
        capture_output=True, text=True, cwd=REPO,
        env=dict(os.environ, HOME=str(home), PYTHONDONTWRITEBYTECODE="1"),
        timeout=timeout,
    )


# `arl-lite run` 必然出网(默认 subfinder+crtsh),所以验出路时它是个
# **会偶发失败**的步骤:网络抖动 / 被墙 / 全量负载下超时,都会让它
# 非零退出,而那跟本轮要守的东西没关系。
#
# 判据必须能区分「出路是死的」和「网络此刻不通」—— 否则它就成了
# 随机红/绿的信号,而恒真或恒假的断言都比没有断言更糟。
# 判据:网络类症状(超时 / 连不上 / 解析失败)一律 skip,
# 其余非零退出照常判死路。
_NET_MARKERS = (
    "timeout", "timed out", "Connection", "connection",
    "Temporary failure", "Name or service not known", "network",
    "Max retries exceeded", "SSLError", "ProxyError",
)


def _looks_like_network(tr: subprocess.CompletedProcess) -> bool:
    out = (tr.stdout + tr.stderr)
    if isinstance(tr, subprocess.TimeoutExpired):
        return True
    return any(m in out for m in _NET_MARKERS)


def _fresh_home(tmp_path, name):
    """造一个干净的工作区根:确保那些目录真的不存在。"""
    h = tmp_path / name
    h.mkdir(parents=True, exist_ok=True)
    return h


def _way_out_commands(combined: str) -> list[str]:
    r"""从报错输出里抠出「像命令的 token」。

    换行处必须断开。首版写成 `(?:\s+[^\s]+)*`,而 `\s` 含换行,
    于是贪婪地把**下一行散文**也吞成命令的一部分,抠出
    `arl-lite run -t <target>\n            (想换个名字...` 这种
    根本不存在的东西,再真跑一遍 rc=2,判据把自己判红。
    这跟 r64 记的「值边界丢失」是同一类:提取器吃到了不该吃的。
    判据错了改判据,不许改实现去迁就它。
    """
    return re.findall(r"(arl-lite\s+[\w-]+(?:[ \t]+[\w<>-]+)*)", combined)


# ---------------------------------------------------------------- 零工作区


def test_every_readonly_cmd_reports_the_missing_workspace(tmp_path):
    """前提校验:这 7 条命令确实都走这条报错路径。

    没有这条,后面的判据会在「命令列表变了」时静默空转 ——
    判据比它守的事窄是对的,但前提得先钉住。
    """
    for i, argv in enumerate(READONLY_CMDS):
        home = _fresh_home(tmp_path, f"pre{i}")
        r = _run(argv, home)
        combined = r.stdout + r.stderr
        assert "workspace not found" in combined, (
            f"`arl-lite {' '.join(argv)}` 不再走 workspace 校验了"
            f"(rc={r.returncode}):{combined[-200:]}"
        )


def test_missing_workspace_error_always_gives_a_way_out(tmp_path):
    """核心:零工作区时报错必须给出路,不许只有一句「not found」。

    端到端跑 7 条命令,逐条看输出里有没有一条**真能敲**的出路。
    """
    for i, argv in enumerate(READONLY_CMDS):
        home = _fresh_home(tmp_path, f"way{i}")
        r = _run(argv, home)
        combined = r.stdout + r.stderr
        # 抓「像命令的 token」:必须带 arl-lite 前缀,这样才落进
        # r60-r65 那套建议判据的视野,而不是又变成一条隐形建议。
        cmds = _way_out_commands(combined)
        assert cmds, (
            f"`arl-lite {' '.join(argv)}` 在零工作区下只报错没给出路:"
            f"{combined[-300:]}"
        )


def test_the_way_out_actually_works(tmp_path):
    """出路必须**照着敲真能跑通** —— 本轮最硬的一条。

    实测过:`workspace create default` 在空环境下报
    「already exists」rc=1(Storage 先静默建了库),
    所以那条最像样的出路本身就是条错路。只有端到端跑一遍才发现得了。
    """
    home = _fresh_home(tmp_path, "e2e")
    r = _run(["stats"], home)
    combined = r.stdout + r.stderr
    cmds = _way_out_commands(combined)
    assert cmds, f"没抓到出路:{combined[-300:]}"

    failed = []
    net_blocked = []
    for cmd in cmds:
        argv = cmd.split()[1:]
        # `<target>` / `<name>` 是占位符,换成真值再跑
        argv = ["example.com" if a == "<target>" else a for a in argv]
        try:
            rr = _run(argv, home, timeout=300)
        except subprocess.TimeoutExpired as e:
            net_blocked.append(f"  `{cmd}` 超时")
            continue
        if _looks_like_network(rr):
            net_blocked.append(f"  `{cmd}` 像是网络不通:{(rr.stdout + rr.stderr)[-120:]}")
            continue
        if rr.returncode != 0:
            failed.append(f"  `{cmd}` rc={rr.returncode}: "
                          f"{(rr.stdout + rr.stderr).strip()[-200:]}")
    assert not failed, "报错里给出的出路照着敲跑不通:\n" + "\n".join(failed)

    # 照着敲完之后,原本报错的命令必须真的能跑了。
    # 网络不通时这一步无从谈起(出路里的 run 没能建出工作区),
    # 那就如实 skip —— 但**要先确认网络真的是原因**,不能把
    # 「出路根本没建出工作区」这个真问题也一起 skip 掉。
    if net_blocked:
        ws_root = home / ".arl-lite" / "workspaces"
        made = [d.name for d in ws_root.iterdir()] if ws_root.exists() else []
        assert "default" in made, (
            f"网络不通可以 skip,但工作区压根没建出来 —— "
            f"那不是网络的问题,是出路的问题。net_blocked={net_blocked},made={made}"
        )
        pytest.skip(f"网络不可用,跳过后半截:{net_blocked}")

    after = _run(["stats"], home)
    assert after.returncode == 0, (
        f"照出路敲完,{after.stdout + after.stderr}[-300:]"
    )


def test_the_way_out_does_not_point_at_workspace_create(tmp_path):
    """出路不许指向 `workspace create` —— 它在空环境下是条错路。

    实测:全新 HOME 下 `arl-lite workspace create default` 报
    「workspace 'default' already exists」rc=1。
    这是本轮专门加的一条负向判据:出路被"改成看起来更正式"的
    workspace 子命令,是这类修复最容易犯的错。
    """
    home = _fresh_home(tmp_path, "neg")
    r = _run(["stats"], home)
    combined = r.stdout + r.stderr
    assert "workspace create" not in combined, (
        f"出路指向了 workspace create,实测那是条错路:{combined[-300:]}"
    )


# ---------------------------------------------------------------- 有工作区


def test_typo_case_still_only_lists_available(tmp_path):
    """有工作区但名字拼错时,输出仍应是 `available: [...]`,不多嘴。

    这条路径一直是对的,不许被本轮的改动带跑偏 ——
    判据要比它守的事窄,已有的好行为也要钉住。
    """
    home = _fresh_home(tmp_path, "typo")
    # 建工作区用 `workspace list` 而不是 `run -t example.com -m whois`:
    # 实测全量下这条判据红了,而单跑绿的 —— 首版在这里跑了一次**联网**的
    # `run`(whois 模块要出网),全量负载下它不稳就连锁失败。
    # 判据自己不许有这种爆炸半径(r62 立的规矩):要建工作区就用
    # 零网络的 `workspace list`(实测 rc=0,0.3s,且会建出 default)。
    assert _run(["workspace", "list"], home).returncode == 0
    r = _run(["stats", "-w", "defalt"], home)
    combined = r.stdout + r.stderr
    assert "workspace not found" in combined
    assert "available:" in combined, f"拼错时没列出可用工作区:{combined[-300:]}"
    assert "还没有任何工作区" not in combined, (
        f"明明有工作区,却说了「还没有任何工作区」:{combined[-300:]}"
    )


def test_existing_workspace_is_not_auto_created(tmp_path):
    """不许为了给出路而让只读命令静默建库。

    `_ensure_workspace_exists` 的 docstring 写明:Storage() 会静默自动
    创建 workspace —— 拼错 -w 的结果是「全 0 统计」加一个垃圾目录,
    用户会误判数据丢失。那是**有意的设计**。
    本轮要改的是错误信息,不是取消校验。这条钉住那个不变量。
    """
    home = _fresh_home(tmp_path, "noautocreate")
    r = _run(["stats", "-w", "typo-name"], home)
    assert r.returncode != 0
    root = home / ".arl-lite" / "workspaces"
    made = [d.name for d in root.iterdir()] if root.exists() else []
    assert "typo-name" not in made, (
        f"拼错的名字被静默建出了工作区:{made} —— 用户会误判数据丢失"
    )


def test_no_empty_or_passing_tests_in_this_file():
    """本文件里每个 test_ 函数都必须真有断言。

    r66 实测踩到:编辑时留下一个只有 docstring 的空函数定义,
    真实现定义在它后面同名,Python 里后者覆盖前者,于是多了一个
    永远通过的空测试 —— 而 `pytest` 报的 passed 数照样好看。
    """
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
    assert not empty, f"这些测试没有断言(空函数体 = 恒真测试):{empty}"
    assert len(seen) == 7, f"本文件应当有 7 个测试,实测 {len(seen)}:{sorted(seen)}"
