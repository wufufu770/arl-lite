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

- **阈值全局,不 per-asset 学习。** 按资产各自历史长度调阈值要一张新表。
- **只看条数不看时间。** 一年前变过 3 次的资产,今天再变第 4 次仍会被
  当噪声 —— 除非 `asset_hash` 变了(那通常意味着重建)。
- **要有 before/after 快照才学得出来。** 只传单边快照的变更
  (NEW_ASSET / DISAPPEARED)没有字段级 diff,一律照报。

## r41:分清「抖动」和「单向演进」

上面第一条局限已经修掉了。原来只数"这个字段变过几次",于是

    A→B1, B1→B2, B2→B3, B3→B4

和

    A→B, B→A, A→B, B→A

判成同一件事,前者的第 4 次起被静默。可证书到期日往后推、IP 段迁移、
DNS 切机房**全都是单向的** —— 静默掉的恰恰是真正值得看的东西。

现在多一条判据:除了"变够 `threshold` 次",还要求**变回过**
(某个取值出现过不止一次)。见下面 `test_one_way_*` 那组。

## r42:这套机制在生产路径上够不着(能力缺失,不是测试的错觉)

r42 给 `record_change` 的 `change_type` 加了白名单(按能力,不按 schema
注释里那 6 种词表),合法值只剩 `NEW_ASSET` / `DISAPPEARED`。而判基线
必须有**双边**快照,生产里唯一的两处调用 —— watcher 的 `NEW_ASSET` 只传
`after`、`DISAPPEARED` 只传 `before` —— 都是单边的。

所以:**本文件里所有 `record_change` 调用都传双边快照,是在构造一种
生产中还不存在的情况。** 这是如实反映现状,不是测试在绕开实现。
资产自身的属性变了(换 IP、换标题、换证书),系统现在根本看不见 ——
那是能力缺失,要等一种带字段快照的变更类型落地。

`_CT` 是个合法取值,不代表"NEW_ASSET 真的会有双边快照"。
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

# r42:一个**合法**的 change_type。判基线要双边快照,而本模块能产出的
# 两种类型在生产里都是单边的(见文件头),所以这里传的是一个
# 「合法但生产中还没发生」的组合 —— 如实反映,不是替实现圆场。
_CT = "NEW_ASSET"
_OTHER_CT = "DISAPPEARED"


@pytest.fixture
def st(tmp_path):
    return Storage(workspace="t", workspace_root=tmp_path)


def _flip(st, h, field, n, ctype=_CT, threshold=None):
    """让某资产某字段在两个取值之间来回变 n 次,返回每次有没有被记下来

    `threshold` 默认走 `record_change` 的默认值,**不**在 helper 里写死 ——
    第一版直接 `threshold=0` 传不进来(签名里没有),于是那条测试其实是
    在用默认阈值跑,断言"全部记下"自然失败。helper 悄悄替被测逻辑选参数,
    比没有 helper 更糟。

    ## r41:这里原来造的是**单向演进**,不是抖动

    原版是 `v0→v1→v2→v3`,每次都换新值、永不回头 —— 那正是要修的
    误判本身,却被 `test_flap_beyond_threshold_is_suppressed` 当成
    "抖动被正确压住"来断言。测试全绿,绿的是 bug。
    教训和 r40 那次一样:**测试通过只说明断言和实现一致,不说明断言
    描述的是对的东西**。所以现在 `test_one_way_*` 专门造演进,
    `_flip` 专门造抖动,两者不再共用一个 helper。
    """
    kw = {} if threshold is None else {"baseline_threshold": threshold}
    vals = ("x", "y")
    return [record_change(st, "host", ctype, h,
                          before={field: vals[i % 2]},
                          after={field: vals[(i + 1) % 2]}, **kw)
            for i in range(n)]


def _evolve(st, h, field, n, ctype=_CT, threshold=None):
    """让某资产某字段**一路换新值**变 n 次(v0→v1→v2→…),永不回头

    这是单向演进:证书到期日一路往后推、IP 段迁移、DNS 切机房。
    它"变过很多次",但从不停留在某个值上,所以**不是**抖动。
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
    """变够次数**而且**变回过之后,就不再刷屏

    静默点仍是第 4 次 —— 判据是在这条变更写进历史**之前**跑的,
    所以轮到第 4 条时它能看见的证据才刚好是 3 条。
    """
    kept = _flip(st, "h1", "title", 5)
    assert kept[:DEFAULT_BASELINE_LEARN] == [True] * DEFAULT_BASELINE_LEARN
    assert not any(kept[DEFAULT_BASELINE_LEARN:]), (
        f"基线判据没起作用,仍在刷屏:{kept}"
    )


# ── 判据的边界:谁也不牵连谁 ──

def test_other_assets_are_unaffected(st):
    """一个资产判了基线,不能把别的资产也一起静默"""
    _flip(st, "noisy", "title", DEFAULT_BASELINE_LEARN + 1)
    assert not is_baseline_noise(st, "quiet", _CT, "title"), (
        "别的资产被牵连判成了基线"
    )


def test_other_fields_are_unaffected(st):
    """title 判了基线,不代表同一资产的 ip 也算基线

    这是"逐字段判"和"整条判"的区别。`title` 每轮变而 `ip` 一年不变,
    是常态;反过来把整条记录静默掉,真发生的 ip 变化也就被吞了。
    """
    _flip(st, "h1", "title", DEFAULT_BASELINE_LEARN + 1)
    assert not is_baseline_noise(st, "h1", _CT, "ip"), (
        "title 的基线牵连到了 ip"
    )


def test_other_change_types_are_unaffected(st):
    """一个 change_type 的基线不牵连另一个

    r42 之前这条用的是 `TITLE_CHANGED` / `STATUS_CHANGED` 两个假类型
    —— 两者都不在白名单里,`record_change` 也不校验,于是"隔离生效"
    只需要两个字符串不同,和真实取值无关。收紧白名单后合法值只剩
    `NEW_ASSET` / `DISAPPEARED` 两种,这条反而更真了:**它隔离的
    是仅有的另一个合法类型**。
    """
    _flip(st, "h1", "title", DEFAULT_BASELINE_LEARN + 1)
    assert not is_baseline_noise(st, "h1", _OTHER_CT, "title"), (
        f"{_CT} 的基线牵连到了 {_OTHER_CT}"
    )


# ── 阈值可调,且 0 关掉这个机制 ──

def test_threshold_is_configurable(st):
    """阈值传 1 = 抖动第二轮就当基线;传 0 = 整个机制关掉

    传 0 必须在**一开始**就关,而不是等判据跑起来再判 —— 那样第一轮
    就会被静默掉,而调用方以为"只是这资产抖"。

    ## r41:阈值 1 压不住"一直换新值"的演进,这是对的

    第一版这里用的是 `1→2` 然后 `2→3`(序列 1,2,3 没有重复取值),
    r41 之后第二条照报。**不是回归**:一条从不回头的序列不管阈值多低
    都不该被静默,否则把阈值调小就等于把真变化一起关掉。压不压得住
    抖动,得先真的抖起来(见下一条 `test_low_threshold_...`)。
    """
    assert record_change(st, "host", _CT, "t1",
                         before={"a": 1}, after={"a": 2},
                         baseline_threshold=1) is True
    assert record_change(st, "host", _CT, "t1",
                         before={"a": 2}, after={"a": 1},
                         baseline_threshold=1) is True
    assert record_change(st, "host", _CT, "t1",
                         before={"a": 1}, after={"a": 2},
                         baseline_threshold=1) is False

    kept = _flip(st, "t2", "a", 5, threshold=0)
    assert all(kept), f"threshold=0 时整个机制应关掉:{kept}"


def test_low_threshold_still_suppresses_a_flap(st):
    """阈值 1 配真抖动:第二轮就压得住

    上一条说"阈值 1 压不住演进",那得同时证明它**压得住抖动**,
    否则这句话也可以解释成"阈值 1 什么都不干"。
    """
    kept = _flip(st, "t3", "a", 4, threshold=1)
    assert kept == [True, True, False, False], f"阈值 1 压不住抖动:{kept}"


def test_threshold_zero_makes_is_baseline_noise_false(st):
    """threshold<=0 时 `is_baseline_noise` 直接 False,不查库"""
    _flip(st, "h1", "title", 10)
    assert is_baseline_noise(st, "h1", _CT, "title",
                             threshold=0) is False
    assert is_baseline_noise(st, "h1", _CT, "title",
                             threshold=-1) is False


# ── 记录本身不能被这个机制弄坏 ──

def test_suppressed_change_is_not_written_to_the_table(st):
    """被判为基线的那条,数据库里确实没有 —— 不是"记了但不显示"

    这一条是判别力的关键:一个"照写不误,只是别处过滤"的实现也能让
    上面那些 `False` 全绿。而基线的意义正是**不再落盘**。

    断言对齐 `sum(kept)` 而不是写死条数:静默点本来就随判据挪过位
    (r41),写死的数字一改判据就假红,而这条要验的是"表里条数 == 实际
    被记下来的条数"这个恒等关系,不是某个具体数字。
    """
    from arl_lite.core.monitor import list_changes
    kept = _flip(st, "h1", "title", DEFAULT_BASELINE_LEARN + 2)
    rows = [c for c in list_changes(st, "host", _CT)
            if c["asset_hash"] == "h1"]
    assert len(rows) == sum(kept), (
        f"表里有 {len(rows)} 条,实际记下来 {sum(kept)} 条 —— "
        f"被判为基线的那些被写进去了,或者该记的没记"
    )
    assert len(rows) < len(kept), (
        "一条都没压住,这条测试就没有判别力了"
    )


def test_first_change_is_still_written(st):
    """反证:没达阈值的那些确实在表里,否则上面那条也可能是"一条都没写" """
    from arl_lite.core.monitor import list_changes
    _flip(st, "h1", "title", 1)
    rows = [c for c in list_changes(st, "host", _CT)
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
    record_change(st, "host", _CT, "hd",
                  before={"title": "a", "ip": "1.1.1.1"},
                  after={"title": "b", "ip": "1.1.1.1"})
    row = next(c for c in list_changes(st, "host", _CT)
               if c["asset_hash"] == "hd")
    import json
    d = json.loads(row["diff"])
    assert list(d) == ["title"], f"diff 里有不该在的字段:{d}"


# ══════════════════════════════════════════════════════════════
# r41:分清「抖动」和「单向演进」
# ══════════════════════════════════════════════════════════════
#
# 修的是实测出来的误判:证书到期日一路往后推、IP 段迁移、DNS 切机房
# 都是单向的,原判据把它们和抖动一视同仁,默认阈值 3 之后全部静默。
# 复现(修前):
#     单向演进 A->B1..B4  记录=[True, True, True, False]   ← 第 4 次被吃
#     抖动     A<->B x4   记录=[True, True, True, False]
# 两者完全一样 —— 判据分不出这两件事。


def test_one_way_evolution_is_never_suppressed(st):
    """单向演进:每次都换新值,永不回头 → 一次都不许被静默

    这是 r41 的主判别力测试。r40 的实现在这里会返回 `[T,T,T,F]`。
    """
    kept = _evolve(st, "h1", "cert_expiry", 8)
    assert all(kept), f"单向演进被当成抖动吃掉了:{kept}"


def test_one_way_evolution_is_not_baseline_noise(st):
    """直接问判据本身:变够多次也不构成噪声

    `test_one_way_evolution_is_never_suppressed` 走的是 `record_change`
    的门面。这里直接问 `is_baseline_noise`,防止将来有人从外面把它
    绕过去 —— 门面的行为对了、被测判据本身坏了,前者一样会绿。
    """
    _evolve(st, "h1", "ip", 10)
    assert is_baseline_noise(st, "h1", _CT, "ip") is False, (
        "变过 10 次但从不回头的演进,被判成了基线噪声"
    )


def test_one_way_evolution_is_actually_stored(st):
    """反向确认:演进的那几条确实在表里

    光断言返回值全是 True 不够 —— 一个"什么都不记"的实现也能全 True。
    """
    from arl_lite.core.monitor import list_changes
    n = DEFAULT_BASELINE_LEARN + 5
    _evolve(st, "h1", "title", n)
    rows = [c for c in list_changes(st, "host", _CT)
            if c["asset_hash"] == "h1"]
    assert len(rows) == n, f"表里有 {len(rows)} 条,应该是 {n}"


def test_one_way_evolution_still_respects_threshold_zero(st):
    """threshold=0 关掉机制时,演进照记(不能因为是新判据就绕过它)"""
    kept = _evolve(st, "h1", "title", 5, threshold=0)
    assert all(kept), f"threshold=0 时整个机制应关掉:{kept}"


def test_value_cycle_of_three_is_still_treated_as_noise(st):
    """三值轮转 A→B→C→A 照样判成抖动

    这条是「问回来过没有」而不是「取值不超过 2 个」的理由:
    轮转抖得比两值还厉害,可它有 3 个取值。按"不超过 2 个"判会把
    它误当成演进,于是永远刷屏。序列 A,B,C,A 里 A 出现两次 —— 回来过。

    静默点仍是第 4 次起:判据是在这条变更写进历史**之前**跑的,
    所以轮到第 4 条时它能看见的证据才刚好是 3 条。而三值轮转要到
    第 4 次才第一次出现重复取值 —— 序列 A,B,C 里还没有任何值回来过。
    """
    vals = ["A", "B", "C"]
    kept = [record_change(st, "host", _CT, "h1",
                          before={"t": vals[i % 3]},
                          after={"t": vals[(i + 1) % 3]})
            for i in range(6)]
    assert kept[:3] == [True, True, True], f"学习期不该被静默:{kept}"
    assert not any(kept[3:]), f"三值轮转没被当成抖动:{kept}"


def test_nested_key_with_same_name_is_not_evidence(st):
    """别的字段的取值里含有同名字段,不能算到这个字段头上

    diff 是 `{"geo": {"before": {"ip": ...}, "after": {"ip": ...}}}`,
    文本里确实有 `"ip"`,所以按 `LIKE '%"ip"%'` 一定会命中。但那是
    **geo 这个字段的取值内容**,顶层根本没有 `ip` 这个 key。

    这条防的是"拿文本子串当结构判据"—— 也就是本项目反复栽的那种坑。
    """
    for i in range(DEFAULT_BASELINE_LEARN + 2):
        record_change(st, "host", _CT, "hn",
                      before={"geo": {"ip": f"10.0.0.{i}"}},
                      after={"geo": {"ip": f"10.0.1.{i}"}})
    assert is_baseline_noise(st, "hn", _CT, "ip") is False, (
        "geo 里的嵌套 ip 被当成了顶层 ip 字段的抖动证据"
    )


def test_unreadable_diff_row_is_not_evidence(st):
    """diff 是坏 JSON 的行不算证据,也不该把整轮判据搞崩

    方向很重要:少一条证据 = 少一次抑制 = 多报一次;反过来会把真变化吃掉。

    坏 diff 特意写成**截断**的 `{"title": ` —— 它含 `"title"` 所以躲得过
    LIKE 预筛,进到 Python 里才 parse 失败。第一版这里写的是 `{不是 json`,
    预筛那一关就把它挡在外面了,于是这条测试压根测不到解析分支 ——
    变异测试里 M7 正是靠这个漏洞活下来的。
    """
    _flip(st, "h1", "title", 2)  # 两条有效证据(且确实变回过),还没到 3
    with st._conn() as conn:
        conn.execute(
            """INSERT INTO asset_changes
               (workspace_id, asset_hash, asset_type, change_type, diff)
               VALUES (?, ?, ?, ?, ?)""",
            (st.workspace_id, "h1", "host", _CT, '{"title": '))
    assert is_baseline_noise(st, "h1", _CT, "title") is False, (
        "坏 diff 的一行被当成了有效证据,把阈值凑够了"
    )


def test_type_change_still_produces_a_field_diff(st):
    """`true → 1` 必须留下字段级 diff,不能因为 `True == 1` 就整条吞掉

    这是 r41 变异测试挖出来的第二个真 bug,和本轮的判据无关但在同一段
    代码上:变更记录写进去了,`diff` 却是 NULL —— 记了等于没记,基线
    系统永远看不见它。`False → 0` 同理。
    """
    import json
    from arl_lite.core.monitor import list_changes
    record_change(st, "host", _CT, "hb",
                  before={"ok": True}, after={"ok": 1})
    row = next(c for c in list_changes(st, "host", _CT)
               if c["asset_hash"] == "hb")
    assert row["diff"], "true→1 的字段级 diff 是空的,变更等于没记"
    d = json.loads(row["diff"])
    assert d["ok"] == {"before": True, "after": 1}, f"diff 内容不对:{d}"


def test_bool_and_one_are_different_values(st):
    """JSON 的 true 和 1 必须算两个不同的取值

    `True == 1` 在 Python 里为真。不带类型名的话,序列 `True → 1 → 2`
    会被读成"1 出现了两次"= 回来过,把一次正常的类型收敛当抖动。
    """
    record_change(st, "host", _CT, "hb",
                  before={"ok": True}, after={"ok": 1})
    record_change(st, "host", _CT, "hb",
                  before={"ok": 1}, after={"ok": 2})
    assert is_baseline_noise(st, "hb", _CT, "ok",
                             threshold=2) is False, (
        "true 和 1 被当成了同一个取值"
    )


def test_field_none_is_never_noise(st):
    """field=None 时没有字段可判,一律 False

    没有字段就无从判断"是不是变回过"。按老规矩,判断不了不等于没有,
    更不构成噪声结论。
    """
    _flip(st, "h1", "title", 10)
    assert is_baseline_noise(st, "h1", _CT, None) is False


def test_flap_after_a_long_one_way_run_is_still_detected(st):
    """先演进很久再开始抖,仍然学得出来

    防的是"只认最早的几条历史"这种实现:前面 4 条演进的记录占住了
    历史,后面真的开始抖了也不该被放过。
    """
    _evolve(st, "h1", "ttl", 4)  # v0→v1→v2→v3→v4,四次演进
    kept = [record_change(st, "host", _CT, "h1",
                          before={"ttl": "A" if i % 2 == 0 else "B"},
                          after={"ttl": "B" if i % 2 == 0 else "A"})
            for i in range(4)]
    # 第一次抖动后序列是 v0..v4,A,B —— 全是不同取值,还没回来过,照报
    assert kept[:2] == [True, True], f"还没出现重复取值就被静默了:{kept}"
    # 第三次是 A→B,序列 v0..v4,A,B,A 里 A 第二次出现 = 回来过
    assert kept[2:] == [False, False], f"演进段之后的真抖动没学到:{kept}"
