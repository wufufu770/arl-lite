"""arl-lite 并发 + 隔离测试

覆盖:
- 多 workspace 隔离
- 并发写不冲突(SQLite WAL)
- 大量 task 入库
- 跨 workspace 数据库独立
- TaskRunner source_status 1 module = 1 条
- CLI exit code 区分
- 端到端短稳定性
"""
import sys, tempfile, time, asyncio, threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# 1. 多 workspace 隔离
print("\n[K.1] 多 workspace 隔离")
from arl_lite.db.storage import Storage
with tempfile.TemporaryDirectory() as td:
    s_a = Storage("ws-a", td)
    s_b = Storage("ws-b", td)
    s_c = Storage("ws-c", td)
    # 在 A 入 5 个 domain
    tid = s_a.create_task(s_a.workspace_id, "a.com")
    for i in range(5):
        s_a.add_domain(task_id=tid, domain=f"sub{i}.a.com", source="crtsh", confidence=50)
    # 在 B 入 3 个
    tid = s_b.create_task(s_b.workspace_id, "b.com")
    for i in range(3):
        s_b.add_domain(task_id=tid, domain=f"sub{i}.b.com", source="crtsh", confidence=50)
    # 验证
    assert len(s_a.query("domains")) == 5
    assert len(s_b.query("domains")) == 3
    assert len(s_c.query("domains")) == 0
    print(f"  ✓ ws_a={len(s_a.query('domains'))}  ws_b={len(s_b.query('domains'))}  ws_c={len(s_c.query('domains'))}  互不干扰")

# 2. 并发写不冲突
print("\n[K.2] 并发写不冲突(SQLite WAL)")
with tempfile.TemporaryDirectory() as td:
    s = Storage("concurrent", td)
    tid = s.create_task(s.workspace_id, "x.com")

    errors = []
    def worker(i):
        try:
            local_s = Storage("concurrent", td)
            local_s.add_domain(task_id=tid, domain=f"sub{i}.x.com", source="crtsh", confidence=50)
        except Exception as e:
            errors.append(f"thread {i}: {e}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    start = time.time()
    for t in threads: t.start()
    for t in threads: t.join()
    elapsed = time.time() - start
    assert len(errors) == 0, f"有 {len(errors)} 个错误:{errors[:3]}"
    rows = s.query("domains", limit=100)
    assert len(rows) == 20, f"应该 20 条,实际 {len(rows)}"
    print(f"  ✓ 20 线程并发写,完成 {elapsed:.2f}s,共 {len(rows)} 条")

# 3. 同一 workspace 大量 task
print("\n[K.3] 同一 workspace 大量 task")
with tempfile.TemporaryDirectory() as td:
    s = Storage("many-tasks", td)
    for i in range(50):
        tid = s.create_task(s.workspace_id, f"target{i}.com")
        s.update_task_status(tid, "DONE")
    rows = s.query("tasks", limit=100)
    assert len(rows) == 50
    print(f"  ✓ 50 task 入库,query 返回 50 条")

# 4. 跨 workspace 任务清理
print("\n[K.4] 跨 workspace 数据库共享")
with tempfile.TemporaryDirectory() as td:
    s1 = Storage("ws1", td)
    s2 = Storage("ws2", td)
    # 两个不同 workspace 共享同一 workspace_root
    # 但 db 文件分别?
    import os
    db1 = f"{td}/ws1/data.db"
    db2 = f"{td}/ws2/data.db"
    assert os.path.exists(db1)
    assert os.path.exists(db2)
    assert db1 != db2
    print(f"  ✓ 不同 workspace 用不同 db 文件:ws1={db1}  ws2={db2}")

# 5. TaskRunner source_status 写库不重复
print("\n[K.5] TaskRunner source_status 去重")
from arl_lite.core.task_runner import TaskRunner
with tempfile.TemporaryDirectory() as td:
    s = Storage("src-dedupe", td)
    runner = TaskRunner(storage=s, workspace_id=s.workspace_id)
    result = asyncio.run(runner.run(target="example.com", modules=["hackertarget", "rapiddns"]))
    sstatus = s.query("source_status")
    print(f"  跑了 2 个 module,source_status 实际入库 {len(sstatus)} 条")
    assert len(sstatus) == 2, f"应该 2 条,实际 {len(sstatus)}"
    print(f"  ✓ source_status 严格 1 module = 1 条")

# 6. CLI 错误处理
print("\n[K.6] CLI exit code 区分")
import subprocess, os
tests = [
    (["python3", "-m", "arl_lite", "version"], 0, "version 成功"),
    (["python3", "-m", "arl_lite", "tools", "check"], 0, "tools check 成功"),
    (["python3", "-m", "arl_lite", "query", "domains"], 0, "query 成功"),
    (["python3", "-m", "arl_lite", "query", "INVALID_TABLE"], 2, "无效表报错"),
    (["python3", "-m", "arl_lite", "query", "domains", "--filter", "drop table x"], 2, "SQL 注入报错"),
    (["python3", "-m", "arl_lite", "diff", "--since", "0d0d0d"], 2, "无效 since 报错"),
    (["python3", "-m", "arl_lite", "nonexistent-cmd"], 2, "无效子命令报错"),
]
for cmd, expected_exit, desc in tests:
    r = subprocess.run(cmd, cwd=str(Path(__file__).parent.parent),
                       env={**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent)},
                       capture_output=True, text=True, timeout=10)
    actual = r.returncode
    if actual == expected_exit:
        print(f"  ✓ {desc}: exit={actual}")
    else:
        print(f"  ✗ {desc}: expect={expected_exit} got={actual}  stderr={(r.stderr or '')[:60]}")

# 7. 长时间稳定性(子域爆破 + 端口扫描连跑)
print("\n[K.7] 短稳定性测试(端到端)")
with tempfile.TemporaryDirectory() as td:
    s = Storage("stability", td)
    runner = TaskRunner(storage=s, workspace_id=s.workspace_id)
    start = time.time()
    result = asyncio.run(runner.run(
        target="example.com",
        modules=["crtsh", "rapiddns", "hackertarget", "portscan", "httpx_probe", "fingerprint"]
    ))
    elapsed = time.time() - start
    print(f"  6 modules 端到端 {elapsed:.1f}s,found={result.found}")
    rows = s.query("source_status")
    print(f"  source_status: {len(rows)} 条")
    assert len(rows) == 6, f"应该 6 条 source_status,实际 {len(rows)}"
    print(f"  ✓ 端到端 6 modules 全跑完")
