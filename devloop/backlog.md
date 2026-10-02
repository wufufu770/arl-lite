# devloop 待办清单（人工追加）

格式：`- [P0..P3] kind: title | detail | verify`

kind 取值：`change` / `test` / `doc` / `research` / `refactor`

这条清单是循环的**长期燃料**。队列耗尽时 `seed_if_empty()` 会先读这里；
这里空了才会退回从代码现状自动推导（`queue.py` 的 Tier 2），
再不行才用固定的长期演进项（Tier 3）。

---

## 已排期

- [P1] change: 给关联分析规则接上置信度 | schema 里已有 `confidence INTEGER 0-100` 但无模块消费；`risk_score.py` 只用 `risk`，`correlation_engine.py` 不读 `confidence`，两套口径没打通 | python3 -m arl_lite.devloop gate rules_have_advice
- [P1] change: 补架构约束测试 | 抄 HunterX 的思路（不必抄它 2197 行）：模块不得反向依赖、rule 必有 advice、correlation_engine 不得直接 import sqlite3 | python3 -m pytest tests/test_devloop.py
- [P2] change: storage 异常分类去字符串化 | `db/storage.py:676-682` 靠 `if "unique" in msg` 判重复键，SQLite 改错误串就失效；应查 `sqlite_errorname` 或捕获具体异常类 | python3 -m pytest tests/
- [P2] change: check_filter_sql 补 UNION 禁令 | `db/storage.py:58-61` 禁了 DROP/DELETE 但没禁 UNION，理论上可绕过表名白名单 | python3 -m pytest tests/
- [P2] change: 证书 SAN/issuer/fingerprint 入库 | `httpx_probe.py` 只记 `tls_verified: bool`，证书归属维度完全空白；用 `ssl.get_server_certificate` 解析，sites 表加 cert_sha256/cert_issuer_cn/cert_san | python3 -m arl_lite.devloop test
- [P2] doc: 重写 PROJECT_PLAN.md | 文档描述的技术栈（typer/textual/litellm/PyYAML/MCP SDK）一个都没用，实现时全推翻了；`doc_freshness` 门禁已报 39 处过期引用 | python3 -m arl_lite.devloop gate doc_freshness
- [P3] change: 实现 DISAPPEARED 变更类型 | `core/monitor.py:115` 枚举里有但没实现，schema 注释写"需要 snapshot"；资产下线收不到通知 | python3 -m pytest tests/
- [P3] research: 误报率实测 | 在受控样本上跑规则集，统计 false positive 率，输出到 docs/FP_RATE.md，作为规则调优的客观输入 | test -f docs/FP_RATE.md

## 候选（待细化）

- 离线内嵌 RIPE/APNIC/ARIN delegated 数据做 IP→ASN 归属（约 16MB，公约免费）
  - 注意：IP2Location LITE 是 CC BY-SA（传染性）、CAIDA 是 CC BY-NC（禁商用），**都不能内嵌**
- CDN/云厂商 IP 段清单（AWS/Cloudflare/Fastly 公开可自动更新；阿里云/腾讯云/华为云**无官方 JSON 端点**，只能人工季度复核）
- 变更监控去抖 + 基线学习（5 轮窗口，连续 3 次同状态判为稳定基线）
