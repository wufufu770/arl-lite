"""r64:「提取截断」不是良性的桶 —— 截断点后面的部分要接回去重验

## 起因(实测,不是推测)

r63 把判据放过的 10 条分成 提取截断 8 / 占位符 2,并明确写下「别一上来就
放宽提取器」。紧接着做的第一件事就是把那 8 条的**完整形态**补上值真跑一遍:

| 重建出来的完整命令 | 判定 |
|---|---|
| `arl-lite query domains --limit 10000` | 干净 |
| `arl-lite query sites --filter "title like '%admin%'"` | 干净 |
| `arl-lite devloop done-item <槽>` | 干净 |
| `arl-lite devloop drop <槽> --reason ...` | 干净 |
| `arl-lite ai ask 解释这个关联分析` | 干净 |
| `arl-lite devloop accept <gate> --reason 为什么可以放宽` | 干净 |
| `arl-lite devloop add foo 标题` | 干净 |
| `arl-lite devloop add <id> 标题 -p 2` | **死路:多余参数 ['-p', '2']** |

**8 条里有 1 条藏着第 9 处死路**:`-p` 不存在,真签名是 `--priority`。
提取器在中文引号值 `"标题"` 处就截断了,`-p 2` 落在**截断点后面** ——
A1/A2/A3 全都看不见它。也就是说「提取截断」不是「查过了」,是
**一片没人扫过的地**,而 r63 之前的判据把它算作「已处理」。

## 这一条判据守什么

1. **重建必须真的跑过 parser**,而且**带值**跑 —— 槽填哑元,argparse 回
   `invalid choice` 时才用 argparse 自己给的第一个合法取值重试(不是猜)。
2. **重建完仍然干净** —— 现在是 8/8,但判据钉住这个数,免得哪天重建悄悄
   失效(比如又被换回反向规则)而没人知道。
3. **重建不出来也是合法结果,但不许有** —— 现在 0 条,一旦出现说明提取器
   或重建规则变了,得看清楚。
4. **重建必须能逮到那类死路** —— 合成输入钉:`-p` 那种挂在截断点后面的
   多余参数,重建之后必须变成 deadend。这条是本判据的**牙齿**。
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_cli_advice_commandable import (  # noqa: E402
    UNVERIFIED,
    FSTRING_SLOT,
    _reconstruct,
    _verified_by_reconstruction,
    _why_unverified,
)

PINNED_VERDICTS = {"重建后干净": 10}


def test_every_truncated_item_reconstructs_and_is_reverified():
    """每一条「提取截断」都要能重建出来 —— 重建不出来是不许的(现在 0 条)。"""
    unbuildable = [(c, t) for _, c, t in UNVERIFIED if _reconstruct(c, t) is None]
    assert not unbuildable, f"有 {len(unbuildable)} 条重建不出来:{unbuildable}"


def test_the_reconstruction_verdicts_are_pinned():
    counts = Counter(_why_unverified(c, t) for _, c, t in UNVERIFIED)
    assert dict(counts) == PINNED_VERDICTS, (
        f"重建后的判定变了:{dict(counts)} != {PINNED_VERDICTS}。"
        f"先弄清是哪条建议变了形状,再决定更新判据还是修建议"
    )


def test_no_truncated_item_is_actually_a_dead_end():
    """实测 8 条全过 —— 但这条随时可能红,而那正是它在起作用的时候。"""
    bad = [(c, _verified_by_reconstruction(c, t)[0]) for _, c, t in UNVERIFIED
           if _why_unverified(c, t) == "重建后死路"]
    assert not bad, f"补全值之后重验发现死路:{bad}"


# ---------------------------------------------------------------- 重建规则的牙齿


@pytest.mark.parametrize(
    "cmd, tail, expect",
    [
        # 值后面挂着多余参数 —— 这就是 `-p 2` 那一族,重建必须把它逮到
        ("arl-lite devloop add <id>", '"标题" -p 2 | 手动加待办 |',
         "deadend:多余参数 ['-p', '2']"),
        ("arl-lite devloop add <id>", '"标题" --priority 2 | 手动加待办 |', "clean"),
        # 值后面挂着合法参数
        ("arl-lite query", f"{FSTRING_SLOT} --limit 10000 逐段导出;后面是散文", "clean"),
        # 中文引号值
        ("arl-lite ai ask", '"解释这个关联分析"', "clean"),
        # 值里有空格:返回 token 列表才不会被 split 散掉
        ("arl-lite query sites --filter", "\"title like '%admin%'\"", "clean"),
        # f-string 槽:填哑元 → argparse 说不合法 → 用它给的第一个合法取值
        ("arl-lite query", f"{FSTRING_SLOT} --limit 10000", "clean"),
    ],
)
def test_reconstruction_actually_catches_what_is_behind_the_cut(cmd, tail, expect):
    """这一条是本判据的牙齿:藏在截断点后面的死路,重建之后必须现形。

    `arl-lite devloop add <id> "标题" -p 2` 是 r64 亲手逮到的第 9 处死路,
    它在提取器眼里只值三个字:`arl-lite devloop add <id>`。
    """
    assert _verified_by_reconstruction(cmd, tail)[0].startswith(expect)


def test_reconstruction_does_not_invent_values_for_the_slot():
    """f-string 槽不许瞎补 —— 补错会凭空造出假死路。

    槽先填哑元,只有 argparse **自己**回 `invalid choice: '哑元'` 时才改填
    它给的第一个合法取值。那不是猜,那是 argparse 在回答「这位置能填什么」。
    """
    argv = _reconstruct("arl-lite query", f"{FSTRING_SLOT} --limit 10000")
    assert argv is not None
    assert argv[2] == FSTRING_SLOT, f"重建时不该把槽换掉:{argv}"
    # 哑元确实会被 argparse 拒 —— 证明「靠 argparse 纠正」这条路是有牙齿的
    from test_cli_advice_commandable import _run_argv
    says, _ = _run_argv([a.replace(FSTRING_SLOT, "ZZSLOTZZ") for a in argv])
    assert "invalid choice" in says and "ZZSLOTZZ" in says, (
        f"哑元没被 argparse 拒掉,那「靠它纠正」就是假的:{says!r}"
    )


@pytest.mark.parametrize(
    "tail, keep",
    [
        ('"标题" --priority 2 | 手动加待办 |', ["标题", "--priority", "2"]),
        ('"标题" -p 2', ["标题", "-p", "2"]),
        ("直接调  q.finish(...) 而不传 note。", None),   # 散文,不重建
        ("# 跑单个门禁", None),                          # 注释,不重建
        ("", None),
    ],
)
def test_reconstruction_stops_at_prose_and_comments(tail, keep):
    """散文和注释都不许接回命令 —— 那是给人看的,不是参数值。"""
    argv = _reconstruct("arl-lite devloop gate", tail)
    if keep is None:
        assert argv is None, f"散文/注释被接成了命令:{argv}"
    else:
        assert argv[-len(keep):] == keep, f"接回去的 token 不对:{argv}"
