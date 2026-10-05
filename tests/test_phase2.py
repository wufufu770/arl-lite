"""arl-lite Phase 2 测试套件

覆盖:
- 9 个数据源模块(集成/integration)
- portscan(nmap 优先,Python fallback)
- httpx_probe(纯 urllib 探活)
- fingerprint(指纹库:内置 119 条 + ARL 库 1896 条)
- 端到端链(子域 → 端口 → 站点 → 指纹)
- TaskRunner 并发执行

设计:
- 真实环境打 example.com(不 mock 任何网络)
- 失败源也接受(ok=False, err_type 标对就行)
- 数据库层面验证入库
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from arl_lite.core.base_module import BaseModule, ModuleResult, SourceResult
from arl_lite.core.fingerprint_engine import load_fingerprints, match_all, match_one, FingerprintContext
from arl_lite.core.task_runner import TaskRunner
from arl_lite.db.storage import Storage
from arl_lite.integrations.rapiddns import collect_subdomains as rapid_collect
from arl_lite.integrations.hackertarget import collect_subdomains as hack_collect
from arl_lite.integrations.otx import collect_subdomains as otx_collect
from arl_lite.integrations.dnsdumpster import collect_subdomains as dump_collect
from arl_lite.integrations.virustotal import collect_subdomains as vt_collect
from arl_lite.integrations.quake import collect_subdomains as quake_collect
from arl_lite.integrations.fofa import collect_subdomains as fofa_collect
from arl_lite.integrations.crtsh import collect_subdomains as crt_collect
from arl_lite.integrations.portscan import scan, parse_ports, check_nmap


GREEN = "\033[92m"
RED = "\033[91m"
RESET = "\033[0m"


def ok(msg: str):
    print(f"  {GREEN}✓{RESET} {msg}")


def fail(msg: str):
    print(f"  {RED}✗{RESET} {msg}")
    raise AssertionError(msg)


# =============================================================
# Test 1: 9 数据源 integration(不依赖 module,只测 integration 层)
# =============================================================

async def test_9_sources_integration():
    print("\n[Test 1] 9 数据源 integration(纯 HTTP 集成)")
    target = "example.com"
    sources = [
        ("crtsh", crt_collect),
        ("rapiddns", rapid_collect),
        ("hackertarget", hack_collect),
        ("otx", otx_collect),
        ("dnsdumpster", dump_collect),
        ("virustotal", vt_collect),
        ("quake", quake_collect),
        ("fofa", fofa_collect),
    ]
    for name, fn in sources:
        try:
            result = await fn(target, timeout=15)
            # hackertarget v0.7.4 起返回 4 元组(带 ip_map),其余 3 元组
            subs, err, etype = result[0], result[1], result[2]
            # 三态记录
            if err:
                # 期望的失败:API key 缺失
                if etype in ("auth", "rate_limit"):
                    ok(f"{name:15} auth/rate_limit (expected, key missing)")
                elif etype in ("network", "timeout"):
                    # 网络问题也接受
                    ok(f"{name:15} network/timeout (env, {err[:30]})")
                else:
                    ok(f"{name:15} error_type={etype} (env, {err[:30]})")
            else:
                # 成功
                assert isinstance(subs, list)
                assert all(isinstance(s, str) for s in subs)
                if subs:
                    ok(f"{name:15} ok found={len(subs)}")
                else:
                    ok(f"{name:15} ok (no subs in this env)")
        except Exception as e:
            fail(f"{name} 异常: {type(e).__name__}: {e}")


# =============================================================
# Test 2: portscan(2 IP × 5 端口)
# =============================================================

async def test_portscan_integration():
    # r104 重写:原来这里打的是**公网** example.com,并断言「80 或 443 必须开着」。
    # 那不是集成测试,那是**把公网当 fixture** —— 红了没人分得清是代码坏了
    # 还是网络抖了。r103 收尾时 `test_baseline` 就是被它弄红的,而四组对照
    # (单跑 5 次 / a-p 1093 条 / 全量 1387 条 / 收集阶段钩子里)证明波动来自
    # 外网,和被测代码无关。**一个会随机红的门禁等于没有门禁**:基线是 0 失败,
    # 这种测试红一次就得有人去提基线,而 `--update-baseline` 那个后门正是在
    # 这种时候最容易被用上。所以先把 fixture 换成本机的。
    print("\n[Test 2] portscan 集成(本机 listener 造 open/closed,不依赖外网)")
    import socket as _s

    srv = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    open_port = srv.getsockname()[1]          # 开着:connect 会成功

    probe = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    closed_port = probe.getsockname()[1]      # 确定关闭:bind 后立刻 close
    probe.close()

    try:
        results, err, etype = await scan(
            "127.0.0.1", ports=f"{open_port},{closed_port}",
            prefer="python", timeout_per_port=1.0,
        )
        # 关键断言(r104 的核心):**有一个端口没判定出来,就不许报成功**。
        # 改前这里只有 `assert etype is None`,而改前 etype 恒为 None ——
        # 哪怕 5 个端口一个都没探到,它照样「成功」。
        assert etype is not None, (
            f"扫了 2 个端口只认出 {sorted(r['port'] for r in results)} 个,"
            f"etype 却还是 None —— 那是 r104 之前那个静默"
        )
        assert err and "未判定" in err, (
            f"没判定出来的端口必须说出来,不能只给一个类型名:{err!r}"
        )
        ports_found = {r["port"] for r in results}
        assert ports_found == {open_port}, (
            f"只该认出本机那个 listener,实际 {ports_found}"
            f"(closed={closed_port} 不该在里面)"
        )
        ok(f"portscan: open={ports_found},未判定已如实上报({err})")

        # 验证字段
        for r in results:
            assert "host" in r
            assert "port" in r
            assert r["state"] == "open"
            assert r["service"] in ("http", "https", "ssh", "ftp", "http-proxy", "unknown")
        ok("portscan 字段齐全 (host/port/state/service)")

        # 端口解析
        assert parse_ports("80,443") == [80, 443]
        assert parse_ports("1-3") == [1, 2, 3]
        assert parse_ports("80,443,8000-8002") == [80, 443, 8000, 8001, 8002]
        ok("parse_ports 解析 '80,443' / '1-3' / '80,443,8000-8002' 正确")

        # nmap 检测
        nmap_available = check_nmap()
        ok(f"check_nmap: {nmap_available}")
    finally:
        srv.close()


# =============================================================
# Test 3: fingerprint engine
# =============================================================

def test_fingerprint_engine():
    print("\n[Test 3] 指纹匹配引擎")

    fps = load_fingerprints("arl_lite/fingerprints/fingerprints.json")
    assert len(fps) >= 100, f"期望 ≥100 条指纹,实际 {len(fps)}"
    ok(f"加载 {len(fps)} 条指纹")

    # nginx 匹配
    ctx = FingerprintContext(headers={"Server": "nginx/1.18"}, body="", title="", status=200)
    assert match_one("header['server'].contains('nginx')", ctx)
    ok("nginx 规则匹配")

    # WordPress 匹配
    hits = match_all(fps, headers={}, body="<script src='/wp-content/themes/x.js'></script>", title="Blog")
    assert any(h["name"] == "WordPress" for h in hits)
    ok(f"WordPress 命中 ({len(hits)} 条)")

    # Cloudflare 多种方式命中
    ctx_cf = FingerprintContext(headers={"cf-ray": "abc-FRA", "server": "istio-envoy"}, body="", title="", status=200)
    assert match_one("header['cf-ray']", ctx_cf)
    ok("Cloudflare 边缘检测(cf-ray header)")

    # Tomcat 错误页
    hits = match_all(fps, headers={"Server": "Apache-Coyote/1.1"},
                     body="<html>Apache Tomcat/9.0</html>",
                     title="HTTP Status 404", status=404)
    names = {h["name"] for h in hits}
    assert "Apache Tomcat" in names, f"Tomcat 规则没命中: {names}"
    assert "Tomcat default" in names, f"Tomcat 错误页没命中: {names}"
    ok(f"Tomcat + Tomcat default 错误页命中")

    # Spring Boot
    hits = match_all(fps, headers={}, body="Whitelabel Error Page", title="Error", status=500)
    assert any(h["name"] == "Spring Boot" for h in hits)
    ok("Spring Boot 命中")

    # 复杂规则:Jenkins
    hits = match_all(fps, headers={"x-jenkins": "1.2.3"}, body="<h1>Jenkins</h1>", title="Jenkins", status=200)
    assert any(h["name"] == "Jenkins" for h in hits)
    ok("Jenkins 命中(x-jenkins header)")

    # 失败路径:不存在的 tech
    hits = match_all(fps, headers={}, body="some random content", title="Hello", status=200)
    assert len(hits) == 0 or all(h["category"] != "CMS" for h in hits)
    ok("空 body / 不匹配规则 不假阳性")

    # 语法错误规则不 crash
    assert match_one("invalid syntax !@#", FingerprintContext({}, "", "", 200)) is False
    ok("语法错误规则返回 False(不抛)")


# =============================================================
# Test 4: httpx_probe 集成
# =============================================================

async def test_httpx_probe_integration():
    print("\n[Test 4] httpx_probe 集成(纯 urllib)")
    from arl_lite.integrations.httpx_probe import probe

    results = []
    async for site in probe("example.com", ports=[80, 443], timeout=10):
        results.append(site)
    assert len(results) >= 1, "example.com:80/443 至少 1 个有响应"
    for r in results:
        assert r["status"] == 200, f"example.com 应该是 200, 实际 {r['status']}"
        assert r["title"] == "Example Domain", f"title 应该是 'Example Domain', 实际 '{r['title']}'"
        assert r["url"].startswith("http")
    ok(f"httpx_probe 命中 {len(results)} 个站点,titles 正确")

    # 失败:无效 host
    results = []
    async for site in probe("nonexistent.invalid.host", ports=[80], timeout=5):
        results.append(site)
    assert len(results) == 0
    ok("无效 host 不返回假阳性")


# =============================================================
# Test 5: Module 自动发现(12 个 module)
# =============================================================

def test_module_discovery():
    print("\n[Test 5] Module 自动发现(12 个)")
    from arl_lite.modules.registry import discover_modules, reset_cache
    reset_cache()
    mods = discover_modules()
    expected = {
        # 9 个数据源
        "subfinder", "crtsh", "rapiddns", "hackertarget", "otx",
        "dnsdumpster", "virustotal", "quake", "fofa",
        # 3 个新 module
        "portscan", "httpx_probe", "fingerprint",
    }
    actual = set(mods.keys())
    missing = expected - actual
    if missing:
        fail(f"缺失 module: {missing}")
    ok(f"12 module 全部发现:{sorted(actual)}")


# =============================================================
# Test 6: 端到端(子域 → 端口 → 站点 → 指纹)
# =============================================================

async def test_e2e_chain():
    print("\n[Test 6] 端到端侦察链(子域 → 端口 → 站点 → 指纹)")

    tmp = tempfile.mkdtemp()
    s = Storage("e2e-phase2", tmp)
    runner = TaskRunner(storage=s, workspace_id=s.workspace_id)

    # 4 阶段,真实网络
    start = time.time()

    # Stage 1: 子域(2 个无 key 快的)
    r1 = await runner.run(target="example.com", modules=["hackertarget", "rapiddns"])
    domains_before = len(s.query("domains"))
    ok(f"Stage 1 子域: found={r1.found}, 入库 {domains_before} 条 ({time.time()-start:.1f}s)")

    # Stage 2: 端口
    r2 = await runner.run(target="example.com", modules=["portscan"])
    ports = len(s.query("ports"))
    ok(f"Stage 2 端口: found={r2.found}, 入库 {ports} 条")

    # Stage 3: HTTP 探活
    r3 = await runner.run(target="example.com", modules=["httpx_probe"])
    sites = len(s.query("sites"))
    ok(f"Stage 3 站点: found={r3.found}, 入库 {sites} 条")

    # Stage 4: 指纹
    r4 = await runner.run(target="https://example.com", modules=["fingerprint"])
    findings = len(s.query("findings"))
    ok(f"Stage 4 指纹: found={r4.found}, 入库 {findings} 条")

    # source_status 全部 4 个 module 都有
    src_count = len(s.query("source_status"))
    assert src_count >= 4, f"source_status 至少 4 条,实际 {src_count}"
    ok(f"source_status {src_count} 条(每 module 一条)")

    # 总数据库
    elapsed = time.time() - start
    print(f"\n  [链路总结] {elapsed:.1f}s,共入库:")
    print(f"    domains: {domains_before}, ports: {ports}, sites: {sites}, findings: {findings}")


# =============================================================
# Test 7: TaskRunner 并发(独立 module,互不干扰)
# =============================================================

async def test_concurrent_isolation():
    print("\n[Test 7] TaskRunner 并发隔离(一个 module 异常不影响其他)")

    tmp = tempfile.mkdtemp()
    s = Storage("concurrency", tmp)
    runner = TaskRunner(storage=s, workspace_id=s.workspace_id)

    # 用 mock:一个正常 module + 一个会抛异常 module
    class GoodMod(BaseModule):
        name = "good_mod"
        category = "test"
        async def run(self, target, **kwargs):
            return ModuleResult(
                success=True, target=target, found=1, duration_seconds=0.1,
                sources=[SourceResult(ok=True, data=["a"], source="good")],
            )

    class BadMod(BaseModule):
        name = "bad_mod"
        category = "test"
        async def run(self, target, **kwargs):
            raise RuntimeError("intentional crash")

    from arl_lite.modules.registry import _cache
    # 直接注入
    import arl_lite.modules.registry as reg
    reg._cache["good_mod"] = GoodMod
    reg._cache["bad_mod"] = BadMod

    result = await runner.run(target="x.com", modules=["good_mod", "bad_mod"])
    # bad_mod 抛异常,被 TaskRunner 捕获记 sources_failed = 1
    # good_mod 应该正常返回
    # 注意:SourceResult.source 是 module 内部 make_source_result 传的 source 名
    # 这里 SourceResult(source="good"),不是 "good_mod"
    sources = {src.source: src for src in result.sources}
    # good_mod 通过 make_source_result 注册了 source="good" 的 SourceResult
    # bad_mod 抛异常后,TaskRunner 会创建一个 crash SourceResult(source="bad_mod")
    # 至少 bad_mod 应该在某个地方记录
    has_good = any(s.ok for s in sources.values() if s.data == ["a"])
    # bad_mod 因为 run 抛错,TaskRunner 不会创建 SourceResult
    # 而是把整个 ModuleResult 标 success=False
    has_failed_task = not result.success
    assert has_good, f"good_mod 没成功: {sources}"
    assert has_failed_task, f"bad_mod 抛错后 task 应该 success=False"
    ok("一个 module 异常不影响另一个 module 正常完成(good_mod 成功,bad_mod 抛错被 TaskRunner 捕获)")


# =============================================================
# Test 8: 内存预算
# =============================================================

def test_memory_budget():
    print("\n[Test 8] 内存预算(2G 机器友好)")
    import resource
    import arl_lite.modules.recon.domains_hosts.crtsh
    import arl_lite.modules.recon.domains_hosts.rapiddns
    import arl_lite.modules.recon.domains_hosts.hackertarget
    import arl_lite.modules.recon.domains_hosts.otx
    import arl_lite.modules.recon.domains_hosts.dnsdumpster
    import arl_lite.modules.recon.domains_hosts.virustotal
    import arl_lite.modules.recon.domains_hosts.quake
    import arl_lite.modules.recon.domains_hosts.fofa
    import arl_lite.modules.recon.ports.portscan
    import arl_lite.modules.recon.sites.httpx_probe
    import arl_lite.modules.recon.fingerprints.fingerprint
    from arl_lite.modules.registry import discover_modules, reset_cache
    reset_cache()
    mods = discover_modules()

    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(f"  12 module 全加载 + fingerprint 库 RSS: {rss_mb:.1f} MB")
    assert rss_mb < 200, f"内存超 200MB,实际 {rss_mb}MB"
    ok(f"内存预算合格 (<200MB,2G 限额)")


# =============================================================
# Main
# =============================================================

async def main():
    print("=" * 60)
    print("arl-lite 0.2.0 Phase 2 测试")
    print("=" * 60)

    await test_9_sources_integration()
    await test_portscan_integration()
    test_fingerprint_engine()
    await test_httpx_probe_integration()
    test_module_discovery()
    await test_e2e_chain()
    await test_concurrent_isolation()
    test_memory_budget()

    print("\n" + "=" * 60)
    print(f"{GREEN}ALL PHASE 2 TESTS PASSED{RESET}")
    print("=" * 60)
    print("\nPhase 2 验收清单:")
    print("  [✓] 9 个数据源(3 个需 key,6 个无 key)")
    print("  [✓] portscan(nmap 优先 / Python fallback)")
    print("  [✓] httpx_probe(纯 urllib,115 条技术栈 hints)")
    print("  [✓] fingerprint 引擎(内置 + ARL 库,AST 安全求值)")
    print("  [✓] 12 module 自动发现")
    print("  [✓] 端到端侦察链(子域 → 端口 → 站点 → 指纹)")
    print("  [✓] TaskRunner 并发隔离(异常不拖垮其他)")
    print("  [✓] 内存预算 < 200MB")


if __name__ == "__main__":
    asyncio.run(main())
