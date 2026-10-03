"""r43:barren 计数器看不见 `done-item` 交的活

## r42 实测出来的

round 42 触发了 `RETREATED`(3 barren in a row)。可 r40、r41 各自都有
真实提交(`28ba6e3` / `d22e311`,门禁 6/0/0,验收命令跑通)—— 工作是
通过 `devloop done-item` 从**循环外**交上来的。

代价不是误报,是实打实的:round 42 在播种和门禁**之前**就 return 了,
0.0 秒,门禁没跑、队列没播种。退路不但判错,还把那一轮整个吃掉。

## 根因:判据写在信息还不存在的时候

`commit_round` 里原来是

    st.barren_rounds = st.barren_rounds + 1 if record.result == RESULT_NOOP else 0

可窗口是在两轮**之间**闭合的,`devloop done-item` 交的活正好落在那个
间隙里 —— 它落盘那一刻,这一轮的窗口还开着。于是这一行实际测的是
「`round()` 有没有领到条目」,而 state.json 里 `barren_rounds` 的
注释承诺的是「连续无净增量的轮数」。字段名承诺的语义和承载的
语义对不上(r35 那条原则)。

## 先验前提,再动判据(r38 撤过一次闸门,r40 的 `_flip` 又犯过一次)

用真实 44 轮历史跑了一遍:8 个 NOOP 轮里 **7 个窗口内是有提交的**,
只有 r44 真的没产出。也比过一个更便宜的候选信号 —— 队列里
`done_round == N` 的条目数 —— **8 轮里 3 轮和提交窗口对不上**,
因为 `done-item` 打的是当时 state 的轮次,比它真正所属的那一轮晚一拍
(r41 的活被记成了 r40)。所以只有提交窗口是准的。

## 判据与它的一个后果

上一轮是 NOOP **且**它的窗口里仓库没动过 → barren;否则清零。
判断不了(非 git 仓库 / git 超时)时**不判** —— 宁可多干活,
也不因为测不出就退路:那是把「不知道」当成「没干」(r22 的形状)。

**后果:退路比原来晚一轮触发。** 补判要等窗口闭合,而窗口要到下一轮
开始才闭。原来 3 轮空转后第 4 轮退路,现在第 5 轮。方向是安全的 ——
晚触发比早误触发好,但它确实改了不变式 3 的节奏,所以写在这里。

## 两条构造上的坑(都是本文件实测踩出来的)

- **仓库里别预先留 init 提交。** 窗口判到秒,而建仓库那一下往往和
  第一轮同秒,于是「空窗口」测的其实是「刚建好的窗口」。
  所以 `_make_loop` 只 `git init`,不提交。
- **空 dev 目录落不成 NOOP。** 第一轮播种、第二轮就领到活做完了,
  于是连着两轮都不是 NOOP —— 而要验的判据偏偏只在 NOOP 轮上生效。
  所以把真实的 `queue.json` 拷进去(里面没有 pending、只有 dropped),
  播种会如实报「无到期项,不重复提出」,轮次才真的空转。
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path

from arl_lite.devloop.protocol import Loop, RETREAT_THRESHOLD
from arl_lite.devloop.state import (
    LoopState, RoundRecord, StateStore, RESULT_DONE, RESULT_NOOP,
)

FAST = ["no_import_cycle"]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True, text=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
                        "PATH": "/usr/bin:/bin", "HOME": str(repo)})


def _make_loop(git: bool = True) -> tuple[Loop, Path]:
    """一个自足的 Loop:仓库和状态目录都在临时目录里

    `git=False` 造一个**不是** git 仓库的目录,验「判断不了」那条分支。
    队列直接拷真的 —— 见文件头「构造上的坑」第二条。
    """
    root = Path(tempfile.mkdtemp(prefix="barren-"))
    repo, dev = root / "repo", root / "dev"
    repo.mkdir(parents=True)
    dev.mkdir(parents=True)
    if git:
        _git(repo, "init", "-q")          # 只 init,不提交
    # 只放一条**已 drop 的信号条目**:r13 实测过,它在队列里时
    # `ensure_next_step` 不会重复提出,轮次于是真的空转成 NOOP。
    # 拷真队列不行 —— 真队列里随时有 pending 条目,轮次会领活干,
    # 根本落不成 NOOP,而要验的判据偏偏只在 NOOP 轮上生效。
    (dev / "queue.json").write_text(json.dumps({"version": 1, "items": [{
        "id": "no-due-maintenance-review-r6", "title": "当前无到期维护项",
        "detail": "", "priority": 2, "kind": "research", "verify": "true",
        "tags": ["maintenance", "review"], "status": "dropped", "attempts": 0,
        "created_round": 13, "done_round": None, "note": "", "owner": "",
        "claimed_at": 0.0}]}), encoding="utf-8")
    return Loop(repo, state_dir=dev), repo


def _commit(repo: Path, msg: str = "work") -> None:
    """在窗口里落一个提交 —— 模拟 agent 通过 done-item 交了活"""
    (repo / "work.txt").write_text(msg, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


def _push_round(lp: Loop, result: str = RESULT_NOOP) -> int:
    """往历史里直接塞一轮,不经过 `round()`

    `barren_delta=0` 是刻意的:计数只该由补判推动,这条路径不能自己加,
    否则下面那些测试验的就不是这轮改的东西了。
    """
    s = lp.status()
    rec = RoundRecord(started_at=time.time(), round=s.round + 1)
    rec.result = result
    lp.store.commit_round(s, rec)
    return rec.round


# ── 判据本身 ──

def test_noop_round_with_no_commits_is_barren():
    """上一轮空转且窗口里没动过仓库 → 算 barren"""
    lp, _ = _make_loop()
    _push_round(lp)
    delta, reset, _judged = lp.judge_prev_round_barren(lp.status())
    assert (delta, reset) == (1, False), (
        f"窗口空的空转轮应判 barren,实得 ({delta}, {reset})"
    )


def test_noop_round_with_a_commit_is_not_barren():
    """上一轮空转但窗口里有提交 → 不算 barren(r42 那个场景)"""
    lp, repo = _make_loop()
    _push_round(lp)
    _commit(repo, "agent 交的活")
    delta, reset, _judged = lp.judge_prev_round_barren(lp.status())
    assert (delta, reset) == (0, True), (
        f"窗口里有提交的轮不该算 barren,实得 ({delta}, {reset})"
    )


def test_non_noop_round_resets_the_streak():
    """上一轮不是 NOOP(它自己干了活)→ 连续计数清零,不是继续加"""
    lp, _ = _make_loop()
    _push_round(lp, RESULT_DONE)
    delta, reset, _judged = lp.judge_prev_round_barren(lp.status())
    assert (delta, reset) == (0, True)


def test_judge_is_idempotent_for_the_same_round():
    """同一轮不被重复判 —— 否则连着几轮下来计数会飞"""
    lp, _ = _make_loop()
    rnd = _push_round(lp)
    d1, r1, j1 = lp.judge_prev_round_barren(lp.status())
    s = lp.status()
    s.barren_judged_round = j1          # 模拟这一轮已经判过并落盘
    d2, r2, j2 = lp.judge_prev_round_barren(s)
    assert (d2, r2) == (0, False), f"第 {rnd} 轮被重复判了"
    assert (d1, r1) == (1, False), "第一次判的结果就不对"


def test_not_a_git_repo_is_not_barren():
    """非 git 仓库 = 判断不了,不是「没产出」

    测不出就退路,是把一次测量失败当成了结论。
    """
    lp, _ = _make_loop(git=False)
    _push_round(lp)
    delta, reset, _judged = lp.judge_prev_round_barren(lp.status())
    assert (delta, reset) == (0, False), (
        "非 git 仓库被判成了 barren —— 那是把「测不出」当成「没干」"
    )


# ── 接线:round 真的把补判结果带进计数了吗 ──

def test_round_wires_the_judge_into_the_counter():
    """造一轮空转,跑一轮,计数应变成 1

    接线断了的话这里会是 0 —— 补判算对了但没进状态。
    """
    lp, _ = _make_loop()
    _push_round(lp)
    out = lp.round(only_gates=FAST)
    assert not out.retreated
    assert lp.status().barren_rounds == 1, (
        f"补判没接进状态,barren={lp.status().barren_rounds}"
    )


def test_round_resets_the_counter_when_the_window_was_productive():
    """窗口里有提交时,round 跑完计数应归零"""
    lp, repo = _make_loop()
    _push_round(lp)
    _commit(repo, "agent 交的活")
    lp.round(only_gates=FAST)
    assert lp.status().barren_rounds == 0, (
        "有产出的窗口没有把连续 barren 计数清零"
    )


def test_rounds_landing_in_a_row_advance_the_streak_by_exactly_one():
    """连着空转,每轮只推进一格 —— 不是两格"""
    lp, _ = _make_loop()
    for i in range(RETREAT_THRESHOLD):
        out = lp.round(only_gates=FAST)
        assert not out.retreated, f"第 {i + 1} 轮不该退路"
        got = lp.status().barren_rounds
        assert got == i, (
            f"第 {i + 1} 轮结束后 barren 应为 {i},实际 {got} —— "
            f"要么漏判要么重复判"
        )


# ── 不变式还在:真的空转到底,退路必须触发 ──

def test_genuinely_barren_rounds_still_retreat():
    """连续无产出的轮次照旧退路 —— 这是不变式 3,不能被改没"""
    lp, _ = _make_loop()
    out = None
    for _ in range(RETREAT_THRESHOLD + 2):
        out = lp.round(only_gates=FAST)
        if out.retreated:
            break
    assert out is not None and out.retreated, "连续空转没有触发退路"
    assert "barren_threshold" in out.record.blocking_failures
    s = lp.status()
    assert s.retreats == 1, f"retreats 落盘不对:{s.retreats}"
    assert s.barren_rounds == 0, "退路后 barren 必须归零"


def test_not_a_git_repo_does_not_retreat():
    """测不出仓库状态就一直干,而不是退路"""
    lp, _ = _make_loop(git=False)
    for _ in range(RETREAT_THRESHOLD + 2):
        out = lp.round(only_gates=FAST)
        assert not out.retreated, "测不出就退路"
    assert lp.status().retreats == 0


# ── 根因回归:判据已经搬走了 ──

def test_commit_round_alone_no_longer_decides_barren():
    """`commit_round` 不得再自己猜 barren

    原来那一行是 `st.barren_rounds + 1 if result == NOOP else 0`,
    而窗口在落盘那一刻还没闭合。
    """
    root = Path(tempfile.mkdtemp(prefix="barren-root-"))
    store = StateStore(root / "state.json")
    s = LoopState()
    rec = RoundRecord(started_at=1.0)
    rec.result = RESULT_NOOP
    store.commit_round(s, rec)
    assert s.barren_rounds == 0, (
        "commit_round 又开始自己判 barren 了 —— 窗口那时还没闭合"
    )


def test_git_commits_between_is_three_valued():
    """窗口查询本身是三态的,不能塌成 bool

    `None` 和 `False` 混在一起,`test_not_a_git_repo_is_not_barren` 就护不住。
    """
    lp, repo = _make_loop()
    start = time.time()
    assert lp._git_commits_between(start) is False, "空窗口应是 False"
    _commit(repo, "x")
    assert lp._git_commits_between(start) is True, "有提交应是 True"
    plain, _ = _make_loop(git=False)
    assert plain._git_commits_between(start) is None, (
        "非 git 仓库应是 None,不是 False"
    )
