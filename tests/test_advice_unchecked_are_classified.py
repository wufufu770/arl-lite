"""r63/r64:建议的判定结果必须**分完**,而且每一类都得有牙齿

## 这条判据在守什么

r63 把「查不动 11 条」拆开,发现 1 条是**死路**(散文收尾),修完剩 10 条;
r64 又发现那 8 条「提取截断」不是良性的桶 —— 补全值重验之后 1 条藏着死路
(`-p` 那个,真签名是 `--priority`)。所以判定现在是**四桶**:

| 桶 | 意思 | r64 实测 |
|---|---|---|
| `CLEAN` | 直接解析通过 | 92 |
| `DEAD` | argparse 明确不认 | 0 |
| `UNVERIFIED` | 提取器吃不下尾巴,但**接回去重验过了**,且过了 | 8 |
| `UNCHECKED` | 验不了:抱怨的值本身是占位符,补了就是在猜 | 2 |

`UNVERIFIED` 和 `UNCHECKED` 必须分开:前者验过了,后者永远验不了。
把它们混成「查不动」正是 r60→r63 连续三轮漏掉死路的原因。

## 三个坑,每一个都是判据自己或变异测试逮出来的

1. **「每一类都不许是 0」是错的断言**。我这么写过,当场把自己判红 ——
   `散文收尾` 恰恰因为把死路改对才归零。断言写成了和事实作对的话,那它
   守的就不是它宣称的东西(和 r61 那条「三类必须分完」同一个毛病)。
2. **类列表是牙齿,而牙齿可以直接拿掉**。把 `DEAD_CLASSES` 清成 `()`,
   `parametrize` 一个用例都不生成,判据**静默变成空转**。所以类列表的内容
   本身被钉死。
3. **禁用词表这种写法本身就是洞**。原本用
   `banned = ("arl_lite/", "README.md", "docs/", ".py:")` 守「不许按来源文件
   硬编码」,而变异 M6 写的是 `if "devloop-protocol.md" in cmd` —— 不在表里,
   判据一声不响。改成盯不变量 + 盯函数签名。
"""
from __future__ import annotations

import inspect
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
    UNVERIFIED,
    _why_unchecked,
    _why_unverified,
)

BUCKETS = ("干净", "死路", "重验过", "查不动")
UNVERIFIED_CLASSES = ("重建后干净",)
DEAD_VERDICT_CLASSES = ("重建后死路", "重建不出来")
UNCHECKED_CLASSES = ("占位符",)
DEAD_UNCHECKED_CLASSES = ("散文收尾", "没给")

# r64 实测。数字钉死不是为了「不许变」,是为了让提取器一改、条数一变,
# 那次变化必须是有人**看着数字改的**,而不是悄悄漂过去的。
#
# r65:干净 92 → 101。原因是 tui/app.py 的 TUI 兜底建议原本写的是裸
# 子命令(`query / stats / export`),判据靠字面量 `arl-lite` 前缀提取,
# 一条都看不见 —— 那处死路(`arl-lite query` 缺必填 table)正是这么
# 躲过 r60-r64 四轮的。修法把文案改成带前缀的完整可跑命令,于是
# 「干净」+9,「死路」仍是 0:多出来的正是以前看不见的那批。
# 总条数 102 → 111,四桶之外没有第五桶。
PINNED_BUCKETS = {"干净": 101, "死路": 0, "重验过": 8, "查不动": 2}
PINNED_UNVERIFIED = {"重建后干净": 8}
PINNED_UNCHECKED = {"占位符": 2}


def _bucket(a) -> str:
    for name, group in (("干净", CLEAN), ("死路", DEAD),
                        ("重验过", UNVERIFIED), ("查不动", UNCHECKED)):
        if a in group:
            return name
    return "?"


def _bucket_counts() -> Counter:
    return Counter(_bucket(a) for a in ADVICE)


def test_the_four_buckets_partition_every_advice_item():
    """穷尽且互斥:四桶之和 == 建议总数,不许有第五桶漏网。"""
    counts = _bucket_counts()
    assert set(counts) <= set(BUCKETS), f"冒出了没定义过的桶:{set(counts) - set(BUCKETS)}"
    assert sum(counts.values()) == len(ADVICE), (
        f"四桶没分完:{dict(counts)} 合计 {sum(counts.values())},建议有 {len(ADVICE)} 条"
    )
    assert len(CLEAN) + len(DEAD) + len(UNVERIFIED) + len(UNCHECKED) == len(ADVICE)


def test_the_bucket_names_themselves_have_teeth():
    """桶名是断言的**牙齿**:改掉名字,parametrize 就不生成用例了。

    变异 M5 实测:把类列表清空,判据静默空转而不是变红。
    """
    assert BUCKETS == ("干净", "死路", "重验过", "查不动")
    assert UNVERIFIED_CLASSES == ("重建后干净",)
    assert DEAD_VERDICT_CLASSES == ("重建后死路", "重建不出来")
    assert UNCHECKED_CLASSES == ("占位符",)
    assert DEAD_UNCHECKED_CLASSES == ("散文收尾", "没给")


@pytest.mark.parametrize("name", UNVERIFIED_CLASSES)
def test_the_verified_bucket_is_not_empty(name):
    """「重验过」这一桶要是空了,说明重建这一步被拆了 —— 而它逮到过真死路。"""
    counts = Counter(_why_unverified(c, t) for _, c, t in UNVERIFIED)
    assert counts[name] > 0, f"「{name}」是 0 条:重建这一步可能没接上"


@pytest.mark.parametrize("name", DEAD_VERDICT_CLASSES)
def test_dead_verdicts_never_appear_among_verified_items(name):
    """这两类代表「重验之后确实坏了」/「重验不了」,都不该出现在已验过里。"""
    counts = Counter(_why_unverified(c, t) for _, c, t in UNVERIFIED)
    assert counts[name] == 0, f"「{name}」混进了 UNVERIFIED:{dict(counts)}"


@pytest.mark.parametrize("name", UNCHECKED_CLASSES)
def test_the_unchecked_bucket_is_not_empty(name):
    counts = Counter(_why_unchecked(c, t) for _, c, t in UNCHECKED)
    assert counts[name] > 0, f"「{name}」是 0 条 —— 那 UNCHECKED 装的就不是占位符了"


@pytest.mark.parametrize("name", DEAD_UNCHECKED_CLASSES)
def test_dead_classes_never_show_up_as_unchecked(name):
    """这两类必须是 0 —— 它们代表建议真的没给参数,该判死路。

    首版我把四个类一视同仁地要求「都不许是 0」,结果这一条当场把自己判红了。
    """
    counts = Counter(_why_unchecked(c, t) for _, c, t in UNCHECKED)
    assert counts[name] == 0, f"「{name}」是死路而不是查不动,却混进了 UNCHECKED:{dict(counts)}"


def test_the_counts_are_pinned():
    """钉死具体条数,让漂移变成一次有意的决定。"""
    assert dict(_bucket_counts()) == {k: v for k, v in PINNED_BUCKETS.items() if v}, (
        f"四桶条数变了:{dict(_bucket_counts())} != {PINNED_BUCKETS}。"
        f"先弄清是哪条建议变了形状,再决定是更新判据还是修那条建议"
    )
    assert dict(Counter(_why_unverified(c, t) for _, c, t in UNVERIFIED)) == PINNED_UNVERIFIED
    assert dict(Counter(_why_unchecked(c, t) for _, c, t in UNCHECKED)) == PINNED_UNCHECKED


def test_verified_and_unchecked_are_not_the_same_bucket():
    """「验过了」和「验不了」必须分开 —— 混起来正是 r60→r63 连漏三轮的根因。"""
    assert not set(map(id, UNVERIFIED)) & set(map(id, UNCHECKED))
    assert UNVERIFIED, "UNVERIFIED 是空的:重建那一步没接上?"
    assert UNCHECKED, "UNCHECKED 是空的:占位符那两条去哪了?"


@pytest.mark.parametrize("fname", ["_why_unchecked", "_why_unverified"])
def test_classification_keys_off_shape_not_off_specific_commands(fname):
    """打标必须只认**形状**,不许认具体那条命令或具体那个文件。

    首版这里列的是禁用文件名,变异 M6 写的是 `if "devloop-protocol.md" in cmd`
    —— 不在禁用词表里,判据一声不响。**禁用词表这种写法本身就是洞**。
    真正的不变量:归类函数里不许出现任何含命令文本或路径形状的字符串字面量。

    用 AST 取字面量而不是正则扫源码:正则会把 docstring 也扫进去 ——
    实测第一版就因为 docstring 里有个「 / 」把自己判红了(判据自己犯的
    同一个毛病:断言和事实作对)。docstring 里**应该**能自由写字。
    """
    import ast

    src = Path(__file__).resolve().parent / "test_cli_advice_commandable.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == fname)
    docstring = ast.get_docstring(fn, clean=False)
    for node in ast.walk(fn):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if node.value == docstring:      # docstring 里可以自由写字
            continue
        for shape in ("arl-lite ", ".md", ".py", "/", "docs", "README"):
            assert shape not in node.value, (
                f"{fname} 里出现了硬编码字面量 {node.value!r}(含 {shape!r},"
                f"行 {node.lineno})—— 归类只该看形状"
            )


@pytest.mark.parametrize("fname", ["_why_unchecked", "_why_unverified"])
def test_classification_does_not_read_the_source_label_at_all(fname):
    """归类函数的签名里压根没有来源标签 —— 所以它**不可能**按文件归类。"""
    import test_cli_advice_commandable as crit

    params = list(inspect.signature(getattr(crit, fname)).parameters)
    assert params == ["cmd", "tail"], f"{fname} 的参数变成了 {params},多出来的多半是来源标签"
