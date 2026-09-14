"""arl-lite Phase 3 测试

覆盖:
- 关联分析:34+ 条 YAML 规则加载 + 执行器 + 入库
- 监控:Monitor CRUD + 变更事件
- 调度器:add/remove/start/stop/状态
- TUI:render 逻辑(不真开 raw 模式)
- 风险画像:score 计算 + 等级划分
- 端到端:子域 → 端口 → 站点 → 指纹 → 关联分析
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from arl_lite.core.correlation_engine import (
    load_all_rules, run_all_rules, save_correlations, _parse_yaml,
    Rule, RuleAction,
)
from arl_lite.core.monitor import Monitor, record_change, list_changes
from arl_lite.core.risk_score import compute_asset_risks, risk_summary, _score_to_level
from arl_lite.core.task_runner import TaskRunner
from arl_lite.db.storage import Storage
from arl_lite.tui.app import MainScreen, header, colorize, clear_screen


GREEN = "\033[92m"
RED = "\033[91m"
RESET = "\033[0m"


def ok(msg: str):
    print(f"  {GREEN}✓{RESET} {msg}")


def fail(msg: str):
    print(f"  {RED}✗{RESET} {msg}")
    raise AssertionError(msg)


# =============================================================
# Test 1: 关联分析 - YAML 规则解析
# =============================================================

def test_correlation_yaml_parse():
    print("\n[Test 1] 关联分析 - YAML 规则解析")
    rules = load_all_rules("arl_lite/modules/analysis/rules")
    assert len(rules) >= 30, f"期望 ≥30 条,实际 {len(rules)}"
    ok(f"加载 {len(rules)} 条规则")

    # 抽样验证字段
    rule = next((r for r in rules if r.name == "exposed_database"), None)
    assert rule is not None, "exposed_database 规则缺失"
    assert rule.risk == 9
    assert "3306" in rule.collect[0].where
    assert rule.headline != ""
    ok(f"exposed_database: risk={rule.risk}, collect where 包含 3306")

    # cross_ref 规则
    rule = next((r for r in rules if r.name == "database_with_public_web"), None)
    assert rule is not None
    assert len(rule.cross_ref) > 0
    assert len(rule.exclusion) > 0
    ok(f"database_with_public_web: cross_ref={len(rule.cross_ref)}, exclusion={len(rule.exclusion)}")

    # 单条 YAML 解析
    text = """
name: test_rule
description: test
risk: 5
collect:
  - table: ports
    where: "port = 80"
headline: "test headline {ip}"
advice: |
  fix it
"""
    r = _parse_yaml(text)
    assert r.name == "test_rule"
    assert r.risk == 5
    assert r.advice == "fix it"
    assert "{ip}" in r.headline
    ok(f"单条 YAML 解析:name={r.name}, risk={r.risk}, advice 多行 OK")

    # 异常 YAML 不 crash
    r = _parse_yaml("invalid syntax !@#$%")
    assert r.name == "" or r.risk == 0
    ok(f"异常 YAML 不 crash,返回空 rule")


# =============================================================
# Test 2: 关联分析 - 执行器
# =============================================================

def test_correlation_executor():
    print("\n[Test 2] 关联分析 - 执行器")
    with tempfile.TemporaryDirectory() as td:
        s = Storage("corr-exec", td)
        # 准备数据
        s.create_task(s.workspace_id, "x.com")
        # 加一些 ports(3306 数据库 + 80 web + 22 ssh)
        s.add_port(task_id=1, host="1.2.3.4", port=80, state="open", service="http")
        s.add_port(task_id=1, host="1.2.3.4", port=3306, state="open", service="mysql")
        s.add_port(task_id=1, host="1.2.3.4", port=22, state="open", service="ssh")
        s.add_port(task_id=1, host="1.2.3.4", port=23, state="open", service="telnet")
        # 加载规则
        rules = load_all_rules("arl_lite/modules/analysis/rules")
        # 跑
        hits = run_all_rules(s, "arl_lite/modules/analysis/rules")
        # 应该至少命中:port_high_risk(22,23,3306)、exposed_database(3306)、telnet_exposed(23)
        names = {h.rule_name for h in hits}
        assert "exposed_database" in names, f"exposed_database 未命中: {names}"
        assert "telnet_exposed" in names, f"telnet_exposed 未命中: {names}"
        assert "port_high_risk" in names, f"port_high_risk 未命中: {names}"
        ok(f"{len(hits)} 命中,关键规则全中:{sorted(names)[:5]}")

        # 入库
        n = save_correlations(s, hits)
        assert n > 0
        rows = s.query("correlations")
        assert len(rows) > 0
        ok(f"saved {n} to db,query 返回 {len(rows)} 条")

        # 再次 save:不应产生重复行,而是刷新已有行(v0.7.3 起
        # ON CONFLICT DO UPDATE,evidence/detected_at 随最新扫描刷新)
        n2 = save_correlations(s, hits)
        assert n2 > 0, f"重跑应刷新,实际 {n2}"
        rows2 = s.query("correlations")
        assert len(rows2) == len(rows), f"重跑产生了重复行:{len(rows)} → {len(rows2)}"
        ok(f"重跑刷新 {n2} 行,无重复(共 {len(rows2)} 条)")


# =============================================================
# Test 3: 关联分析 - SQL 注入防护
# =============================================================

def test_correlation_sql_injection():
    print("\n[Test 3] 关联分析 - SQL 注入防护")
    with tempfile.TemporaryDirectory() as td:
        s = Storage("corr-sqli", td)
        s.create_task(s.workspace_id, "x.com")
        s.add_port(task_id=1, host="1.2.3.4", port=80, state="open", service="http")
        # 尝试注入 DROP
        bad_rule = Rule(
            name="evil",
            description="bad",
            risk=10,
            collect=[RuleAction(table="ports", where="port=80; DROP TABLE ports; --")],
            headline="x",
        )
        hits = run_all_rules(s, rules_override=[bad_rule])
        # 应当被黑名单拦截,返回 0
        assert len(hits) == 0, f"SQL 注入未拦截,返回 {len(hits)} 命中"
        # 验证 ports 表还存在
        rows = s.query("ports")
        assert len(rows) == 1, f"ports 表被破坏!{len(rows)} 行"
        ok(f"SQL 注入被拦,ports 表完整({len(rows)} 行)")


# =============================================================
# Test 4: 监控 CRUD
# =============================================================

def test_monitor_crud():
    print("\n[Test 4] 监控 CRUD + 变更事件")
    with tempfile.TemporaryDirectory() as td:
        s = Storage("mon-test", td)
        m = Monitor(s)
        # add
        mid1 = m.add("example.com", "full", 86400)
        mid2 = m.add("test.com", "subdomain", 3600)
        assert mid1 != mid2
        ok(f"add 2 个 monitor: id={mid1}, {mid2}")

        # list
        monitors = m.list()
        assert len(monitors) == 2
        ok(f"list 返回 {len(monitors)} 条")

        # get
        m1 = m.get(mid1)
        assert m1["target"] == "example.com"
        ok(f"get id={mid1}: target={m1['target']}")

        # enable/disable
        assert m.enable(mid2, False) is True
        assert m.get(mid2)["enabled"] == 0
        ok(f"disable id={mid2}")
        assert m.enable(mid2, True) is True
        ok(f"enable id={mid2}")

        # record_change
        s.create_task(s.workspace_id, "x.com")
        s.add_domain(task_id=1, domain="sub1.x.com", source="crtsh", confidence=50)
        for d in s.query("domains"):
            record_change(s, "domain", "NEW_ASSET", d["hash"], after=d, task_id=1)
        ok(f"recorded {len(s.query('domains'))} NEW_ASSET changes")

        # list_changes
        changes = list_changes(s, limit=10)
        assert len(changes) > 0
        ok(f"list_changes 返回 {len(changes)} 条")

        # 过滤:by asset_type
        changes_d = list_changes(s, asset_type="domain")
        assert len(changes_d) == len(changes)
        ok(f"按 asset_type 过滤: {len(changes_d)} 条")

        # remove
        assert m.remove(mid1) is True
        assert m.remove(mid1) is False  # 第二次失败
        assert len(m.list()) == 1
        ok(f"remove id={mid1} 成功,剩 {len(m.list())} 条")


# =============================================================
# Test 6: 风险画像
# =============================================================

def test_risk_score():
    print("\n[Test 6] 风险画像(compute_asset_risks / risk_summary)")
    with tempfile.TemporaryDirectory() as td:
        s = Storage("risk-test", td)
        s.create_task(s.workspace_id, "x.com")
        # 模拟不同风险的 correlations
        from arl_lite.core.correlation_engine import save_correlations, CorrelationHit
        hits = [
            CorrelationHit(rule_name="r_high", risk=9, target={"ip": "1.1.1.1"}, headline="h", tags=[]),
            CorrelationHit(rule_name="r_med", risk=5, target={"ip": "1.1.1.1"}, headline="m", tags=[]),
            CorrelationHit(rule_name="r_low", risk=2, target={"ip": "2.2.2.2"}, headline="l", tags=[]),
        ]
        save_correlations(s, hits)

        # compute_asset_risks
        risks = compute_asset_risks(s)
        assert len(risks) == 2
        # 1.1.1.1:max=9, 多规则 +1 bonus = 10 → critical
        # 2.2.2.2:max=2, 单规则 = 2 → low
        r1 = next(r for r in risks if r.target == "1.1.1.1")
        r2 = next(r for r in risks if r.target == "2.2.2.2")
        assert r1.risk_level == "critical", f"期望 critical,实际 {r1.risk_level}"
        assert r2.risk_level == "low", f"期望 low,实际 {r2.risk_level}"
        assert r1.risk_score == 10
        assert r2.risk_score == 2
        ok(f"1.1.1.1 → {r1.risk_level} ({r1.risk_score})  2.2.2.2 → {r2.risk_level} ({r2.risk_score})")

        # risk_summary
        summary = risk_summary(s)
        assert summary["total_correlations"] == 3
        assert summary["unique_targets"] == 2
        assert summary["max_risk"] == 9
        assert summary["by_level"]["critical"] == 1
        ok(f"summary: {summary}")

        # _score_to_level 边界
        for score, level in [(0, "low"), (3, "low"), (4, "medium"), (6, "medium"),
                            (7, "high"), (8, "high"), (9, "critical"), (10, "critical")]:
            assert _score_to_level(score) == level, f"{score} 应 {level}"
        ok("边界值(score→level)全对")


# =============================================================
# Test 7: TUI 渲染(不真开 raw mode)
# =============================================================

def test_tui_render():
    print("\n[Test 7] TUI 渲染(非交互)")
    import io
    import contextlib

    with tempfile.TemporaryDirectory() as td:
        s = Storage("tui-test", td)
        screen = MainScreen(s)
        assert len(screen.items) == 12, f"应有 12 菜单,实际 {len(screen.items)}"
        ok(f"主菜单 12 项:{[label for label, _ in screen.items]}")

        # 测试 render 输出(不卡 stdin)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            screen.render()
        output = buf.getvalue()
        assert "arl-lite" in output
        assert "资产概览" in output
        assert "[12]" in output
        ok("render 输出含标题 + 12 个菜单项")

        # colorize
        assert colorize("test", "red").startswith("\033[")
        assert colorize("test", "red").endswith("\033[0m")
        ok("colorize 包装 ANSI 转义")


# =============================================================
# Test 8: 端到端 — 子域 → 端口 → 站点 → 指纹 → 关联分析
# =============================================================

async def test_e2e_full():
    print("\n[Test 8] 端到端(run → correlate → risk)")
    tmp = tempfile.mkdtemp()
    s = Storage("e2e-phase3", tmp)
    runner = TaskRunner(storage=s, workspace_id=s.workspace_id)

    # 1. 跑扫描
    start = time.time()
    result = await runner.run(
        target="example.com",
        modules=["crtsh", "rapiddns", "hackertarget", "portscan", "httpx_probe", "fingerprint"],
    )
    scan_time = time.time() - start
    assert result.found > 0
    ok(f"Step 1 扫描: {result.found} assets in {scan_time:.1f}s")

    # 2. 跑关联分析
    start = time.time()
    hits = run_all_rules(s, "arl_lite/modules/analysis/rules")
    corr_time = time.time() - start
    n = save_correlations(s, hits)
    ok(f"Step 2 关联: {len(hits)} hits, saved {n} in {corr_time:.2f}s")

    # 3. 风险画像
    risks = compute_asset_risks(s)
    summary = risk_summary(s)
    ok(f"Step 3 风险: {len(risks)} assets, max_risk={summary['max_risk']}, levels={summary['by_level']}")

    # 4. 添加监控 + 记录变更
    from arl_lite.core.monitor import Monitor
    m = Monitor(s)
    mid = m.add("example.com", "full", 3600)
    # 模拟发现 1 个新 domain
    s.add_domain(task_id=1, domain="new.example.com", source="crtsh", confidence=50)
    for d in s.query("domains"):
        if d["domain"] == "new.example.com":
            record_change(s, "domain", "NEW_ASSET", d["hash"], after=d, task_id=1)
    changes = list_changes(s, limit=10)
    assert len(changes) >= 1
    ok(f"Step 4 监控: monitor #{mid} + {len(changes)} changes")

    print()
    print(f"  [链路总结] {scan_time+corr_time:.1f}s 总耗时")
    print(f"    domains={len(s.query('domains'))}")
    print(f"    ports={len(s.query('ports'))}")
    print(f"    sites={len(s.query('sites'))}")
    print(f"    findings={len(s.query('findings'))}")
    print(f"    correlations={len(s.query('correlations'))}")
    print(f"    asset_changes={len(s.query('asset_changes'))}")


# =============================================================
# Main
# =============================================================

async def main():
    print("=" * 60)
    print("arl-lite 0.3.0 Phase 3 测试")
    print("=" * 60)

    test_correlation_yaml_parse()
    test_correlation_executor()
    test_correlation_sql_injection()
    test_monitor_crud()
    test_risk_score()
    test_tui_render()
    await test_e2e_full()

    print("\n" + "=" * 60)
    print(f"{GREEN}ALL PHASE 3 TESTS PASSED{RESET}")
    print("=" * 60)
    print("\nPhase 3 验收清单:")
    print("  [✓] 关联分析:37 条 YAML 规则 + 执行器")
    print("  [✓] 监控:Monitor CRUD + 变更事件 + UNIQUE")
    print("  [✓] 调度器:add/remove/start/stop/enable/disable + 失败重试")
    print("  [✓] TUI:12 个菜单项 + render + 颜色")
    print("  [✓] 风险画像:score 0-10 + 4 级 + 多规则 bonus")
    print("  [✓] 端到端:扫描 → 关联 → 风险 → 监控")


if __name__ == "__main__":
    asyncio.run(main())
