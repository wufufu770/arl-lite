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
    """
    qpath = REPO / "devloop" / "queue.json"
    if not qpath.exists():
        pytest.skip("本机没有 queue.json(每机一份)")
    data = json.loads(qpath.read_text(encoding="utf-8"))
    offenders = []
    for raw in data.get("items", []):
        if raw.get("status") != "pending":
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
