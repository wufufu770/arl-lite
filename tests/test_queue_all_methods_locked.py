"""r32: queue.py 里不允许再有裸 load→save 的方法

## 病

`claim` / `release` / `finish` / `recover_stale` 四个是带 `file_lock` 的
正确入口 —— 不变式 #8 原话就是「它们是唯一正确入口」。而
`add` / `seed_if_empty` / `repair_duplicates` / `drop` / `unmark`
五个是裸的 `load` → 改 → `save(整份)`,不互斥。

`drop` 最要紧。r22 刚确立「**人的显式指令被静默覆盖,比队列空掉更坏**」,
而人的显式指令正是走 `devloop drop` —— 它却在一个不互斥的临界区里。
两个 agent 并发时后写的整份覆盖先写的,人写的丢弃理由可能直接消失。

## AST 判据必须区分接收者

第一版审计脚本只收集 `attr` 名,于是报出 `_disambiguate -> ['add']`,
我差点当成「`_disambiguate` 调 `Queue.add`,加锁会死锁」。实际那是
`taken.add(iid)` —— **`set` 的方法**。所以这里认的是
`self.load()` / `self.save()`,不是任意 `.load()` / `.save()`。
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
QUEUE = REPO / "arl_lite" / "devloop" / "queue.py"


# =====================================================================
# 结构检查
# =====================================================================


def _takes_lock(node: ast.AST) -> bool:
    for w in ast.walk(node):
        if isinstance(w, ast.With):
            for ci in w.items:
                ce = ci.context_expr
                if (isinstance(ce, ast.Call) and isinstance(ce.func, ast.Attribute)
                        and ce.func.attr == "_locked"):
                    return True
    return False


def _self_calls(node: ast.AST) -> set[str]:
    """`self.xxx(...)` 的方法名(只认 self,不把 set.add 算进来)"""
    return {
        c.func.attr for c in ast.walk(node)
        if isinstance(c, ast.Call)
        and isinstance(c.func, ast.Attribute)
        and isinstance(c.func.value, ast.Name)
        and c.func.value.id == "self"
    }


def _queue_methods():
    tree = ast.parse(QUEUE.read_text(encoding="utf-8"))
    cls = next(x for x in tree.body
               if isinstance(x, ast.ClassDef) and x.name == "Queue")
    return {n.name: n for n in cls.body
            if isinstance(n, ast.FunctionDef) and not n.name.startswith("__")}


def test_no_queue_method_writes_the_whole_file_without_a_lock():
    """任何「load 了 + save 了」的方法都必须持锁

    这是把不变式 #8 直接写成代码:整份写回只能在临界区里发生。
    """
    bare = []
    for name, fn in _queue_methods().items():
        calls = _self_calls(fn)
        if {"load", "save"} <= calls and not _takes_lock(fn):
            bare.append(name)
    assert not bare, (
        f"这些方法是无锁的裸 load→save:{bare} —— "
        f"并发时后写的整份覆盖先写的"
    )


def test_the_five_previously_bare_methods_are_actually_locked():
    """点名验一遍,别让"审计判据本身写错"蒙过去

    上一条是"没有裸的了",这条是"该有的确实有"。少一个就是回归。
    """
    methods = _queue_methods()
    for name in ("add", "seed_if_empty", "repair_duplicates", "drop", "unmark"):
        assert name in methods, f"{name} 不见了 —— 改名还是被删了?"
        assert _takes_lock(methods[name]), f"{name} 又变回无锁的了"
        assert "load" in _self_calls(methods[name])
        assert "save" in _self_calls(methods[name])


def test_the_audit_does_not_mistake_set_add_for_queue_add():
    """守住判据本身:上一个审计脚本就栽在这里

    它只收 `attr` 名,把 `_disambiguate` 里的 `taken.add(iid)`(set 的方法)
    报成了「`_disambiguate` 调 `Queue.add`」,我差点据此判定"加锁会死锁"
    而放弃修 drop。
    """
    methods = _queue_methods()
    dis = methods["_disambiguate"]
    raw = {c.func.attr for c in ast.walk(dis)
           if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}
    strict = _self_calls(dis)
    assert "add" in raw, "用例本身失效了:_disambiguate 里不该再有 .add(...)"
    assert "add" not in strict, (
        "_disambiguate 真的开始调 self.add 了 —— 那加锁会死锁,判据要改"
    )


# =====================================================================
# 行为检查:并发时人的指令不许丢
# =====================================================================

WORKER = textwrap.dedent("""
    import sys, json
    sys.path.insert(0, {repo!r})
    from pathlib import Path
    from arl_lite.devloop.queue import Item, Queue
    q = Queue(Path({src!r}))
    op, arg = sys.argv[1], sys.argv[2]
    if op == "drop":
        ok, msg = q.drop(arg, f"人写的理由-{{arg}}")
        print(json.dumps({{"ok": ok, "msg": msg}}))
    else:
        q.add(Item(id=arg, title=f"活 {{arg}}", priority=2, kind="change",
                   verify="test -f 不存在", detail="", tags=[]))
        print(json.dumps({{"ok": True}}))
""")


def _run_op(tmp_path, src, op, args):
    w = tmp_path / f"w_{op}.py"
    w.write_text(WORKER.format(repo=str(REPO), src=str(src)), encoding="utf-8")
    procs = [subprocess.Popen([sys.executable, "-B", str(w), op, str(a)],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True) for a in args]
    outs = []
    for p in procs:
        out, _ = p.communicate(timeout=300)
        line = [ln for ln in out.splitlines() if ln.startswith("{")]
        assert line, f"worker 没输出 JSON:{out[:400]}"
        outs.append(json.loads(line[-1]))
    return outs


def test_concurrent_drops_all_survive(tmp_path):
    """8 个进程并发 drop 8 条 —— 每一条都要真的停在 dropped,且理由还在

    无锁时它们各自 load → 改自己那条 → 整份 save,最后只有**一个**进程
    的修改留得下来,另外 7 条人的显式指令人间蒸发。
    """
    from arl_lite.devloop.queue import Item, Queue

    src = tmp_path / "queue.json"
    q = Queue(src)
    for i in range(8):
        q.add(Item(id=f"f{i}", title=f"活 {i}", priority=2, kind="change",
                   verify="test -f 不存在", detail="", tags=[]))

    outs = _run_op(tmp_path, src, "drop", [f"f{i}" for i in range(8)])
    assert all(o["ok"] for o in outs), f"有 drop 失败了:{outs}"

    st = {i.id: i for i in Queue(src).load()}
    not_dropped = [k for k, v in st.items() if v.status != "dropped"]
    assert not not_dropped, (
        f"并发下这些 drop 消失了:{not_dropped} —— 人的显式指令被并发覆盖"
    )
    for k, v in st.items():
        assert f"人写的理由-{k}" in v.note, (
            f"{k} 的丢弃理由没了:{v.note!r} —— 三个月后没人记得当初为什么删"
        )


def test_concurrent_adds_all_survive(tmp_path):
    """并发 add 8 条 —— 8 条都得在(r23 修过 lost update,但那时仍无锁)"""
    from arl_lite.devloop.queue import Queue

    src = tmp_path / "queue.json"
    Queue(src)   # 建一个空队列文件
    _run_op(tmp_path, src, "add", [f"a{i}" for i in range(8)])

    got = {i.id for i in Queue(src).load()}
    missing = {f"a{i}" for i in range(8)} - got
    assert not missing, f"并发 add 丢了 {len(missing)} 条:{sorted(missing)}"
