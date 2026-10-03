"""r63:判据放过的「查不动」不是一个数,是一类 —— 逐条打标,不许有第四类

## 为什么要有这条

r61 修完之后,判据提到 102 条建议 / 91 条干净 / 1 条死路 / 10 条查不动。
那个「10」是个**数**,不是**一类**。而这一轮的真收获恰恰来自把它拆开:

r61 的规则是**反向**的 ——「尾巴非空就放过」。r63 把它改成**正向**的
「尾巴里有没有一个能补上缺失参数的**值**」,立刻从 10 条里分出 3 条形状:

| 类 | 条数 | 尾巴长什么样 | 判据能不能自己验 |
|---|---|---|---|
| `提取截断` | 8 | `"标题"` / `\\x00` / `"解释这个关联分析"` | 能:引号/槽是机器可判的 |
| `占位符` | 2 | argparse 抱怨的值就是 `<subcmd>` / `...` | 能:从错误文本里抠出来 |
| `散文收尾` | 1 | `直接调 q.finish(...) 而不传 note` | 能:首 token 不是值形状 |
| `没给` | 0 | 空 | 能 |

而那 1 条 `散文收尾` **就是第 8 处死路**:文档写 `arl-lite devloop done-item`
却不说 `item_id`,而 `done-item` 必填它。反向规则放过了整整两轮。

## 这条判据自己守什么

1. 四类**穷尽且互斥** —— 三类之和必须等于查不动总数,不许有第四类漏网。
2. 四类**都不许是 0** —— 全 0 说明标签成了摆设,比不分类更坏。
3. **条数钉死具体数字** —— 提取器一改就漂移,钉住才能让漂移变成一次有意的决定。
4. 打标必须是**机器可判**的 —— 判据用的是命令行形状和 argparse 的错误文本,
   没有人肉归类,也没有任何一条按来源文件硬编码。
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_cli_advice_commandable import (  # noqa: E402
    ADVICE,
    CLEAN,
    DEAD,
    UNCHECKED,
    _tail_carries_a_value,
    _why_unchecked,
)

# 「查不动」只该有两种:提取器够不着,或者值本来就是占位符。
# 另外两种(`散文收尾` / `没给`)不是「查不动」,是**死路** —— 建议真的没给
# 必填参数。所以它们在 UNCHECKED 里必须是 0,这是断言,不是例外。
UNCHECKED_CLASSES = ("提取截断", "占位符")
DEAD_CLASSES = ("散文收尾", "没给")

# r63 实测:102 条建议 / 91 干净 / 1 死路 / 10 查不动,查不动里 8 + 2 + 0 + 0。
# 数字钉死不是为了「不许变」,是为了让提取器一改、条数一变,
# 那次变化必须是有人**看着数字改的**,而不是悄悄漂过去的。
PINNED = {"提取截断": 8, "占位符": 2, "散文收尾": 0, "没给": 0}
PINNED_UNCHECKED = 10


def _counts() -> Counter:
    return Counter(_why_unchecked(cmd, tail) for _, cmd, tail in UNCHECKED)


def test_the_unchecked_ones_split_into_exactly_these_classes():
    """穷尽且互斥:两类之和 == 查不动总数,不许有第三类混进来。"""
    counts = _counts()
    unknown = set(counts) - set(UNCHECKED_CLASSES)
    assert not unknown, f"冒出了没定义过的类:{unknown}"
    assert sum(counts.values()) == len(UNCHECKED), (
        f"分类没覆盖完:{dict(counts)} 合计 {sum(counts.values())},"
        f"而查不动有 {len(UNCHECKED)} 条"
    )


def test_the_class_lists_themselves_have_teeth():
    """类列表是断言的**牙齿**:清空它,parametrize 就一个用例都不生成,
    判据会**静默变成空转**而不是变红。变异 M5 实测就是靠这条才被杀。

    所以这里把两个列表的**内容**也钉死 —— 光断言「加起来等于总数」不够,
    因为清空两边之后那个等式两边都变成 0,照样成立。
    """
    assert UNCHECKED_CLASSES == ("提取截断", "占位符")
    assert DEAD_CLASSES == ("散文收尾", "没给")


@pytest.mark.parametrize("name", UNCHECKED_CLASSES)
def test_no_unchecked_class_is_ever_empty(name):
    """「查不动」的这一类要是空了,说明标签成了摆设 —— 比不分类更坏。

    注意:这是**逐类**判,不是判总数不为 0。所以哪怕提取器改进让某一类
    彻底消失,这里也会红 —— 那时候应该把这行从 UNCHECKED_CLASSES 删掉
    并写下原因,而不是把断言改成 `> 0` 蒙混过去。
    """
    counts = _counts()
    assert counts[name] > 0, (
        f"「{name}」这一类现在是 0 条。要么是提取器改进让它消失了"
        f"(那就从 UNCHECKED_CLASSES 删掉并写清原因),要么是标签根本没在用"
    )


@pytest.mark.parametrize("name", DEAD_CLASSES)
def test_dead_classes_never_show_up_as_unchecked(name):
    """这两类必须是 0 —— 它们代表建议真的没给参数,该判死路。

    首版我把四个类一视同仁地要求「都不许是 0」,结果这一条当场把自己判红了:
    `散文收尾` 恰恰因为**把第 8 处死路改对**才归零的。断言写成了和事实作对的话,
    那它守的就不是它宣称的东西(和 r61 那条「三类必须分完」一个毛病)。
    """
    counts = _counts()
    assert counts[name] == 0, (
        f"「{name}」是死路而不是查不动,却混进了 UNCHECKED:{dict(counts)}"
    )


def test_the_counts_are_pinned():
    """钉死具体条数,让漂移变成一次有意的决定。"""
    counts = _counts()
    assert dict(counts) == {k: v for k, v in PINNED.items() if v}, (
        f"查不动的分类变了:{dict(counts)} != {PINNED}。"
        f"先弄清是哪条建议变了形状,再决定是更新判据还是修那条建议"
    )
    assert len(UNCHECKED) == PINNED_UNCHECKED


def test_classification_keys_off_shape_not_off_specific_commands():
    """打标必须只认**形状**,不许认具体那条命令或具体那个文件。

    首版这里列的是禁用文件名(`arl_lite/`、`README.md`、`docs/`、`.py:`),
    变异 M6 写的是 `if "devloop-protocol.md" in cmd` —— 不在禁用词表里,
    判据一声不响。**禁用词表这种写法本身就是洞**:下一个人会写一个
    没进表的名字。

    真正的不变量是:归类函数里不许出现**任何含命令文本或路径形状的字符串字面量**
    (含 `arl-lite `、`.md`、`.py`、`/`)。它只该看引号、槽、占位符、空这几样形状。
    """
    src = Path(__file__).resolve().parent / "test_cli_advice_commandable.py"
    text = src.read_text(encoding="utf-8")
    body = text.split("def _why_unchecked(")[1].split("def _verdict(")[0]
    for literal in re.findall(r'"([^"\n]*)"', body):
        for shape in ("arl-lite ", ".md", ".py", "/", "docs", "README"):
            assert shape not in literal, (
                f"归类逻辑里出现了硬编码的字面量 {literal!r}(含 {shape!r}) —— "
                f"归类只该看形状,不该认具体命令或具体来源文件"
            )


def test_classification_does_not_read_the_source_label_at_all():
    """`_why_unchecked` 的签名里压根没有来源标签 —— 所以它**不可能**按文件归类。

    这条比任何文本检查都硬:哪怕有人在函数体里写死一个文件名,签名不变
    也拦不住;但签名一旦要加 `src` 参数,这里立刻红。
    """
    import inspect

    params = list(inspect.signature(_why_unchecked).parameters)
    assert params == ["cmd", "tail"], f"归类函数的参数变成了 {params},多出来的多半是来源标签"


@pytest.mark.parametrize(
    "tail, expect",
    [
        ('"标题"', True),                          # 引号值
        ('"解释这个关联分析"', True),                # 引号值含中文
        ('\x00 --limit 10000', True),              # f-string 槽
        ('`arl-lite watch add` first', True),      # 反引号
        ("<target>", True),                        # 占位符
        ("# 跑单个门禁并打印实测值", False),         # 注释
        ("直接调  q.finish(...) 而不传 note。", False),  # 散文
        ("| 手动加待办 |", False),                   # 表格说明列
        ("", False),
    ],
)
def test_value_shaped_tail_is_a_positive_test(tail, expect):
    """正向判定:先问「有没有值」,而不是「尾巴空不空」。

    r61 那条反向规则(尾巴非空就放过)会把 `直接调 q.finish(...)` 当成
    「值在这儿」,于是 `devloop done-item` 少写 `item_id` 没人管。
    """
    assert _tail_carries_a_value(tail) is expect


@pytest.mark.parametrize(
    "tail, expect",
    [
        # 两个死路类:它们在真实数据里恒为 0(会被判成 DEAD),
        # 所以只能拿合成输入验 —— 否则这两支就是永远走不到的死代码。
        ("", "没给"),
        ("直接调  q.finish(...) 而不传 note。", "散文收尾"),
        ("# 跑单个门禁并打印实测值", "散文收尾"),
    ],
)
def test_dead_classes_are_reachable_at_all(tail, expect):
    """这两支不许是死代码:它们在 UNCHECKED 里恒为 0,只靠真实数据验不到。"""
    cmd = "arl-lite devloop gate"  # argparse 会抱怨缺 gate_name
    assert _why_unchecked(cmd, tail) == expect


def test_prose_tail_means_the_advice_really_did_omit_it():
    """`散文收尾` 和 `没给` 都是**死路**,不是「查不动」。

    所以 r63 修完,UNCHECKED 从 11 降到 10 —— 少的那 1 条不是被放过,
    是被判成了死路然后**把文档改对了**(`docs/devloop-protocol.md` 里的
    `arl-lite devloop done-item` 补成 `arl-lite devloop done-item <id>`)。
    """
    assert not [a for a in UNCHECKED if _why_unchecked(a[1], a[2]) == "散文收尾"], (
        "还有建议的尾巴是散文 —— 那是建议没给必填参数,应该判死路而不是放过"
    )
    assert len(DEAD) == 0, f"仓库里还有死路:{[a[1] for a in DEAD]}"
    assert len(CLEAN) + len(UNCHECKED) + len(DEAD) == len(ADVICE)
