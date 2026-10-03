"""r40:变更监控的基线学习 —— 反复变的东西不该一直报

## 缺的是什么

`core/monitor.py` 已经有 `filter_newly_disappeared` 这一层去抖,判据是
**状态转移**(报过 DISAPPEARED 且之后没复活)而不是水位检测 —— 这一层
做得对,r40 不动它。

缺的是另一类噪音:一个资产**反复上下线**或者**某一项属性每轮都在变**
(geo 漂移、指纹抖动、A 站和 B 站来回切)。系统每次都当成新变化报出去,
可它并不知道"这个资产平时就是这个样子"。

## 判据:连着变够多次才算基线

同一 workspace 里,同一 `asset_hash` + 同一 `change_type` + 同一字段,
已经出现过至少 N 次(默认 3),就判为基线 —— 再报就是噪音。

前 N-1 次照常上报。这不是缺陷,是"学习"的含义:**刚开始抖的时候没人
知道它是抖动**,连着抖够多次才敢下结论。少这一次,新出现的抖动就永远
学不出来。

## 零新增存储字段

`asset_changes` 表自己就是历史:它记了每一次变更的 asset_hash /
change_type / diff / detected_at。拿它当基线库,schema 一个字不用动,
基线还随历史自然演化 —— 资产稳定久了,下次再变就重新计次。

## 局限,说在前面

- **只认"变过几次",不认"变回过"。** 连着 3 次 A→B 和连着 3 次在 A/B
  之间来回,这里判成同一件事。前者其实是真变化。
- **阈值全局,不 per-asset 学习。** 按资产各自历史长度调阈值要一张新表。
- **只看条数不看时间。** 一年前变过 3 次的资产,今天再变第 4 次仍会被
  当噪声 —— 除非 `asset_hash` 变了(那通常意味着资产被重建)。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from arl_lite.core.monitor import (
    DEFAULT_BASELINE_LEARN,
    is_baseline_noise,
    record_change,
)
from arl_lite.db.storage import Storage


@pytest.fixture
def st(tmp_path):
    return Storage(workspace="t", workspace_root=tmp_path)


def _flip(st, h, field, n, ctype="TITLE_CHANGED", threshold=None):
    """让某资产某字段连续变 n 次,返回每次有没有被记下来

    `threshold` 默认走 `record_change` 的默认值,**不**在 helper 里写死 ——
    第一版直接 `threshold=0` 传不进来(签名里没有),于是那条测试其实是
    在用默认阈值跑,断言"全部记下"自然失败。helper 悄悄替被测逻辑选参数,
    比没有 helper 更糟。
    """
    kw = {} if threshold is None else {"baseline_threshold": threshold}
    return [record_change(st, "host", ctype, h,
                          before={field: f"{field}-v{i}"},
                          after={field: f"{field}-v{i + 1}"}, **kw)
            for i in range(n)]


# ── 学习期:前几次必须照报,否则新抖动永远学不出来 ──

def test_first_two_flaps_are_still_reported(st):
    """连着变的前 N-1 次照常上报

    少这一次,新出现的抖动就永远学不出来 —— 基线机制会变成"一出现就
    静默",那比噪音更糟。
    """
    kept = _flip(st, "h1", "title", DEFAULT_BASELINE_LEARN - 1)
    assert all(kept), f"学习期不该被静默:{kept}"


def test_flap_beyond_threshold_is_suppressed(st):
    """变够次数之后就不再刷屏"""
    kept = _flip(st, "h1", "title", DEFAULT_BASELINE_LEARN + 2)
    assert kept[:DEFAULT_BASELINE_LEARN] == [True] * DEFAULT_BASELINE_LEARN
    assert not any(kept[DEFAULT_BASELINE_LEARN:]), (
        f"基线判据没起作用,仍在刷屏:{kept}"
    )


# ── 判据的边界:谁也不牵连谁 ──

def test_other_assets_are_unaffected(st):
    """一个资产判了基线,不能把别的资产也一起静默"""
    _flip(st, "noisy", "title", DEFAULT_BASELINE_LEARN + 1)
    assert not is_baseline_noise(st, "quiet", "TITLE_CHANGED", "title"), (
        "别的资产被牵连判成了基线"
    )


def test_other_fields_are_unaffected(st):
    """title 判了基线,不代表同一资产的 ip 也算基线

    这是"逐字段判"和"整条判"的区别。`title` 每轮变而 `ip` 一年不变,
    是常态;反过来把整条记录静默掉,真发生的 ip 变化也就被吞了。
    """
    _flip(st, "h1", "title", DEFAULT_BASELINE_LEARN + 1)
    assert not is_baseline_noise(st, "h1", "TITLE_CHANGED", "ip"), (
        "title 的基线牵连到了 ip"
    )


def test_other_change_types_are_unaffected(st):
    """TITLE_CHANGED 的基线不牵连 STATUS_CHANGED"""
    _flip(st, "h1", "title", DEFAULT_BASELINE_LEARN + 1)
    assert not is_baseline_noise(st, "h1", "STATUS_CHANGED", "title")


# ── 阈值可调,且 0 关掉这个机制 ──

def test_threshold_is_configurable(st):
    """阈值传 1 = 第一次变就当基线;传 0 = 整个机制关掉

    传 0 必须在**一开始**就关,而不是等判据跑起来再判 —— 那样第一轮
    就会被静默掉,而调用方以为"只是这资产抖"。
    """
    assert record_change(st, "host", "C", "t1",
                         before={"a": 1}, after={"a": 2},
                         baseline_threshold=1) is True
    assert record_change(st, "host", "C", "t1",
                         before={"a": 2}, after={"a": 3},
                         baseline_threshold=1) is False

    kept = _flip(st, "t2", "a", 5, threshold=0)
    assert all(kept), f"threshold=0 时整个机制应关掉:{kept}"


def test_threshold_zero_makes_is_baseline_noise_false(st):
    """threshold<=0 时 `is_baseline_noise` 直接 False,不查库"""
    _flip(st, "h1", "title", 10)
    assert is_baseline_noise(st, "h1", "TITLE_CHANGED", "title",
                             threshold=0) is False
    assert is_baseline_noise(st, "h1", "TITLE_CHANGED", "title",
                             threshold=-1) is False


# ── 记录本身不能被这个机制弄坏 ──

def test_suppressed_change_is_not_written_to_the_table(st):
    """被判为基线的那条,数据库里确实没有 —— 不是"记了但不显示"

    这一条是判别力的关键:一个"照写不误,只是别处过滤"的实现也能让
    上面那些 `False` 全绿。而基线的意义正是**不再落盘**。
    """
    from arl_lite.core.monitor import list_changes
    _flip(st, "h1", "title", DEFAULT_BASELINE_LEARN + 2)
    rows = [c for c in list_changes(st, "host", "TITLE_CHANGED")
            if c["asset_hash"] == "h1"]
    assert len(rows) == DEFAULT_BASELINE_LEARN, (
        f"表里有 {len(rows)} 条,应该是 {DEFAULT_BASELINE_LEARN} —— "
        f"被判为基线的那些被写进去了"
    )


def test_first_change_is_still_written(st):
    """反证:没达阈值的那些确实在表里,否则上面那条也可能是"一条都没写" """
    from arl_lite.core.monitor import list_changes
    _flip(st, "h1", "title", 1)
    rows = [c for c in list_changes(st, "host", "TITLE_CHANGED")
            if c["asset_hash"] == "h1"]
    assert len(rows) == 1


def test_change_without_before_after_is_never_suppressed(st):
    """没有 before/after 就无从判字段,一律照记

    `record_change` 允许只传 asset_hash(比如 NEW_ASSET 就没快照)。
    那种情况下如果还去判基线,判据就没有依据,只能瞎猜。
    """
    from arl_lite.core.monitor import list_changes
    for _ in range(DEFAULT_BASELINE_LEARN + 2):
        assert record_change(st, "host", "NEW_ASSET", "h9") is True
    rows = [c for c in list_changes(st, "host", "NEW_ASSET")
            if c["asset_hash"] == "h9"]
    assert len(rows) == DEFAULT_BASELINE_LEARN + 2


def test_diff_is_still_computed_for_recorded_changes(st):
    """基线机制不能顺手把字段级 diff 弄坏"""
    from arl_lite.core.monitor import list_changes
    record_change(st, "host", "TITLE_CHANGED", "hd",
                  before={"title": "a", "ip": "1.1.1.1"},
                  after={"title": "b", "ip": "1.1.1.1"})
    row = next(c for c in list_changes(st, "host", "TITLE_CHANGED")
               if c["asset_hash"] == "hd")
    import json
    d = json.loads(row["diff"])
    assert list(d) == ["title"], f"diff 里有不该在的字段:{d}"
