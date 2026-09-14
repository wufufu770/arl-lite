"""test_phase7.py — v0.7 批量入库 / Webhook / Watcher 测试"""
import os
import sys
import tempfile
import time
import json
import threading
from pathlib import Path

# 测试 harness
tests_passed = 0
tests_failed = 0

def ok(msg: str):
    global tests_passed
    tests_passed += 1
    print(f"  \033[32m✓\033[0m {msg}")

def fail(msg: str):
    global tests_failed
    tests_failed += 1
    print(f"  \033[31m✗\033[0m {msg}")
    raise AssertionError(msg)


def setup():
    home = tempfile.mkdtemp()
    os.environ["HOME"] = home
    sys.path.insert(0, str(Path(__file__).parent.parent))
    return home


def test_bulk_insert_basic():
    print("\n[1] bulk_insert 基础")
    home = setup()
    from arl_lite.db.storage import Storage

    s = Storage("t1", home)
    s.create_task(s.workspace_id, "x.com")

    # hosts
    rows = [{"task_id": 1, "host": f"h{i}.com", "source": "t"} for i in range(100)]
    r = s.bulk_insert("hosts", rows)
    if r["inserted"] == 100 and len(r["errors"]) == 0:
        ok(f"100 hosts: inserted={r['inserted']}")
    else:
        fail(f"100 hosts: {r}")

    # findings
    rows = [{"task_id": 1, "target": f"f{i}", "finding_type": "x", "title": f"T{i}", "severity": "low", "source": "t"} for i in range(50)]
    r = s.bulk_insert("findings", rows)
    if r["inserted"] == 50:
        ok(f"50 findings: inserted={r['inserted']}")
    else:
        fail(f"50 findings: {r}")

    # domains
    rows = [{"task_id": 1, "domain": f"d{i}.com", "source": "crtsh"} for i in range(30)]
    r = s.bulk_insert("domains", rows)
    if r["inserted"] == 30:
        ok(f"30 domains: inserted={r['inserted']}")
    else:
        fail(f"30 domains: {r}")


def test_bulk_insert_alias():
    print("\n[2] bulk_insert 列名 alias")
    home = setup()
    from arl_lite.db.storage import Storage

    s = Storage("t2", home)
    s.create_task(s.workspace_id, "x.com")

    # ports:host → ip 自动映射
    rows = [{"task_id": 1, "host": "1.2.3.4", "port": 80, "state": "open", "source": "p"}]
    r = s.bulk_insert("ports", rows)
    if r["inserted"] == 1 and not r["errors"]:
        ok("ports host→ip alias OK")
    else:
        fail(f"ports alias: {r}")

    # 5 表都能 source → module alias
    for table, sample in [
        ("domains", {"task_id": 1, "domain": "d.com", "source": "x"}),
        ("hosts", {"task_id": 1, "host": "h.com", "source": "x"}),
        ("ports", {"task_id": 1, "host": "1.1.1.1", "port": 22, "source": "x"}),
        ("sites", {"task_id": 1, "url": "https://s.com", "host": "s.com", "source": "x"}),
        ("findings", {"task_id": 1, "target": "f", "finding_type": "x", "title": "t", "source": "x"}),
    ]:
        r = s.bulk_insert(table, [sample])
        if r["inserted"] == 1:
            ok(f"{table} source→module")
        else:
            fail(f"{table}: {r}")


def test_bulk_insert_conflict():
    print("\n[3] bulk_insert 冲突策略")
    home = setup()
    from arl_lite.db.storage import Storage

    s = Storage("t3", home)
    s.create_task(s.workspace_id, "x.com")

    # ignore 模式:重复 hash 跳过
    rows = [{"task_id": 1, "host": "dup.com", "source": "t"} for _ in range(5)]
    r = s.bulk_insert("hosts", rows, on_conflict="ignore")
    if r["inserted"] == 1 and r["skipped"] == 4:
        ok(f"ignore: inserted={r['inserted']} skipped={r['skipped']}")
    else:
        fail(f"ignore: {r}")

    # replace 模式:覆盖
    rows = [{"task_id": 1, "host": "rep.com", "risk": 5, "source": "t"}]
    s.bulk_insert("hosts", rows, on_conflict="replace")
    rows2 = [{"task_id": 1, "host": "rep.com", "risk": 9, "source": "t"}]
    s.bulk_insert("hosts", rows2, on_conflict="replace")
    h = s.query("hosts", limit=10000)
    rep = [x for x in h if x.get("host") == "rep.com"]
    if rep and rep[0].get("risk") == 9:
        ok("replace: 最终 risk=9")
    else:
        fail(f"replace: risk={rep[0].get('risk') if rep else 'N/A'}")


def test_bulk_insert_perf():
    print("\n[4] bulk_insert 性能")
    home = setup()
    from arl_lite.db.storage import Storage

    s = Storage("t4", home)
    s.create_task(s.workspace_id, "x.com")

    rows = [{"task_id": 1, "host": f"perf{i}.com", "source": "t"} for i in range(1000)]
    t0 = time.time()
    r = s.bulk_insert("hosts", rows)
    elapsed_ms = (time.time() - t0) * 1000

    if r["inserted"] == 1000 and elapsed_ms < 500:
        ok(f"1000 hosts in {elapsed_ms:.0f}ms (< 500ms)")
    else:
        fail(f"1000 hosts: {elapsed_ms:.0f}ms, {r}")


def test_bulk_insert_errors():
    print("\n[5] bulk_insert 错误处理")
    home = setup()
    from arl_lite.db.storage import Storage

    s = Storage("t5", home)
    s.create_task(s.workspace_id, "x.com")

    # 不允许的表
    try:
        s.bulk_insert("bad_table", [])
        fail("bad_table 没拒绝")
    except ValueError as e:
        if "not allowed" in str(e):
            ok("bad_table 拒绝")
        else:
            fail(f"bad_table 错: {e}")

    # 错 on_conflict
    try:
        s.bulk_insert("hosts", [{"task_id": 1, "host": "x.com", "source": "t"}], on_conflict="merge")
        fail("on_conflict=merge 没拒绝")
    except ValueError as e:
        ok(f"on_conflict 拒绝: {e}")

    # 空 list 不报错
    r = s.bulk_insert("hosts", [])
    if r == {"inserted": 0, "skipped": 0, "errors": []}:
        ok("空 list 不报错")
    else:
        fail(f"空 list: {r}")

    # 行错误捕获
    rows = [
        {"task_id": 1, "host": "ok.com", "source": "t"},
        {"task_id": 1, "host": None, "source": "t"},  # 错
        {"task_id": 1, "host": "ok2.com", "source": "t"},
    ]
    r = s.bulk_insert("hosts", rows)
    if r["inserted"] == 2 and len(r["errors"]) > 0:
        ok(f"行错误捕获: {r['inserted']} ok, {len(r['errors'])} errs")
    else:
        fail(f"行错误: {r}")


def test_webhook_config():
    print("\n[6] WebhookConfig validation")
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from arl_lite.notify import WebhookConfig, is_valid_url

    # URL 验证
    for case, expected, desc in [
        ("https://ntfy.sh/topic", True, "ntfy"),
        ("http://example.com", True, "http"),
        ("ftp://x.com", False, "ftp"),
        ("", False, "空"),
        ("not-url", False, "无 scheme"),
    ]:
        if is_valid_url(case) == expected:
            ok(f"is_valid_url({case!r}) = {expected}")
        else:
            fail(f"{desc}: is_valid_url({case!r}) = {is_valid_url(case)}")

    # 配置
    for kwargs, should_pass, desc in [
        ({"provider": "local"}, True, "local"),
        ({"url": "https://x.com", "provider": "generic"}, True, "generic"),
        ({"url": "https://ntfy.sh/x", "provider": "ntfy"}, True, "ntfy"),
        ({"url": "https://hooks.slack.com/x", "provider": "slack"}, True, "slack"),
        ({"url": "ftp://x.com", "provider": "generic"}, False, "invalid url"),
        ({"url": "https://x.com", "provider": "fake"}, True, "unknown provider (fallback)"),
        ({"url": "https://x.com", "min_severity": "weird"}, True, "invalid sev (fallback)"),
        ({"url": "https://x.com", "timeout": 999}, True, "bad timeout (fallback)"),
    ]:
        try:
            c = WebhookConfig(**kwargs)
            if should_pass:
                ok(f"WebhookConfig({desc})")
            else:
                fail(f"{desc} 没拒绝")
        except Exception as e:
            if not should_pass:
                ok(f"WebhookConfig({desc}) rejected: {type(e).__name__}")
            else:
                fail(f"{desc}: {e}")


def test_webhook_should_notify():
    print("\n[7] WebhookConfig.should_notify")
    from arl_lite.notify import WebhookConfig

    c = WebhookConfig(provider="local", min_severity="high")
    if c.should_notify("info") is False and c.should_notify("low") is False:
        ok("info/low 被过滤")
    else:
        fail("info/low 触发")
    if c.should_notify("high") and c.should_notify("critical"):
        ok("high/critical 触发")
    else:
        fail("high/critical 没触发")

    # disabled
    c2 = WebhookConfig(provider="local", enabled=False)
    if not c2.should_notify("critical"):
        ok("disabled 全部不触发")
    else:
        fail("disabled 还触发")


def test_webhook_local_notify():
    print("\n[8] Webhook local notify")
    from arl_lite.notify import WebhookConfig, notify, notify_critical_finding, notify_correlation, notify_task_done

    c = WebhookConfig(provider="local")

    # 基础
    if notify(c, "title", "message", severity="critical"):
        ok("基础通知")
    else:
        fail("基础通知失败")

    # critical finding
    f = {"title": "X", "description": "Y", "severity": "critical", "target": "t"}
    if notify_critical_finding(c, f):
        ok("critical finding 通知")
    else:
        fail("critical finding 失败")

    # correlation
    corr = {"rule_name": "r", "severity": "high", "risk": 7, "headline": "h", "target": "t"}
    if notify_correlation(c, corr):
        ok("correlation 通知")
    else:
        fail("correlation 失败")

    # task done (无 errors → info,被过滤)
    c_filter = WebhookConfig(provider="local", min_severity="high")
    if not notify_task_done(c_filter, 1, "x", 10, 5.0, []):
        ok("task done (无 errors, info 级别被过滤)")
    else:
        fail("task done 不应触发")

    # task done (有 errors → medium,被过滤)
    if not notify_task_done(c_filter, 1, "x", 10, 5.0, ["err"]):
        ok("task done (errors → medium,被过滤)")
    else:
        fail("medium 不应触发")

    # task done (high threshold + critical trigger)
    c_low = WebhookConfig(provider="local", min_severity="info")
    if notify_task_done(c_low, 1, "x", 10, 5.0, []):
        ok("task done (info threshold, 触发)")
    else:
        fail("info threshold 不触发")


def test_webhook_unreachable():
    print("\n[9] Webhook 不可达 URL")
    from arl_lite.notify import WebhookConfig, notify

    c = WebhookConfig(url="http://127.0.0.1:1/none", provider="generic", timeout=2)
    if not notify(c, "x", "y", severity="critical"):
        ok("不可达 URL 不抛异常,返回 False")
    else:
        fail("不可达 URL 应返回 False")


def test_watcher_basic():
    print("\n[10] Watcher 基础")
    from arl_lite.db.storage import Storage
    from arl_lite.core.watcher import Watcher, WatchTarget

    home = setup()
    s = Storage("t10", home)
    w = Watcher(s)

    # WatchTarget validation
    try:
        WatchTarget(target="", modules=["dns"])
        fail("空 target 没拒绝")
    except ValueError:
        ok("空 target 拒绝")

    try:
        WatchTarget(target="x", modules=[], interval_seconds=10)
        fail("interval < 60 没拒绝")
    except ValueError:
        ok("interval < 60 拒绝")

    try:
        WatchTarget(target="a"*1001, modules=[])
        fail("超长 target 没拒绝")
    except ValueError:
        ok("超长 target 拒绝")

    # add / list / remove
    w.add("example.com", modules=["dns"], interval_seconds=3600)
    w.add("foo.com", modules=["dns", "whois"], interval_seconds=7200)
    if len(w.list()) == 2:
        ok(f"add 2 targets")
    else:
        fail(f"list={len(w.list())}")

    if w.remove("foo.com"):
        ok("remove ok")
    else:
        fail("remove 失败")

    if len(w.list()) == 1:
        ok(f"剩 1 个 target")
    else:
        fail(f"剩 {len(w.list())}")


def test_watcher_to_dict():
    print("\n[11] Watcher to_dict / from_dict")
    from arl_lite.core.watcher import WatchTarget

    wt = WatchTarget(target="example.com", modules=["dns"], interval_seconds=3600)
    d = wt.to_dict()
    wt2 = WatchTarget.from_dict(d)
    if wt2.target == wt.target and wt2.modules == wt.modules and wt2.interval_seconds == wt.interval_seconds:
        ok("to_dict / from_dict 圆环 OK")
    else:
        fail(f"round trip: {wt2.to_dict()}")


def test_17_modules():
    print("\n[12] 17 modules 注册")
    from arl_lite.modules.registry import discover_modules
    registry = discover_modules()
    if len(registry) == 17:
        ok(f"已注册 17 module")
    else:
        fail(f"实际 {len(registry)}")

    for m in ["dns", "whois", "dirscan", "nuclei", "github", "portscan", "fingerprint"]:
        if m in registry:
            ok(f"  含 {m}")
        else:
            fail(f"缺 {m}")


def main():
    print("=" * 60)
    print("Phase 7 测试 — bulk_insert / notify / watcher")
    print("=" * 60)

    test_bulk_insert_basic()
    test_bulk_insert_alias()
    test_bulk_insert_conflict()
    test_bulk_insert_perf()
    test_bulk_insert_errors()
    test_webhook_config()
    test_webhook_should_notify()
    test_webhook_local_notify()
    test_webhook_unreachable()
    test_watcher_basic()
    test_watcher_to_dict()
    test_17_modules()

    print()
    print("=" * 60)
    if tests_failed == 0:
        print(f"\033[32mALL PHASE 7 TESTS PASSED\033[0m  ({tests_passed} passed)")
    else:
        print(f"\033[31m{tests_failed} FAILED\033[0m  ({tests_passed} passed)")
    print("=" * 60)
    return 0 if tests_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
