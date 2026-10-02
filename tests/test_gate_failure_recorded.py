"""r27:协议不变式 #2 的**行为**测试 —— 门禁红了,状态必须降级,不许当成通过

## 为什么要这个文件

审计发现:`RESULT_DONE_WITH_FAILURES` 在全仓库被 import 过,却被**零个测试
断言**过。唯一的候选是 `test_gate_failure_is_recorded_not_swallowed` ——
名字叫「门禁失败必须写进状态,不能被'处理掉'」,但它实际:

- 跑的是 `no_import_cycle`(一道**绿**门禁)
- 断言 `RESULT_DONE`,也就是「通过」这一支
- 那句把 baseline 改成 `failed: 0` 想逼出失败的 monkeypatch,因为只跑了别的
  门禁,`test_baseline` 根本没被调用,**从未生效**

也就是说,它验的恰好是这条不变式的**反面**,却顶着这条不变式的名字。

本文件补上,方法是伪造门禁结果(`run_all` 是唯一的门禁入口,`Loop` 从这里
拿事实),然后断言状态真降级了。刻意不跑真门禁:test_baseline 要 2 分钟,
而这里要验的是「收到红门禁之后协议怎么反应」,不是门禁本身跑不跑得过。

## 判别力(变异测试验过)

把 `protocol.py:502` 的 `RESULT_DONE_WITH_FAILURES` 改成 `RESULT_DONE`,
本文件 3 条测试立刻失败;把 `target.status = "pending"` 改成 `"done"`,
`test_red_gate_does_not_mark_item_done` 失败。没有一条是恒真的。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arl_lite.devloop.gates import GateResult
from arl_lite.devloop.protocol import Loop
from arl_lite.devloop.state import RESULT_DONE, RESULT_DONE_WITH_FAILURES


def _find_repo_root() -> Path:
    here = Path(__file__).resolve()
    for cand in here.parents:
        if (cand / "pyproject.toml").is_file() and (cand / "arl_lite").is_dir():
            return cand
    raise RuntimeError("找不到项目根")


REPO = _find_repo_root()


def _gate(name, passed, blocking=True):
    return GateResult(
        name=name,
        passed=passed,
        detail="faked by r27 test",
        measured=0 if passed else 1,
        baseline=0,
        blocking=blocking,
    )


class TestRedGateIsDowngradedNotSwallowed(unittest.TestCase):
    """不变式 2:门禁失败必须降级,而不是被记成通过"""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.lp = Loop(REPO, state_dir=self.d)

    def _round_with(self, *results):
        """用假门禁结果跑一整轮。finally 恢复,不留污染。"""
        orig = self.lp.gates.run_all
        self.lp.gates.run_all = lambda repo, only=None: list(results)
        try:
            return self.lp.round(only_gates=["faked"])
        finally:
            self.lp.gates.run_all = orig

    # --- 核心:红门禁必须记成失败,不是 DONE ---

    def test_red_gate_records_done_with_failures(self):
        out = self._round_with(_gate("fake_red", False))
        self.assertEqual(
            out.record.result, RESULT_DONE_WITH_FAILURES,
            "门禁红了却记成 DONE —— 这正是这条不变式要防的事",
        )

    def test_red_gate_does_not_mark_item_done(self):
        """失败的待办不能被吃掉:必须退回 pending,下一轮还能做"""
        out = self._round_with(_gate("fake_red", False))
        self.assertEqual(
            self.lp.status().items_done, 0,
            "门禁没过却把待办算成做完了 —— 队列会假装前进",
        )
        # 按 id 精确定位,不能只断言"集合里有个 pending":
        # 不变式 #4 会在这轮末尾播种新待办,那种写法恒真
        # (变异 target.status="pending"->"done" 杀不掉,实测存活)。
        item_id = out.record.item_id
        self.assertIsNotNone(item_id, "本轮没处理任何待办,断言没有意义")
        from arl_lite.devloop.queue import Queue
        q = Queue(self.lp.dev_dir / "queue.json")
        target = next(i for i in q.load() if i.id == item_id)
        self.assertEqual(
            target.status, "pending",
            f"门禁没过,{item_id} 却是 {target.status},失败被吞了",
        )

    def test_red_gate_names_the_failing_gate(self):
        """失败要能定位到是哪道门,否则'如实记录'没有意义"""
        out = self._round_with(
            _gate("fake_green", True),
            _gate("fake_red", False),
        )
        self.assertIn("fake_red", out.record.blocking_failures)
        self.assertNotIn("fake_green", out.record.blocking_failures)

    # --- 对照组:证明上面几条不是恒真 ---

    def test_green_gate_records_done(self):
        """同样的流程,门禁绿时必须是 DONE。

        没有这条,上面三条可能因为'round() 根本不写状态'而假通过。
        """
        out = self._round_with(_gate("fake_green", True))
        self.assertEqual(out.record.result, RESULT_DONE)
        self.assertEqual(self.lp.status().items_done, 1)

    def test_non_blocking_failure_does_not_downgrade_the_round(self):
        """非阻塞门禁(warned)红了不算这轮失败 —— 否则门禁会全成阻塞"""
        out = self._round_with(_gate("fake_warn", False, blocking=False))
        self.assertEqual(
            out.record.result, RESULT_DONE,
            "非阻塞门禁的告警被当成了阻塞失败",
        )

    def test_result_survives_a_reload(self):
        """降级结果必须真的落盘,不是只活在内存里"""
        self._round_with(_gate("fake_red", False))
        reloaded = self.lp.store.load()
        self.assertEqual(reloaded.history[-1].result, RESULT_DONE_WITH_FAILURES)


if __name__ == "__main__":
    unittest.main()
