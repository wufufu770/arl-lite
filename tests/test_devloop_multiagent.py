"""多 agent 并行:锁、原子认领、字段往返

## 为什么需要这些

队列的读写是 `load() → 改 → save()`。`save()` 走 `tmp + os.replace`,
文件不会写坏,但**整段读-改-写不是原子的**。

实测两个 agent 各领一条活、各自保存:

    A 领到 task-0,B 领到 task-1
    A.save() → B.save()
    最终 task-0 退回 pending —— A 的认领被静默覆盖

A 正在干的活重新可领,第三个 agent 会重复领走。"多 agent 同时推进"
这条需求就卡在这儿。

## 本文件的重点

不是"加锁了没",而是**并发下真的没有重复领取**。所以核心用例都起
**真子进程**(不是线程 —— GIL 会让线程测试看不出跨进程竞态)。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from arl_lite.devloop import lock as lockmod
from arl_lite.devloop.queue import Item, Queue

REPO = Path(__file__).parents[1]


@pytest.fixture
def q(tmp_path):
    qq = Queue(tmp_path / "queue.json")
    qq.save([
        Item(id=f"task-{i}", title=f"活 {i}", detail="d", priority=1)
        for i in range(4)
    ])
    return qq


def _spawn_claimers(qpath: Path, n: int, workdir: Path) -> list[subprocess.Popen]:
    """起 n 个真进程同时 claim,各自把结果打到 stdout"""
    worker = workdir / "claimer.py"
    worker.write_text(textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(REPO)!r})
        from pathlib import Path
        from arl_lite.devloop.queue import Queue
        name = sys.argv[1]
        got = Queue(Path({str(qpath)!r})).claim(owner=name)
        print((got.id if got else "NONE") + "\\t" + (got.owner if got else ""))
    """), encoding="utf-8")
    return [
        subprocess.Popen(
            [sys.executable, str(worker), f"agent-{i}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for i in range(n)
    ]


# =====================================================================
# 锁本身
# =====================================================================


def test_lock_is_available_on_this_platform():
    """本机必须有锁 —— 否则整套多 agent 语义都是假的

    退化路径(无 fcntl / 无 msvcrt)时 file_lock 是空操作,
    那必须让"测试"和"日志"都明确说出来,不能默认没事。
    """
    assert lockmod.HAVE_LOCKING, (
        "本平台没有文件锁能力,多 agent 并发会丢更新。"
        "file_lock 会退化成空操作 —— 那是已知的不可用状态,不是安全"
    )


def test_lock_is_exclusive(tmp_path):
    """一个进程持锁时,另一个拿不到"""
    target = tmp_path / "x.json"
    target.write_text("{}", encoding="utf-8")

    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(REPO)!r})
            from pathlib import Path
            from arl_lite.devloop.lock import file_lock
            with file_lock(Path({str(target)!r}), owner="holder"):
                print("locked", flush=True)
                time.sleep(3)
        """)],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        assert lockmod.is_locked(target) is True
        with pytest.raises(lockmod.LockTimeout):
            with lockmod.file_lock(target, timeout=0.3, owner="probe"):
                pass
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_lock_released_after_critical_section(tmp_path):
    target = tmp_path / "x.json"
    target.write_text("{}", encoding="utf-8")
    with lockmod.file_lock(target, timeout=1):
        pass
    # 能立刻再拿到,说明锁没泄漏
    with lockmod.file_lock(target, timeout=0.5):
        pass


def test_lock_released_even_when_body_raises(tmp_path):
    """临界区抛异常不能把锁带进坟墓,否则后面全卡死"""
    target = tmp_path / "x.json"
    target.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        with lockmod.file_lock(target, timeout=1):
            raise ValueError("boom")
    with lockmod.file_lock(target, timeout=0.5):
        pass


def test_lock_records_the_holder(tmp_path):
    target = tmp_path / "x.json"
    target.write_text("{}", encoding="utf-8")
    with lockmod.file_lock(target, timeout=1, owner="alice"):
        assert "alice" in lockmod.lock_holder(target)
        assert str(os.getpid()) in lockmod.lock_holder(target)


# =====================================================================
# 原子认领:核心是"并发下不重复领"
# =====================================================================


def test_concurrent_claims_never_collide(q, tmp_path):
    """6 个真进程抢 4 条待办:每条最多被领一次

    这是本文件最重要的一条。线程测试看不出跨进程竞态(GIL),
    所以这里起的是真进程。
    """
    procs = _spawn_claimers(q.path, 6, tmp_path)
    got = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert not err.strip(), f"claimer 报错: {err[:300]}"
        if out.strip():
            got.append(out.strip().split("\t"))

    claimed = [g[0] for g in got if g[0] != "NONE"]
    assert len(claimed) == len(set(claimed)), f"有条目被重复认领: {claimed}"
    assert set(claimed) == {"task-0", "task-1", "task-2", "task-3"}, got
    # 4 条活 / 6 个 agent → 2 个应该拿到 NONE
    assert len(got) - len(claimed) == 2, got


def test_claim_persists_the_owner(q):
    """owner 必须真的落盘

    这条抓到过一个真 bug:_coerce_item 手工列字段,漏了 owner,
    于是写进去能读到就没了 —— 认领记录等于没有。
    """
    got = q.claim(owner="agent-x")
    # r33:claim 会补 pid,所以 owner 是 "agent-x#<pid>"。断言要按
    # 规范化后的值比,而不是字面量 —— 否则一改行为就红,红的原因
    # 还会掩盖真正的问题。
    want = Queue(q.path)._owner_with_pid("agent-x")
    assert got is not None and got.owner == want
    reloaded = {i.id: i for i in q.load()}
    assert reloaded[got.id].status == "in_progress"
    assert reloaded[got.id].owner == want, "owner 没落盘"
    assert q.claimed_by() == {got.id: want}


def test_claim_does_not_double_count_attempts(q):
    """attempts 只 +1 一次

    早先 round() 会 mark_in_progress(+1) 之后再 mark_in_progress(+1),
    一次轮次算两次重试,反复失败的判断就失真了。

    早先这条断言写的是 `assert q.claim(...) is None or True` ——
    `X is None or True` 恒为真,它什么都验不到。恒真的测试比没有测试
    更坏:它让人以为"验过了"。改成断言**具体次数**。
    """
    it = q.claim(owner="a")
    assert it.attempts == 1
    reloaded = {i.id: i for i in q.load()}
    assert reloaded[it.id].attempts == 1, "认领一次不该把 attempts 记成两次"

    # 再领一次别的,原条的 attempts 不能被顺带改掉
    q.claim(owner="b")
    reloaded = {i.id: i for i in q.load()}
    assert reloaded[it.id].attempts == 1


def test_reclaiming_a_claimed_item_bumps_attempts(q):
    """同一条被反复领,attempts 要真的往上走(否则认不出反复失败的活)"""
    it = q.claim(owner="a")
    assert it.attempts == 1
    again = q.claim(owner="b", item_id=it.id)
    assert again is not None
    assert again.attempts == 2
    # owner 换成后一个 —— 认领可以被顶替,但次数不能丢
    reloaded = {i.id: i for i in q.load()}
    assert reloaded[it.id].owner == Queue(q.path)._owner_with_pid("b")
    assert reloaded[it.id].attempts == 2


def test_claim_specific_item_by_id(q):
    it = q.claim(owner="a", item_id="task-2")
    assert it is not None and it.id == "task-2"


def test_claim_returns_none_when_nothing_pending(q):
    # 注意:改完要 save **同一批对象**。写成 q.save(q.load()) 存的是
    # 重新读出来的一份,前面那些 status 改动根本没落盘 —— 队列里那条
    # 还是 pending,claim 照样领得到。这条测试原本就假绿。
    items = q.load()
    for i in items:
        i.status = "done"
    q.save(items)
    assert q.claim(owner="a") is None


# =====================================================================
# 收尾与释放
# =====================================================================


def test_finish_marks_done(q):
    it = q.claim(owner="a")
    assert q.finish(it.id, ok=True, owner="a", round_no=7) is True
    reloaded = {i.id: i for i in q.load()}
    assert reloaded[it.id].status == "done"
    assert reloaded[it.id].done_round == 7
    assert reloaded[it.id].owner == ""


def test_finish_with_failure_returns_to_pending(q):
    it = q.claim(owner="a")
    assert q.finish(it.id, ok=False, owner="a") is True
    reloaded = {i.id: i for i in q.load()}
    assert reloaded[it.id].status == "pending"
    assert reloaded[it.id].owner == ""


def test_finish_refuses_to_overwrite_another_owner(q):
    """两个 agent 领了同一条(不该发生,但万一)时,后者不能覆盖前者结果"""
    it = q.claim(owner="agent-a")
    assert q.finish(it.id, ok=True, owner="agent-b") is False
    reloaded = {i.id: i for i in q.load()}
    assert reloaded[it.id].status == "in_progress", "被覆盖了"
    assert reloaded[it.id].owner == Queue(q.path)._owner_with_pid("agent-a")


def test_release_returns_the_claim(q):
    """agent 挂掉时必须能释放,否则那条被永远锁住"""
    it = q.claim(owner="a")
    assert q.release(it.id, note="超时放弃") is True
    reloaded = {i.id: i for i in q.load()}
    assert reloaded[it.id].status == "pending"
    assert reloaded[it.id].owner == ""
    assert "超时放弃" in reloaded[it.id].note
    # 释放后别人能领
    assert q.claim(owner="b") is not None


def test_release_on_a_non_claimed_item_is_a_noop(q):
    assert q.release("task-0") is False


# =====================================================================
# 字段往返:守住整类"两份清单漂移"的 bug
# =====================================================================


def test_every_item_field_round_trips(q):
    """Item 的**每个**字段都必须能存进去再读出来

    `_coerce_item` 和 `_item_to_dict` 是两份手维护的清单。
    加了 `owner` 之后忘了在反序列化里加,写进去能读到就没 ——
    这条断言就是为了让"加字段"不再是需要人记得同步两处的事。
    """
    import dataclasses

    it = q.load()[0]
    it.status = "in_progress"
    it.owner = "agent-z"
    it.note = "备注"
    it.done_round = 3
    it.attempts = 2
    it.verify = "v"
    it.detail = "d2"
    it.tags = ["a", "b"]
    it.kind = "test"
    it.priority = 3
    it.created_round = 4
    q.save([it])

    back = q.load()[0]
    for f in dataclasses.fields(Item):
        got = getattr(back, f.name)
        want = getattr(it, f.name)
        assert got == want, f"字段 {f.name} 往返丢失: {want!r} -> {got!r}"


def test_unknown_fields_in_the_file_are_ignored_not_fatal(tmp_path):
    """老版本写的文件带新字段,或别人塞了野字段,都不该读崩"""
    p = tmp_path / "queue.json"
    p.write_text(json.dumps({
        "version": 1,
        "items": [{
            "id": "a", "title": "t", "status": "pending",
            "some_future_field": {"nested": 1},
        }],
    }), encoding="utf-8")
    items = Queue(p).load()
    assert len(items) == 1 and items[0].id == "a"


# =====================================================================
# 认领感知的过期复位
# =====================================================================
#
# 这是整个多 agent 设计里最容易坏的一环,所以测试分两层:
#   一层验判定规则(is_stale_claim)本身
#   一层验端到端行为(真的跑 round,活着的认领不能被抹掉)


def _in_progress(owner: str = "", claimed_at: float = 0.0) -> Item:
    return Item(id="x", title="t", detail="d", status="in_progress",
                owner=owner, claimed_at=claimed_at)


def test_owner_pid_parsing():
    assert Queue.owner_pid("agent#1234") == 1234
    assert Queue.owner_pid("pid-1234") == 1234
    assert Queue.owner_pid("agent-x") is None      # 名字里没 pid
    assert Queue.owner_pid("") is None


def test_stale_claim_resets_when_there_is_no_owner():
    """r11 的软死局形态:没人认领却卡在 in_progress —— 必须复位

    这条不能被"认领感知"改坏:多 agent 之后最容易犯的错就是
    "凡 in_progress 都算有人在干",于是这个老死局又回来了。
    """
    reset, why = Queue.is_stale_claim(_in_progress(owner=""))
    assert reset is True
    assert "no owner" in why


def test_stale_claim_keeps_a_live_owner():
    """owner 进程还活着 → 绝不复位(否则两个 agent 干同一件事)"""
    reset, why = Queue.is_stale_claim(
        _in_progress(owner=f"agent#{os.getpid()}", claimed_at=time.time())
    )
    assert reset is False, f"活着的认领被当成过期了: {why}"
    assert "仍在运行" in why


def test_stale_claim_resets_when_the_owner_process_is_gone():
    """owner 进程没了 → 复位,否则这条被永久锁死"""
    # 找一个确定不存在的 pid:先 fork 拿一个,再确保它已经退出
    dead = _definitely_dead_pid()
    reset, why = Queue.is_stale_claim(
        _in_progress(owner=f"agent#{dead}", claimed_at=time.time())
    )
    assert reset is True, f"死掉的认领没被复位: {why}"
    assert "已退出" in why


def test_stale_claim_keeps_unprobeable_owner_when_claim_is_fresh():
    """判断不了 owner(名字里没 pid)但刚领的 → 当它是活的

    CI runner 上的 agent 名字常常没有 pid,这时唯一的信号是认领时间。
    刚领的东西就复位掉,等于让 agent 领完活发现活被系统抢回去了。
    """
    reset, why = Queue.is_stale_claim(
        _in_progress(owner="ci-runner-7", claimed_at=time.time()),
        stale_after_sec=3600.0,
    )
    assert reset is False, why


def test_stale_claim_resets_unprobeable_owner_after_the_timeout():
    """判断不了但认领超时了 → 复位,否则崩在 CI 里的 agent 永久锁死队列

    这是两条坏路之间的取舍:不确定时倾向不复位(避免重复劳动),
    但必须有超时兜底(避免永久锁死)。这条守的是那个兜底。
    """
    old = time.time() - 10_000
    reset, why = Queue.is_stale_claim(
        _in_progress(owner="ci-runner-7", claimed_at=old),
        stale_after_sec=3600.0,
    )
    assert reset is True, f"超时的认领没被复位: {why}"


def test_stale_claim_treats_missing_claimed_at_as_ancient():
    """老格式文件里的 in_progress 没有 claimed_at → 当成很老,必须复位

    否则升级之后,r11 当年的软死局会在新版本里复活:owner 为空的条目
    走规则 1 已能复位,但 owner 非空、claimed_at 为 0 的(比如手工改过
    的文件)会掉到规则 4,age=inf 才对 —— 这条把 inf 那个分支钉住。
    """
    reset, why = Queue.is_stale_claim(
        _in_progress(owner="ci-runner-7", claimed_at=0.0),
        stale_after_sec=3600.0,
    )
    assert reset is True, why


def test_recover_keeps_a_live_claim_end_to_end(tmp_path):
    """端到端:agent A 领着活,B 跑一整轮,A 的认领必须还在

    这条是本文件针对多 agent 最重要的一条。早先 recover_stale 是
    "复位全部 in_progress",于是 B 的 round 会把 A 正在干的活退回 pending,
    B 的 next() 接着捡起同一条 —— 两个 agent 同时改同一处代码。

    注意用**真 pid**(本进程):is_stale_claim 认的是"owner 进程还在不在",
    用假 pid 会让这条测试因为另一条规则而变绿,验不到它要验的东西。
    """
    from arl_lite.devloop.protocol import Loop

    # state_dir 指向 tmp_path —— 用 Loop(REPO) 会写真实的 devloop/queue.json,
    # 测试就有权改仓库的工作状态了
    loop = Loop(REPO, state_dir=tmp_path)
    q = Queue(loop.dev_dir / "queue.json")
    q.save([
        Item(id="held", title="A 正在干的活", detail="d", priority=1),
        Item(id="free", title="B 该干的活", detail="d", priority=1),
    ])

    # A 认领 held,owner 带本进程 pid = A 确实"活着"。
    # 显式指定 id,不然挑到的是排序第一条(priority/created_round/id 都
    # 相同 → 按 id 字典序,"free" < "held"),那条假设本身就是错的。
    got = q.claim(owner=f"agentA#{os.getpid()}", item_id="held")
    assert got is not None and got.id == "held"

    # B 跑一轮(门禁只挑最便宜的那个,避免测试依赖全量测试结果)
    loop.recover_stale_in_progress()

    after = {i.id: i for i in q.load()}
    assert after["held"].status == "in_progress", "A 的活被 B 的 round 抹掉了"
    assert after["held"].owner == f"agentA#{os.getpid()}", "owner 被清空了"

    # B 只能拿到 free 那条 —— 绝不能和 A 撞车
    assert q.next().id == "free"


def test_recover_still_breaks_the_r11_soft_deadlock(tmp_path):
    """认领感知不能把 r11 的软死局带回来

    反向验证:上一条说"活着的要留",这条说"没主的要清"。
    两条一起在,规则才不是单向放宽。
    """
    from arl_lite.devloop.protocol import Loop

    # state_dir 指向 tmp_path —— 用 Loop(REPO) 会写真实的 devloop/queue.json,
    # 测试就有权改仓库的工作状态了
    loop = Loop(REPO, state_dir=tmp_path)
    q = Queue(loop.dev_dir / "queue.json")
    q.save([Item(id="orphan", title="没人管的", detail="d",
                 status="in_progress", owner="")])

    # 软死局的形态:队列非空(有 in_progress)→ 不播种
    assert q.exhausted() is False
    assert q.next() is None, "前置条件不成立:这条本来就选不出待办"

    assert loop.recover_stale_in_progress() == 1
    after = {i.id: i for i in q.load()}
    assert after["orphan"].status == "pending"
    assert q.next() is not None, "复位后仍然选不出待办 → 软死局没解"


def test_recover_is_safe_against_a_concurrent_claim(tmp_path):
    """复位和认领并发时不能互相覆盖

    复位是 load→判→save,认领也是 load→判→save。两者都进临界区才成立。
    这条起一个真子进程持续认领,主进程同时反复复位,最后检查:
    认领方拿到的条目不能凭空消失。
    """
    from arl_lite.devloop.protocol import Loop

    # state_dir 指向 tmp_path —— 用 Loop(REPO) 会写真实的 devloop/queue.json,
    # 测试就有权改仓库的工作状态了
    loop = Loop(REPO, state_dir=tmp_path)
    q = Queue(loop.dev_dir / "queue.json")
    q.save([
        Item(id=f"t{i}", title=f"活 {i}", detail="d", priority=1)
        for i in range(30)
    ])

    procs = [
        subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(f"""
                import sys
                sys.path.insert(0, {str(REPO)!r})
                from pathlib import Path
                from arl_lite.devloop.queue import Queue
                q = Queue(Path({str(q.path)!r}))
                for _ in range(20):
                    q.claim(owner="live")
                    q.release("t0", note="x")
            """)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(3)
    ]
    for _ in range(20):
        loop.recover_stale_in_progress()
    for p in procs:
        p.communicate(timeout=60)

    # 30 条必须一条不少 —— 复位不该吃掉任何条目
    after = q.load()
    assert len(after) == 30, f"条目数变了:{len(after)}"
    assert {i.id for i in after} == {f"t{i}" for i in range(30)}


def test_protocol_does_not_duplicate_the_locked_read_modify_write():
    """protocol 不得自己再实现一套"加锁的读-改-写"

    早先 `recover_stale_in_progress` 把 lock+load+判+save 整套写在
    protocol 里,而 Queue 同时又有一套加锁原子操作。**同一条规则两个实现**,
    而其中一版真的漏了(load 在锁外、save 在锁内),安静地丢更新 ——
    没有报错,没有告警,只是别的 agent 刚抢到的活被过期快照抹掉了。

    这条断言直接把那个漂移钉死:protocol 只能调 Queue 的方法。
    """
    import inspect
    from arl_lite.devloop.protocol import Loop

    src = inspect.getsource(Loop.recover_stale_in_progress)
    # 复位必须在 Queue 的临界区内完成
    assert "q.recover_stale(" in src, "protocol 没走 Queue.recover_stale"
    # protocol 不该自己碰锁
    assert "file_lock" not in src, "protocol 自己在加锁,规则又变成两处"
    # 也不该自己 load→save
    assert "q.load()" not in src and "q.save(" not in src, \
        "protocol 自己做读-改-写,绕开了 Queue 的加锁入口"
    # 顺带守住 Queue 那一侧:复位必须真的在锁里 load
    qsrc = inspect.getsource(Queue.recover_stale)
    locked_at = qsrc.index("with self._locked(")
    assert locked_at < qsrc.index("self.load()"), \
        "Queue.recover_stale 的 load 在锁外 —— 丢更新会回来"


def test_production_modules_do_not_carry_their_own_self_tests():
    """arl_lite/ 里不许出现手工自测

    queue.py 曾有一个 62 行的 `_self_test()`,挂在
    `python3 -m arl_lite.devloop.queue` 上,验 mark_dropped / stats /
    排序键 / by_priority。问题不是它写错了,而是它**是那四项的唯一覆盖**,
    却:

    - 用裸 `assert`,`python -O` 下整条被剥掉,变成"通过"而什么都没验
    - 不进 CI,没人跑,坏了没人知道
    - 是 pytest 之外的第二套入口,漂移了没人发现

    已经把这四项迁到 `tests/test_devloop_queue_invariant.py`(2026-10),
    这条守住迁移结果不被逆转。

    判据是 `__main__` 块 + 函数名,不靠文本匹配 —— 因为 docstring 里
    完全可能在**解释**为什么删掉自测,那也含有 "self_test" 这个词。
    靠文本匹配会把解释当成违规(和第 14 轮踩过的那个坑同一类)。
    """
    import ast

    offenders = []
    for py in sorted((REPO / "arl_lite").rglob("*.py")):
        src = py.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        has_main = any(
            isinstance(n, ast.If) and ast.dump(n.test).find("__main__") >= 0
            for n in tree.body
        )
        self_tests = [
            n.name for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
            and ("self_test" in n.name or n.name.startswith("_selftest"))
        ]
        if self_tests:
            offenders.append(f"{py.relative_to(REPO)}: 自测函数 {self_tests}")
        elif has_main:
            # 允许 __main__ 块,但它不许自己调测试
            for n in ast.walk(tree):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                        and ("self_test" in n.func.id or "selftest" in n.func.id):
                    offenders.append(
                        f"{py.relative_to(REPO)}: __main__ 里调用了 {n.func.id}()"
                    )
    assert not offenders, (
        "生产代码里又出现了自测:\n  " + "\n  ".join(offenders)
        + "\n测试属于 tests/。跑 pytest,别自己搭第二套入口。"
    )


def _definitely_dead_pid() -> int:
    """拿一个确定已经退出的 pid

    直接用 os.getpid()+一个偏移去撞是不行的 —— 那个 pid 可能真的活着。
    办法是起一个子进程,等它退出,拿它的 pid。
    """
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait(timeout=30)
    pid = p.pid
    # 再确认一次:万一被别的进程抢了这个 pid(极罕见,但要验而不是假设)
    alive = Queue.owner_alive(f"x#{pid}")
    assert alive is False, f"pid {pid} 居然还活着,测不出死亡分支"
    return pid


# =====================================================================
# r33:owner 里必须带一个「认领者探测得出来」的 pid
# =====================================================================


def test_claim_always_stores_an_owner_with_a_parsable_pid(q):
    """传不含 pid 的 owner 也要补上 —— 不补是**静默**退化

    owner_alive 解析不出 pid 时返回 None,于是 recover_stale 只能靠
    "认领多久了"猜:一条早就死掉的认领会一直占着 in_progress,直到
    STALE_CLAIM_SEC 超时才复位,而不是立刻。
    """
    got = q.claim(owner="no-pid-here")
    assert got is not None
    assert Queue.owner_pid(got.owner) is not None, (
        f"owner 记成了 {got.owner!r},解析不出 pid —— "
        f"recover_stale 会退化成按认领时长猜,而这里不报错、不告警"
    )
    assert got.owner.startswith("no-pid-here"), "不该把用户写的标识改掉"


def test_owner_pid_parses_every_format_claim_can_produce():
    """`round-pid-<pid>` 是 r31 加的,当时解析不出来(r33 才修好)

    原来正则是 (?:#|^pid-)(\\d+)$,`^` 锚在字符串开头,所以
    `round-pid-12345` 解析不出 pid —— owner 确实记上了,recover_stale
    照样只能猜。r31 的测试只断言"owner 非空且以 round-pid- 开头",
    那是**存在检查**,不是行为检查。
    """
    import os
    me = os.getpid()
    for s in (f"round-pid-{me}", f"pid-{me}", f"agent#{me}", f"a-b-pid-{me}"):
        assert Queue.owner_pid(s) == me, f"{s!r} 解析不出 pid"


def test_finish_accepts_the_bare_name_when_the_stored_owner_has_a_pid(q):
    """两侧都规范化 —— 否则 claim 写 "a#123"、finish 验 "a",必然被拒

    这不是理论问题:r33 改完 claim 之后,`finish(owner="a")` 如果不做
    规范化,会被**自己的 owner 校验**拒掉,于是每一轮收尾都失败。
    """
    it = q.claim(owner="a")
    assert it is not None
    assert q.finish(it.id, ok=True, owner="a", round_no=7) is True, (
        "claim 存了带 pid 的 owner,finish 传裸名就被拒 —— 两侧没规范化"
    )
