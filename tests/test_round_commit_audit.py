"""r39:记账审计 —— 上一轮报完成,仓库里真的有过提交吗?

## r38 的教训直接决定了这一轮的设计

r38 我在 round **之内**比对 `head_before/head_after`,结果这道闸门拒掉了
**29/34 个完全合法的轮次**。实测数据(r38):

    报完成的轮次里 —— 提交发生在 round 之内:          4
                      提交发生在 round 结束之后 1 小时内: 29

工作流本来就是「先跑 round 验门禁,再提交」。**轮内窗口是错的。**
r38 的闸门已完整撤回(见 docs 7.29)。

r39 用**正确的窗口**:「上一轮 started_at → 现在」。

## 它只报告,不拦

上一轮的账不该由这一轮来拒 —— 拒了就是把上一轮的问题混进本轮的
记录,而本轮自己的工作是真的。真正要拦的是**本轮**的记账,那些闸门
(r35 验收闸门、r37 恒真闸门)都在收尾处。

`test_audit_does_not_penalise_the_current_round` 钉住这一点。

## 为什么"回看"比"轮内"更诚实

轮内只能看到"这一轮跑的过程中有没有提交",而真实的因果是:
干完活 → 跑 round 验门禁 → **然后**提交。轮内看不到提交,不是没干活,
是提交还没发生。回看到下一轮,证据才到齐。
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from arl_lite.devloop.gates import GateResult
from arl_lite.devloop.protocol import Loop
from arl_lite.devloop.queue import Item, Queue

REPO = Path(__file__).resolve().parents[1]
GREEN = [GateResult("fake", True, "ok", 0, 0, True)]
PASSING = "test -f pyproject.toml"


def _loop(tmp_path, *, gates=..., seed=True) -> Loop:
    lp = Loop(REPO, state_dir=tmp_path)
    (lp.dev_dir / "backlog.md").write_text("# backlog\n", encoding="utf-8")
    lp.gates.run_all = lambda r, only=None: list(gates if gates is not ... else GREEN)
    if seed:
        Queue(lp.dev_dir / "queue.json").add(
            Item(id="work", title="活", priority=1, kind="change",
                 verify=PASSING, detail="", tags=[]))
    return lp


def _real_commit_exists(since: float) -> bool:
    """本仓库里,从 `since` 起真的有过提交吗 —— 不 mock git,用真数据

    审计逻辑就是"问 git 有没有提交",测试它当然要真的问 git。
    """
    r = subprocess.run(
        ["git", "log", "--since", str(int(since)), "--pretty=%H"],
        cwd=REPO, capture_output=True, text=True, timeout=30)
    return r.returncode == 0 and bool(r.stdout.strip())


# ── 核心:上一轮报完成却没提交,要报出来 ──

def test_audit_flags_a_completed_round_with_no_commit_behind_it(tmp_path):
    """跑两轮,中间不提交 → 审计必须报出来

    这就是 r34/r35 反复出现的那类假账的自动化探针。
    """
    lp = _loop(tmp_path)
    lp.round()                                  # 第 1 轮:报完成
    out2 = lp.round()                           # 第 2 轮:回看第 1 轮

    audits = [m for m in out2.messages if m.startswith("[audit]")]
    assert audits, f"第 1 轮报完成却无提交,审计没报:{out2.messages}"
    assert "第 1 轮" in audits[0], f"没说是哪一轮:{audits[0]}"


def test_audit_stays_quiet_when_a_real_commit_exists(tmp_path):
    """窗口内有真实提交时,不许报 —— 报了就成噪音,而噪音的审计没人看

    用真数据:把 `since` 设到**上一次真实提交之前**,git 一定有提交。
    """
    lp = _loop(tmp_path)
    # 造一个"报完成"的上一轮记录,但窗口取到很久以前 -> 那儿一定有提交
    lp.round()
    # 窗口起点取到**本仓库最早那次提交之前**。第一版写的是拍脑袋的
    # 1_000_000(1970 年),而本仓库最早提交是 1789346314(2026 年),
    # 那个时间点之前压根没有提交 —— 前提不成立,断言自己先炸了。
    # 这里不写死数字,现场问 git 要。
    earliest = int(subprocess.run(
        ["git", "log", "--reverse", "--pretty=%ct"],
        cwd=REPO, capture_output=True, text=True, timeout=30
    ).stdout.split()[0])
    early = float(earliest - 3600)
    assert _real_commit_exists(early), "前提不成立:这个时间点之前没有提交"
    # 把 history 的 started_at 改到很早,那时 git log 一定有东西
    st = lp.store.load()
    st.history[-1].started_at = early
    lp.store.save(st)
    assert lp.audit_prev_round_commits() is None, (
        "窗口内有真实提交却报了问题 —— 审计成了噪音"
    )


def test_audit_does_not_penalise_the_current_round(tmp_path):
    """审计**只报告**,不改本轮的 result —— 上一轮的账不该由这一轮来拒

    没有这条,一个"发现上一轮有问题就降级本轮"的实现也能让上面两条
    变绿 —— 而那是把上一轮的问题混进本轮的记录,比漏报还糟。
    """
    lp = _loop(tmp_path)
    lp.round()
    out2 = lp.round()

    assert any(m.startswith("[audit]") for m in out2.messages)
    assert out2.record.result == "DONE", (
        f"上一轮有问题就把本轮也降级了:{out2.record.result}"
    )
    assert not out2.record.blocking_failures, (
        f"上一轮的问题不该进本轮的 blocking_failures:"
        f"{out2.record.blocking_failures}"
    )


# ── 不该报的情况 ──

def test_noop_round_is_not_audited(tmp_path):
    """NOOP 轮不该被审计 —— 它本来就没干活,查它没有意义

    r36 实测过 NOOP 路径:轮初没活就如实空转,那是**正确**行为。
    """
    lp = _loop(tmp_path, seed=False)
    # 空队列会播出**复查信号**(`no-due-maintenance-review`,r36 起的行为),
    # 所以第一轮不是空转轮 —— 前提一写错,这条测试就变成在测别的东西。
    # 于是连跑几轮,只在"上一轮确实是 NOOP"的那一次看审计信号。
    prev, audited, saw_noop = None, [], False
    for _ in range(3):
        out = lp.round()
        if prev == "NOOP":
            saw_noop = True
            audited = [m for m in out.messages if m.startswith("[audit]")]
            break
        prev = out.record.result
    assert saw_noop, "三轮里没出现 NOOP,这条测试没测到它该测的东西"
    assert not audited, f"空转轮被审计了:{audited}"


def test_first_round_has_nothing_to_audit(tmp_path):
    """没有上一轮时无事可查 —— 不能因为"查不到"就当成"有问题"

    这是三态里的第三态:**判断不了**。
    """
    lp = _loop(tmp_path, seed=False)
    assert lp.audit_prev_round_commits() is None

    out = lp.round()
    assert not [m for m in out.messages if m.startswith("[audit]")]


def test_non_git_repo_audit_stays_quiet(tmp_path):
    """项目根不是 git 仓库时,审计应保持沉默而不是报"没有提交"

    拿不到仓库就说"没有提交",是把**判断不了**当成了**否定结论** ——
    r35 的 verify 三态、第 38 条 HEAD 三态,同一个原则的第三次应用。
    """
    nogit = tmp_path / "not-a-repo"
    nogit.mkdir()
    lp = Loop(nogit, state_dir=tmp_path)
    (lp.dev_dir).mkdir(parents=True, exist_ok=True)
    (lp.dev_dir / "backlog.md").write_text("# backlog\n", encoding="utf-8")
    lp.gates.run_all = lambda r, only=None: list(GREEN)
    Queue(lp.dev_dir / "queue.json").add(
        Item(id="w", title="活", priority=1, kind="change",
             verify=PASSING, detail="", tags=[]))
    lp.round()
    out2 = lp.round()

    assert not [m for m in out2.messages if m.startswith("[audit]")], (
        f"非 git 仓库却报了审计信号:{out2.messages}"
    )


# ── 判据本身 ──

def test_audit_window_is_since_the_previous_round_started():
    """窗口的起点必须是**上一轮 started_at**,不是上一轮 finished_at

    这个区别决定了 audit 会不会漏报:如果从 `finished_at` 起算,
    那么"上一轮跑完之后才提交"的正常情况,提交落在窗口**之外** ——
    正好把 29/34 的正常情况判成异常。
    """
    import ast
    src = (REPO / "arl_lite" / "devloop" / "protocol.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == "audit_prev_round_commits")
    seg = ast.get_source_segment(src, fn)
    assert 'prev.get("started_at")' in seg, (
        "窗口起点用的不是上一轮的 started_at"
    )
    assert 'prev.get("finished_at")' not in seg, (
        "窗口起点用了 finished_at —— 会把「跑完之后才提交」判成异常"
    )
