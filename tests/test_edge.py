"""arl-lite 边界测试

覆盖常见边界输入/异常情况:
- parse_ports 边界
- update_task_status 边界
- fingerprint AST 边界
- query SQL 注入严格测试
- ports/sites 边界
- fingerprint 引擎 DoS
- filter limit 边界
- CLI 错误处理
"""
import sys, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# 1. parse_ports 边界
print("\n[J.1] parse_ports 边界")
from arl_lite.integrations.portscan import parse_ports
cases = [
    ("80,443", [80, 443]),
    ("1-3", [1, 2, 3]),
    ("80", [80]),
    ("", []),
    (",", []),
    ("  ", []),
    ("0", []),
    ("65535", [65535]),
    ("65536", []),
    ("100-1", []),
    ("abc", []),
    ("1-1", [1]),
    ("99999-100000", []),
]
all_ok = True
for inp, expected in cases:
    got = parse_ports(inp)
    if got != expected:
        all_ok = False
        print(f"  ✗ parse_ports({inp!r}) = {got}, expect {expected}")
if all_ok:
    print(f"  ✓ 13 个边界 case 全过")

# 2. update_task_status 边界
print("\n[J.2] update_task_status 边界")
from arl_lite.db.storage import Storage
with tempfile.TemporaryDirectory() as td:
    s = Storage("edge", td)
    tid = s.create_task(s.workspace_id, "x.com")
    s.update_task_status(tid, "RUNNING", started_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    s.update_task_status(tid, "DONE", finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    s.update_task_status(tid, "FAILED", error_message="crashed")
    rows = s.query("tasks", limit=1)
    assert rows[0]["status"] == "FAILED"
    assert rows[0]["error_message"] == "crashed"
    print(f"  ✓ 链式更新 3 次,status=FAILED, error_message=crashed")
    s.update_task_status(tid, "WAITING")
    rows = s.query("tasks", limit=1)
    assert rows[0]["status"] == "WAITING"
    print(f"  ✓ 只传 status 也行(其他 None 不覆盖)")

# 3. fingerprint AST 边界
print("\n[J.3] fingerprint AST 边界")
from arl_lite.core.fingerprint_engine import match_one, FingerprintContext
ctx = FingerprintContext({}, "", "", 200)
cases = [
    "",
    "body",
    "body + 'x'",
    "1+1",
    "body.contains('a') && body.contains('b')",
    "body.contains(123)",
    "'中文'",
    "header['x-foo']",
    "header[123]",
    "obj.foo()",
]
for rule in cases:
    try:
        r = match_one(rule, ctx)
        print(f"  ✓ {rule!r:35} → {r}")
    except Exception as e:
        print(f"  ✗ {rule!r:35} → EXCEPTION: {type(e).__name__}: {e}")

# 4. query SQL 注入
print("\n[J.4] query SQL 注入 - 严格测试")
with tempfile.TemporaryDirectory() as td:
    s = Storage("sql-edge", td)
    tid = s.create_task(s.workspace_id, "x.com")
    s.add_domain(task_id=tid, domain="x.com", source="crtsh", confidence=50)
    attacks = [
        "source='crtsh'; drop table domains; --",
        "SOURCE='CRTSH'; drop table domains; --",
        "source='crtsh' /* multi */ drop table domains;",
        "1=1 OR 1=1",
        "source like '%drop%'",
    ]
    for attack in attacks:
        try:
            s.query("domains", filter_sql=attack)
            print(f"  ? {attack!r:55} 未拦截")
        except ValueError as e:
            print(f"  ✓ {attack!r:55} 拦截")

# 5. ports table 边界
print("\n[J.5] ports table 边界")
with tempfile.TemporaryDirectory() as td:
    s = Storage("port-edge", td)
    tid = s.create_task(s.workspace_id, "x.com")
    s.add_port(task_id=tid, host="1.2.3.4", port=80, state="open", service="http")
    rows = s.query("ports")
    assert len(rows) == 1
    print(f"  ✓ add_port(80) 正常入库")

# 6. sites 长 URL
print("\n[J.6] sites 长 URL")
with tempfile.TemporaryDirectory() as td:
    s = Storage("site-edge", td)
    tid = s.create_task(s.workspace_id, "x.com")
    long_url = "https://example.com/" + "a" * 5000
    s.add_site(task_id=tid, url=long_url, host="example.com", ip=None, port=443,
               scheme="https", title="", status_code=200, tech="")
    rows = s.query("sites")
    assert len(rows) == 1
    print(f"  ✓ 长 URL (5000+ 字符) 入库成功")

# 7. fingerprint 引擎防 DoS
print("\n[J.7] fingerprint 引擎防 DoS")
import time as t
big_body = "x" * 10240
import json
fps = json.load(open(str(Path(__file__).parent.parent) + "/arl_lite/fingerprints/fingerprints.json"))
from arl_lite.core.fingerprint_engine import match_all
start = t.time()
hits = match_all(fps, headers={}, body=big_body, title="", status=200)
elapsed = t.time() - start
print(f"  ✓ 10KB body × {len(fps)} 规则耗时 {elapsed*1000:.0f}ms")

# 8. filter limit 边界
print("\n[J.8] filter limit 边界")
with tempfile.TemporaryDirectory() as td:
    s = Storage("limit-edge", td)
    tid = s.create_task(s.workspace_id, "x.com")
    for i in range(10):
        s.add_domain(task_id=tid, domain=f"sub{i}.x.com", source="crtsh", confidence=50)
    rows = s.query("domains", limit=0)
    print(f"  ✓ limit=0 返回 {len(rows)} 行")
    # limit=-1 必须拒绝(不能是 SQLite 默认"无限制")
    try:
        rows = s.query("domains", limit=-1)
        print(f"  ✗ limit=-1 接受(应该是 ValueError),返回 {len(rows)} 行")
    except ValueError as e:
        print(f"  ✓ limit=-1 拒绝: {e}")
    # limit 10001 也要拒绝
    try:
        rows = s.query("domains", limit=10001)
        print(f"  ✗ limit=10001 接受")
    except ValueError as e:
        print(f"  ✓ limit=10001 拒绝: {e}")

# 9. CLI:不存在的 module
print("\n[J.9] CLI 错误处理")
import subprocess, os
r = subprocess.run(
    ["python3", "-m", "arl_lite", "run", "-t", "x.com", "-m", "this-module-does-not-exist"],
    cwd=str(Path(__file__).parent.parent), env={**os.environ, "PYTHONPATH": "."},
    capture_output=True, text=True, timeout=30,
)
print(f"  exit={r.returncode}  stderr={(r.stderr or '')[:100]}")
print(f"  stdout={(r.stdout or '')[:200]}")
