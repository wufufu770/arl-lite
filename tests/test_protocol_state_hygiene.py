"""r30: 落盘只提交增量,绝不写回整份旧快照

## 病

`round()` 一轮要切四次阶段,每次 `set_phase` 都把调用方手上那份
**轮次开始时的快照**整个 `save` 回去;结尾 `commit_round` 又 `save` 一次。
两个 agent 并发跑 round 时,后写的把先写的 round 号、history、累计
门禁数全盖掉 —— 那不是"重复记录",是纯粹的丢失。

r29 把 `StateStore.mutate()` 做出来了(探针证明 1200 次自增零丢失),
但 `protocol.py` 里 `store.load()` / `store.save()` 全是裸调用,
**修好的能力没有任何生产代码在用**。

## 为什么其中一条用 AST 而不是文本匹配

r26 的教训:文本子串匹配分不清「在用某模式」和「在解释该模式为何
废弃」——本文件自己的注释里就写着 `self.store.save(state)`。所以结构
检查一律走 AST。
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROTOCOL = REPO / "arl_lite" / "devloop" / "protocol.py"


# =====================================================================
# 结构检查:protocol 不许再把整份 state 写回去
# =====================================================================


def _calls_on_store(tree: ast.AST) -> set[str]:
    """protocol.py 里所有 `<x>.store.<方法>(...)` 的方法名"""
    out = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "store"
        ):
            out.add(node.func.attr)
    return out


def test_protocol_never_writes_the_whole_state_back():
    """`self.store.save(...)` 整个消失 —— 它就是 lost update 的形状

    保留了唯一合法的入口 `mutate`,以及只读的 `load`。
    """
    tree = ast.parse(PROTOCOL.read_text(encoding="utf-8"))
    calls = _calls_on_store(tree)
    assert "save" not in calls, (
        "protocol.py 又出现了 self.store.save(...) —— "
        "整份旧快照写回,两个并发 round 会互相覆盖"
    )
    assert "mutate" in calls, (
        "protocol.py 一个 store.mutate 都没有 —— r29 做的能力没人用,"
        "那这一轮等于什么都没修"
    )
    assert "load" in calls, "load 当然还得有(读是允许的)"


def test_ensure_next_step_does_not_write_back_the_snapshot():
    """单独盯住那处 —— 它不在 round() 主干里,容易漏"""
    tree = ast.parse(PROTOCOL.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "ensure_next_step":
            names = {
                c.func.attr for c in ast.walk(node)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            }
            assert "save" not in names, f"ensure_next_step 里还有 save:{names}"
            assert "mutate" in names, f"ensure_next_step 没有走 mutate:{names}"
            return
    raise AssertionError("ensure_next_step 不见了 —— 改名了还是被删了?")


# =====================================================================
# 行为检查:并发 commit_round 不丢
# =====================================================================


WORKER = textwrap.dedent("""
    import sys
    sys.path.insert(0, {repo!r})
    from arl_lite.devloop.state import RESULT_DONE, RoundRecord, StateStore
    store = StateStore({state!r})
    n, idx = int(sys.argv[1]), int(sys.argv[2])
    errs = []
    for i in range(n):
        try:
            s = store.load()                      # 旧快照(可能就是过期的)
            rec = RoundRecord(started_at=1.0, result=RESULT_DONE, item_id=f"w{{idx}}")
            store.commit_round(s, rec, done_delta=1, log_mark=len(s.gate_log))
        except Exception as e:
            errs.append(f"{{type(e).__name__}}: {{e}}")
    print(json.dumps(errs))
""")

N_PROC, N_ROUNDS = 6, 40


def test_concurrent_commits_lose_no_counters(tmp_path):
    """6 x 40 次并发 commit_round:round 与 items_done 一个都不能少

    旧实现下这是必挂的 —— 每个进程拿着轮次开始时的旧快照整个写回,
    最后只剩一个进程的计数。
    """
    state = tmp_path / "state.json"
    w = tmp_path / "w.py"
    w.write_text(WORKER.format(repo=str(REPO), state=str(state)), encoding="utf-8")
    procs = [
        subprocess.Popen([sys.executable, "-B", str(w), str(N_ROUNDS), str(i)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for i in range(N_PROC)
    ]
    errs = []
    for p in procs:
        out, _ = p.communicate(timeout=300)
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("["):
                errs.extend(json.loads(line))

    assert not errs, f"{len(errs)} 次提交抛异常:{errs[:3]}"
    d = json.loads(state.read_text(encoding="utf-8"))
    expect = N_PROC * N_ROUNDS
    assert d["round"] == expect, (
        f"round {d['round']} != {expect},丢 {expect - d['round']} 轮 —— "
        f"commit_round 又在写回旧快照了"
    )
    assert d["items_done"] == expect, (
        f"items_done {d['items_done']} != {expect},丢 {expect - d['items_done']} 条"
    )
    # history 有 MAX_HISTORY 上限,拿它当判据是错的(r29 已经栽过一次)
    assert len(d["history"]) <= 50


def test_log_mark_slices_only_this_rounds_entries(tmp_path):
    """`log_mark` 划错一刀,历史日志就会被反复重放

    不划这一刀:commit_round 会把**整份** gate_log(含几十轮之前的)
    再追加一遍,/tmp 无关但状态文件会一直涨。
    """
    from arl_lite.devloop.state import (PHASE_TEST, RESULT_DONE, RoundRecord,
                                        StateStore)

    store = StateStore(tmp_path / "state.json")
    s = store.load()
    s.gate_log = [{"round": 1, "gate": "old"}, {"round": 2, "gate": "older"}]
    store.save(s)                      # 先落盘,否则 mutate 读到的盘上还是空的
    mark = len(s.gate_log)
    s.gate_log.append({"round": 3, "gate": "mine"})

    store.commit_round(s, RoundRecord(started_at=1.0, result=RESULT_DONE),
                       done_delta=1, log_mark=mark)

    after = store.load().gate_log
    assert len(after) == 3, f"本轮只该加 1 条,实际 {len(after)} 条:{after}"
    assert after[-1]["gate"] == "mine"
    # 再提交一轮,旧的不能被重放
    s2 = store.load()
    mark2 = len(s2.gate_log)
    store.commit_round(s2, RoundRecord(started_at=1.0, result=RESULT_DONE),
                       done_delta=1, log_mark=mark2)
    assert len(store.load().gate_log) == 3, "空轮次不该往 gate_log 里塞历史"


def test_set_phase_updates_disk_and_the_caller_object(tmp_path):
    """切阶段要同时更新盘上和调用方那份,两边不能显示不一致"""
    from arl_lite.devloop.state import PHASE_TEST, LoopState, StateStore

    store = StateStore(tmp_path / "state.json")
    s = LoopState()
    store.save(s)

    store.set_phase(s, PHASE_TEST)

    assert store.load().phase == PHASE_TEST, "盘上没切"
    assert s.phase == PHASE_TEST, "调用方手里那份没同步"
