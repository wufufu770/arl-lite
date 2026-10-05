"""r37:`verify="true"` 是恒真命令 —— 信号可以用,真活不行

## 漏洞是怎么坐实的

不是读代码读出来的,是**跑出来的**。r36 收尾时往队列里塞了一条
`verify="true"` 的**真活**,跑完整 round:

    活干了没有?  没有 —— 一行代码没写
    round 结果 : DONE
    队列状态   : done
    note       : done r1 · source=operator

引擎把它标成了 done,note 还写着"由人工断言"。而那行断言是假的。

## 为什么这个洞特别难堵

三条防线全部失效,而且每一条都是绿的:

1. **r35 的收尾闸门**问的是 `verify_result(verify) == FAIL`。`true`
   跑出来是 `pass`,所以**放行**。闸门问对了问题,拿到的是假答案。
2. **r36 的 `test_backlog_verify_is_commandable.py`** 检查 verify 是不是
   可执行命令。`true` **确实**在白名单里、**确实**是合法 shell、
   **确实**语法正确。三项全过。
3. **`detail` 段会不会写成散文**那条检查 —— 这里根本没写 detail。

判据恒真是第 16 轮「恒真测试」那篇的**极端形式**:那一篇里恒真的测试
至少还会误导人,这里的恒真连测试都绿着 —— 它伪装成一道**通过了**的
验收。

## 修法:按 id 分流,不按 verify 内容分流

信号条目(`no-due-maintenance-review*`)的完成判据本来就是「人确认过」,
它**必须**能写一条恒真的 verify,否则永远完不成。所以拦的是
「**非信号**条目拿恒真 verify」,不是「恒真 verify」本身。

## 一版写错过的地方

第一版写成 `vr == VERIFY_PASS and is_constant_true(verify)`,结果 `:`
漏网了 —— 它的首词不在 `VERIFY_RUNNERS` 白名单里,`vr` 是 `unknown`,
恒真判断被整个短路掉。而 `:` 恰恰是最该拦的那种。

**恒真与否是 verify 内容自身的性质,和机器能不能评它无关。**
这条错误是靠下面 `test_vacuous_detection_does_not_depend_on_evaluation`
钉住的。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from arl_lite.devloop.gates import GateResult
from arl_lite.devloop.protocol import Loop
from arl_lite.devloop.queue import Item, Queue, is_signal_id

REPO = Path(__file__).resolve().parents[1]
GREEN = [GateResult("fake", True, "ok", 0, 0, True)]

# 四种恒真写法。判据若只认字面量 `true`,换个写法就绕过去了。
VACUOUS = [
    "true",
    "true && true",
    ":",
    "test -f /dev/null || true",
]


def _run_one(item_id: str, verify: str) -> tuple[str, str]:
    """真跑一轮,返回 (队列状态, round 结果)。**不重新实现被测逻辑**。"""
    d = Path(tempfile.mkdtemp())
    lp = Loop(REPO, state_dir=d)
    (d / "devloop").mkdir(exist_ok=True)
    (d / "devloop" / "backlog.md").write_text("# backlog\n", encoding="utf-8")
    lp.gates.run_all = lambda repo, only=None: list(GREEN)
    q = Queue(lp.dev_dir / "queue.json")
    q.add(Item(id=item_id, title="一条从没做过的活", priority=1, kind="change",
               verify=verify, detail="", tags=[]))
    out = lp.round()
    after = next(i for i in Queue(lp.dev_dir / "queue.json").load() if i.id == item_id)
    return after.status, out.record.result


# ── 闸门:真活拿恒真 verify 不许完成 ──

@pytest.mark.parametrize("verify", VACUOUS)
def test_real_work_with_constant_true_verify_is_refused(verify):
    """一条活都没干、验收却恒真的真活,不许被标 done

    跑的是完整 `Loop.round()`,不是直接调 `finish` —— 闸门装在收尾
    那一步,绕开 round 就等于没测。
    """
    status, result = _run_one("never-done", verify)

    assert status == "pending", (
        f"verify={verify!r} 恒真,一条没干的活却被标成 {status}"
    )
    assert result == "DONE_WITH_FAILURES", (
        f"拒绝了却没如实报失败:{result}"
    )


def test_refusal_says_which_verify_was_vacuous():
    """拒绝时要说清是**恒真**而不是"没过",两者要能区分

    只写"失败了"会让人以为去跑一遍就能过 —— 而恒真的东西跑一百遍
    还是恒真。分不清这两者,下一个就会白花时间。
    """
    d = Path(tempfile.mkdtemp())
    lp = Loop(REPO, state_dir=d)
    (d / "devloop").mkdir(exist_ok=True)
    (d / "devloop" / "backlog.md").write_text("# backlog\n", encoding="utf-8")
    lp.gates.run_all = lambda repo, only=None: list(GREEN)
    q = Queue(lp.dev_dir / "queue.json")
    q.add(Item(id="v", title="活", priority=1, kind="change",
               verify="true", detail="", tags=[]))
    out = lp.round()

    joined = " ".join(out.messages)
    assert "constant" in joined or "恒真" in joined, (
        f"拒绝时没说明这是恒真的 verify:{out.messages}"
    )
    assert any("vacuous_verify" in b for b in out.record.blocking_failures), (
        f"blocking_failures 里没记 vacuous_verify:"
        f"{out.record.blocking_failures}"
    )


# ── 反证:信号条目必须仍然能完成 ──

@pytest.mark.parametrize("item_id", [
    "no-due-maintenance-review",
    "no-due-maintenance-review-r7",
])
def test_signal_items_can_still_close_with_true(item_id):
    """信号的完成判据就是「人确认过」,恒真 verify 对它是**对的**

    没有这条,上面那些拒绝就变成了"信号永远关不掉" —— 队列里会永远
    挂着一条关不掉的信号,那比假账更难收拾。
    """
    status, result = _run_one(item_id, "true")

    assert status == "done", f"信号 {item_id} 关不掉了:{status}"
    assert result == "DONE"


def test_ordinary_work_with_a_real_verify_still_completes():
    """正常真活照常完成 —— 闸门不能变成"什么都拦"

    写"只拦恒真"很容易变成"拦一切"。这条是它的反证。
    """
    status, result = _run_one("real-work", "test -f pyproject.toml")

    assert status == "done", f"正常真活被误拦:{status}"
    assert result == "DONE"


# ── 判据本身:恒真检测不能依赖"机器能不能评它" ──

def test_vacuous_detection_does_not_depend_on_evaluation():
    """`:` 的首词不在白名单,`verify_result` 给 `unknown` —— 仍必须被认出恒真

    这是 r37 第一版写错的地方:判断写成
    `vr == VERIFY_PASS and is_constant_true(...)`,于是 `:` 的 `vr` 是
    `unknown`,整个恒真判断被短路,`:` 溜过去了。

    **恒真与否是 verify 内容自身的性质,和机器能不能评它无关。**
    """
    from arl_lite.devloop.queue import _is_vacuous_verify

    assert Queue.verify_result(":") == Queue.VERIFY_UNKNOWN, (
        "前提变了:`:` 现在能被判成 pass/unknown 之外的第三种,"
        "本测试的前提需要重写"
    )
    assert _is_vacuous_verify(":"), (
        "`:` 恒真,不能因为机器评不了它就放过"
    )


def test_non_vacuous_verifies_are_not_flagged():
    """真命令不能被误判成恒真 —— 判宽了同样出事

    `test -f /dev/null` 返回 1,它**不恒真**,不该被拦。
    """
    from arl_lite.devloop.queue import _is_vacuous_verify

    for v in ("test -f pyproject.toml",
              "python3 -m pytest tests/test_x.py",
              "python3 -c \"import sys; sys.exit(0)\""):
        assert not _is_vacuous_verify(v), f"{v!r} 被误判成恒真"


def test_is_signal_id_covers_derived_copies():
    """`-rN` 派生副本也是信号 —— 判据漏了派生就会把信号当真活拦掉

    真实队列里就有那种 id(`no-due-maintenance-review-r1..r6`)。
    """
    for iid in ("no-due-maintenance-review",
                "no-due-maintenance-review-r1",
                "no-due-maintenance-review-r6"):
        assert is_signal_id(iid), f"{iid} 没被认成信号"
    for iid in ("no-due-maintenance-reviewish", "review", "signal"):
        assert not is_signal_id(iid), f"{iid} 被误认成信号"
