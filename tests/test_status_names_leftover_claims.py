"""r82:收尾时必须能看出「认领完忘了 done-item」

## 事故(r79,我自己的记账错误)

r77 认领了 `devloop-unmark-drop-rc-inconsistent`、干完了、**忘了跑
`done-item`**。那条就一直卡在 `in_progress`。r79 又领一条,于是变成 2 条,
`test_real_repo_queue_has_at_most_one_in_progress` 才报红。

讽刺的是那条不变量**两次红、含义完全不同**:一次是引擎的合法中间态
(它 docstring 里写了第 10 轮那次),一次是我自己的漏。

## 真正缺的不是检测,是「区分」

r82 实测:`devloop status` **早就会点名** in_progress 条目和年龄
(`claimed` 段,带 owner)。所以信号一直在,是我没往那儿看。

而且它写的是「N 条正在**别人**做」—— 对单人 CLI 工作流来说,我自己的
在途条目读起来跟上一轮的残留一模一样。**两件事读起来一样,就等于没报。**

于是这轮补的是那个区分,判据只用**可判定的事实**:

    条目的 claimed_at < 最后一轮的 finished_at
    ⟺ 有一轮是在它还开着的时候结束的 ⟺ 它是上一轮的残留

## 为什么判据不用 owner 的 pid(这条差点让我做出一个恒红的守卫)

r82 第一版想用「owner 进程已退出」当告警条件。实测:

    in_progress: done-item-must-not-be-forgettable
      owner       = 'agent#271111'
      owner_alive = False
      is_stale_claim = (True, 'owner agent#271111 的进程已退出')

**我这条正在合法推进的条目,当场被判成陈旧。** 因为 `devloop claim`
是从一次性的短命进程发的,命令跑完进程立刻退出,owner 里的 pid 永远是
死的 —— `is_stale_claim` 对每一条 CLI 认领都返回「该复位」。

拿它当门禁条件会 **100% 常红**。那不是守卫,是个摆设。所以:
判据只报上面那个跨轮事实,pid 只当**旁证**写进提示语里,
而且明确区分「探测不到」和「确认已退出」。

## 这轮自己也栽了一次

第一版 `status_text` 里写了 `Queue.owner_alive(...)`,而 `Queue`
在那个作用域里根本没导入 —— 于是**恰好在检测到残留时**抛 NameError,
把 `devloop status` 弄崩。也就是说:这个特性唯一要帮忙的那个命令,
被这个特性自己弄崩了,而且只在它真该说话的时候崩。
下面 `test_status_survives_a_leftover_claim` 就是钉这一条的。
"""
from __future__ import annotations

import dataclasses
import pathlib
import time

import pytest

from arl_lite.devloop import gates
from arl_lite.devloop.protocol import Loop
from arl_lite.devloop.queue import Item, Queue
from arl_lite.devloop.state import RoundRecord

LEFTOVER_HINT = "忘了 done-item"
OWNER_ALIVE_HINT = "owner 仍在运行"


class _AlwaysGreenGate:
    def __init__(self, name: str) -> None:
        self.name = name

    def run(self, *a, **kw):
        return gates.GateResult(self.name, ok=True, detail="", seconds=0.0)


def _loop(tmp_path: pathlib.Path, monkeypatch) -> Loop:
    dev = tmp_path / "devloop"
    dev.mkdir(parents=True, exist_ok=True)
    (dev / "backlog.md").write_text("# backlog\n", encoding="utf-8")
    gates.update_baseline(tmp_path, "loc_budget", {"total_loc": 1})
    for name in gates.all_gate_names():
        monkeypatch.setitem(gates._REGISTRY, name, _AlwaysGreenGate(name))
    return Loop(tmp_path)


def _with_history(loop: Loop, finished_at: float) -> None:
    """给状态塞一条已完成的轮次,这样「上一轮结束时刻」才是确定的"""
    def _apply(st):
        st.history = [RoundRecord(round=1, result="DONE", gates_passed=1,
                                  gates_failed=0, started_at=finished_at - 1.0,
                                  finished_at=finished_at)]
    loop.store.mutate(_apply)


def _claim(loop: Loop, claimed_at: float, owner: str, item_id: str = "x-1") -> Queue:
    q = Queue(loop.dev_dir / "queue.json")
    q.add(Item(id=item_id, title="标题", detail="细节", verify="判据"))
    q.claim(owner=owner, item_id=item_id)
    it = next(i for i in q.load() if i.id == item_id)
    q.save([dataclasses.replace(it, claimed_at=claimed_at)])
    return q


# ── 判据 ──

def test_a_leftover_claim_is_named_at_wrap_up(tmp_path, monkeypatch):
    """上一轮结束时还开着的条目,收尾必须点名并说清是遗留"""
    loop = _loop(tmp_path, monkeypatch)
    last_finished = time.time() - 3600
    _with_history(loop, last_finished)
    _claim(loop, last_finished - 600, owner="agent#999999")

    out = loop.status_text()
    assert "x-1" in out, f"收尾输出没点名那条残留:\n{out}"
    assert LEFTOVER_HINT in out, (
        f"收尾输出没说明这是「忘了 done-item」的形态:\n{out}")


def test_an_in_flight_claim_is_not_called_leftover(tmp_path, monkeypatch):
    """**反例控制组**:本轮刚领的活不能被误报成遗留

    这条是 r82 的关键控制组。用 pid 判的实现在这里必然翻车 ——
    它对每一条 CLI 认领都返回「owner 已退出」,于是这条判据会常红。
    """
    loop = _loop(tmp_path, monkeypatch)
    last_finished = time.time() - 3600
    _with_history(loop, last_finished)
    _claim(loop, time.time() - 60, owner="agent#999999")

    out = loop.status_text()
    assert "x-1" in out, f"在途条目连名字都没显示:\n{out}"
    assert LEFTOVER_HINT not in out, (
        f"把本轮在途的活误报成遗留了:\n{out}")


def test_a_leftover_held_by_a_live_owner_is_still_reported(tmp_path, monkeypatch):
    """owner 还活着但跨了轮 —— 也要报,只是措辞不同

    多 agent 场景下这是合法形态(agent 领了活,跨轮继续做),
    所以措辞是「确认下是不是忘了收尾」而不是「多半是忘了 done-item」。
    """
    import os
    loop = _loop(tmp_path, monkeypatch)
    last_finished = time.time() - 3600
    _with_history(loop, last_finished)
    _claim(loop, last_finished - 600, owner=f"agent#{os.getpid()}")

    out = loop.status_text()
    assert "x-1" in out, f"跨轮持有的条目没被点名:\n{out}"
    assert OWNER_ALIVE_HINT in out, (
        f"owner 还活着时措辞应与「已退出」区分开:\n{out}")
    assert LEFTOVER_HINT not in out, (
        f"owner 还活着就不该断言「多半是忘了 done-item」:\n{out}")


def test_status_survives_a_leftover_claim(tmp_path, monkeypatch):
    """收尾命令**不许**在检测到残留时崩掉

    r82 第一版在这里踩了:`status_text` 里写了 `Queue.owner_alive(...)`,
    而 `Queue` 在那个作用域里没导入,于是恰好在**该说话的时候**抛
    NameError,把 `devloop status` 弄崩 —— 特性唯一要帮忙的命令,
    被特性自己弄崩了。
    """
    loop = _loop(tmp_path, monkeypatch)
    last_finished = time.time() - 3600
    _with_history(loop, last_finished)
    _claim(loop, last_finished - 600, owner="agent#999999")

    try:
        out = loop.status_text()
    except NameError as e:  # pragma: no cover —— 回归钉的就是这个
        pytest.fail(f"status_text 在报残留时炸了:{e}")
    assert out.strip(), "status_text 返回了空串"


def test_no_history_means_no_leftover_verdict(tmp_path, monkeypatch):
    """没有历史轮次可比时,不许硬判「上一轮残留」

    否则新仓库第一轮就会凭空报一条遗留。
    """
    loop = _loop(tmp_path, monkeypatch)
    _claim(loop, time.time() - 100000, owner="agent#999999")

    out = loop.status_text()
    assert "x-1" in out
    assert LEFTOVER_HINT not in out, (
        f"没有可比的历史轮次却判了遗留:\n{out}")


def test_the_real_repo_queue_leaves_nothing_unnamed(tmp_path, monkeypatch):
    """钉住真实仓库那条:有 in_progress 就必须被点名,不能只报个计数"""
    repo = pathlib.Path(__file__).resolve().parents[1]
    real = Queue(repo / "devloop" / "queue.json")
    in_progress = [i.id for i in real.load() if i.status == "in_progress"]
    out = Loop(repo).status_text()
    for iid in in_progress:
        assert iid in out, (
            f"真实队列里有 in_progress 的 {iid},但收尾输出没点名它 —— "
            f"这正是 r77 漏 done-item 时那条检查没做到的事:\n{out}")


def test_wrap_up_output_is_not_always_the_same_shape(tmp_path, monkeypatch):
    """收尾输出必须**随队列变化**,否则前面那些判据全是恒真的

    一个恒定不变的输出能让上面每一条都通过,却什么也守不住。
    """
    loop = _loop(tmp_path, monkeypatch)
    empty = loop.status_text()
    last_finished = time.time() - 3600
    _with_history(loop, last_finished)
    _claim(loop, last_finished - 600, owner="agent#999999")
    claimed = loop.status_text()
    assert claimed != empty, (
        "认领之后收尾输出一个字都没变 —— "
        "上面那些判据能过只是因为输出恒定,不是因为我改了它")
