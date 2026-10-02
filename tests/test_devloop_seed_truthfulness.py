"""播种产出的每一条待办,都必须是一件**真能干的活**

## 为什么这个文件存在

第 13、14、16 轮各撞到过一次同一类 bug,每次长得都不一样:

- 第 13 轮:Tier 2 还在找 `low-confidence` **标签**,而标签早改成
  `confidence:` **字段**了 → 25 条明明合规的规则被要求补标注
- 第 14 轮:播种提出「规则数补到 40」,而 40 是个没人拥有的数字
- 第 16 轮:五条推导检查逐条实测,**没有一条能产出真活** ——
  三条阈值早就过了永不触发,一条提不出东西,还有一条
  (`align-project-plan-doc`)**它自己的 verify 此刻就通过**,
  于是永远完不成,却一直挂在队列头等人去干

第 16 轮把整个「扫项目现状自动推导」层删掉了(`queue.REMOVED_TIER2_WHY`
记了逐条实测数据)。删掉之后,防止它回来的办法不是"记得别加" ——
是让"假活"这件事**在机制上就通不过**。

## 本文件验的是「能不能干完」,不是「说法对不对」

早先这个文件验的是"播种的断言是否符合仓库现状"。那是**真的**但不够:
「规则数 37 < 40」这句话是真的,可是 40 本身没人拥有,照着它干活
就是凑数字。

真正该拦的是另一件事:**一条待办的验收条件此刻是否已经成立**。
已经成立的待办是假活 —— 没人能"重新做一遍"一个已经为真的条件,
它会永远挂在队列里,消耗注意力并污染 done 记录。
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

from arl_lite.devloop.queue import REMOVED_TIER2_WHY, Item, Queue

REPO = Path(__file__).parents[1]
RULES = REPO / "arl_lite" / "modules" / "analysis" / "rules"

# 播种可能产出的**信号类**条目:它们不是工作,是"当前无活可干"的显式声明。
# 放在这里而不是按需豁免,是因为豁免本身需要被 review —— 一个只写在
# 代码注释里的豁免,和没有豁免是一回事。
# `test_signal_items_must_say_so` 守住"信号必须自报家门",防止它退化成
# 伪装成信号的假活。
#
# ## 为什么按「前缀」而不是精确 id 匹配
#
# 播种的兜底项会带轮次后缀(`no-due-maintenance-review-r1`)——
# `_disambiguate` 撞上已占用的 id 就加后缀。于是精确 id 名单会漏掉
# 每一份派生副本,而那些副本的 verify 同样是恒真的。
#
# r21 实测踩到:我关掉队首那条信号之后,下一轮又播种出一条 `-r1` 副本,
# 反假活门禁立刻报「播种产出了假活」。门禁没白写 —— 它抓到的不是假活,
# 是**豁免机制本身的缺陷**:按 id 开名单,就必然漏掉派生 id。
#
# 所以改成前缀匹配。代价是:任何以该前缀开头的条目都会被豁免。
# 这个代价可控,因为前缀带了完整的语义标识,而 `test_signal_items_must_say_so`
# 仍然逐条验它的三条硬要求(verify 恒真 / 标题自报家门 / detail 给指引)。
_SIGNAL_PREFIXES = (
    "no-due-maintenance-review",
)


def _is_signal(item_id: str) -> bool:
    """是不是信号类条目(按前缀,含 `-rN` 派生副本)"""
    return any(item_id == pfx or item_id.startswith(pfx + "-r")
               for pfx in _SIGNAL_PREFIXES)


def _run_verify(verify: str) -> tuple[bool, str]:
    """跑一条 verify,返回 (是否通过, 输出)

    verify 全部来自我们自己的源码常量或本仓库的 backlog.md,是只读检查。
    仍然加超时:一条写坏的 verify 绝不能让测试套件挂死。
    """
    if not verify.strip():
        return False, "(空 verify)"
    try:
        r = subprocess.run(
            ["bash", "-c", verify],
            cwd=REPO, capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False, "(超时)"
    return r.returncode == 0, (r.stdout + r.stderr).strip()[:200]


def _fallback_items(round_no: int = 999) -> list[Item]:
    """保底层能产出的全部待办"""
    return Queue(REPO / "devloop" / "queue.json")._seed_fallback(set(), round_no)


def _seeded_items() -> list[Item]:
    """走**真实**播种路径,返回这次新增的待办

    ## 为什么不直接调 `_seed_from_backlog(...)`

    早先这个辅助函数是 `_seed_from_backlog(set(), 999, skip_existing=True)`
    —— 第一个参数传的是**空集**,也就是"什么都不跳过"。于是它把 8 条早已
    完成的 backlog 条目全当成新待办返回,测试红在"播种产出了假活"上。

    而生产路径 `seed_if_empty()` 传的是 `done_ids`,那 8 条本来就被正确跳过。
    **产品代码是对的,测试是错的** —— 它绕过了一个关键参数,测的根本不是
    真实行为。

    这正是本项目记过的教训:测试辅助函数**不得重新实现**被测逻辑。
    这里"重新实现"的是"怎么调",而那个"怎么调"里恰好藏着唯一要紧的
    参数 —— 抄近路的那一步,正是出问题的那一步。

    所以改成:复制一份真实队列 → 跑真的 `seed_if_empty()` → 取新增项。
    """
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tq = Path(td) / "queue.json"
        src = REPO / "devloop" / "queue.json"
        if src.exists():
            shutil.copy2(src, tq)
        q = Queue(tq)
        before = {i.id for i in q.load()}
        q.seed_if_empty()
        return [i for i in q.load() if i.id not in before]


# =====================================================================
# 核心不变式:播种不许产出「已经完成」的待办
# =====================================================================


def test_no_auto_seeded_item_is_already_done():
    """自动播种产出的每一条**工作项**,verify 此刻都必须**不通过**

    这条是本文件的核心。它直接对应第 16 轮删掉的那一整层里
    `align-project-plan-doc` 的死法:

        verify = test -f docs/PROJECT_PLAN.md   # 此刻就成立

    一条 verify 已经通过的待办,**永远不可能被判定为完成** ——
    没有人能"重新做一遍"一个已经为真的条件。它会一直挂在队列头,
    被下一轮选中、被标成 done(其实什么都没做),或者永远 pending
    挡着真正的活。

    > 假活比队列空掉更坏:队列空掉会报错,假活不会。

    保底层因为 verify 带 90 天时间边界(见 `queue._FRESH_WITHIN`),
    只提**已经到期**的项,所以这一条对它是恒成立的 ——
    刚做完的周期性任务不会因为"总被提出"而变成假活。

    ## `no-due-maintenance-review` 为什么被豁免

    r18 实测到:三条周期项全部未到期时,保底层返回空,队列空掉,
    不变式 #4(#2 轮就有测试守着)真的破了。兜底加了这条复查信号。

    它 `verify="true"` —— 恒真,所以会被这条断言抓到(实测确实抓到了,
    这条门禁没白写)。但它**不是工作项**:它的内容是「当前无到期维护项,
    请人工决定下一步」,完成判据是**人确认过**,不是某个产物存在。
    拿「工作完成」的判据去要求一条信号,只会逼着人伪造一个产物。

    所以豁免是**按类别**给的,不是按 id 开后门:见 `_SIGNAL_IDS`,
    并且 `test_signal_items_must_say_so` 守住"信号必须自报家门"。
    """
    for item in _fallback_items() + _seeded_items():
        if _is_signal(item.id):
            continue
        passed, out = _run_verify(item.verify)
        assert not passed, (
            f"播种产出了假活:{item.id} —— {item.title}\n"
            f"  它的验收条件此刻就已经通过,这条待办永远完不成:\n"
            f"    verify: {item.verify}\n"
            f"  要么把 verify 改成能区分'做了'和'本来就成立'的判据\n"
            f"  (比如查工作产物,而不是查一条常驻不变式),\n"
            f"  要么这条就不该被提出。"
        )


def test_signal_does_not_multiply_while_one_is_still_pending():
    """队里已有一条 **pending** 信号时,不许再提第二条

    ## r22 抓到的自指空转

    第 19 轮加了 `no-due-maintenance-review` 兜底,条件是
    `not out and not existing_ids`。但 `seed_if_empty` 传给
    `_seed_fallback` 的是 `set()`(第 16 轮为了让保底层总能提出候选
    才改的),于是 `existing_ids` 恒为空。

    ## 这条测试改过一次,因为它原本是恒真的

    初版循环里写的是:

    ```python
    for i in q.load():
        if i.status == "pending": i.status = "done"
    q.save(q.load())        # ← 又 load 了一次
    ```

    `q.load()` 第二次返回的是**重新读出来的、没被改动过**的列表。
    信号自始至终是 pending,于是"每轮只有一条"的原因是
    **"它一直没被关掉"**,不是"修复起了作用"。

    恒真测试比没有测试更糟:它让人以为这一支被守住了。
    证据是三个变异(判据恒假 / 删掉 id 判断 / 前缀退化)全都全绿。
    把 close 真正做对之后,才暴露出下面那件更要紧的事 ——
    close 之后**本来就会**重提(那是 #4 要求的,见
    `test_a_closed_signal_is_deliberately_reproposed`),
    所以这条测试守的是"**别在同一次等待里提两条**",
    而把"别追问"交给 drop 开关去管。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        q = Queue(Path(td) / "queue.json")
        for rnd in range(1, 6):
            q.seed_if_empty()
            sig = [i for i in q.load() if "no-due" in i.id]
            assert len(sig) == 1, (
                f"第 {rnd} 轮后有 {len(sig)} 条信号:{[i.id for i in sig]}\n"
                f"  同一次等待里提了两条 —— 人还是得回答同一个问题一次,"
                f"只是队列里多了个位置"
            )
            assert sig[0].status == "pending", (
                f"第 {rnd} 轮信号状态是 {sig[0].status} —— "
                "前置条件不成立:本测试要守的是'已有 pending 时不重复提'"
            )


def test_a_pending_derived_signal_copy_also_blocks_reproposal():
    """队里躺着一条 **pending 的 `-rN` 派生副本**时,不再提新的

    ## 这条是被变异测试逼出来的

    `_no_signal_pending()` 写的是「精确 id **或** `-rN` 前缀」。
    但 `test_signal_does_not_multiply_itself_across_rounds` 里,
    信号是**引擎自己提的** —— 修好之后第二轮根本不会产生 `-r1`,
    于是前缀那一支**永远走不到**。

    变异 18(把前缀退化成精确 id)和变异 19(把 pending 判据改成恒假)
    都是 **16 条测试全绿**。也就是说那段防御代码当时是死的,
    而没人发现。

    ## 它为什么不是死代码

    真实的 `devloop/queue.json` 里**确实躺着**
    `no-due-maintenance-review-r1` 和 `-r2` —— 那是修复前长出来的。
    把前缀退化成精确 id 的话,一旦有一条派生副本还挂在 pending 上,
    引擎就会当成"没有信号"再提一条,同一个问题在队列里占两个位置。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        q = Queue(Path(td) / "queue.json")
        # 手工放一条 pending 的派生副本,模拟修复前遗留的数据
        q.add(Item(
            id="no-due-maintenance-review-r7",
            title="当前无到期维护项,请人工确认下一步",
            detail="(测试构造:模拟修复前遗留的派生副本)",
            verify="人为确认",
            kind="research",
            tags=["maintenance", "review"],
        ))
        assert [i for i in q.load() if i.status == "pending"], "前置条件不成立"

        q.seed_if_empty()

        sigs = [i for i in q.load() if "no-due" in i.id]
        ids = sorted(i.id for i in sigs)
        assert ids == ["no-due-maintenance-review-r7"], (
            f"队里已有 pending 的派生副本,却又提了新的:{ids} —— "
            "前缀匹配这一支是死的,同一个问题会在队列里占两个位置"
        )


def test_a_dropped_derived_signal_also_blocks_reproposal():
    """dropped 的**派生副本**同样关闭提醒

    和上一条是同一件事的两个方向:前缀匹配必须对 `-rN` 成立,
    否则人 drop 掉的那条拦不住下一条长出来。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        q = Queue(Path(td) / "queue.json")
        q.add(Item(
            id="no-due-maintenance-review-r9",
            title="当前无到期维护项,请人工确认下一步",
            detail="(测试构造:模拟遗留的派生副本)",
            verify="人为确认",
            kind="research",
            tags=["maintenance", "review"],
        ))
        ok, _ = q.drop("no-due-maintenance-review-r9", reason="别再提了")
        assert ok

        q.seed_if_empty()
        assert not [i for i in q.load()
                    if "no-due" in i.id and i.status == "pending"], \
            "drop 了派生副本却又提了新的 —— 前缀匹配对 -rN 不成立"


def test_a_dropped_unrelated_item_does_not_disable_the_signal():
    """一条**被 drop 的普通待办**不该永久关掉「那接下来干什么」

    ## 这条是被变异测试逼出来的

    把 `_should_propose_signal()` 里的 id 判断整个删掉(变成
    「只要队里有任何 pending/dropped 就算有信号」),
    **62 条测试全绿**。

    原因很直白:此前所有测试的队列里**只装过信号**,
    所以「同类信号才拦」和「随便什么都拦」分不开。

    ## 为什么这个区分在真实队列里重要

    真实队列里同时躺着信号和**普通待办**(比如被 drop 掉的
    `false-positive-rate-measurement` 误报率实测 —— 那条和信号无关)。
    如果 id 判断失效,那条 dropped 的旧待办会**永久压住**复查信号:
    人明明已经回答过一次"暂时没活",引擎却再也不提醒了。

    症状是队列长期空转而不报错 —— 比队列直接空掉更难发现,
    因为所有不变式看起来都还满足。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        q = Queue(Path(td) / "queue.json")
        # 一条和信号毫无关系的、被 drop 的旧待办
        q.add(Item(
            id="false-positive-rate-measurement",
            title="误报率实测",
            detail="(测试构造:与信号无关的旧待办)",
            verify="跑 fp-bench",
            kind="research",
        ))
        ok, _ = q.drop("false-positive-rate-measurement", reason="已由 r14 覆盖,重复")
        assert ok

        q.seed_if_empty()

        assert [i for i in q.load()
                if "no-due" in i.id and i.status == "pending"], (
            "一条无关的 dropped 待办把复查信号压住了 —— "
            "id 判断失效,队里随便什么都算数。"
            "症状是队列长期空转且所有不变式看起来都还满足"
        )


def test_a_closed_signal_is_deliberately_reproposed():
    """信号被 close 之后,**会**再提一条 —— 这是不变的 #4 要求的

    真实队列里 `no-due-maintenance-review`(done) 后面跟着
    `-r1`、`-r2`,一开始看着像引擎抽风。查下来不是:
    人 close 掉信号,队列就空了,不变式 #4(队列永远非空)要求引擎
    重新挂点什么,而"眼下确实没有自动推导的活"这个事实
    需要有人持续面对 —— 信号就是那个逼迫。

    所以重提本身是**设计**,不是 bug。真正该测的是下一条:
    人要是不想被反复问,有 `drop` 这个显式开关(见下一条测试)。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        q = Queue(Path(td) / "queue.json")
        q.seed_if_empty()
        sig = next(i for i in q.load() if "no-due" in i.id)

        items = q.load()
        target = next(i for i in items if i.id == sig.id)
        target.status = "done"
        q.save(items)

        q.seed_if_empty()
        assert [i for i in q.load() if "no-due" in i.id and i.status == "pending"], \
            "close 之后没重提 —— 不变式 #4 会因为队列空掉而破"


def test_a_dropped_signal_is_never_reproposed():
    """但人**主动 drop** 之后,永远不再提 —— drop 是显式关闭开关

    `(r22 改写)` 这条测试原先断言的是**反的**:"信号被 drop 后该再提,
    因为人丢掉它正是想继续被提醒"。

    那句话是我自己推的,推错了。真实队列里躺着两条 dropped 副本
    (`-r1`、`-r2`),说明人当时**正是在试图让它闭嘴**,
    而引擎照提不误 —— 一个接一个地又长出新的。

    `drop` 在 r17 加的时候是**必须带理由**的,语义就是
    "我明确不要这条,并且我知道为什么"。静默覆盖人的显式指令,
    比队列空掉更坏:队列空掉至少是诚实的。

    注意这和上一条不矛盾:close 之后重提(维持 #4),
    drop 之后不提(尊重显式指令)。人想关,有专门的开关。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        q = Queue(Path(td) / "queue.json")
        q.seed_if_empty()
        sig = next(i for i in q.load() if "no-due" in i.id)
        ok, _ = q.drop(sig.id, reason="别再提醒,我已确认暂时没有活")
        assert ok

        # 连喊三轮,一次都不许再提
        for rnd in range(1, 4):
            q.seed_if_empty()
            pend = [i for i in q.load()
                    if "no-due" in i.id and i.status == "pending"]
            assert not pend, (
                f"第 {rnd} 轮又提了信号:{[i.id for i in pend]} —— "
                "人已经明确 drop 过了。静默覆盖显式指令,比队列空掉更坏"
            )


def test_signal_items_must_say_so():
    """信号类条目必须自报家门 —— 豁免不许变成万能后门

    「这条不是工作,是信号」是一条很方便的说法:说的人多了,假活就能
    堂堂正正地进队列了。所以每个信号 id 必须同时满足三条:

    1. verify 确实是恒真的(它不声称自己能被自动验证)
    2. 标题里明说"无到期/请人工确认"这类话 —— 读队首的人一眼就知道
       这不是活
    3. detail 里给出**该做什么的指引**,而不是描述一项虚构的工作

    这三条任意一条不满足,信号就退化成了伪装成信号的假活。
    """
    for item in _fallback_items() + _seeded_items():
        if not _is_signal(item.id):
            continue
        assert _run_verify(item.verify)[0], (
            f"{item.id} 被列为信号,但它的 verify 并不恒真 —— "
            f"那它就是一条普通工作项,不该享受豁免"
        )
        assert any(w in item.title for w in ("无到期", "请人工确认", "无待办")), (
            f"{item.id} 是信号类,但标题看不出它不是活:{item.title!r}\n"
            f"  读队首的人只看得到标题,他必须能一眼分辨"
        )
        assert any(w in item.detail for w in ("请人工决定", "不是故障", "backlog.md")), (
            f"{item.id} 是信号类,但 detail 没给出该做什么的指引"
        )


def test_recurring_items_are_not_reproposed_before_they_are_due():
    """周期性任务没到期就不该被反复提出

    第 16 轮的实测:`FP_RATE.md` 刚在第 14 轮产出,而保底层
    "总是提出"的语义让 `false-positive-rate-measurement` 每轮都冒出来,
    而它的 verify(那时是 `test -f`)早就在 pass —— 一条永远完不成的
    条目一直占着队列。

    修法是让 verify 回答"到点了吗"(90 天边界),保底层只提不通过的。
    这条守住那个修复不被退回去。

    注意用 `_fallback_items(existing_ids=...)` 绕开 id 占位 ——
    这里验的是"到期判断",不是"哪些 id 已经在队列里"。
    """
    q = Queue(REPO / "devloop" / "queue.json")
    proposed = {i.id for i in q._seed_fallback(set(), 999)}
    for spec in q._FALLBACK:
        due = not q.verify_passes(spec["verify"])
        if due:
            assert spec["id"] in proposed, (
                f"{spec['id']} 已到期(verify 不通过)却没被提出 —— "
                f"到期任务漏掉,队列就少了真活"
            )
        else:
            assert spec["id"] not in proposed, (
                f"{spec['id']} 还没到期(verify 通过)却被提出了 —— "
                f"刚做完的周期性任务每轮冒一次,就是噪音"
            )


def test_periodic_verify_can_tell_done_from_overdue():
    """周期性条目的 verify 必须能区分「刚做完」和「该重做了」

    ## 为什么这条不能省

    变异验证时发现过一个漏网:把 `false-positive-rate-measurement` 的
    verify 从 `_FRESH_WITHIN("docs/FP_RATE.md", 90)` 退回裸
    `test -f docs/FP_RATE.md`,**两个相关测试都照样绿** ——
    因为到期判断会看到它「还没到期」而跳过它。

    但那是真退化:文件一旦存在就永远存在,于是这条周期性工作
    **再也不会被提出**。不是"多提了",是"彻底不干了",而且悄无声息。

    教训:「没有产出假活」不等于「机制是对的」。到期判断会**掩盖**
    verify 本身退化。所以这里直接验 verify 本身 ——
    同样的文件,新的时候通过、旧的时候必须不通过。
    """
    import os
    import time

    from arl_lite.devloop.queue import _FRESH_WITHIN

    with tempfile.TemporaryDirectory() as td:
        art = Path(td) / "report.md"
        art.write_text("x", encoding="utf-8")
        verify = _FRESH_WITHIN(str(art), 90)

        # 刚产出 → 通过
        now = time.time()
        os.utime(art, (now, now))
        passed, out = _run_verify(verify)
        assert passed, f"刚产出的报告应判为'本周期已做':{out}"

        # 100 天前 → 必须不通过(该重做了)
        old = now - 100 * 86400
        os.utime(art, (old, old))
        passed, out = _run_verify(verify)
        assert not passed, (
            f"100 天前的报告仍被判为'已做' —— 这条 verify 永远成立,\n"
            f"  于是这条周期性工作再也不会被提出(静默失效):{out}"
        )

        # 边界内(89 天)→ 仍通过,别让边界本身过敏
        os.utime(art, (now - 89 * 86400, now - 89 * 86400))
        passed, _ = _run_verify(verify)
        assert passed, "89 天还在 90 天窗口内,不该判为过期"


def test_every_fallback_item_uses_a_time_bounded_verify():
    """三条周期项都必须用带时间边界的 verify,不许用裸 `test -f`

    裸 `test -f <报告>` 只能被完成一次:文件是持久的,第一次做完之后
    条件永远成立。这条把 `_FRESH_WITHIN` 钉成硬要求。

    判据用 AST 查函数调用,不用文本匹配 —— 源码里完全可能在**解释**
    为什么不能用裸 test -f,那也含 "test -f" 这个词。
    """
    import ast

    from arl_lite.devloop import queue as qmod

    src = Path(qmod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    # _FALLBACK 这个 ClassVar 的字面量里,每条 verify 都该是 _FRESH_WITHIN(...)
    found = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Dict) and any(
                isinstance(k, ast.Constant) and k.value == "verify" for k in node.keys)):
            continue
        for k, v in zip(node.keys, node.values):
            if not (isinstance(k, ast.Constant) and k.value == "verify"):
                continue
            if isinstance(v, ast.Constant):
                # 纯字面量:那就必须是 _FRESH_WITHIN(...) 的调用结果,
                # 静态字面量在这里就是裸 test -f
                found += 1
                pytest.fail(
                    f"_FALLBACK 里有字面量 verify: {v.value!r}\n"
                    f"  周期性条目必须用 _FRESH_WITHIN(path, days) —— "
                    f"裸 test -f 只能完成一次,之后永远满足"
                )
            elif isinstance(v, ast.Call) and getattr(v.func, "id", "") == "_FRESH_WITHIN":
                found += 1
    assert found == 3, (
        f"只找到 {found} 条 _FRESH_WITHIN verify,_FALLBACK 里应有 3 条"
    )


# =====================================================================
# drop:让一条假活消失,并且必须带原因
# =====================================================================


def test_drop_refuses_without_a_reason(tmp_path):
    """丢弃不给理由必须被拒

    丢弃原因记录的是「我们试过这条路,它不成立」—— 那是这条记录里
    最值钱的部分。没有它,三个月后下一个人会把同样的东西再加回来,
    而且这次连"当初为什么不行"都查不到。
    """
    q = Queue(tmp_path / "q.json")
    q.save([Item(id="x", title="t", detail="", status="pending")])
    ok, msg = q.drop("x", reason="")
    assert not ok
    assert "理由" in msg
    assert q.load()[0].status == "pending", "被拒之后不该改动状态"


def test_drop_records_the_reason_and_clears_the_claim(tmp_path):
    """drop 要记原因、清 owner、保留原 note"""
    q = Queue(tmp_path / "q.json")
    it = Item(id="x", title="t", detail="", status="pending", note="前情")
    it.owner = "agent#1"
    it.claimed_at = 123.0
    q.save([it])
    ok, msg = q.drop("x", reason="阈值是拍脑袋的")
    assert ok and "dropped" in msg
    back = q.load()[0]
    assert back.status == "dropped"
    assert "前情" in back.note and "阈值是拍脑袋的" in back.note
    assert back.owner == "" and back.claimed_at == 0.0
    # done_round 不能被抹掉:审计要看出它曾经是什么状态
    assert back.done_round is None or isinstance(back.done_round, int)


def test_drop_is_not_reversible_by_seeding(tmp_path):
    """dropped 的 id 不会被播种捡回来

    这是 drop 和 unmark 的根本区别:unmark 把误标的 done 改回 pending
    (「其实做了,要重新标」),drop 是让假活**消失**(「这东西不成立」)。
    要是 dropped 还会被播种捡回来,drop 就等于没做。

    ## 为什么 id 必须从真实播种里取,不能手写

    早先这条测试在 fixture 里手写了 `id="item-1"`,而 `backlog.md` 那行
    实际推导出的 id 是 `item-a7438e`。于是播种提出的是一条**全新条目**,
    `item-1` 原封不动地留在那儿 —— 断言通过,但它验的是"没有东西动过",
    不是"dropped 不被复活"。**假绿。**

    改法:先用真实播种拿到条目,再 drop 它,再重新播种。
    id 一致性由生产代码自己保证,测试不插手。
    """
    (tmp_path / "backlog.md").write_text(
        "- [P1] change: 一条会被丢弃的待办 | 细节 | test -f 不存在的文件\n",
        encoding="utf-8",
    )
    q = Queue(tmp_path / "q.json")

    # 先让真实播种种出这条,拿到**它自己的** id
    q.seed_if_empty()
    seeded = q.load()
    assert len(seeded) == 1, f"前置条件不成立:{[i.id for i in seeded]}"
    victim = seeded[0]
    assert victim.status == "pending"

    ok, _ = q.drop(victim.id, reason="假活")
    assert ok
    assert q.load()[0].status == "dropped"

    # 再播种一次:它不该被复活,也不该换个后缀重新出现。
    # 注意不能断言"队列里只有 1 条" —— backlog 跳过它之后保底层会补
    # 长期项,总数会变。要断的是"**这一条**没回来",不是"队列没长"。
    q.seed_if_empty()
    back = q.load()
    mine = [i for i in back if i.id == victim.id or i.id.startswith(victim.id + "-r")]
    # 集合推导不能写在 f-string 表达式里,先算出来
    seen = sorted((i.id, i.status) for i in mine)
    assert len(mine) == 1, f"被丢弃的条目以别的身份回来了:{seen}"
    assert mine[0].status == "dropped", (
        f"{mine[0].id} 被重新提成了 {mine[0].status}"
    )

def test_drop_rejects_a_duplicate_id(tmp_path):
    """id 有重复时拒绝 drop —— 丢哪一条是歧义的,猜错等于改得更乱"""
    p = tmp_path / "q.json"
    p.write_text(json.dumps({
        "version": 1,
        "items": [
            {"id": "dup", "title": "a", "status": "pending"},
            {"id": "dup", "title": "b", "status": "pending"},
        ],
    }), encoding="utf-8")
    ok, msg = Queue(p).drop("dup", reason="x")
    assert not ok
    assert "repair" in msg, msg


def test_every_seeded_item_states_how_to_verify_it():
    """没有 verify 的待办等于没定验收标准

    引擎在人工模式下不会替人干活,它只能靠 verify 让人事后自查。
    verify 为空的条目,唯一的"完成"判据是引擎自己拍板 ——
    那正是第 12 轮 `completion_source` 要暴露的事。
    """
    for item in _fallback_items() + _seeded_items():
        assert item.verify.strip(), (
            f"{item.id} 缺 verify —— 没有验收标准的待办没法判定完成"
        )
        assert item.detail.strip(), (
            f"{item.id} 缺 detail —— 只给标题的话执行者不知道要做什么"
        )


def test_seeded_item_ids_are_derivable_and_readable():
    """id 必须能看出它是什么,且不含随机成分

    id 是引擎按 id 定位每一条记录的唯一依据
    (`mark_in_progress`/`mark_done`/`finish` 全是 `next(i.id == ...)`)。
    id 一旦含轮次/行号,人工源文件动一行就会让所有 done 记录失效,
    已完成的活被重新排队 —— 第 12 轮踩过。
    """
    for item in _fallback_items() + _seeded_items():
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", item.id), (
            f"{item.id!r} 不符合 id 规范(小写字母/数字/短横线)"
        )
        assert item.id == item.id.strip().lower(), item.id


# =====================================================================
# 删除的那一层不许悄悄回来
# =====================================================================


def test_project_state_derivation_is_gone_and_its_reason_is_recorded():
    """「扫项目现状自动推导」不许原样回来,且删除理由必须在代码里

    这条不是"禁止加新功能",是"禁止把**这一层**加回来" ——
    因为它的五个检查里没有一条能产出真活(数据见常量)。
    真要加新的推导,先过 `test_no_auto_seeded_item_is_already_done`。

    顺带钉住 `REMOVED_TIER2_WHY` 非空:没有记录,后人只能靠 git log
    考古,而 `git log` 不会告诉他那五个阈值的实测数字。
    """
    assert not hasattr(Queue, "_seed_from_project_state"), (
        "扫项目现状自动推导被加回来了。它的五个检查实测产不出真活:\n"
        + REMOVED_TIER2_WHY
    )
    assert len(REMOVED_TIER2_WHY) > 500, "删除理由的记录太短,后人查不到"
    # 五条检查的实测数据必须还在记录里,不然下一个人会重新踩一遍
    for marker in ("(a)", "(b)", "(c)", "(d)", "(e)", "假活比队列空掉更坏"):
        assert marker in REMOVED_TIER2_WHY, f"删除理由里缺了 {marker}"


def test_confidence_is_enforced_by_a_gate_not_by_seeding():
    """「每条规则都要有 confidence」现在住在门禁里

    它原先是播种的检查 (a),后果是**只在第一次被检查** —— 提过一次
    之后就不响了,之后谁新加一条漏写 confidence 的规则都没人知道。
    常驻不变式该住在每轮都执行的地方。
    """
    from arl_lite.devloop.gates import _RULES_REQUIRED_KEYS

    assert "confidence:" in _RULES_REQUIRED_KEYS, (
        "confidence 要求不在门禁的白名单里 —— 又退回成'缺了才响一次'了"
    )
    # 现状必须真的合规,否则门禁一直红着
    for p in sorted(RULES.glob("*.yml")):
        txt = p.read_text(encoding="utf-8")
        assert re.search(r"^confidence:\s*\S+", txt, re.M), (
            f"{p.name} 缺 confidence 字段,门禁会红"
        )


# =====================================================================
# 真实队列:不许有已经完成却还挂在 pending 的条目
# =====================================================================


def test_real_queue_has_no_pending_item_whose_verify_already_passes():
    """真实队列里不该有"验收条件已通过却还 pending"的条目

    比单测更狠:它直接查**正在用的那份** queue.json。第 16 轮实测就
    抓到 `align-project-plan-doc` 一直挂在队头,而它的 verify 早通过。

    ## `(r23 修)` 漏了信号豁免

    这条**没有**调用同文件里的 `_is_signal()`,所以信号一挂到队头就红。

    为什么之前一直没暴露:队头通常是 `confidence-risk` 这类真待办,
    verify 是一条真命令,跑不过。而 `no-due-maintenance-review`
    的 verify 是 `true`(它的完成判据是"人确认过",不是某条命令),
    于是**必然**通过 —— 正是它该被豁免的那一类。

    r21 把豁免从"精确 id 名单"改成"前缀匹配"(`_is_signal`)时,
    改了同文件里其他几处,**漏了这一处**。它能潜伏这么久,是因为
    「队头恰好不是信号」;一旦队列空到只剩信号,立刻炸。
    """
    qpath = REPO / "devloop" / "queue.json"
    if not qpath.exists():
        pytest.skip("本机没有 queue.json(每机一份)")
    data = json.loads(qpath.read_text(encoding="utf-8"))
    offenders = []
    for raw in data.get("items", []):
        if raw.get("status") != "pending":
            continue
        if _is_signal(raw.get("id", "")):
            # 信号不是工作,是「当前没有活」的显式声明。
            # 它的 verify 恒真是设计,不是假活 —— 该验的是
            # test_signal_items_must_say_so 那三条硬要求。
            continue
        verify = raw.get("verify") or ""
        if not verify:
            continue
        passed, _ = _run_verify(verify)
        if passed:
            offenders.append(f"{raw['id']} — {raw.get('title','')}")
    assert not offenders, (
        "真实队列里有验收条件已通过却还 pending 的假活:\n  "
        + "\n  ".join(offenders)
        + "\n  用 devloop unmark <id> 改回 pending,或确认它是真的没干完"
        "就换一条能区分'做了'和'本来就成立'的 verify。"
    )
