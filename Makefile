.PHONY: help install test test1 test2 test3 test-edge test-concurrency clean run query stats version tools correlate monitor risk tui all-in-one

help:
	@echo "arl-lite Makefile v0.7.7"
	@echo ""
	@echo "  make install        安装 arl-lite 本体"
	@echo "  make test           跑全部 9 套测试(phase1-7 + edge + concurrency)"
	@echo "  make test1          Phase 1 基础测试"
	@echo "  make test2          Phase 2 集成测试(需网络)"
	@echo "  make test3          Phase 3 关联/监控/调度/TUI/风险"
	@echo "  make test-edge      边界测试"
	@echo "  make test-concurrency  并发测试"
	@echo "  make clean          清理缓存文件(不动数据)"
	@echo "  make clean-data     删除全部 workspace 数据(危险,需确认)"
	@echo "  make all-in-one     端到端真实网络验证"

install:
	bash install.sh

test: test1 test2 test3 test4 test5 test6 test-edge test-concurrency

test1:
	PYTHONPATH=. python3 tests/test_phase1.py

test2:
	PYTHONPATH=. python3 tests/test_phase2.py

test3:
	PYTHONPATH=. python3 tests/test_phase3.py

test4:
	PYTHONPATH=. python3 tests/test_phase4.py

test5:
	PYTHONPATH=. python3 tests/test_phase5.py

test6:
	PYTHONPATH=. python3 tests/test_phase6.py

test-edge:
	PYTHONPATH=. python3 tests/test_edge.py

test-concurrency:
	PYTHONPATH=. python3 tests/test_concurrency.py

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache
	@echo "[✓] cleaned (缓存已清,数据未动;删数据用 make clean-data)"

# 危险操作:删除所有 workspace 的长期侦察数据,必须显式确认
clean-data:
	@echo "!!! 这将删除 ~/.arl-lite/workspaces 下所有数据(不可恢复) !!!"
	@read -p "输入 yes 确认: " a && [ "$$a" = "yes" ] || { echo "aborted"; exit 1; }
	rm -rf ~/.arl-lite/workspaces
	@echo "[✓] all workspaces deleted"

run:
	PYTHONPATH=. python3 -m arl_lite run -t example.com -m crtsh,rapiddns,hackertarget,portscan,httpx_probe,fingerprint -w smoke

query:
	PYTHONPATH=. python3 -m arl_lite query domains -l 20

stats:
	PYTHONPATH=. python3 -m arl_lite stats

version:
	PYTHONPATH=. python3 -m arl_lite version

tools:
	PYTHONPATH=. python3 -m arl_lite tools check

correlate:
	PYTHONPATH=. python3 -m arl_lite correlate -w smoke --min-risk 5 --limit 20

monitor:
	PYTHONPATH=. python3 -m arl_lite monitor list -w smoke

risk:
	PYTHONPATH=. python3 -m arl_lite risk top -w smoke

tui:
	PYTHONPATH=. python3 -m arl_lite tui -w smoke

all-in-one: clean version tools test
	@echo ""
	@echo "=== 端到端真实网络验证 ==="
	@PYTHONPATH=. python3 -m arl_lite run -t example.com -m crtsh,rapiddns,hackertarget,portscan,httpx_probe,fingerprint -w e2e 2>&1 | tail -10
	@echo ""
	@PYTHONPATH=. python3 -m arl_lite correlate -w e2e --limit 20
	@echo ""
	@PYTHONPATH=. python3 -m arl_lite risk top -w e2e
	@echo ""
	@echo "=== 内存预算 ==="
	@PYTHONPATH=. python3 -c "import resource; \
		import arl_lite.modules.recon.domains_hosts.crtsh; \
		import arl_lite.modules.recon.ports.portscan; \
		import arl_lite.modules.recon.sites.httpx_probe; \
		import arl_lite.modules.recon.fingerprints.fingerprint; \
		print(f'  12 module RSS: {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024:.1f} MB (限额 2048 MB)')"
