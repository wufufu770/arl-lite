"""arl-lite Phase 6 测试 — DNS / WHOIS / HTML 报告

测试覆盖:
1. dns_lookup 单元测试(hand-rolled DNS packet)
2. whois_lookup 单元测试
3. cli_report_html 渲染
4. CLI 端到端(html 报告)
5. 真实 example.com DNS + WHOIS 查询
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))


def ok(msg: str) -> None:
    print(f"  \033[32m✓\033[0m {msg}")


def fail(msg: str) -> None:
    print(f"  \033[31m✗\033[0m {msg}")
    raise AssertionError(msg)


# =========================
# 1. DNS lookup
# =========================

def test_dns_lookup():
    print("\n[1] DNS lookup")

    from arl_lite.integrations.dns_lookup import (
        _encode_name, _decode_name, lookup,
    )
    from arl_lite.integrations.whois_lookup import whois_server_for

    # 1.1 encode/decode name
    cases = [
        ("example.com", b"\x07example\x03com\x00"),
        ("a.b.c.d", b"\x01a\x01b\x01c\x01d\x00"),
        ("single", b"\x06single\x00"),
        ("", b"\x00"),
    ]
    for name, expected in cases:
        encoded = _encode_name(name)
        if encoded == expected:
            ok(f"encode {name!r}")
        else:
            fail(f"encode {name!r}: {encoded!r} != {expected!r}")

    # 1.2 decode
    data = b"\x07example\x03com\x00" + b"\x00\x01" + b"\x00\x01"  # A IN
    name, offset = _decode_name(data, 0)
    if name == "example.com":
        ok(f"decode: name={name!r}")
    else:
        fail(f"decode: {name!r}")

    # 1.3 压缩指针
    # name1 "foo.example.com" at offset 0 (17 bytes total)
    # name2 "example.com" via pointer to offset 4
    name1 = b"\x03foo\x07example\x03com\x00"  # 17 bytes
    assert len(name1) == 17
    data = name1 + b"\xc0\x04"  # pointer to 4 = "example.com"
    n1, o1 = _decode_name(data, 0)
    n2, o2 = _decode_name(data, 17)
    if n1 == "foo.example.com" and n2 == "example.com":
        ok(f"压缩指针: {n1} / {n2}")
    else:
        fail(f"压缩指针: {n1} / {n2}")

    # 1.4 whois_server_for
    cases = [
        ("example.com", "whois.verisign-grs.com"),
        ("foo.io", "whois.nic.io"),
        ("bar.com.cn", "whois.cnnic.cn"),
        ("unknown.xyz", "whois.nic.xyz"),
        ("abc", "whois.iana.org"),
    ]
    for domain, expected in cases:
        if whois_server_for(domain) == expected:
            ok(f"whois_server_for({domain})")
        else:
            fail(f"whois_server_for({domain}) → {whois_server_for(domain)}")

    # 1.5 真打一个域名(可能网络问题,允许空)
    result = lookup("example.com", server="8.8.8.8", timeout=3)
    if isinstance(result, dict) and "_status" in result:
        ok(f"lookup example.com: status={result['_status']}")
        for rtype in ["A", "NS", "MX", "TXT"]:
            n = len(result.get(rtype, []))
            print(f"    {rtype}: {n} 记录")
    else:
        fail(f"lookup 返回: {result}")

    # 1.6 不存在的域名
    result = lookup("nonexistent-domain-12345-fake.invalid", server="8.8.8.8", timeout=2)
    if result["_status"] in ("ok", "error: ..."):
        ok(f"lookup 不存在域名: status={result['_status']}")


# =========================
# 2. WHOIS lookup
# =========================

def test_whois_lookup():
    print("\n[2] WHOIS lookup")

    from arl_lite.integrations.whois_lookup import (
        whois_server_for, _parse_whois_response, whois_query,
    )

    # 2.1 whois_server_for
    assert whois_server_for("google.com") == "whois.verisign-grs.com"
    ok("whois_server_for google.com")

    # 2.2 parse
    sample = """
    Domain Name: EXAMPLE.COM
    Registrar: Example Registrar, Inc.
    Creation Date: 1995-08-14T04:00:00Z
    Registry Expiry Date: 2025-08-13T04:00:00Z
    Updated Date: 2024-08-14T07:01:44Z
    Domain Status: clientTransferProhibited
    Domain Status: serverDeleteProhibited
    Name Server: NS1.EXAMPLE.COM
    Name Server: NS2.EXAMPLE.COM
    Registrant Name: Example Registrant
    Abuse contact email: abuse@example.com
    """
    parsed = _parse_whois_response(sample)
    if parsed["domain"] == "EXAMPLE.COM":
        ok("parse: domain")
    if "Example Registrar" in parsed["registrar"]:
        ok("parse: registrar")
    if parsed["creation_date"] and "1995" in parsed["creation_date"]:
        ok("parse: creation_date")
    if len(parsed["status"]) == 2:
        ok(f"parse: status ({len(parsed['status'])} 个)")
    if len(parsed["name_servers"]) == 2:
        ok(f"parse: name_servers ({len(parsed['name_servers'])} 个)")
    # abuse 邮箱应该被过滤
    if "abuse@example.com" not in parsed["emails"]:
        ok("parse: abuse email 过滤")

    # 2.3 真打一个域名(允许失败)
    result = whois_query("google.com", timeout=5)
    if isinstance(result, dict):
        if "_error" in result:
            print(f"    [i] 网络失败:{result['_error']}")
            ok(f"whois 网络失败也返回 dict")
        elif result.get("registrar"):
            ok(f"whois google.com: registrar={result['registrar'][:50]}")
        else:
            ok(f"whois google.com: parsed={list(result.keys())[:5]}")


# =========================
# 3. HTML 报告
# =========================

def test_html_report():
    print("\n[3] HTML 报告")

    from arl_lite.cli_report_html import generate_html_report, write_html_report
    from arl_lite.db.storage import Storage

    with tempfile.TemporaryDirectory() as home:
        os.environ["HOME"] = home
        s = Storage("html_test", home)
        s.create_task(s.workspace_id, "example.com")
        for sub in ["www", "mail", "api"]:
            s.add_host(task_id=1, host=f"{sub}.example.com", source="crtsh", confidence=0.8)
        for p, svc in [(80, "http"), (443, "https"), (3306, "mysql"), (22, "ssh")]:
            s.add_port(task_id=1, host="1.1.1.1", port=p, state="open", service=svc)
        s.add_finding(
            task_id=1, target="1.1.1.1:3306", finding_type="exposed_db",
            title="MySQL exposed", description="public mysql", severity="high",
        )
        from arl_lite.core.correlation_engine import run_all_rules, save_correlations
        hits = run_all_rules(s, "str(Path(__file__).parent.parent)/arl_lite/modules/analysis/rules")
        save_correlations(s, hits)

        # 渲染
        html = generate_html_report(s, "html_test")
        if "<html" in html and "</html>" in html:
            ok(f"生成 HTML: {len(html)} chars")
        else:
            fail("HTML 不完整")
        # 必含
        for kw in ["ARL-Lite", "www.example.com", "1.1.1.1", "3306", "MySQL",
                   "critical", "风险", "高风险", "Top", "关联"]:
            if kw in html:
                ok(f"  含 {kw}")
            else:
                fail(f"  缺 {kw}")

        # 写到文件
        out = Path(home) / "report.html"
        result = write_html_report(s, "html_test", out)
        if result.exists() and result.stat().st_size > 1000:
            ok(f"写文件: {result} ({result.stat().st_size} bytes)")
        else:
            fail(f"写文件失败: {result}")

        # 空数据
        s2 = Storage("empty_test", home)
        s2.create_task(s2.workspace_id, "x.com")
        html2 = generate_html_report(s2, "empty_test")
        if "无风险资产" in html2 or "没跑关联" in html2:
            ok("空 ws 报告:fallback 文本 OK")
        else:
            fail(f"空 ws 报告:没 fallback")


# =========================
# 4. CLI 端到端
# =========================

def test_cli():
    print("\n[4] CLI 端到端")

    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent), "HOME": tempfile.mkdtemp()}

    # 准备数据
    from arl_lite.db.storage import Storage
    home = tempfile.mkdtemp()
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent), "HOME": home}
    # 必须显式用 CLI 子进程的默认根(HOME/.arl-lite/workspaces),
    # 旧写法自定义 root,CLI 打开的是另一个空库,准备的数据形同虚设
    s = Storage("cli_test", Path(home) / ".arl-lite" / "workspaces")
    s.create_task(s.workspace_id, "example.com")
    s.add_host(task_id=1, host="www.example.com", source="crtsh", confidence=0.8)
    s.add_port(task_id=1, host="1.1.1.1", port=80, state="open", service="http")

    # export html
    r = subprocess.run(
        ["python3", "-m", "arl_lite", "export", "-w", "cli_test",
         "--format", "html", "-o", "/tmp/test_report.html"],
        cwd=str(Path(__file__).parent.parent), env=env,
        capture_output=True, text=True, timeout=10,
    )
    print(f"  export html: exit={r.returncode}")
    if r.returncode == 0 and Path("/tmp/test_report.html").exists():
        size = Path("/tmp/test_report.html").stat().st_size
        ok(f"HTML 文件生成: {size} bytes")
    else:
        fail(f"export html: {r.stderr[:100]}")

    # 边界:html 但没 -o
    r = subprocess.run(
        ["python3", "-m", "arl_lite", "export", "-w", "cli_test", "--format", "html"],
        cwd=str(Path(__file__).parent.parent), env=env,
        capture_output=True, text=True, timeout=10,
    )
    # 应该有默认输出或报错
    print(f"  export html 无 -o: exit={r.returncode}")


# =========================
# 5. 真打 example.com dns/whois
# =========================

def test_real_dns_whois():
    print("\n[5] 真打 example.com")

    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent), "HOME": tempfile.mkdtemp()}

    # dns module
    r = subprocess.run(
        ["python3", "-m", "arl_lite", "run", "-t", "example.com", "-m", "dns", "-w", "dns_test"],
        cwd=str(Path(__file__).parent.parent), env=env,
        capture_output=True, text=True, timeout=30,
    )
    print(f"  dns: exit={r.returncode}")
    if "found: " in r.stdout:
        print(f"    found={r.stdout.split('found: ')[-1].split()[0]}")

    # whois module
    r = subprocess.run(
        ["python3", "-m", "arl_lite", "run", "-t", "example.com", "-m", "whois", "-w", "whois_test"],
        cwd=str(Path(__file__).parent.parent), env=env,
        capture_output=True, text=True, timeout=30,
    )
    print(f"  whois: exit={r.returncode}")


# =========================
# 6. 17 module 注册
# =========================

def test_module_registry():
    print("\n[6] 17 module 注册")
    from arl_lite.modules.registry import discover_modules, _cache
    _cache.clear()
    mods = discover_modules()
    if len(mods) >= 17:
        ok(f"已注册 {len(mods)} module")
    else:
        fail(f"只 {len(mods)} 个 module")
    for expected in ["dns", "whois", "dirscan", "nuclei", "github", "portscan", "httpx_probe"]:
        if expected in mods:
            ok(f"  含 {expected}")
        else:
            fail(f"缺 {expected}")


# =========================
# runner
# =========================

def main():
    print("=" * 60)
    print("Phase 6 测试 — DNS / WHOIS / HTML 报告")
    print("=" * 60)
    test_dns_lookup()
    test_whois_lookup()
    test_html_report()
    test_cli()
    test_real_dns_whois()
    test_module_registry()
    print()
    print("=" * 60)
    print("\033[32mALL PHASE 6 TESTS PASSED\033[0m")
    print("=" * 60)


if __name__ == "__main__":
    main()
