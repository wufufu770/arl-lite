"""r31: round() 必须走队列的带锁入口,不许自己 load→改→save

## 病

r30 把**状态**那一侧的落盘全改成"只提交增量"了,队列这一侧是同一个病没修。

`queue.py` 的 `claim` / `release` / `finish` / `recover_stale` 才是带
`file_lock` 的正确入口 —— 不变式 #8 原话就是「queue.Queue.claim/release/
finish/recover_stale 是唯一正确入口」。而 `round()` 一处都没用:

    481  items = q.load()
    500  q.mark_in_progress(item)      ← 原地改
    503  q.save(items)                 ← 整份写回,无锁
    ...
    579  q.save(items)                 ← 同上

两个并发 round 会同时挑中同一条,然后互相把对方的 status 改回去。

## 顺带修掉的一处更隐蔽的

`phase_plan` 里:

```python
state.current_note = f"seeded {added} new item(s)"
q.save(items)          # ← 改的是 state,保存的却是原封不动的队列
```

改的是 `state`、存的是队列,而且队列一个字都没变。一次**无锁、无意义、
但确实写盘**的 save —— 像是早期把 `self.store.save` 写错对象留下的。

## 顺带修好的一个功能

round 认领以前**不记 owner**,所以 `recover_stale_in_progress` 只能报
「认领者无法探测」,判断一条 in_progress 是死是活只能靠"认领多久了"猜。
改用 `q.claim(owner=...)` 之后 owner 真的记上了。
"""
from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROTOCOL = REPO / "arl_lite" / "devloop" / "protocol.py"


# =====================================================================
# 结构检查:用 AST,不碰文本(r26/r30 两次踩过同一个坑)
# =====================================================================


def _queue_saves_in(node: ast.AST) -> list[str]:
    """`<x>.save(...)` 且 `<x>` 名字像队列变量的调用"""
    found = []
    for n in ast.walk(node):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "save"):
            continue
        base = n.func.value
        if isinstance(base, ast.Name) and base.id in ("q", "queue", "q1", "q2"):
            found.append(base.id)
        elif isinstance(base, ast.Attribute) and base.attr == "queue_mod":
            found.append("self.queue_mod")
    return found


def _func(tree: ast.AST, name: str):
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    return None


def test_round_never_saves_the_queue_itself():
    """round() 里不许出现裸 q.save(...) —— 那就是无锁整份写回"""
    tree = ast.parse(PROTOCOL.read_text(encoding="utf-8"))
    fn = _func(tree, "round")
    assert fn is not None, "round() 不见了"
    bad = _queue_saves_in(fn)
    assert not bad, (
        f"round() 里还有裸队列 save:{bad} —— 两个并发 round 会互相覆盖 status"
    )


def test_phase_plan_no_longer_writes_the_queue():
    """那处「改 state 却存队列」的 save 必须消失,而且不能换个姿势回来"""
    tree = ast.parse(PROTOCOL.read_text(encoding="utf-8"))
    fn = _func(tree, "phase_plan")
    assert fn is not None, "phase_plan() 不见了"
    assert not _queue_saves_in(fn), "phase_plan 又开始写队列了"

    # 而且它现在该把 current_note 推给 store,不是队列
    names = {
        c.func.attr for c in ast.walk(fn)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
    }
    assert "mutate" in names, f"phase_plan 没有把 note 落盘:{names}"


def test_round_goes_through_the_locked_entry_points():
    """反向确认:认领与收尾都走带锁入口,不只是"没有裸 save"而已

    只断言"没有 q.save"是不够的 —— 全部改成 q.load() 然后啥也不存,
    也能过。得确认它真的在用 claim / finish / release。
    """
    tree = ast.parse(PROTOCOL.read_text(encoding="utf-8"))
    fn = _func(tree, "round")
    assert fn is not None
    called = {
        c.func.attr for c in ast.walk(fn)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
    }
    for need in ("claim", "finish", "release"):
        assert need in called, f"round() 没有用 q.{need}():{sorted(called)}"


# =====================================================================
# 行为检查
# =====================================================================


def _seeded_loop(tmp_path, item_id="real-work", priority=2):
    from arl_lite.devloop.gates import GateResult
    from arl_lite.devloop.protocol import Loop
    from arl_lite.devloop.queue import Item, Queue

    lp = Loop(REPO, state_dir=tmp_path)      # dev_dir 就是 tmp_path 本身
    (lp.dev_dir / "backlog.md").write_text("# backlog\n", encoding="utf-8")
    Queue(lp.dev_dir / "queue.json").add(
        Item(id=item_id, title="一件真活", priority=priority, kind="change",
             verify="test -f 不存在的文件", detail="", tags=[]))
    lp.gates.run_all = lambda repo, only=None: [
        GateResult("fake", True, "ok", 0, 0, True)]
    return lp


def test_round_records_an_owner_when_claiming(tmp_path):
    """round 认领必须记 owner —— 以前记不了,recover_stale 只能靠猜

    r30 那次 round 的日志原文:
        recover_stale_in_progress: 1 条 in_progress 有人认领,保留:
        [('...', '认领者无法探测,但认领才 480s,当它是活的')]
    """
    from arl_lite.devloop.queue import Queue

    lp = _seeded_loop(tmp_path)
    lp.round(item_id="real-work")

    items = Queue(lp.dev_dir / "queue.json").load()
    it = next(i for i in items if i.id == "real-work")
    assert it.status == "done"
    assert it.owner == "", f"done 之后 owner 该被清掉,实际 {it.owner!r}"


def test_claimed_item_carries_owner_while_in_progress(tmp_path):
    """round 认领时必须带 owner —— recover_stale 靠它判断死活

    ## 为什么要在门禁被调用的那一瞬间读

    轮次结束时 owner 已经被 `finish` / `release` 清空了,那时候读永远
    是空。第一版这条直接调 `Queue.claim` 来验,可是那验的是**队列自己**,
    round 就算忘了传 owner 它照样过。

    门禁被调用恰好卡在 claim 之后、finish 之前 —— 那是唯一一个
    「owner 应该还在」的瞬间。
    """
    from arl_lite.devloop.gates import GateResult
    from arl_lite.devloop.queue import Queue

    lp = _seeded_loop(tmp_path)
    seen: dict = {}

    def run_all(repo, only=None):
        it = next(i for i in Queue(lp.dev_dir / "queue.json").load()
                  if i.id == "real-work")
        seen["status"] = it.status
        seen["owner"] = it.owner
        seen["claimed_at"] = it.claimed_at
        return [GateResult("fake", True, "ok", 0, 0, True)]

    lp.gates.run_all = run_all
    lp.round(item_id="real-work")

    assert seen["status"] == "in_progress", (
        f"门禁跑的时候条目该是 in_progress,实际 {seen.get('status')!r}"
    )
    # 只断言"非空"不够:Queue.claim 的默认 owner 是 f"pid-{os.getpid()}",
    # 忘了传也照样非空,变异测试实测放过(Q2 存活)。所以要比具体格式。
    assert seen["owner"].startswith("round-pid-"), (
        f"owner 应该是 round 自己传的标识,实际 {seen['owner']!r} —— "
        f"不传的话 Queue.claim 会用 pid 兜底,而 recover_stale 需要"
        f"认得出来'这是哪个 agent 干的'"
    )
    assert seen["claimed_at"] > 0, "claimed_at 没记,同样判断不了死活"

    # r31 漏掉的那条(r33 补上):格式对了不等于认得出来。
    # _OWNER_PID 是 (?:#|^pid-)(\d+)$ —— `^pid-` 锚在字符串开头,所以
    # "round-pid-12345" **解析不出 pid**,recover_stale 照样只能按时间猜。
    # r31 的测试只断言 owner 非空且以 round-pid- 开头,没验这条,于是
    # "owner 记上了"被当成了"认领者探测得出来"。存在检查冒充行为检查。
    from arl_lite.devloop.queue import Queue as _Q
    assert _Q.owner_pid(seen["owner"]) is not None, (
        f"owner 记成了 {seen['owner']!r},但 owner_pid() 解析不出 pid —— "
        f"recover_stale 依然只能按认领时长猜,这跟 r31 修之前没区别"
    )


# =====================================================================
# 并发:两个 round 不能领到同一条
# =====================================================================

WORKER = textwrap.dedent("""
    import sys, json
    sys.path.insert(0, {repo!r})
    from arl_lite.devloop.gates import GateResult
    from arl_lite.devloop.protocol import Loop
    from pathlib import Path
    lp = Loop(Path({repo!r}), state_dir=Path({dev!r}))
    lp.gates.run_all = lambda repo, only=None: [GateResult("f", True, "ok", 0, 0, True)]
    try:
        out = lp.round()
        print(json.dumps({{"ok": True, "item": out.record.item_id}}))
    except Exception as e:
        print(json.dumps({{"ok": False, "err": f"{{type(e).__name__}}: {{e}}"}}))
""")


def test_two_concurrent_rounds_claim_different_items(tmp_path):
    """N 个并发 round 领到的条目必须互不相同,最后每条都 done

    裸 load→mark_in_progress→save 时,它们会同时挑中同一条(都是优先级
    最高的 pending),然后一个把它标 done、另一个的旧快照把它写回 pending。
    """
    from arl_lite.devloop.queue import Item, Queue

    dev = tmp_path                       # state_dir 就是 dev_dir 本身
    (dev / "backlog.md").write_text("# backlog\n", encoding="utf-8")
    q = Queue(dev / "queue.json")
    for i in range(4):
        q.add(Item(id=f"w{i}", title=f"活 {i}", priority=2, kind="change",
                   verify="test -f 不存在", detail="", tags=[]))

    w = tmp_path / "w.py"
    w.write_text(WORKER.format(repo=str(REPO), dev=str(tmp_path)), encoding="utf-8")
    procs = [subprocess.Popen([sys.executable, "-B", str(w)],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True) for _ in range(4)]
    results = []
    for p in procs:
        out, _ = p.communicate(timeout=300)
        line = [ln for ln in out.splitlines() if ln.startswith("{")]
        assert line, f"worker 没有输出 JSON:{out[:300]}"
        import json
        results.append(json.loads(line[-1]))

    assert all(r["ok"] for r in results), f"有 round 抛异常:{results}"
    claimed = [r["item"] for r in results]
    assert len(set(claimed)) == len(claimed), (
        f"两个 round 领到了同一条:{claimed} —— 裸 load/save 覆盖了对方的 status"
    )
    st = {i.id: i.status for i in Queue(dev / "queue.json").load()}
    assert all(v == "done" for k, v in st.items() if k.startswith("w")), st
