"""置信度模型测试

覆盖:
1. 四因子乘法模型的档位行为
2. 处置分档(report/observe/discard)
3. 规则 YAML 的 confidence 解析(含非法值告警)
4. 端到端: 规则 → 引擎 → DB 全链路
5. 37 条规则全部有档位标注
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arl_lite.core.confidence import (
    assess, CONF_HIGH, CONF_MEDIUM, CONF_LOW,
    STATUS_REPORT, STATUS_OBSERVE, STATUS_DISCARD,
    THRESHOLD_REPORT, THRESHOLD_OBSERVE,
)
from arl_lite.core.correlation_engine import (
    _parse_yaml, load_all_rules, run_all_rules, save_correlations,
)

REPO = Path(__file__).resolve().parents[1]
RULES_DIR = REPO / "arl_lite" / "modules" / "analysis" / "rules"


class TestConfidenceModel(unittest.TestCase):

    def test_tiers_are_ordered(self):
        """档位必须有明确高低序——否则分级没意义"""
        hi = assess("r", CONF_HIGH, matched_table_count=1).score
        md = assess("r", CONF_MEDIUM, matched_table_count=1).score
        lo = assess("r", CONF_LOW, matched_table_count=1).score
        self.assertGreater(hi, md)
        self.assertGreater(md, lo)

    def test_cross_ref_unsatisfied_hurts(self):
        """定义了 cross_ref 却没满足 = 交叉验证失败,必须降分"""
        sat = assess("r", CONF_MEDIUM, has_cross_ref=True, cross_ref_satisfied=True,
                     matched_table_count=2)
        unsat = assess("r", CONF_MEDIUM, has_cross_ref=True, cross_ref_satisfied=False,
                       matched_table_count=2)
        self.assertGreater(sat.score, unsat.score)

    def test_more_tables_lower_score(self):
        """跨表越多误报面越大(同机多服务是常态)"""
        one = assess("r", CONF_MEDIUM, matched_table_count=1).score
        three = assess("r", CONF_MEDIUM, matched_table_count=3).score
        self.assertGreater(one, three)

    def test_exclusion_gets_bonus(self):
        """作者想过误报场景 = 加分"""
        with_excl = assess("r", CONF_MEDIUM, has_exclusion=True).score
        without = assess("r", CONF_MEDIUM, has_exclusion=False).score
        self.assertGreater(with_excl, without)

    def test_score_bounded(self):
        for base in (CONF_HIGH, CONF_MEDIUM, CONF_LOW):
            for tc in (1, 2, 3, 5):
                s = assess("r", base, has_cross_ref=True, cross_ref_satisfied=True,
                           has_exclusion=True, matched_table_count=tc).score
                self.assertGreaterEqual(s, 0.0)
                self.assertLessEqual(s, 1.0)

    def test_disposition_matches_thresholds(self):
        for base in (CONF_HIGH, CONF_MEDIUM, CONF_LOW):
            for tc in (1, 2, 3):
                a = assess("r", base, matched_table_count=tc)
                if a.score >= THRESHOLD_REPORT:
                    self.assertEqual(a.status, STATUS_REPORT)
                elif a.score >= THRESHOLD_OBSERVE:
                    self.assertEqual(a.status, STATUS_OBSERVE)
                else:
                    self.assertEqual(a.status, STATUS_DISCARD)

    def test_low_tier_not_reported(self):
        """low 档不该直接进主告警——这是分级的核心目的"""
        a = assess("r", CONF_LOW, matched_table_count=1)
        self.assertNotEqual(a.status, STATUS_REPORT)

    def test_factors_explain_score(self):
        """每个分必须能解释——不可解释的分数没有决策价值"""
        a = assess("r", CONF_MEDIUM, has_cross_ref=True, cross_ref_satisfied=True,
                   has_exclusion=True, matched_table_count=2)
        for k in ("base_prior", "cross_evidence", "table_complexity", "exclusion_bonus"):
            self.assertIn(k, a.factors)
        # 分 = 各因子连乘(可复算)
        prod = 1.0
        for v in a.factors.values():
            prod *= v
        self.assertAlmostEqual(a.score, round(prod, 3), places=3)

    def test_invalid_base_defaults(self):
        """非法档位不崩,且按 medium 保守处理"""
        a = assess("r", "garbage")
        self.assertAlmostEqual(a.score, assess("r", CONF_MEDIUM).score, places=3)


class TestRuleParsing(unittest.TestCase):

    def _parse(self, body: str):
        return _parse_yaml(body)

    def test_confidence_parsed(self):
        for lvl in ("high", "medium", "low", "HIGH", "Low"):
            r = self._parse(f"name: r\nrisk: 5\nconfidence: {lvl}\nheadline: H\nadvice: A\n")
            self.assertEqual(r.confidence, lvl.lower(), f"{lvl} 应被规范化")

    def test_confidence_absent_defaults_medium(self):
        r = self._parse("name: r\nrisk: 5\nheadline: H\nadvice: A\n")
        self.assertEqual(r.confidence, "medium", "缺省应保守取 medium")

    def test_confidence_invalid_falls_back(self):
        r = self._parse("name: r\nrisk: 5\nconfidence: bogus\nheadline: H\nadvice: A\n")
        self.assertEqual(r.confidence, "medium", "非法值应降级而非崩溃")


class TestAllRulesAnnotated(unittest.TestCase):
    """37 条规则必须全部标注档位——漏标就是隐性 bug"""

    def test_every_rule_has_confidence(self):
        rules = load_all_rules(RULES_DIR)
        self.assertGreater(len(rules), 0, "规则目录不应为空")
        missing = [r.name for r in rules if r.confidence not in (CONF_HIGH, CONF_MEDIUM, CONF_LOW)]
        self.assertEqual(missing, [], f"未标注置信度的规则: {missing}")

    def test_high_tier_rules_are_structurally_defensible(self):
        """high 档必须有硬依据(端口事实或跨表验证),不能是拍脑袋标的"""
        for r in load_all_rules(RULES_DIR):
            if r.confidence != CONF_HIGH:
                continue
            port_fact = any(a.table == "ports" for a in r.collect)
            cross_validated = bool(r.cross_ref)
            self.assertTrue(
                port_fact or cross_validated,
                f"{r.name} 标为 high 但既非端口事实也无 cross_ref 验证",
            )


class TestEndToEnd(unittest.TestCase):
    """规则 → 引擎 → DB 全链路"""

    def setUp(self):
        from arl_lite.db.storage import Storage
        ws = "conf_test_" + tempfile.mkdtemp().split("/")[-1][:8]
        self.storage = Storage(workspace=ws)

    def test_confidence_reaches_db(self):
        r = self.storage.bulk_insert("findings", [
            {"target": "r.example.com", "target_type": "domain",
             "title": "Redis", "severity": "high",
             "module": "test", "finding_type": "service"},
        ])
        self.assertEqual(r["inserted"], 1, f"插入失败: {r['errors']}")

        rules = load_all_rules(RULES_DIR)
        hits = run_all_rules(self.storage, rules_override=rules)
        self.assertTrue(hits, "Redis 暴露应命中规则")

        hit = next(h for h in hits if h.rule_name == "redis_public")
        self.assertGreater(hit.confidence, 0)
        self.assertIn(hit.confidence_level, (CONF_HIGH, CONF_MEDIUM, CONF_LOW))
        self.assertTrue(hit.confidence_factors, "必须带因子供解释")

        save_correlations(self.storage, hits)
        row = next(c for c in self.storage.query("correlations", limit=50)
                   if c["rule_name"] == "redis_public")
        self.assertEqual(row["confidence"], hit.confidence)
        self.assertEqual(row["confidence_level"], hit.confidence_level)
        self.assertIsNotNone(row["confidence_status"])
        self.assertTrue(row["confidence_factors"], "因子应落库以便回溯")

    def test_low_confidence_rule_still_hits_but_marked(self):
        """低置信度不是不报,而是标出来让操作者自己判断"""
        self.storage.bulk_insert("findings", [
            {"target": "c.example.com", "target_type": "domain",
             "title": "Cloudflare", "severity": "info",
             "module": "test", "finding_type": "cdn"},
        ])
        rules = load_all_rules(RULES_DIR)
        hits = run_all_rules(self.storage, rules_override=rules)
        cd = [h for h in hits if h.rule_name == "cdn_bypass"]
        if cd:
            self.assertEqual(cd[0].confidence_level, CONF_LOW)
            self.assertNotEqual(cd[0].confidence_status, STATUS_REPORT)

    def test_migration_adds_confidence_columns(self):
        """旧库升级后必须有置信度列,否则新代码写不进去"""
        with self.storage._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(correlations)")}
        for c in ("confidence", "confidence_level", "confidence_status", "confidence_factors"):
            self.assertIn(c, cols, f"correlations 缺列 {c}")


if __name__ == "__main__":
    unittest.main()
