"""TUI 起不来时的兜底建议必须**逐条能真跑**(r65,第 10 处死路)。

背景:arl_lite/tui/app.py 在 TUI 不可用时(非 Unix / 非交互终端)会打印
替代路径,那是用户在困境中看到的**唯一**指引。原文案写的是
「请用 query / stats / export 子命令」—— 裸子命令,而

    $ arl-lite query
    arl-lite query: error: the following arguments are required: table

rc=2,照着敲必然撞墙(stats / export 实测 rc=0 正常,只有 query 是死路)。

这处死路能一路躲过 r60-r64 四轮,原因很具体:那几轮判据靠**字面量
`arl-lite` 前缀**提取建议,而这条建议写的是裸 `query`,判据一条都看不见。

本文件的判据守两件事:
1. 兜底文案里点名的每条命令都能真跑(端到端执行,不是文本匹配);
2. 兜底文案带 `arl-lite` 前缀 —— 落进 r60-r64 判据的视野,
   免得下一次改文案又退回盲区。

两条都守不住就等于没修:r65 之前那一版正是「文本看着对、跑起来炸」。
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import tempfile

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
APP = REPO / "arl_lite" / "tui" / "app.py"

# 兜底文案是单一来源常量;判据钉它,免得两处报错各写一份又漂开。
CONST = "_NON_TUI_ALTERNATIVES"

# 兜底文案应当点名的条数。r65 实测 9 条。
#
# 这个数字不是「至少 3 条」那种软门槛 —— 变异 M2 实测:把某一条的
# `arl-lite` 前缀拿掉,提取器因为要求前缀而**整行丢弃**,9 条变 8 条,
# 而软门槛照样通过,判据一声不响。前提是「前缀只影响验证,不影响提取」,
# 这条钉死才让前提变成事实。
#
# 第 9 条是 `workspace list`:全量测试时才逮到,全新 HOME 下
# `arl-lite stats` rc=1 报 "workspace not found: 'default'"。
EXPECTED_COMMAND_COUNT = 9


def _advice_text() -> str:
    src = APP.read_text(encoding="utf-8")
    m = re.search(rf"^{CONST} = \((.*?)^\)$", src, re.S | re.M)
    assert m, f"{APP.name} 里找不到 {CONST} 这个单一来源常量"
    return m.group(1)


def _named_commands() -> list[str]:
    """从兜底文案里抠出点名的每条命令(去掉行尾注释)。

    正则**故意不要求 `arl-lite` 前缀** —— 提取必须独立于前缀,
    否则有人把前缀拿掉时,提取器跟着瞎,判据静默漏过
    (变异 M2 实测:8 条 → 7 条,「至少 3 条」的软门槛照样通过)。
    前缀由 `test_every_named_command_is_given_an_arl_lite_prefix` 单独钉。
    """
    cmds = []
    for line in _advice_text().splitlines():
        hit = re.search(r"\s((?:arl-lite\s+)?[\w-]+(?:\s+[\w-]+)?)\s*(#.*)?$", line)
        if hit:
            cmds.append(" ".join(hit.group(1).split()))
    return cmds


# ---------------------------------------------------------------- 判据

def test_the_advice_constant_exists_and_is_shared():
    """兜底文案必须是单一来源。两处手抄必然漂(漂出来的死路已修过 10 处)。

    注意这里数的是**定义处**而不是出现次数:出现次数当然 >1
    (1 处定义 + 2 处引用)。首版写 `count(CONST) == 1`,把定义和引用
    混成一回事,当场把自己判红 —— 前提错的判据要改判据。
    """
    src = APP.read_text(encoding="utf-8")
    defs = re.findall(rf"^{CONST} = ", src, re.M)
    assert len(defs) == 1, f"{CONST} 应当只定义一次,实测 {len(defs)} 处"
    # 两处 TUI 不可用的分支都得引用它,而不是各写一份字面量。
    uses = len(re.findall(rf"\b{CONST}\b", src)) - len(defs)
    assert uses == 2, f"应当正好有 2 处引用(TUI 的两个不可用分支),实测 {uses} 处"
    for banned in ("请用 query", "请用 query / stats / export"):
        assert banned not in src, f"裸子命令兜底建议还在:{banned}"


def test_the_advice_names_some_commands():
    """兜底文案点名的条数必须钉死 —— 少一条就是有人改坏了。

    首版写的是 `>= 3`,变异 M2 实测把它骗过去了:拿掉一条的 `arl-lite`
    前缀,提取器整行丢弃,8 条变 7 条,而 7 >= 3 照样通过。
    「至少 N 条」这种软门槛在**内容被删**时毫无牙齿。
    """
    cmds = _named_commands()
    assert len(cmds) == EXPECTED_COMMAND_COUNT, (
        f"兜底文案点名了 {len(cmds)} 条,应为 {EXPECTED_COMMAND_COUNT} 条:{cmds}"
    )


def test_every_named_command_is_given_an_arl_lite_prefix():
    """每条都得带 `arl-lite` 前缀。

    这是本轮的核心:没前缀 = 落进判据盲区 = 下一轮没人看得见它。
    裸 `query` 就是第 10 处死路的原始形态。
    """
    for cmd in _named_commands():
        assert cmd.startswith("arl-lite "), f"建议缺 `arl-lite` 前缀:{cmd}"


def test_every_named_command_actually_runs():
    """端到端:照着建议**从上往下敲**,每一条都必须 rc=0。

    这是本轮最硬的一条 —— 端到端执行,不是文本匹配。
    r65 之前那版文案在文本上完全合理,真跑却 rc=2。

    为什么是「一个共享 HOME、按顺序敲」而不是每条各自一个 HOME:
    真实用户就是把这段文案从上往下敲在一个 shell 里,状态是连着的。
    实测证据:全新 HOME 下第一条 `arl-lite workspace list` 会建出
    default 工作区,后面的命令才跑得通;而把每条命令各自丢进一个
    干净 HOME,第 2 条起就全 rc=1 报
    "workspace not found: 'default'" —— 那种测法测的是「每条命令
    在任意空环境里独立可跑」,比实际契约严得多,判的是另一件事。
    真实契约是:照着敲得通。

    另两个实测踩出来的坑,记在这里免得再踩:
    1) `arl-lite` 是**给人看的写法**,不是可执行文件名。直接当 argv[0]
       传下去,argparse 报 `invalid choice: 'arl-lite'`,8 条建议全假红。
    2) 必须**显式给子进程一个干净的 HOME**。实测单跑本文件全绿、
       跑全量全红:tests/test_phase4.py 等直接 `os.environ["HOME"] = home`
       (没用 monkeypatch,不还原),临时目录随 with 块退出即被删,
       子进程继承到一个指向不存在路径的 HOME。判据不该依赖跑在什么顺序下。
    """
    cmds = _named_commands()
    with tempfile.TemporaryDirectory() as home:
        failures = []
        for cmd in cmds:
            r = subprocess.run(
                [sys.executable, "-B", "-m", "arl_lite.cli", *cmd.split()[1:]],
                capture_output=True, text=True, cwd=REPO,
                env=dict(os.environ, HOME=home, PYTHONDONTWRITEBYTECODE="1"),
                timeout=120,
            )
            if r.returncode != 0:
                failures.append(
                    f"  `{cmd}` rc={r.returncode}: "
                    f"{(r.stdout + r.stderr).strip()[-200:]}"
                )
        assert not failures, (
            "照着兜底建议从上往下敲,以下命令跑不通:\n" + "\n".join(failures)
        )


def test_query_is_never_named_without_a_table():
    """`arl-lite query` 缺必填的 table —— 单独点名它就是死路。

    单独列出来,是因为它是这一族里唯一带必填位置参数的:
    少了它,别的死路被修掉时它很容易被顺手带回来。
    """
    from arl_lite.cli import build_parser

    for action in build_parser()._actions:
        if action.choices and "query" in action.choices:
            continue
    for cmd in _named_commands():
        parts = cmd.split()
        assert not (parts[1:2] == ["query"] and len(parts) == 2), (
            f"`{cmd}` 缺 table,实测 rc=2,是一条死路"
        )


def test_the_bare_subcommand_form_is_rejected_by_the_broad_criterion():
    """回归钉:裸子命令形态必须会被**通用**判据逮住,而不是只被本文件逮住。

    办法是把裸形态喂给 r60-r64 的提取器,确认它**抓不到** ——
    也就是说,如果有人把文案改回裸形态,兜底就退回盲区,而通用判据
    一声不响。抓不到这件事本身就是本轮要钉住的事实。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "adv_r65", REPO / "tests" / "test_cli_advice_commandable.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # 必须物化成列表再判:`_commands_in` 返回的是**生成器**,而生成器对象
    # 恒为真 —— `assert not gen` 无论抓到几条都通过,是一条恒真断言。
    # r63 立过这条规矩(空操作变异不证明任何事),这里差点又踩一遍。
    bare = list(mod._commands_in("Windows 请用 query / stats / export 子命令。"))
    prefixed = list(mod._commands_in("请改用 arl-lite stats 查看概览。"))

    assert bare == [], (
        f"通用提取器竟然抓到了裸子命令建议:{bare} —— "
        "r60-r64 的覆盖前提变了,本轮那 8 条前缀的必要性需要重新评估"
    )
    assert len(prefixed) == 1, f"带前缀的建议必须能被通用提取器抓到,实测 {prefixed}"
