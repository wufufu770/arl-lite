"""`devloop claim` 必须区分「这一条不可领」与「队列真空」。

背景:这轮是用 devloop 自己的 `claim --item-id` 发现的。实测:

    $ arl-lite devloop claim --item-id ghost-item-xyz      # 队列里明明有 4 条 pending
    [i] nothing to claim (queue has no pending item)      rc=1

**这句话是假的。** 根因在 `_cmd_claim`:指定 item_id 时若找不到,和队列真空一样
都落到 `it is None`,于是共用同一句文案。

为什么这不是小问题:本循环协议每轮都跑 `claim --item-id`。条目 id 打错、或那条
已被别的 agent 领走时,工具报「队列没有待办」—— 照这句话走会误判整个队列已空,
转头去补种子,把真正的待办撂在一边。**一条假消息直接腐蚀循环的状态判断。**

所以判据守的是:消息**不得**在说谎。队列非空时不能说「queue has no pending item」。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
QUEUE = REPO / "devloop" / "queue.json"

PENDING_ID = "r76-test-pending-item"
DONE_ID = "r76-test-done-item"
DROPPED_ID = "r76-test-dropped-item"


def _claim(*args: str, queue: dict | None = None):
    """在指定的队列状态下跑 `devloop claim`,返回 (rc, stdout, stderr)。"""
    backup = QUEUE.read_bytes() if QUEUE.exists() else None
    if queue is not None:
        QUEUE.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["HOME"] = tempfile.mkdtemp(prefix="r76")
        proc = subprocess.run(
            [sys.executable, "-B", "-m", "arl_lite.cli", "devloop", "claim", *args],
            env=env, cwd=tempfile.gettempdir(),
            capture_output=True, text=True, timeout=60)
        return proc.returncode, proc.stdout, proc.stderr
    finally:
        if backup is None:
            QUEUE.unlink(missing_ok=True)
        else:
            QUEUE.write_bytes(backup)


# 真实 queue.json 的结构是 {"version": ..., "items": [...]},item 字段有
# created_round / tags / claimed_at / note / done_round 等。首版我照着印象写了
# 一份 {"schema": "v1", "round": ..., "history": []},schema 是猜的 —— 猜错的
# schema 会让失败原因变得不可读,等于自己给自己制造假象。这里从真实 item 复制
# 一份再改字段,保证结构永远跟得上真实文件。
_TEMPLATE_ITEM = json.loads(QUEUE.read_text(encoding="utf-8"))["items"][0]


def _item(item_id: str, status: str, owner: str = "") -> dict:
    it = dict(_TEMPLATE_ITEM)
    it.update({"id": item_id, "title": f"t-{item_id}", "detail": "", "verify": "",
               "status": status, "owner": owner, "attempts": 0,
               "claimed_at": owner or None, "done_round": None, "note": ""})
    return it


def _queue(items: list[dict]) -> dict:
    real = json.loads(QUEUE.read_text(encoding="utf-8"))
    real["items"] = items
    return real


PENDING_QUEUE = _queue([_item(PENDING_ID, "pending")])
MIXED_QUEUE = _queue([
    _item(PENDING_ID, "pending"),
    _item(DONE_ID, "done"),
    _item(DROPPED_ID, "dropped"),
])
EMPTY_QUEUE = _queue([])


def test_claim_names_the_item_that_cannot_be_claimed():
    """指定了一个不存在的 id,报错必须点名它 —— 不能只说「没领到」。"""
    rc, out, err = _claim("--item-id", "r76-ghost", queue=MIXED_QUEUE)
    assert rc == 1, f"期望 rc=1,实际 {rc}"
    assert "r76-ghost" in err, (
        f"报错没有点名用户指定的那一条,用户无从知道是哪个 id 出了问题:{err!r}"
    )


def test_claim_does_not_claim_the_queue_is_empty_when_it_isnt():
    """核心:队列里明明有 pending,不许报「queue has no pending item」。"""
    rc, out, err = _claim("--item-id", "r76-ghost", queue=MIXED_QUEUE)
    assert "no pending item" not in out, (
        f"队列里有 {PENDING_ID} 这条 pending,却报「queue has no pending item」—— "
        f"这是一条假消息,会让人以为整个队列空了。stdout={out!r}"
    )
    assert "no pending item" not in err, (
        f"队列里有 pending 却报「no pending item」:stderr={err!r}"
    )


def test_claim_says_what_is_still_claimable():
    """报错必须告诉调用方队列里还剩什么,而不是只说他要的那条没了。"""
    rc, out, err = _claim("--item-id", "r76-ghost", queue=MIXED_QUEUE)
    assert PENDING_ID in err, (
        f"报错没有列出仍然可领的条目,调用方无法接着往下做:{err!r}"
    )


def test_claiming_a_finished_item_is_not_reported_as_an_empty_queue():
    """对已完成的条目 claim 也一样 —— 它不可领,但队列状态与之无关。"""
    rc, out, err = _claim("--item-id", DONE_ID, queue=MIXED_QUEUE)
    assert rc == 1, f"已完成条目不可领,期望 rc=1,实际 {rc}"
    assert DONE_ID in err, f"报错没点名是哪一条:{err!r}"
    assert "no pending item" not in out and "no pending item" not in err, (
        f"对已完成条目 claim 却报队列空了:{out!r} / {err!r}"
    )


def test_genuinely_empty_queue_keeps_its_original_message():
    """真空队列仍应报「没东西可领」—— 这次它说的是真话,别把它也一起改掉。"""
    rc, out, err = _claim(queue=EMPTY_QUEUE)
    assert rc == 1, f"期望 rc=1,实际 {rc}"
    assert "nothing to claim" in out, (
        f"真空队列应当仍然说「没东西可领」:{out!r} / {err!r}"
    )
    assert "cannot claim" not in err, (
        f"没指定 item_id 时不该报「cannot claim <某个 id>」:{err!r}"
    )


def test_claiming_a_real_pending_item_still_works():
    """正向:修的只是错误路径,正常认领不能坏。"""
    rc, out, err = _claim("--item-id", PENDING_ID, queue=MIXED_QUEUE)
    assert rc == 0, f"认领一条真实 pending 应当成功,实际 rc={rc}:{out!r}{err!r}"
    assert PENDING_ID in out, f"成功信息里没有回显认领到的 id:{out!r}"


def test_queue_file_is_restored_after_each_case():
    """判据自己不能污染真实队列 —— 否则下一轮的循环状态就被测试搞坏了。"""
    before = QUEUE.read_bytes() if QUEUE.exists() else None
    _claim("--item-id", "r76-ghost", queue=MIXED_QUEUE)
    after = QUEUE.read_bytes() if QUEUE.exists() else None
    assert before == after, "判据跑完之后 devloop/queue.json 变了 —— 复原失败"
