"""r29: StateStore 的并发安全 —— 不变式 #8 说的就是这件事

## 病

不变式 #8 写着「读-改-写整段进临界区:load/save 各自原子,组合起来不原子」。
而 `StateStore` **完全没有锁**。`queue` 有 `file_lock` 且有多进程测试,
`state` 这一侧一处都没有 —— 测试只覆盖了队列。

旧实现两个毛病,同一个病根:**tmp 文件名是固定的**(`state.json.tmp`):

    A 写 state.json.tmp(写了一半)
    B 写 state.json.tmp(覆盖)
    A os.replace(tmp, state.json)   <- 把 B 的半截内容搬成正式文件
    B os.replace(tmp, state.json)   <- tmp 已经不在了

## 实测(修之前)

    8 进程 x 150 次 = 1200 次
    741 次 FileNotFoundError: state.json.tmp -> state.json
    10+ 次 state file corrupt(两种形态:空文件 / Extra data 内容交错)
    1200 次写最后只剩 14 条历史

而 `test_devloop_multiagent.py` 全绿 —— 它测的是**队列**的锁。

## 修后

    0 异常 / 0 损坏 / round 精确 1200 / 无残留 tmp
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

WORKER = textwrap.dedent("""
    import sys
    sys.path.insert(0, {repo!r})
    from arl_lite.devloop.state import RoundRecord, StateStore
    store = StateStore({state!r})
    mode, idx, n = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    errs = []
    for i in range(n):
        try:
            if mode == "save":            # 裸 load -> 改 -> save(lost update 演示)
                s = store.load()
                s.round += 1
                s.history.append(RoundRecord(round=s.round, item_id=f"w{{idx}}"))
                store.save(s)
            else:                          # mutate:整段进临界区
                def bump(s):
                    s.round += 1
                    s.history.append(RoundRecord(round=s.round, item_id=f"w{{idx}}"))
                store.mutate(bump)
        except Exception as e:
            errs.append(f"{{type(e).__name__}}: {{e}}")
    print(json.dumps(errs))
""")


def _spawn(tmp: Path, mode: str, n_proc: int, n_rounds: int) -> tuple[list[str], str]:
    """起 n_proc 个真进程并发写同一个 state.json"""
    state = tmp / "state.json"
    worker_py = tmp / "worker.py"
    worker_py.write_text(WORKER.format(repo=str(REPO), state=str(state)), encoding="utf-8")
    procs = [
        subprocess.Popen([sys.executable, "-B", str(worker_py), mode, str(i), str(n_rounds)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for i in range(n_proc)
    ]
    outs = []
    for p in procs:
        out, _ = p.communicate(timeout=300)
        outs.append(out)
    return outs, state.read_text(encoding="utf-8")


def _errs(outs: list[str]) -> list[str]:
    all_errs = []
    for o in outs:
        for line in o.splitlines():
            line = line.strip()
            if line.startswith("["):
                try:
                    all_errs.extend(json.loads(line))
                except json.JSONDecodeError:
                    all_errs.append(f"unparseable worker output: {line[:120]}")
    return all_errs


N_PROC, N_ROUNDS = 6, 50


def test_two_writes_never_share_a_tmp_name(tmp_path, monkeypatch):
    """tmp 名必须每次唯一,否则就是那个「A 搬走 B 的 tmp」的竞态

    ## 为什么这条是确定性的,而多进程那条不是

    第一版这里只有多进程压测(6 进程 x 50 次),**变异把 tmp 名改回固定
    名之后它照样全绿** —— 窗口太窄,竞态没撞上。这跟恒真测试是同一种
    病,只是更隐蔽:它不是恒真,是**概率性通过**,而"这次没撞上"会被
    读成"没问题"。实测的探针是 8 进程 x 150 次且循环里几乎没有 IO 间隙,
    1200 次里出了 741 次异常 —— 压力不够就等于没测。

    所以改成直接盯性质:spy 住 os.replace,收下每次用的 tmp 名,
    断言它们互不相同。不靠运气。
    """
    from arl_lite.devloop import state as state_mod
    from arl_lite.devloop.state import LoopState, StateStore

    names: list[str] = []
    real_replace = os.replace

    def spy(src, dst, **kw):
        names.append(Path(src).name)
        return real_replace(src, dst, **kw)

    # state_mod.os 就是全局 os 模块,所以这里 patch 的是 os.replace;
    # spy 里转调 real_replace 避免无限递归,monkeypatch 会还原
    monkeypatch.setattr(state_mod.os, "replace", spy)
    store = StateStore(tmp_path / "state.json")
    for i in range(5):
        store.save(LoopState(round=i))

    assert len(names) == 5, f"spy 漏了 {{}}: {names}"
    assert len(set(names)) == 5, f"tmp 名被重复使用,这正是竞态的来源:{names}"


def test_concurrent_saves_never_produce_a_corrupt_state_file(tmp_path):
    """并发 save 不许把状态写成半截

    压力比第一版加大(8 x 150,与实测探针同量级),但**它不是判据** ——
    真正的判据是上面那条确定性的 tmp 名检查。这条防的是回归:
    万一有人把 os.replace 换回直接写,这里能撞上。
    """
    outs, raw = _spawn(tmp_path, "save", 8, 150)
    errs = _errs(outs)
    assert not errs, f"{len(errs)} 次写入抛异常(修之前是 741 次):{errs[:3]}"
    json.loads(raw)


def test_mutate_loses_nothing_under_concurrency(tmp_path):
    """整段临界区:N_PROC x N_ROUNDS 次自增,一次都不能被整段覆盖掉

    对照组:上面那条用裸 load→save,历史照样会丢(round 也丢)。
    这条证明 `mutate` 真的把读-改-写收进同一把锁。
    """
    outs, raw = _spawn(tmp_path, "mutate", N_PROC, N_ROUNDS)
    errs = _errs(outs)
    assert not errs, f"{len(errs)} 次写入抛异常:{errs[:3]}"

    d = json.loads(raw)
    expect = N_PROC * N_ROUNDS
    assert d["round"] == expect, (
        f"round 计数 {d['round']} != {expect},丢了 {expect - d['round']} 次 —— "
        f"读-改-写没有整段进临界区(不变式 #8)"
    )
    # history 有 MAX_HISTORY 上限,那是设计,不是丢失 —— 拿它当判据是错的
    assert len(d["history"]) <= 50


def test_mutate_actually_takes_the_lock(tmp_path):
    """单元判据:临界区期间别人拿不到这把锁

    多进程那条能证明"没丢",但证明不了"是锁起的作用" —— 万一只是
    恰好跑得太快。这条直接问锁。
    """
    from arl_lite.devloop.lock import file_lock
    from arl_lite.devloop.state import LoopState, StateStore

    state = tmp_path / "state.json"
    store = StateStore(state)
    store.save(LoopState())

    seen = []

    def peek(_s):
        # 站在 mutate 的临界区里,试着再拿一次锁 —— 拿不到才是对的
        try:
            with file_lock(state, timeout=0.2):
                seen.append("ACQUIRED")
        except Exception:
            seen.append("blocked")

    store.mutate(peek)
    assert seen == ["blocked"], (
        f"mutate 临界区里锁还能被拿到:{seen} —— 读-改-写整段根本没收进临界区"
    )

