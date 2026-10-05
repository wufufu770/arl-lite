"""devloop 的退出码必须可判定:业务层失败一律 1,用法错误才 2。

背景:实测同一条「条目不存在」,`drop` 返 1 而 `unmark` 返 2 —— 而且两者打的
是**完全相同的消息** `no such item: 'ghost-xyz'`。消息都一样、码却不同,脚本
连按消息匹配都做不到。

同一命令内部也分叉:`devloop gate` 对「门禁红」返 1、对「门禁不存在」返 2。
两种失败同一个命令给出两种码,没法用统一条件判失败。

约定(写进实现,这里钉死):
    rc=1  业务层失败:门禁红、门禁不存在、条目不存在/不可领、队列真空…
    rc=2  argparse 用法错误:缺必填参数、未知选项。argparse 自己 exit 2,
          不由 arl_lite/devloop/cli.py 决定
两条必须可判定地分开,否则脚本/cron 无法用统一条件判失败。
实测无依赖:`protocol.py:492` 只判 `returncode != 0`,不区分 1 与 2。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
QUEUE = REPO / "devloop" / "queue.json"

# (子命令 argv, 该失败条件的描述)
BUSINESS_FAILURES = [
    (["claim", "--item-id", "r77-ghost"], "claim 一个不存在的条目"),
    (["done-item", "r77-ghost"], "交活一条不存在的条目"),
    (["drop", "r77-ghost", "--reason", "x"], "丢弃一条不存在的条目"),
    (["unmark", "r77-ghost", "--reason", "x"], "解标一条不存在的条目"),
    (["release", "r77-ghost"], "释放一条没被认领的条目"),
    (["gate", "r77-ghost-gate"], "跑一个不存在的门禁"),
    (["accept", "r77-ghost-gate", "--reason", "x"], "为一个不存在的门禁提额"),
]

# (子命令 argv, 描述) —— 这些是 argparse 的用法错误,应当 rc=2
USAGE_ERRORS = [
    (["unmark"], "unmark 缺 item_id"),
    (["drop", "r77-x"], "drop 缺 --reason"),
    (["add"], "add 缺位置参数"),
    (["gate"], "gate 缺 gate_name"),
    (["accept", "loc_budget"], "accept 缺 --reason"),
    (["release"], "release 缺 item_id"),
    (["done-item"], "done-item 缺 item_id"),
    (["accept", "nosuch", "--reason"], "accept 的 --reason 缺值"),
]

RC_BUSINESS_FAILURE = 1
RC_USAGE_ERROR = 2


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["HOME"] = tempfile.mkdtemp(prefix="r77")
    return subprocess.run(
        [sys.executable, "-B", "-m", "arl_lite.cli", "devloop", *argv],
        env=env, cwd=tempfile.gettempdir(),
        capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("argv,label", BUSINESS_FAILURES, ids=[c[1] for c in BUSINESS_FAILURES])
def test_business_failure_always_exits_one(argv, label):
    """业务层失败一律 rc=1,不许和 argparse 的 2 混用。"""
    p = _run(argv)
    assert p.returncode == RC_BUSINESS_FAILURE, (
        f"{label}: 期望 rc={RC_BUSINESS_FAILURE}(业务层失败),实际 rc={p.returncode}。"
        f"rc=2 专留给 argparse 用法错误,两者混用脚本就没法统一判失败了。"
        f"stdout={p.stdout[-200:]!r} stderr={p.stderr[-200:]!r}"
    )


@pytest.mark.parametrize("argv,label", USAGE_ERRORS, ids=[c[1] for c in USAGE_ERRORS])
def test_usage_error_always_exits_two(argv, label):
    """argparse 用法错误仍是 rc=2 —— 修业务层不能把这一类也改了。"""
    p = _run(argv)
    assert p.returncode == RC_USAGE_ERROR, (
        f"{label}: 期望 rc={RC_USAGE_ERROR}(argparse 用法错误),实际 rc={p.returncode}"
    )


def test_same_condition_same_message_same_code():
    """`drop` 与 `unmark` 对同一条不存在的条目:消息一致,退出码也必须一致。

    这是本条判据最尖锐的一处 —— 原来两者消息一字不差,码却一个 1 一个 2。
    """
    drop = _run(["drop", "r77-ghost", "--reason", "x"])
    unmark = _run(["unmark", "r77-ghost", "--reason", "x"])
    assert drop.returncode == unmark.returncode, (
        f"同一条不存在的条目,drop rc={drop.returncode} 而 unmark rc={unmark.returncode} —— "
        f"连按消息匹配都做不到"
    )


def test_gate_command_does_not_use_two_codes_for_two_failures():
    """同一命令的两种失败(门禁红 / 门禁不存在)必须给同一个码。"""
    missing = _run(["gate", "r77-ghost-gate"])
    assert missing.returncode == RC_BUSINESS_FAILURE, (
        f"门禁不存在应返 {RC_BUSINESS_FAILURE},实际 {missing.returncode};"
        f"它与「门禁红」的 rc 必须一致,否则同一个命令没法统一判失败"
    )


def test_queue_file_untouched_by_this_judgment():
    """判据自己不能污染真实队列。"""
    before = QUEUE.read_bytes() if QUEUE.exists() else None
    _run(["drop", "r77-ghost", "--reason", "x"])
    after = QUEUE.read_bytes() if QUEUE.exists() else None
    assert before == after, "判据跑完之后 devloop/queue.json 变了 —— 复原失败"


def test_the_code_table_matches_the_pinned_convention():
    """判据自身的期望值表必须与实现里的约定一致,不能各写一份。

    钉死条数:新增一条业务层失败、或删掉一条,都是有人有意的决定。
    """
    real = json.loads(QUEUE.read_text(encoding="utf-8"))
    assert isinstance(real.get("items"), list), "queue.json 结构变了,判据要跟着看"
    assert len(BUSINESS_FAILURES) == 7 and len(USAGE_ERRORS) == 8, (
        f"期望表条数变了:业务层 {len(BUSINESS_FAILURES)} 条(应 7)、"
        f"用法错误 {len(USAGE_ERRORS)} 条(应 8)。"
        f"新增/删除都要先弄清是新增了失败路径还是改错了退出码"
    )
