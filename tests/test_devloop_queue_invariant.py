"""队列永不枯竭 —— 不变式 #4 的回归防线

协议的第一目标是「永远有下一步」。这条不变式被真实场景破过一次:

第 9 轮跑完后队列 0 pending / 8 done,**没有触发播种**。
根因是 `round()` 的阶段顺序:

    1 取待办  → 2 BUILD → 3 TEST → 4 IMPROVE → 5 PLAN → 6 更新队列

第 5 步 PLAN 执行时,当前条目还是 `in_progress`(第 6 步才标 done)。
`phase_plan` 里 `has_pending` 把 in_progress 也算作待办 → 不播种;
而取下一条时只认 `pending` → 找不到 → 返回 "queue has no pending item"。

等第 6 步把它标 done,队列就彻底空了,而且**再没有任何代码路径会回来播种**。

修法:把「耗尽检查 + 播种」移到队列更新**之后**,让"永远有下一步"
成为整轮的**后置条件**,而不是轮中途的一次猜测。

本文件全部离线:用 tmp 目录当仓库,门禁用假门禁替身,
不跑 pytest(否则会递归)。
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import pytest

from arl_lite.devloop import gates
from arl_lite.devloop.gates import GateResult
from arl_lite.devloop.protocol import Loop
from arl_lite.devloop.queue import Item, Queue, _slugify_id, is_backlog_done

REPO = Path(__file__).parents[1]


class _AlwaysGreenGate:
    """替身门禁:永远绿。只在测队列行为,门禁本身另有测试覆盖。"""

    def __init__(self, name):
        self.name = name
        self.blocking = True
        self.promotable = False
        self.promotable_fields = ()

    def run(self, repo: Path) -> GateResult:
        return GateResult(self.name, True, "ok", 0, blocking=True)


@pytest.fixture
def loop(tmp_path, monkeypatch):
    """一个只跑到门禁替身的 Loop,仓库骨架齐全"""
    (tmp_path / "arl_lite").mkdir()
    (tmp_path / "arl_lite" / "x.py").write_text("# placeholder\n", encoding="utf-8")
    # 播种要读 backlog.md,给一份最小的
    dev = tmp_path / "devloop"
    dev.mkdir()
    (dev / "backlog.md").write_text(
        "# backlog\n\n- [P1] test : 补一个排队论用例 |  | 验证反例\n"
        "- [P2] doc  : 写一页架构说明 |  | 验证链接可达\n",
        encoding="utf-8",
    )
    gates.update_baseline(tmp_path, "loc_budget", {"total_loc": 1})
    for name in gates.all_gate_names():
        monkeypatch.setitem(gates._REGISTRY, name, _AlwaysGreenGate(name))
    return Loop(tmp_path)


def _pending_ids(loop: Loop) -> list[str]:
    items = Queue(loop.dev_dir / "queue.json").load()
    return [i.id for i in items if i.status == "pending"]


def _drain(loop: Loop, max_rounds: int = 30) -> int:
    """反复跑轮次直到队列耗尽,返回跑了多少轮

    至少跑一轮:空队列上 `round()` 自己会先播种(取待办那一步),
    所以"跑之前就判断空然后 break"会把首轮播种也一起跳过。
    """
    n = 0
    for _ in range(max_rounds):
        q = Queue(loop.dev_dir / "queue.json")
        items = q.load()
        if n and not any(i.status in ("pending", "in_progress") for i in items):
            break
        loop.round()
        n += 1
    return n


# =====================================================================
# 核心回归:单条目场景(第 9 轮就是这样坏的)
# =====================================================================


def test_queue_is_never_left_empty_after_a_round(loop):
    """跑完一轮后队列必须还有 pending —— 不变式 #4

    第 9 轮的真实场景:队列里只剩一条,跑完就被标 done,
    且因为 PLAN 跑得太早没有播种,循环彻底没了下一步。
    """
    q = Queue(loop.dev_dir / "queue.json")
    q.save([Item(id="only-one", title="唯一一条待办", detail="x",
                 priority=1, kind="change", verify="y")])

    loop.round()

    assert _pending_ids(loop), (
        f"跑完一轮后队列空了,循环没有下一步了。状态={_pending_ids(loop)}"
    )


def test_single_item_queue_gets_seeded(loop):
    """单条目耗尽后必须补上新条目,而不是停在 0"""
    q = Queue(loop.dev_dir / "queue.json")
    q.save([Item(id="only-one", title="唯一一条待办", detail="x",
                 priority=1, kind="change", verify="y")])

    loop.round()

    items = Queue(loop.dev_dir / "queue.json").load()
    ids = [i.id for i in items]
    assert "only-one" in ids, "原有条目不该被删掉"
    assert len(ids) > 1, f"耗尽后没补新条目: {ids}"


def test_repeated_rounds_keep_the_loop_alive(loop):
    """连续跑很多轮,队列永远不空

    这是不变式 #4 最直接的表述:跑多久都不会走到死胡同。
    """
    _drain(loop)
    assert _pending_ids(loop), "耗尽后没播种"
    _drain(loop)
    assert _pending_ids(loop), "第二轮耗尽后没播种"
    _drain(loop)
    assert _pending_ids(loop), "第三轮耗尽后没播种"


# =====================================================================
# PLAN 阶段自身的语义
# =====================================================================


def test_plan_reports_in_progress_as_a_real_next_step(loop):
    """当前 in_progress 的条目就是"下一步",PLAN 不该说没有

    原来的 bug:`has_pending` 把 in_progress 算作有活干,
    但取具体下一条时只认 pending,于是自相矛盾地报 "no pending item"。
    """
    q = Queue(loop.dev_dir / "queue.json")
    q.save([Item(id="busy", title="正在做", detail="x",
                 priority=1, kind="change", verify="y")])
    items = q.load()
    q.mark_in_progress(items[0])
    q.save(items)

    r = loop.phase_plan(loop.status())
    assert r.ok is True, f"PLAN 应对 in_progress 条目给出下一步: {r.detail}"
    assert "busy" in r.detail


def test_plan_seeds_when_queue_is_genuinely_empty(loop):
    """真空了就得播种并如实报出新增数"""
    Queue(loop.dev_dir / "queue.json").save([])
    r = loop.phase_plan(loop.status())
    assert r.ok is True, f"空队列时 PLAN 应播种: {r.detail}"
    assert "seeded" in r.detail
    assert _pending_ids(loop)


# =====================================================================
# 状态记录的诚实性
# =====================================================================


def test_done_items_are_marked_done(loop):
    """修好播种之后,原本的完成标记不能被弄丢"""
    q = Queue(loop.dev_dir / "queue.json")
    q.save([Item(id="only-one", title="唯一一条待办", detail="x",
                 priority=1, kind="change", verify="y")])
    loop.round()
    items = {i.id: i for i in Queue(loop.dev_dir / "queue.json").load()}
    assert items["only-one"].status == "done"


def test_round_records_the_seeding_in_state(loop):
    """播种这件事要落进状态,否则事后无从知道循环靠什么活着"""
    q = Queue(loop.dev_dir / "queue.json")
    q.save([Item(id="only-one", title="唯一一条待办", detail="x",
                 priority=1, kind="change", verify="y")])
    outcome = loop.round()
    assert any("seed" in m.lower() for m in outcome.messages), outcome.messages


# =====================================================================
# 三层播种需要三种不同语义
# =====================================================================
#
# 之前三层共用"撞上就改名复活",结果第 12 轮后队列里 8 条待办全是
# 已完成工作的重推导:置信度(r1 已做)、架构测试(r2)、UNION(r3)、
# 证书(r5)、storage(r7)、DISAPPEARED(r8)……
#
# 队列"非空"但没有一件真活可干 —— 不变式 #4 被满足成了文字游戏。
#
# 根因:backlog.md 是人写的清单,引擎做完一条只在 queue.json 标 done,
# 文件本身不删行;播种再读它就又读到同一批。而"改名复活"把这种
# 重复读变成了无限重新排队。
#
# 正确的三种语义:
#   Tier 1 backlog —— 跳过已完成的。人写下的待办做完了不该自己回来。
#   Tier 2 现状推导 —— 总是提出。条件仍成立 = 活确实没干完。
#   Tier 3 长期项  —— 总是提出。本来就是"还会再来"的。


def _backlog_ids(loop: Loop) -> list[str]:
    """从 backlog.md 解析出的条目 id —— **走真实播种路径**

    不要在这里重新实现 id 派生:那等于在测试里维护一份平行实现,
    一旦实现变了测试就会测错东西(已经踩过:这里写的是按行号派生,
    而真实实现已改成按同名出现次数)。
    """
    return [i.id for i in Queue(loop.dev_dir / "queue.json")._seed_from_backlog(set(), 1)]


def test_backlog_item_is_not_resurrected_after_being_done(loop):
    """人工 backlog 里做完的条目,不能再被播种捡回来"""
    q = Queue(loop.dev_dir / "queue.json")
    ids = _backlog_ids(loop)
    q.save([
        Item(id=i, title=f"已完成-{i}", detail="d", status="done", done_round=1)
        for i in ids
    ])
    before = {i.id for i in q.load()}

    q.seed_if_empty()

    # 只看**新增**的 id。done 记录本来就该留在队列里(那是历史),
    # 断言它们"不存在"是另一回事。
    added = {i.id for i in q.load()} - before
    resurrected = sorted(added & set(ids))
    assert not resurrected, f"已完成的 backlog 条目被复活了: {resurrected}"


def test_backlog_item_is_still_picked_up_when_never_seen(loop):
    """没做过的 backlog 条目必须照常播种 —— 跳过不能过头"""
    q = Queue(loop.dev_dir / "queue.json")
    q.save([])
    q.seed_if_empty()
    after = {i.id for i in q.load()}
    assert after, "全新队列应当从 backlog 播种出待办"
    assert set(_backlog_ids(loop)) & after, (
        f"没做过的 backlog 条目没被播种: {after}"
    )


def test_backlog_item_in_flight_is_not_duplicated(loop):
    """在队列里已经 pending 的 backlog 条目,不能再补一份同内容的"""
    q = Queue(loop.dev_dir / "queue.json")
    q.save([Item(id="item-2-1b85dd", title="甲", detail="d", status="pending")])

    # 队列非空,seed_if_empty 不会补;但强制补一次也不该产出重复内容
    added = q._seed_from_backlog({"item-2-1b85dd"}, 99, skip_existing=True)
    assert all(i.id != "item-2-1b85dd" for i in added), added


def test_fallback_items_do_come_back(loop):
    """长期演进项(Tier 3)本来就该再来 —— 三层语义不能一刀切"""
    q = Queue(loop.dev_dir / "queue.json")
    fb = q._seed_fallback(set(), 1)
    assert fb, "保底层必须总能提出东西"
    # 再叫一次(不传 skip),它还是得提出 —— Tier 3 语义是"总是提出"
    fb2 = q._seed_fallback(set(), 2)
    assert fb2 and {i.id for i in fb2} == {i.id for i in fb2}, fb2


def test_seeding_never_resurrects_completed_backlog_work(loop):
    """整轮视角:跑几轮之后,队列里不能全是已完成工作的鬼影"""
    q = Queue(loop.dev_dir / "queue.json")
    ids = _backlog_ids(loop)
    q.save([
        Item(id=i, title=f"已完成-{i}", detail="d", status="done", done_round=1)
        for i in ids
    ])
    for _ in range(6):
        cur = Queue(loop.dev_dir / "queue.json")
        if not any(i.status in ("pending", "in_progress") for i in cur.load()):
            break
        loop.round()
    pending = [i.id for i in Queue(loop.dev_dir / "queue.json").load()
               if i.status in ("pending", "in_progress")]
    ghosts = sorted(set(pending) & set(ids))
    assert not ghosts, f"已完成的 backlog 工作以鬼影形式回到队列: {ghosts}"


def test_real_backlog_titles_still_map_to_their_done_records(loop):
    """backlog.md 里标了 ✅ 的行,必须在队列里是 done/dropped

    id 是从**标题**派生的。我一度用 `~~删除线~~` 标完成,结果每条都换了
    新 id,`done_ids` 完全拦不住,一次就多造出 7 条鬼影。

    所以真正要守的不变式是:**"标了完成的条目"不能因为改标题而失去它的
    done 记录**。改标题 → id 变 → 队列里那条 done 认不出来 → 变成新待办。

    ## 早先这条测试还断言了什么,以及为什么删掉

    早先它还断言「backlog.md 里每一条在队列里都找得到对应记录」。那是
    **瞬时状态**,不是不变式:人往 backlog.md 加一行新待办,它本来就
    该还没有队列记录 —— 那正是它该被播种成新活的时刻。r18 往 backlog.md
    加了一条待人工裁决的 P0,这条断言就红了,而那完全正常。

    早先还有第二个 bug:`"✅" not in text` 查的是**整个文件**而不是那一条
    条目。文件里只要有**任何**一处 ✅,这个检查对所有条目都通过 ——
    它从来没验到过自己想验的东西。

    现在改成:逐条看 detail 里有没有 ✅,有就必须 done/dropped。

    ## r36:不能再用 `_seed_from_backlog` 来"读"这个文件

    这条测试原来用播种函数读 backlog("走真实解析路径,不重新实现")。
    r36 让 `_seed_from_backlog` 真的跳过 `detail` 开头的 ✅ 行(那个标记
    原来对播种毫无作用,见 docs 7.27),于是这个读法**恰好读不到自己要
    验的那批行** —— `marked_done` 恒为空,前置条件断言直接红。

    改用 `Queue._BACKLOG_LINE` 直接读。**不是重新实现解析器** —— 那就是
    引擎自己的正则,只是绕开了播种的过滤。播种是"该做什么",这里要的是
    "文件里写了什么",两件事。

    这也记一笔:拿一个**会过滤**的函数当数据源用,迟早出事。

    ## r109:这里自己又抄了一份判定,而且已经和实现漂了

    原来这行是 `if "✅" in e["detail"]`,而播种那边是
    `detail.startswith("✅")`。**两份判定不一样**,后果实测:一条 detail
    写 `做完了,✅ r99 完成` 的行,本测试判定它是「标了完成」(于是要求
    队列里有 done 记录),而播种判定它**没标完成**,照样捡回来当新活。

    比「两处都写错」更难查的是这一种:判据漂成**看不见实现的真实行为**,
    于是它绿着,而引擎在做另一件事。r28 的 `is_signal_id` 就是为同一个
    理由抽出来的(`round` / CLI / 测试各写一份前缀匹配)。现在完成标记
    也只有一份:`queue.is_backlog_done`,两边都问它。
    """
    dev = REPO / "devloop"
    q = Queue(dev / "queue.json")
    # 用引擎自己的正则读**全部**条目(含已完成行),不走播种的过滤
    entries = []
    for line in (dev / "backlog.md").read_text(encoding="utf-8").splitlines():
        m = Queue._BACKLOG_LINE.match(line)
        if m:
            entries.append({
                "id": _slugify_id(m.group(3).strip(), 0),
                "detail": m.group(4).strip(),
            })
    assert entries, "真实 backlog.md 解析不出条目"

    known = {i.id: i.status for i in q.load()}
    # 问**引擎那一份**判定,不要在这里再抄一次 —— r109 的教训
    marked_done = [e for e in entries if is_backlog_done(e["detail"])]
    assert marked_done, (
        "backlog.md 里一条 ✅ 都没有 —— 前置条件不成立,"
        "这条测试现在什么都验不到"
    )

    lost = sorted(
        f"{e['id']}(队列里是 {known.get(e['id'], '无记录')})"
        for e in marked_done
        if known.get(e["id"]) not in ("done", "dropped")
    )
    assert not lost, (
        f"backlog.md 里标了 ✅ 却不是 done/dropped:{lost}\n"
        f"  多半是改过标题导致 id 变了 —— id 是从标题派生的。"
        f"改标题前先确认队列里那条 done 记录的 id。"
    )


def test_every_line_that_looks_like_a_backlog_entry_actually_parses():
    """凡是 `- [P…]` 形状的行,**必须**能被 `_BACKLOG_LINE` 解析

    r109 撞上的:补 4 个 ✅ 时把 `- [P1]` 写成了 `- [1]`(`m.group(1)`
    捕到的是 `1`,`P` 是字面量不在捕获组里),于是那 4 行**整行不再匹配**。

    后果是所有判据和播种**一起看不见它们**,而且全都报绿:

    - 播种:`_seed_from_backlog` 认不出 → 走 `done_ids` 兜住了,没出事
    - 「标了 ✅ ⇒ done」:它不在 entries 里,不参与检查
    - 「done ⇒ 标了 ✅」(r109 新加):同样不参与检查
    - 格式校验:我把 `|` 数量查成 2 个,**通过**了 —— 竖线对上了,
      优先级标签错了,没有一条判据看那个标签

    这是 r83 记过的「探针静默漏报,然后我拿它的输出下结论」,这次是我
    自己在同一轮里又踩了一遍。**任何只遍历「解析成功的那些」的判据,
    都有一个共同的盲区:解析失败的那些。** 而 `_BACKLOG_LINE` 匹配不上
    时不报错、不告警,只是安静地不参与。

    所以判据不查「内容对不对」,只查**「我以为在查的行,是不是真的在」**。
    """
    text = (REPO / "devloop" / "backlog.md").read_text(encoding="utf-8")
    looks_like_entry, unparsed = 0, []
    for n, line in enumerate(text.splitlines(), 1):
        if not re.match(r"^\s*-\s*\[", line):
            continue
        looks_like_entry += 1
        if not Queue._BACKLOG_LINE.match(line):
            unparsed.append(f"L{n}: {line[:60]}")
    assert looks_like_entry, (
        "backlog.md 里一条 `- [` 开头的行都没有 —— 前置条件不成立,"
        "这条判据现在什么都验不到")
    assert not unparsed, (
        f"这些行看着是待办条目,却解析不出来,对所有判据和播种都是隐形的:"
        f"{unparsed}\n"
        f"  常见原因是优先级标签写错(`[P1]` 写成 `[1]`)—— "
        f"`_BACKLOG_LINE` 要求 `[P([0-3])]`,那个 `P` 是字面量,不在捕获组里。\n"
        f"  判据只遍历解析成功的行,所以这类错误对它们**全部报绿**。"
    )


def test_no_backlog_entry_is_split_across_two_lines():
    """一条条目的**下半截**落到裸行里,对所有判据和播种都是隐形的

    r110 实测:r106 把一条条目写成了**两行** —— L88 是正常的
    `标题 | detail | verify`,L89 是它的下半截(974 字符,**不带 `- [P` 前缀**),
    里面装着这条待办的**真正做法**「在 backlog.md 顶部写清三条记录指向哪」
    和**第二个 verify**。

    后果:`_BACKLOG_LINE` 匹配不上 L89,于是它不参与播种、不参与任何一致性
    判据、连上面那条「每行都得解析」都看不见它(那条只查 `- [` 开头的行)。
    而读的人以为它是 L88 的一部分。**r109 的判据挡住了「整个条目解析不了」,
    挡不住「条目只写了一半」。**

    ## 为什么判据是「紧跟在条目之后」而不是「任何裸行」

    实测 `backlog.md` 里有 26 处既不是条目也不是小标题/列表/引用的裸行,
    其中 **25 处全在第一条条目之前**(文件头说明区),只有 1 处紧跟在条目后面
    —— 就是那条被劈开的。所以「紧跟在条目之后」这个信号是干净的:文件头里
    的说明本来就是裸行,把它们一起禁掉等于把说明也判成缺陷。
    """
    entries = _real_backlog_entries()
    assert entries, "真实 backlog.md 解析不出条目"

    lines = (REPO / "devloop" / "backlog.md").read_text(
        encoding="utf-8").splitlines()
    orphans = []
    for n, line in enumerate(lines, 1):
        if n < 2 or not line.strip():
            continue
        prev = lines[n - 2].strip()
        if not Queue._BACKLOG_LINE.match(prev):
            continue                       # 上一行不是条目 → 说明区的正常裸行
        if Queue._BACKLOG_LINE.match(line):
            continue                       # 这一行自己就是条目
        if line.lstrip().startswith(("#", "- ", ">")) or line.strip() == "---":
            continue                       # 小标题 / 列表 / 引用 / 分隔线
        orphans.append(f"L{n}(接在 L{n-1} 的条目后): {line[:60]}")
    assert not orphans, (
        f"这些行紧跟在一个条目后面,却又不是条目 —— 八成是条目被劈成了两行,"
        f"下半截对所有判据和播种都是隐形的:\n  {' '.join(orphans)}\n"
        f"  条目必须**一整行**写完(允许很长,长不是问题,断开才是)。")


def test_queue_and_backlog_agree_on_every_shared_items_verify():
    """同一件活的两份副本,`verify` 必须一致

    `backlog.md` 是人维护的**源文件**,`queue.json` 是引擎实际读的那份
    (`protocol.py` 判「这条活做完没有」走的是 `q.verify_result(item.verify)`,
    **队列里那份**)。改源文件不会更新副本,于是引擎可能拿着一条**已经不成立
    甚至早就失效**的验收标准去判定完成。

    ## r111 实测:11 条不一致,而且后果不是「慢一点」

    | 条目 | backlog.md 里 | queue.json 里 | 引擎看到的 `verify_result` |
    |---|---|---|---|
    | `confidence` | `pytest tests/test_confidence_reporting.py -q` | **散文**(怎么做) | `unknown` |
    | `confidence-risk` | 真命令 | **散文** | `unknown` |
    | `confidence-status` | 真命令 | **散文** | `unknown` |
    | `item-c66b81`(误报率实测) | `fp-bench --out ...` | **空字符串** | `unknown` |
    | `check-filter-sql-union` | `pytest tests/test_sql_injection.py` | **`pytest tests/`** | `unknown`(超时) |
    | `storage` / `disappeared` | 单个测试文件 | **`pytest tests/`** | `unknown` |
    | `san-issuer-fingerprint` | `pytest tests/test_tls_cert.py` | **整套门禁** | `unknown` |

    关键在 `verify_result` 的超时是 **30 秒**,而超时和散文都返回 `unknown`;
    `protocol.py` **只拦 `VERIFY_FAIL`**。所以这 11 条里那几条在引擎眼里是
    **「判断不了」→ 不拦 → 任何时候都能被标 done**。

    散文被 `unknown` 是 `verify_result` docstring 里**写明的取舍**(拿散文去挡
    完成会把所有待办永久卡死)。但**超时不是作者的问题,是工具慢** —— 两者被
    归成同一档,这是另一回事,已单独立项(要改 `queue.py`,预算不够)。

    ## 哪一份权威:这里不替人决定

    反过来也成立:队列里那条是「排进队列时约定的验收条件」,拿 backlog.md
    覆盖它,就给了「把验收标准改弱」一条路。**两份都该一致,但谁是源不是
    这条判据该定的。** 它只负责让不一致**可见**。
    """
    q = Queue(REPO / "devloop" / "queue.json")
    known = {i.id: i.verify.strip() for i in q.load()}
    entries = {e["id"]: e for e in _real_backlog_entries()}
    shared = sorted(set(entries) & set(known))
    assert len(shared) >= 30, (
        f"backlog.md 与队列只有 {len(shared)} 条 id 对得上 —— "
        f"范围不对,这条判据的结论不能拿来用")
    drift = [
        f"{iid}\n      backlog.md: {entries[iid]['verify'][:70]}\n"
        f"      queue.json: {known[iid][:70] or '（空字符串）'}"
        for iid in shared if entries[iid]["verify"].strip() != known[iid]
    ]
    assert not drift, (
        f"这 {len(drift)} 条在两边都存在,但 verify 不一致 —— 引擎判完成用的是"
        f"**queue.json 里那份**:\n    " + "\n    ".join(drift)
        + "\n  改 backlog.md 不会更新队列副本,改队列也不会更新源文件。\n"
        f"  哪一份权威是需要人拍板的事;但不一致本身必须先可见。")


def _real_backlog_entries() -> list[dict]:
    """读**真实** `devloop/backlog.md` 的全部条目,不走播种的过滤。

    走 `Queue._BACKLOG_LINE` 而不是 `_seed_from_backlog`:播种是「该做什么」,
    这里要的是「文件里写了什么」。拿一个**会过滤**的函数当数据源用,
    迟早出事(见上面那条 docstring)。
    """
    out = []
    for line in (REPO / "devloop" / "backlog.md").read_text(
            encoding="utf-8").splitlines():
        m = Queue._BACKLOG_LINE.match(line)
        if m:
            out.append({
                "id": _slugify_id(m.group(3).strip(), 0),
                "detail": m.group(4).strip(),
                "verify": m.group(5).strip(),      # r111:队列/backlog 的 verify 对账要用
            })
    return out


def test_done_work_is_marked_done_in_the_human_facing_index():
    """**反方向**:队列里 done/dropped 的行,`backlog.md` 必须标 ✅

    上面那条只查「标了 ✅ ⇒ 队列里 done」。反过来没人查,而那正是人受
    伤的方向:`backlog.md` 是**给人查的索引**(r106 实测确认过这个定位),
    一个人打开它想知道「还剩什么」,看到的是四条没打标记的条目,而队列
    里它们早在 r55/r56/r57/r60 就 done 了。

    r109 实测:`bulk-insert` / `200` / `asset-changes` /
    `watcher-n-3-ports-sites` 四条,队列 done、代码里也真做完了
    (`_ASSET_IDENTITY` 归一 / `Monitor._ASSET_TABLES` 派生 /
    `monitor changes --since` + `monitor prune` / `CHANGE_RECORD_CAP`
    可见截断),但 detail 段一个 ✅ 都没有。

    ## 功能上为什么没出事

    因为播种还有第二道保护:`done_ids` 也会跳过它们。所以这四条**不会**
    被重新排进队列 —— 代价只是索引在骗人。**这也是它能活到今天的原因**:
    没有任何可观测的功能故障,只有一处会误导下一个人的记录。
    所以判据只能盯**一致性**,盯不了「有没有出事」。
    """
    q = Queue(REPO / "devloop" / "queue.json")
    known = {i.id: i.status for i in q.load()}
    entries = _real_backlog_entries()
    assert entries, "真实 backlog.md 解析不出条目"

    finished = [e for e in entries if known.get(e["id"]) in ("done", "dropped")]
    assert finished, (
        "真实队列里一条 done/dropped 都没有 —— 前置条件不成立,"
        "这条测试现在什么都验不到"
    )
    unmarked = sorted(
        f"{e['id']}(队列里是 {known[e['id']]})"
        for e in finished if not is_backlog_done(e["detail"])
    )
    assert not unmarked, (
        f"这些活队列里已经 done/dropped,backlog.md 却没标 ✅:{unmarked}\n"
        f"  backlog.md 是给人查的索引 —— 不标完成,下一个打开它的人"
        f"会以为这些还没做。\n"
        f"  标之前先确认代码里真的做完了,别只信队列的记录。"
    )


def test_the_done_marker_predicate_matches_what_seeding_actually_honours():
    """完成标记的判定只有一份,而且那份**就是播种用的那份**

    r109 的原发缺陷:播种 `detail.startswith("✅")`,判据 `"✅" in detail`,
    两份不一样。后果不是「两处都错」,是**判据看不见实现的真实行为** ——
    `做完了,✅ r99 完成` 这种行,判据放行而播种照样捡回来。

    正控制组用真实判定跑一遍播种:同样的输入,判定说 done 就**不该**被播。
    """
    d = REPO / "devloop" / "_tmp_done_marker_probe"
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / "backlog.md").write_text(
            "# backlog\n"
            "- [P1] change : 开头打的 | ✅ r109 完成 | test -f pyproject.toml\n"
            "- [P1] change : 打在中间的 | 做完了,✅ r109 完成 | test -f pyproject.toml\n",
            encoding="utf-8")
        q = Queue(d / "queue.json")
        q.seed_if_empty()
        seeded = {i.title for i in Queue(d / "queue.json").load()}

        for title, detail in (("开头打的", "✅ r109 完成"),
                              ("打在中间的", "做完了,✅ r109 完成")):
            said_done = is_backlog_done(detail)
            got_seeded = title in seeded
            assert said_done != got_seeded, (
                f"{title}: 判定说标了完成={said_done},实际被播种={got_seeded} —— "
                f"判定和播种用的不是同一份规则"
            )
        assert "开头打的" not in seeded, "开头打了 ✅ 却被播种成新活"
        assert "打在中间的" in seeded, (
            "✅ 不在开头就不算完成标记,这条必须被播种 —— "
            "若它没被播种,说明 `is_backlog_done` 变成了子串包含,"
            "那正是 r109 修掉的那个漂移")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# =====================================================================
# id 稳定性:人工源文件里最要命的脆弱性
# =====================================================================


def test_id_does_not_depend_on_line_position():
    """id 只能由标题决定,不能由行号决定

    早期版本 `_slugify_id(title, idx)` 被调用方喂了**行号**,于是
    backlog.md 里任何一处的增删都会让下方所有条目的 id 全变。
    `backlog.md` 是人手工维护的源文件 —— 加一段说明、调顺序、
    删掉一条做过的,都会让 queue.json 里的 done 记录瞬间失效,
    播种把它们当全新待办重新排队。

    实测踩过两次:一次用 `~~删除线~~` 改标题,一次在文件顶部加了 6 行说明。
    """
    from arl_lite.devloop.queue import _slugify_id

    for title in ("给关联分析规则接上置信度", "add-confidence-grading", "误报率实测"):
        # 行号不参与:第 0 次出现(唯一一次出现)必须就是基础 id
        assert _slugify_id(title, 0) == _slugify_id(title), (
            f"{title!r} 的第 0 次出现不该带序号"
        )
        assert _slugify_id(title).count("-") >= 1


def test_id_is_stable_under_backlog_reordering(loop):
    """在 backlog 里插入/删除行,播种出的 id 全部不变

    走真实播种路径,不手工喂行号 —— 否则测的是错误的用法。
    """
    bp = loop.dev_dir / "backlog.md"
    original = bp.read_text(encoding="utf-8")
    q = Queue(loop.dev_dir / "queue.json")

    before = sorted(i.id for i in q._seed_from_backlog(set(), 1))
    assert before, "解析不到条目,用例失效"

    bp.write_text("# 新加的一段说明\n\n第二行说明\n\n" + original, encoding="utf-8")
    after = sorted(i.id for i in q._seed_from_backlog(set(), 1))
    assert before == after, (
        f"插入说明行后 id 变了:\n  before={before}\n  after={after}"
    )

    bp.write_text("\n".join(original.splitlines()[1:]), encoding="utf-8")
    after2 = sorted(i.id for i in q._seed_from_backlog(set(), 1))
    assert before == after2, (
        f"删掉首行后 id 变了:\n  before={before}\n  after={after2}"
    )


def test_same_title_gets_a_sequence_suffix_not_a_shared_id():
    """同名条目仍要能区分 —— 但序号只在真的同名时才出现"""
    from arl_lite.devloop.queue import _slugify_id

    base = _slugify_id("重复标题")
    assert _slugify_id("重复标题", 0) == base
    assert _slugify_id("重复标题", 2) != base


# =====================================================================
# 软死局:只剩别人处理不了的 in_progress
# =====================================================================


def test_stale_in_progress_is_recovered_not_left_to_block(loop):
    """上一轮遗留的 in_progress 不能把后续轮次卡成 NOOP

    软死局的形状:队列**非空**(所以不播种),但 `next()` 只认 pending,
    选不出待办 → 整轮 NOOP,而且会一直 NOOP 下去。
    实测第 11 轮就是这样空转的。
    """
    q = Queue(loop.dev_dir / "queue.json")
    q.save([Item(id="stuck", title="卡住的", detail="d",
                 priority=1, kind="change", status="in_progress")])

    outcome = loop.round()

    assert any("recover" in m.lower() for m in outcome.messages), (
        f"没有记录恢复动作,本轮多半是空转: {outcome.messages}"
    )
    assert outcome.record.result != "NOOP", "仍空转,软死局没解"
    after = {i.id: i.status for i in q.load()}
    assert after.get("stuck") in ("pending", "done"), after


def test_recovery_only_touches_previous_rounds_items(loop):
    """本轮刚标的 in_progress 不能被自己复位掉"""
    q = Queue(loop.dev_dir / "queue.json")
    q.save([Item(id="a", title="甲", detail="d", priority=1, kind="change")])
    loop.round()
    # 正常跑完不该有 in_progress 残留
    assert not [i.id for i in q.load() if i.status == "in_progress"]


def test_recover_stale_is_idempotent(loop):
    Queue(loop.dev_dir / "queue.json").save(
        [Item(id="x", title="x", detail="d", status="in_progress")]
    )
    assert loop.recover_stale_in_progress() == 1
    assert loop.recover_stale_in_progress() == 0


# =====================================================================
# done 由谁断言
# =====================================================================


def test_operator_asserted_completion_is_labelled(loop):
    """纯人工模式下,done 必须标明是人工断言而非引擎核实

    引擎只看到"门禁全绿",看不到"活干了没有"。实测里误报率实测这条
    待办被连标两次 done,而那两轮都没真的做它 —— 不标注就等于
    让状态文件替执行者背书它没做过的事。
    """
    q = Queue(loop.dev_dir / "queue.json")
    q.save([Item(id="todo", title="要做的事", detail="d",
                 priority=1, kind="change")])

    outcome = loop.round()

    assert outcome.record.completion_source == "operator"
    assert any("no build executor" in m for m in outcome.messages), outcome.messages
    # summary 里也要看得见
    assert "asserted by operator" in outcome.summary()


def test_build_fn_completion_is_marked_as_verified(loop):
    """注入了 build 回调时,done 才是引擎问过执行者的"""
    q = Queue(loop.dev_dir / "queue.json")
    q.save([Item(id="todo", title="要做的事", detail="d",
                 priority=1, kind="change")])

    outcome = loop.round(build=lambda item: (True, "做了"))

    assert outcome.record.completion_source == "build_fn"
    assert not any("no build executor" in m for m in outcome.messages)
    assert "asserted by operator" not in outcome.summary()


# =====================================================================
# id 唯一性:重复 id 会让循环原地空转
# =====================================================================
#
# 真实事故:seed_if_empty 传给旋转逻辑的是 active_ids(只含 pending/in_progress),
# 而 docstring 写的是"已存在且处于 done/dropped 则加 -r 后缀"。文档和代码
# 不是一回事,于是队列全 done 时不触发旋转,补出一批完全同 id 的条目。
#
# 而引擎所有按 id 定位的地方(mark_in_progress / mark_done / round 取待办)
# 都是 `next(i for i in items if i.id == ...)`,永远命中第一条。于是引擎
# 反复翻转那条早已 done 的记录,新条目永远卡 pending —— 循环原地空转。
#
# 实测真实队列被搞成 8 个 id 各两条、8 条全"活跃",而 status 只显示
# 聚合计数,肉眼完全看不出来。


def test_seeding_never_creates_duplicate_ids(loop):
    """播种产出的 id 必须全局唯一"""
    loop.round()
    loop.round()
    dupes = Queue(loop.dev_dir / "queue.json").find_duplicates()
    assert not dupes, f"播种造出了重复 id: {dupes}"


def test_ids_stay_unique_across_many_rounds(loop):
    """跑很多轮也不能攒出重复 id"""
    for _ in range(12):
        q = Queue(loop.dev_dir / "queue.json")
        if not any(i.status in ("pending", "in_progress") for i in q.load()):
            break
        loop.round()
    dupes = Queue(loop.dev_dir / "queue.json").find_duplicates()
    assert not dupes, f"多轮后出现重复 id: {dupes}"


def test_seeding_after_full_drain_rotates_ids(loop):
    """全部 done 后再播种,补出来的必须是新变体而不是同名副本"""
    q = Queue(loop.dev_dir / "queue.json")
    q.save([Item(id="job-a", title="甲", detail="x", priority=1, kind="change")])
    loop.round()  # 甲做完 → done,并触发播种

    ids = [i.id for i in Queue(loop.dev_dir / "queue.json").load()]
    assert ids.count("job-a") == 1, f"原始条目被复制了: {ids}"
    assert any(i != "job-a" for i in ids), f"耗尽后没补出新条目: {ids}"


def test_loop_actually_makes_progress_each_round(loop):
    """每轮都必须真的推进一条,不能原地打转

    这是重复 id 最直接的表现:引擎每轮都"选中"同一个 id,
    但改的是另一条记录,于是这条永远选不中、也永远完不成。
    """
    seen_done: set[str] = set()
    for _ in range(8):
        q = Queue(loop.dev_dir / "queue.json")
        if not any(i.status in ("pending", "in_progress") for i in q.load()):
            break
        loop.round()
        now_done = {i.id for i in q.load() if i.status == "done"}
        assert now_done - seen_done, "这一轮没有任何新条目被标记完成 —— 原地空转了"
        seen_done |= now_done


# ── repair_duplicates ──────────────────────────────────────────────────


def test_repair_removes_duplicate_records(loop):
    q = Queue(loop.dev_dir / "queue.json")
    q.save([
        Item(id="dup", title="旧", detail="d", status="done", done_round=1, attempts=2),
        Item(id="dup", title="旧", detail="d", status="pending"),
        Item(id="dup", title="旧", detail="d", status="in_progress"),
        Item(id="solo", title="独苗", detail="d", status="pending"),
    ])
    assert q.find_duplicates() == {"dup": 3}

    removed = q.repair_duplicates()
    assert removed == 2
    items = q.load()
    assert len(items) == 2
    assert q.find_duplicates() == {}


def test_repair_keeps_the_most_advanced_record(loop):
    """同一个 id 保留进度最靠前的那条"""
    q = Queue(loop.dev_dir / "queue.json")
    q.save([
        Item(id="dup", title="done 记录", detail="d", status="done", done_round=1),
        Item(id="dup", title="pending 记录", detail="d", status="pending"),
        Item(id="dup", title="in_progress 记录", detail="d", status="in_progress"),
    ])
    q.repair_duplicates()
    kept = q.load()
    assert len(kept) == 1
    assert kept[0].status == "in_progress"
    assert kept[0].title == "in_progress 记录"


def test_repair_is_idempotent(loop):
    q = Queue(loop.dev_dir / "queue.json")
    q.save([Item(id="dup", title="dup", detail="d", status="done"), Item(id="dup", title="dup", detail="d", status="pending")])
    assert q.repair_duplicates() == 1
    assert q.repair_duplicates() == 0
    assert len(q.load()) == 1


def test_repair_on_clean_queue_is_a_noop(loop):
    q = Queue(loop.dev_dir / "queue.json")
    q.save([Item(id="a", title="a", detail="d", status="pending"), Item(id="b", title="b", detail="d", status="done")])
    before = [i.id for i in q.load()]
    assert q.repair_duplicates() == 0
    assert [i.id for i in q.load()] == before


def test_repair_preserves_original_order(loop):
    q = Queue(loop.dev_dir / "queue.json")
    q.save([
        Item(id="a", title="a", detail="d", status="pending"),
        Item(id="b", title="b", detail="d", status="done"),
        Item(id="b", title="b", detail="d", status="pending"),
        Item(id="c", title="c", detail="d", status="pending"),
    ])
    q.repair_duplicates()
    assert [i.id for i in q.load()] == ["a", "b", "c"]


def test_repaired_queue_can_drive_the_loop_again(loop):
    """修完的队列必须能重新驱动循环 —— 不然只是看着干净"""
    q = Queue(loop.dev_dir / "queue.json")
    q.save([
        Item(id="a", title="甲", detail="d", status="done", done_round=1),
        Item(id="a", title="甲", detail="d", status="pending"),
    ])
    q.repair_duplicates()
    loop.round()
    # 修完只剩一条 pending 的 a,跑完应该变 done 并重新播种出唯一 id 的新条目
    items = q.load()
    assert not q.find_duplicates(), f"修复后跑一轮又出现重复: {items}"
    assert any(i.status == "done" for i in items)


# ── unmark ─────────────────────────────────────────────────────────────


def test_unmark_puts_a_false_done_back_to_pending(loop):
    """引擎没做那件事却标了 done,unmark 能改回去"""
    q = Queue(loop.dev_dir / "queue.json")
    q.save([Item(id="liar", title="没真做", detail="d", status="done", done_round=3)])

    ok, detail = q.unmark("liar", "本轮实际没做这项")
    assert ok is True, detail

    it = q.load()[0]
    assert it.status == "pending"
    assert it.done_round is None
    assert "unmarked" in it.note and "本轮实际没做" in it.note


def test_unmark_unknown_item_fails_loudly(loop):
    Queue(loop.dev_dir / "queue.json").save([Item(id="real", title="r", detail="d", status="done")])
    ok, detail = q_unmark_helper(loop, "ghost")
    assert ok is False
    assert "no such item" in detail


def q_unmark_helper(loop: Loop, item_id: str):
    return Queue(loop.dev_dir / "queue.json").unmark(item_id)


def test_unmark_refuses_when_id_is_ambiguous(loop):
    """id 重复时不猜改哪一条 —— 提示先 repair

    重复 id 下"改哪条"是歧义的,猜错等于把记录改得更乱。
    """
    q = Queue(loop.dev_dir / "queue.json")
    q.save([
        Item(id="dup", title="甲", detail="d", status="done"),
        Item(id="dup", title="甲", detail="d", status="done"),
    ])
    ok, detail = q.unmark("dup")
    assert ok is False
    assert "repair" in detail
    # 不该有任何改动
    assert len(q.load()) == 2
    assert all(i.status == "done" for i in q.load())


def test_unmark_already_pending_is_a_noop(loop):
    q = Queue(loop.dev_dir / "queue.json")
    q.save([Item(id="p", title="p", detail="d", status="pending")])
    ok, detail = q.unmark("p")
    assert ok is False
    assert "already pending" in detail


def test_real_repo_queue_has_no_duplicate_ids():
    """真实队列的完整性 —— 这条曾经红过

    防止同类问题再次悄悄溜进真实状态。
    """
    real = Queue(REPO / "devloop" / "queue.json")
    dupes = real.find_duplicates()
    assert not dupes, (
        f"真实队列有重复 id: {dupes}\n"
        f"  跑 `arl-lite devloop repair` 修复"
    )


def test_real_repo_queue_has_at_most_one_in_progress():
    """真实队列里 in_progress 的条目最多一条 —— 这条曾经红过

    ## 为什么不是"一条都没有"

    原本写的是"不该有卡在 in_progress 的条目",结果在第 10 轮红了一次:
    `devloop round` 的 TEST 阶段会在队列更新**之前**跑 pytest,
    那一刻当前条目正当着 in_progress(引擎直到门禁跑完才落 done/pending)。
    于是测试读到了轮次中间态,把合法状态当成损坏。

    单独跑 pytest 时不会红 —— 所以它只在 `devloop round` 里偶发,
    这种"换个入口才复现"的失败比稳定失败更难查。

    真正的不变式是"**轮次结束后**不该有残留",但那没法从一轮自己的
    测试运行里断言(测试就跑在这一轮中间)。所以退一步断言随时都成立的
    性质:一轮最多一条 in_progress。

    这仍然抓得住真实事故的signature —— 当时是 8 条同时 in_progress。
    """
    real = Queue(REPO / "devloop" / "queue.json")
    in_progress = [i.id for i in real.load() if i.status == "in_progress"]
    assert len(in_progress) <= 1, (
        f"真实队列有 {len(in_progress)} 条同时 in_progress: {in_progress}\n"
        f"  一轮最多一条;多条说明引擎在按 id 改同一条的另一份副本。"
    )


# =====================================================================
# 队列基础操作的覆盖
# =====================================================================
#
# 这几条是从 `arl_lite/devloop/queue.py` 里那个 62 行的 `_self_test`
# 迁过来的。那是个手写自测,挂在 `python3 -m arl_lite.devloop.queue` 上,
# 问题是三重的:
#
#   1. 它用裸 `assert`,而 `python -O` 会把 assert 整条剥掉 —— 那个开关下
#      它会"通过"而什么都没验
#   2. 它不进 CI,没人跑它,坏了也没人知道
#   3. 它是**第二套**测试入口,和 pytest 各验一遍,漂移了没人发现
#
# 更要紧的是它是这四个行为的**唯一**覆盖:`mark_dropped` / `stats` /
# `next` 的排序键 / `by_priority` 分档,在 pytest 里一条都没有。删掉它
# 等于删掉真覆盖,所以顺序是:先在这里补上,验住,再删。
# 生产代码里不该留测试 —— 该留的是被测逻辑。


def test_sort_key_orders_by_priority_then_round_then_id():
    """next() 的排序键:(priority, created_round, id) 升序

    三个键的顺序是刻意的:优先级高的先做,同优先级先做早提出的,
    还一样就按 id 稳定排 —— 最后一键保证同样输入永远给同样结果,
    否则队列顺序会在两次运行之间抖动。
    """
    items = [
        Item(id="c", title="c", detail="", priority=1, created_round=1),
        Item(id="a", title="a", detail="", priority=0, created_round=9),
        Item(id="b", title="b", detail="", priority=1, created_round=0),
        Item(id="d", title="d", detail="", priority=1, created_round=1),
    ]
    for it in items:
        it.status = "pending"
    q = Queue(Path("/nonexistent/q.json"))
    got = []
    rest = list(items)
    while rest:
        it = q.next(rest)
        got.append(it.id)
        rest = [x for x in rest if x.id != it.id]
    # P0 优先;同为 P1 时先 created_round=0 的 b,再 round=1 的 c/d
    # (同 round 同 priority 按 id 字典序:c < d)
    assert got == ["a", "b", "c", "d"], got


def test_next_skips_non_pending(tmp_path):
    """done / dropped / in_progress 都不该被 next 选中"""
    q = Queue(tmp_path / "q.json")
    items = [
        Item(id="done", title="d", detail="", status="done"),
        Item(id="dropped", title="d", detail="", status="dropped"),
        Item(id="busy", title="b", detail="", status="in_progress"),
        Item(id="open", title="o", detail="", status="pending", priority=3),
    ]
    q.save(items)
    assert q.next().id == "open", "next 必须跳过非 pending"


def test_mark_dropped_records_the_reason_and_keeps_done_round(tmp_path):
    """dropped 要记原因,且**不覆盖**已有的 done_round

    保留 done_round 是为了审计:一条被丢掉的活曾经是在第几轮被做过的,
    这个事实不能因为后续丢弃而消失。
    """
    q = Queue(tmp_path / "q.json")
    it = Item(id="x", title="t", detail="")
    it.done_round = 5
    q.save([it])

    loaded = q.load()[0]
    q.mark_dropped(loaded, "前提不成立")
    q.save([loaded])

    back = q.load()[0]
    assert back.status == "dropped"
    assert "前提不成立" in back.note
    assert "dropped:" in back.note
    assert back.done_round == 5, "dropped 不该抹掉它曾经被做过的轮次"


def test_mark_dropped_appends_to_existing_note(tmp_path):
    """已有的备注不能被丢弃原因覆盖掉"""
    q = Queue(tmp_path / "q.json")
    it = Item(id="x", title="t", detail="", note="前情提要")
    q.save([it])
    loaded = q.load()[0]
    q.mark_dropped(loaded, "原因二")
    q.save([loaded])
    note = q.load()[0].note
    assert "前情提要" in note and "原因二" in note, note


def test_stats_counts_every_status_and_buckets_by_priority(tmp_path):
    """stats 是 status 面板的数据源,漏一个状态就看不出真实分布

    `status_text` 打印的 pending/in_progress/done/dropped 和 by_priority
    全从这里来,少统计一个,面板就会安静地少显示一类条目 ——
    正是第 12 轮"8 条鬼影"能潜伏那么久的原因。
    """
    q = Queue(tmp_path / "q.json")
    # 5 条 pending 的优先级 0,1,2,3,0 —— 0 故意出现两次,
    # 用来验 by_priority 数的是**条目数**而不是去重后的档位数
    q.save([
        Item(id=f"p{i}", title="t", detail="", status="pending", priority=i % 4)
        for i in range(5)
    ] + [
        Item(id="b", title="t", detail="", status="in_progress", priority=1),
        Item(id="d1", title="t", detail="", status="done", priority=1),
        Item(id="d2", title="t", detail="", status="dropped", priority=2),
    ])
    st = q.stats()
    assert st["pending"] == 5, st
    assert st["in_progress"] == 1, st
    assert st["done"] == 1, st
    assert st["dropped"] == 1, st
    assert st["total"] == 8, st
    # by_priority 必须四个档都在,即使某档是 0 —— 面板要稳定显示 P0..P3
    assert set(st["by_priority"]) == {0, 1, 2, 3}, st
    assert st["by_priority"][0] == 2, st   # p0, p4
    assert st["by_priority"][1] == 3, st   # p1, p3 + in_progress + done
    assert st["by_priority"][2] == 2, st   # p2 + dropped
    assert st["by_priority"][3] == 1, st   # p1... i=3
    assert sum(st["by_priority"].values()) == st["total"], (
        f"by_priority 漏了条目:{st}"
    )


def test_exhausted_is_false_while_anything_is_claimable(tmp_path):
    """pending 或 in_progress 任一存在,队列就算没耗尽

    这条是"永远有下一步"不变式的判据。in_progress 也算没耗尽,
    因为那条活确实有人在做 —— 早先的软死局正是这里判错:
    只认 pending 时,一条永远没人认领的 in_progress 会让队列
    永远"非空"却永远选不出待办。
    """
    q = Queue(tmp_path / "q.json")
    q.save([Item(id="a", title="t", detail="", status="in_progress")])
    assert q.exhausted() is False
    q.save([Item(id="a", title="t", detail="", status="done")])
    assert q.exhausted() is True


# =====================================================================
# CLI 层的 lost update:r23 实测
# =====================================================================


def test_devloop_add_actually_persists(tmp_path, monkeypatch):
    """`devloop add` 报成功就必须真的落盘

    ## r23 抓到的真缺陷

    `_cmd_add` 原来是这么写的:

        items = q.load()      # 读旧快照
        ...
        q.add(item)           # ← add() 自己已经读-追加-原子存盘了
        q.save(items)         # ← 拿旧快照再存一次,把刚写的覆盖掉

    典型的 lost update。症状极其恶劣:

        $ arl-lite devloop add foo "标题"
        [+] queued [P1] foo — 标题      # 退出码 0,看着完全成功
        $ ...回读 queue.json...
        foo 不在里面

    退出码 0、输出漂亮、条目却**根本没进队列**。
    r23 实测:登记的 `test-writes-real-home` 凭空消失,
    既不是 done 也不是 dropped,查不到任何痕迹。

    **登记工作的工具在静默丢弃工作。** 这比"队列空掉"严重得多 ——
    人以为活已经记下了,于是永远不会去做它。

    ## 判据

    走真实的 CLI 入口(不是直接调 `Queue.add`),因为丢更新发生在
    CLI 那一层。只验 `Queue.add` 是绿的,恰恰是它当初能溜过去的原因。
    """
    from arl_lite.devloop import cli as devloop_cli

    dev = tmp_path / "devloop"
    dev.mkdir()
    qpath = dev / "queue.json"
    Queue(qpath).save([])          # 空队列,排除干扰

    monkeypatch.setattr(devloop_cli, "_loop",
                        lambda: type("L", (), {"dev_dir": dev})())

    class A:
        item_id = "cli-persist-probe"
        title = "CLI 落盘探针"
        detail = ""
        priority = 1
        kind = "change"
        verify = "echo x"

    rc = devloop_cli._cmd_add(A())
    assert rc == 0, f"add 返回非 0:{rc}"

    ids = [i.id for i in Queue(qpath).load()]
    assert "cli-persist-probe" in ids, (
        f"devloop add 报成功(退出码 0)但条目没落盘。当前队列:{ids}\n"
        "CLI 在拿 add() 之前的旧快照覆盖回去 —— lost update。"
        "登记工作的工具在静默丢弃工作。"
    )


def test_devloop_add_keeps_previously_queued_items(tmp_path, monkeypatch):
    """连着 add 两次,两条都得在

    上一条只验"新条目在不在"。这条验**没有连带把旧的弄丢** ——
    丢更新最隐蔽的形态就是"新条目进去了,顺手挤掉了别的"。
    """
    from arl_lite.devloop import cli as devloop_cli

    dev = tmp_path / "devloop"
    dev.mkdir()
    qpath = dev / "queue.json"
    Queue(qpath).save([])
    monkeypatch.setattr(devloop_cli, "_loop",
                        lambda: type("L", (), {"dev_dir": dev})())

    def add(iid):
        class A:
            item_id = iid
            title = f"标题{iid}"
            detail = ""
            priority = 1
            kind = "change"
            verify = "echo x"
        assert devloop_cli._cmd_add(A()) == 0

    add("first")
    add("second")
    add("third")

    ids = [i.id for i in Queue(qpath).load()]
    assert ids == ["first", "second", "third"], (
        f"连着 add 三次只剩 {ids} —— 每次 add 都在覆盖前一次的结果"
    )
