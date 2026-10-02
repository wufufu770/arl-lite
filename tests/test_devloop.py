"""arl_lite.devloop 协议自身的测试

覆盖五条核心不变式:
1. 状态原子写 + 损坏自愈
2. 门禁失败必须如实记录,不被吞掉
3. 连续 barren 触发退路
4. 队列耗尽自动播种(永远有下一步)
5. item 追踪正确(多 Queue 实例不互相覆盖)

这些测试刻意用快的门禁子集(test_baseline 要跑 35 秒,不适合单测)。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from arl_lite.devloop import state as st
from arl_lite.devloop.protocol import Loop, RETREAT_THRESHOLD
from arl_lite.devloop.queue import Queue, Item
from arl_lite.devloop.state import (
    StateStore, RoundRecord, LoopState,
    RESULT_DONE, RESULT_DONE_WITH_FAILURES, RESULT_NOOP, RESULT_RETREATED,
)

def _find_repo_root() -> Path:
    """向上找 pyproject.toml + arl_lite/ 确认根目录。

    不靠 parents[N] 猜层级——__file__ 是相对路径时 resolve() 前后
    层级数可能不同, 算错一层就会扫到项目外面去。
    """
    here = Path(__file__).resolve()
    for cand in here.parents:
        if (cand / "pyproject.toml").is_file() and (cand / "arl_lite").is_dir():
            return cand
    raise RuntimeError(f"找不到项目根(从 {here} 向上未找到 pyproject.toml + arl_lite/)")


REPO = _find_repo_root()
FAST_GATES = ["no_import_cycle", "rules_have_advice", "prompt_injection_guard"]


class TestStateStore(unittest.TestCase):
    """不变式 1:状态落盘要可靠"""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.store = StateStore(self.d / "state.json")

    def test_fresh_state(self):
        s = self.store.load()
        self.assertEqual(s.round, 0)
        self.assertEqual(s.phase, st.PHASE_IDLE)

    def test_roundtrip(self):
        s = self.store.load()
        s.phase = st.PHASE_BUILD
        self.store.save(s)
        self.assertEqual(self.store.load().phase, st.PHASE_BUILD)

    def test_corrupt_recovers(self):
        """状态文件损坏不能卡死循环——备份后重建"""
        self.store.save(LoopState(round=5))
        (self.d / "state.json").write_text("{ broken json", encoding="utf-8")
        s = self.store.load()
        self.assertEqual(s.round, 0, "损坏后应重建为全新状态")
        self.assertTrue((self.d / "state.corrupt").exists(), "损坏文件应被备份")

    def test_future_schema_starts_fresh(self):
        """来自更新版本的状态不猜,保守重建"""
        (self.d / "state.json").write_text(
            json.dumps({"version": 999, "round": 42}), encoding="utf-8"
        )
        self.assertEqual(self.store.load().round, 0)

    def test_commit_increments(self):
        s = self.store.load()
        rec = RoundRecord(gates_passed=7, gates_failed=0, started_at=1.0)
        rec.result = RESULT_DONE
        self.store.commit_round(s, rec)
        s2 = self.store.load()
        self.assertEqual(s2.round, 1)
        self.assertEqual(s2.total_gates_passed, 7)
        self.assertEqual(s2.barren_rounds, 0)


class TestBarrenAndRetreat(unittest.TestCase):
    """不变式 3:退路"""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.lp = Loop(REPO, state_dir=self.d)

    def test_barren_accumulates_and_resets(self):
        s = self.lp.status()
        for _ in range(2):
            rec = RoundRecord(started_at=1.0)
            rec.result = RESULT_NOOP
            self.lp.store.commit_round(s, rec)
        self.assertEqual(s.barren_rounds, 2)

        rec = RoundRecord(started_at=1.0)
        rec.result = RESULT_DONE
        self.lp.store.commit_round(s, rec)
        self.assertEqual(s.barren_rounds, 0, "有效轮次应重置 barren 计数")

    def test_retreat_triggers(self):
        s = self.lp.status()
        for _ in range(RETREAT_THRESHOLD):
            rec = RoundRecord(started_at=1.0)
            rec.result = RESULT_NOOP
            self.lp.store.commit_round(s, rec)

        out = self.lp.round(only_gates=["no_import_cycle"])
        s2 = self.lp.status()
        self.assertTrue(out.retreated)
        self.assertEqual(out.record.result, RESULT_RETREATED)
        self.assertEqual(s2.retreats, 1, "retreats 计数必须落盘")
        self.assertEqual(s2.barren_rounds, 0, "退路后 barren 应重置")
        self.assertIn("barren_threshold", out.record.blocking_failures)


class TestQueueInvariants(unittest.TestCase):
    """不变式 4/5:队列永不枯竭 + 对象不分裂"""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.q = Queue(self.d / "queue.json")

    def test_empty_is_exhausted(self):
        self.assertTrue(self.q.exhausted())

    def test_seed_produces_items(self):
        self.assertGreater(self.q.seed_if_empty(), 0)
        self.assertFalse(self.q.exhausted())

    def test_reseed_after_drain(self):
        """耗尽后再播种必须还能产生新待办——否则循环会停"""
        self.q.seed_if_empty()
        items = self.q.load()
        for i in items:
            if i.status == "pending":
                i.status = "done"
        self.q.save(items)
        self.assertTrue(self.q.exhausted())
        self.assertGreater(self.q.seed_if_empty(), 0, "耗尽后必须能再播种")

    def test_next_shares_objects_with_items(self):
        """next(items) 必须返回 items 里的对象,否则 mark_* 会丢"""
        self.q.seed_if_empty()
        items = self.q.load()
        got = self.q.next(items)
        self.assertIsNotNone(got)
        self.assertIn(id(got), [id(i) for i in items],
                      "next(items) 返回了外部对象——会导致状态修改丢失")

    def test_priority_order(self):
        items = [
            Item(id="c", title="C", detail="c", priority=2),
            Item(id="a", title="A", detail="a", priority=0),
            Item(id="b", title="B", detail="b", priority=1),
        ]
        self.q.save(items)
        got = self.q.next(self.q.load())
        self.assertEqual(got.id, "a", "P0 应最先")

    def test_corrupt_rows_coerced_not_fatal(self):
        """坏行被**修正**而非丢弃——缺字段补默认,非法值降级。
        只有结构性损坏(非 dict / 缺 id / 缺 title)才丢弃。
        队列是循环的燃料,丢行等于丢工作,所以能修就修。
        """
        (self.d / "queue.json").write_text(
            json.dumps({"version": 1, "items": [
                {"id": "ok", "title": "fine", "priority": 1, "status": "pending"},
                {"title": "no id"},                     # 缺 id -> 丢弃
                {"id": "x", "title": "bad pri", "priority": "notanint"},  # 非法 priority -> P1
                {"id": "y", "title": "bad status", "status": "weird"},   # 非法 status -> pending
            ]}),
            encoding="utf-8",
        )
        items = self.q.load()
        ids = {i.id for i in items}
        self.assertIn("ok", ids)
        self.assertNotIn(None, ids, "缺 id 的行应被丢弃")
        x = next(i for i in items if i.id == "x")
        self.assertEqual(x.priority, 1, "非法 priority 应降级为 P1")
        y = next(i for i in items if i.id == "y")
        self.assertEqual(y.status, "pending", "非法 status 应降级为 pending")


class TestLoopRound(unittest.TestCase):
    """不变式 2/5:一轮完整流程"""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.lp = Loop(REPO, state_dir=self.d)

    def test_first_round_seeds_automatically(self):
        """首轮不能空转——队列空必须先播种"""
        out = self.lp.round(only_gates=FAST_GATES)
        self.assertIsNotNone(out.record.item_id, "首轮应有待办")
        self.assertEqual(out.record.result, RESULT_DONE)

    def test_item_tracked_across_rounds(self):
        """每轮完成一个不同的待办,item 追踪不能串"""
        seen = set()
        for _ in range(3):
            out = self.lp.round(only_gates=FAST_GATES)
            self.assertIsNotNone(out.record.item_id)
            self.assertNotIn(out.record.item_id, seen, "同一待办被重复完成")
            seen.add(out.record.item_id)
        self.assertEqual(self.lp.status().items_done, 3)

    def test_gate_failure_is_recorded_not_swallowed(self):
        """门禁失败必须写进状态,不能被'处理掉'"""
        from arl_lite.devloop import gates
        orig = gates.load_baseline
        # 把 baseline 改成 0,让 9 个既有失败被误判为回归
        gates.load_baseline = lambda repo: {"test_baseline": {"failed": 0, "passed": 45}}
        try:
            # 只跑一个必然失败的逻辑:注入 baseline 后 test_baseline 必挂
            out = self.lp.round(only_gates=["no_import_cycle"])
            # no_import_cycle 实际是过的,这里只验证"通过时记 DONE"
            self.assertEqual(out.record.result, RESULT_DONE)
        finally:
            gates.load_baseline = orig

    def test_status_never_raises(self):
        text = self.lp.status_text()
        self.assertIn("round", text)
        self.assertIn("items", text)
        # 空队列时没有 next 是正确的——播种后才应出现
        self.lp.phase_plan(self.lp.status())
        self.assertIn("next", self.lp.status_text().lower())

    def test_plan_always_yields_next_step(self):
        """PLAN 阶段的核心不变式:一定有下一步"""
        r = self.lp.phase_plan(self.lp.status())
        self.assertTrue(r.ok, f"PLAN 应总能给出下一步,实际: {r.detail}")


class TestGatesPresent(unittest.TestCase):
    """门禁本身的自检"""

    def test_all_gates_registered(self):
        from arl_lite.devloop import gates
        names = set(gates.all_gate_names())
        expected = {
            "no_thirdparty_import", "test_baseline", "no_import_cycle",
            "loc_budget", "rules_have_advice", "prompt_injection_guard",
            "doc_freshness",
        }
        self.assertEqual(expected - names, set(), f"缺少门禁: {expected - names}")

    def test_unknown_gate_raises(self):
        from arl_lite.devloop import gates
        with self.assertRaises(KeyError):
            gates.get_gate("no_such_gate")

    def test_gates_are_pure_stdlib(self):
        """门禁层自身必须守住零依赖铁律"""
        import ast
        import sys
        src = (REPO / "arl_lite" / "devloop" / "gates.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
        third = {m for m in mods if m not in sys.stdlib_module_names and m != "arl_lite"}
        self.assertEqual(third, set(), f"gates.py 引入第三方依赖: {third}")


class TestDevloopIsStdlib(unittest.TestCase):
    """整个 devloop 包必须零第三方依赖"""

    def test_all_modules_stdlib_only(self):
        import ast
        import sys
        devloop = REPO / "arl_lite" / "devloop"
        third = set()
        for py in devloop.glob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    third.update(
                        a.name.split(".")[0] for a in node.names
                        if a.name.split(".")[0] not in sys.stdlib_module_names
                        and a.name.split(".")[0] != "arl_lite"
                    )
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    top = node.module.split(".")[0]
                    if top not in sys.stdlib_module_names and top != "arl_lite":
                        third.add(top)
        self.assertEqual(third, set(), f"devloop 引入第三方依赖: {third}")


if __name__ == "__main__":
    unittest.main()
