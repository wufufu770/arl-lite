# arl-lite 生态调研与并入决策

> 日期：2026-10-02
> 方法：14 个并行 agent 分区深读 HunterX / AtlasX / domain_hunter_pro + 7 份外部调研
> 声明：本报告区分「亲自核实的代码事实」与「agent 结论」两类可信度

---

## 0. 一句话结论

**HunterX 可借鉴但只能移植约 5%；AtlasX 闭源不可并入；真正该做的是修好 arl-lite 自己的三个缺陷（已完成）+ 加 confidence 分级（下一步）。**

---

## 1. 三项目可合并性判定

| 项目 | 许可 | 能否并入 arl-lite | 方式 | 依据 |
|---|---|---|---|---|
| **HunterX** | Apache-2.0 | ⚠️ 仅约 5% | 移植设计 + 少量代码 | 255K 行中仅 3 个模块值得抄 |
| **AtlasX** | 声称 MIT，实为闭源 | ❌ 不能 | 只能黑盒对照 | 仓库仅 10 个部署脚本，零源码 |
| **domain_hunter_pro** | 见仓库 | ✅ 部分可 | 借鉴 CertInfo 机制 | 证书匹配自动排除 |
| **hunterx.club** | 自有 | 独立产品 | 不并入 | 你的 SRC 运营平台 |

### 1.1 AtlasX 为什么不能碰

仓库实际内容：

```
1158B  .env.example        307B  .gitignore
7862B  README.md           660B  docker-compose.lite.yml
2022B  docker-compose.yml  4455B setup.sh
1673B  update.sh           + scripts/ 4 个
```

**真身是 `ghcr.io/yingfff123/atlasx-docker:0.4.1` 镜像。**

- 你 fork 的 10 个文件是部署脚本（作者自己写的，不算镜像内代码）
- 镜像里的 Python 源码**不受任何开源许可覆盖**
- 从镜像扒代码 = 违约
- 作者 README 自述：0.5 后不再维护，1.0 不再公开、要与企业合作

**唯一可做的事**：跑起来当数据源对照系。README 提到 Pro 版有查询 API。

### 1.2 HunterX 的 5% 是哪些

| 模块 | 行数 | 判定 | 理由 |
|---|---|---|---|
| `architecture/` | 2628 | ✅ 抄（砍到 ~1200） | 16 层依赖图 + 守卫，arl-lite 可简化为 5 层 |
| `tests/architecture/` | 2197 | ✅ 抄（只抄 3 个） | 最高性价比，防架构腐化 |
| `vulnerability_capability/` | 2127 | ⚠️ 抄思路不抄码 | 差分信号优先级可直接用 |
| `vulnerability_validation/` | 3440 | ⚠️ 部分 | 方法论可用 |
| `vulnerability_proof/` | 7248 | ❌ 不抄 | 被回环限制卡死，对真实 SRC 目标无效 |
| 其余 240K | — | ❌ | 业务代码与 arl-lite 零重叠 |

**关键约束**：`probe_executor.py` 硬限制只能打回环地址（5 文件 18 处强制点）。
这是设计上杜绝被当攻击武器的正确选择，但代价是**对真实授权目标零验证能力**。

---

## 2. 亲自核实的代码事实

以下 4 条我在本地代码里直接确认过，不是 agent 转述：

### 2.1 ZIP Evidence Package 是假的

`src/hunterx/reporting/report_renderers.py:386-393`：

```python
def render_package(document: ReportDocument) -> str:
    """Render ``document`` as the structured HunterX finding package.

    This is the canonical machine-readable representation of the report
    document.
    """
    return render_json(document)
```

**直接返回 JSON，根本不是 ZIP。** 全仓 grep `graphviz` 零命中，交互式攻击图同样不存在。

### 2.2 知识图谱只有 148 行

```
53   src/hunterx/knowledge/graph.py
95   src/hunterx/infrastructure/db/graph/__init__.py
```

纯内存 `dict[str, dict]`。docstring 写 "A Neo4j adapter would implement the same port"——**该适配器不存在**。

### 2.3 "10 个 agent 团队"是 0 个

`agents/` 共 536 行，全是协议/注册表/基类。`agents/__init__.py:8` 自承："Concrete agents are provided by plugins"——**plugins 里没有具体 agent**。

### 2.4 Docker 默认配置是危险的

`docker-compose.yml:34-52`：

- API 绑 `0.0.0.0:8080`
- `HUNTERX_API_AUTH_ENABLED` 默认 `false`
- `POST /tools/execute`（`api/tools.py:163-174`）接受任意 `tool_id` + 任意 `target`
- **完全绕过 MissionScopeGuard**——scope 只写审计日志，不参与决策

**任何能访问 8080 的人 = 免费自动化攻击服务。**

---

## 3. arl-lite 自身缺陷（已修 3 个）

### 3.1 已修复并提交（commit 2827484）

| 缺陷 | 位置 | 修复 |
|---|---|---|
| 并发无上限 | `task_runner.py:154` | Semaphore 限制同时活跃 module 数（默认 4，env 可调） |
| module 无超时 | `task_runner.py:145` | `asyncio.wait_for` 兜底（默认 180s），卡死降级为失败 |
| prompt 注入无净化 | `ai/prompts.py` | `_sanitize` 递归净化 + 4 个定界标记 |
| watch 状态丢失 | `cli.py:728` / `watcher.py` | 运行态落盘 + 原子替换 + 恢复 |
| async 测试全被跳过 | `pyproject.toml` | 补 `asyncio_mode = "auto"` |

**验证结果**：改动前后测试均为 `9 failed, 45 passed`（同样 9 个 async 测试因缺
pytest-asyncio 被跳过），**零回归**。三项修复各自单独验证通过。

### 3.2 待修（agent 发现，我未改动）

| 严重度 | 位置 | 问题 |
|---|---|---|
| 🟠 | `ai/commands.py:295` | prompt 净化已覆盖 `to_json`，但 `cmd_ai_fix` 单独取 `finding['title']` 需确认是否绕过 |
| 🟡 | `db/storage.py:58-61` | `check_filter_sql` 未禁 `UNION` |
| 🟡 | `db/storage.py:676-682` | 异常分类靠 `if "unique" in msg` 字符串匹配，SQLite 改错误串即失效 |

### 3.3 37 条规则的结构性问题

统计（agent 逐条读完 37 个 YAML）：

| 维度 | 数量 | 比例 |
|---|---|---|
| 单表规则 | 34 | 91.9% |
| 跨表（带 cross_ref） | 3 | 8.1% |
| 有 exclusion | 3 | 8.1% |
| 依赖指纹 title | 21 | 56.8% |
| 用了置信度 | **0** | 0% |
| 要求多轮观测 | **0** | 0% |

**高误报倾向 10 条**（C 档）：`ftp_anonymous`、`smtp_open_relay`、`snmp_public`、
`vnc_with_weak_auth`、`istio_no_auth`、`cdn_bypass`、`multiple_cms_same_ip`、
`multiple_db_same_ip`、`subdomain_count_high`、`wordpress_old_version`

共性：**看到端口/标题就告警，不验证实际可达**。21 条依赖 title 匹配，
而 1896 条指纹库无交叉验证——**指纹误报会被关联规则放大成高危告警**。

---

## 4. 外部调研结论（按性价比排序）

### 4.1 置信度算法（CVSS/EPSS/Nuclei 借鉴）

四因子乘法模型：

```
confidence = base_prior × signal_factor × cross_evidence × temporal_consistency
```

- `base_prior`：规则分档先验（A 档 0.85 / B 档 0.65 / C 档 0.45）
- `signal_factor`：端口双匹配 1.0 / 仅端口开放 0.85 / 单一指纹 0.75 / 模糊匹配 0.50
- `cross_evidence`：有 cross_ref 且满足 1.15 / 无 1.0 / 命中 exclusion 强制 0.0
- `temporal_consistency`：首轮 0.80 / 连续 2 轮 0.95 / 连续 3 轮 1.00

阈值分层：≥0.75 完整报 / 0.50-0.74 降级+标 low-confidence / 0.30-0.49 仅入观察表 / <0.30 丢弃

**多轮观测的数学依据**（base rate fallacy）：
基础率 1%、单次 FP 率 10% 时，precision ≈ 8.7%。
要求 3 轮都命中（假设 FP 独立），precision ≈ 99%。
代价是延迟几个扫描周期——建议「窗口内命中 ≥2/3」容错，控制 recall 损失。

**注意**：AtlasX 的 76% 是 `P(真漏洞 | 已被 SRC 标记为高危)`，
不是工具自身的 precision，**不能直接当 arl-lite 的目标值**。

### 4.2 资产归属（当前几乎空白）

现状：`hosts` 表有 `asn`/`geo_country`/`geo_city` 字段但**没有任何模块写它**；
`httpx_probe.py` 只记 `tls_verified: bool`，**证书 SAN/issuer/fingerprint 全丢弃**。

可合法离线内嵌：

| 数据源 | 大小 | 许可 |
|---|---|---|
| RIPE/APNIC/ARIN/AFRINIC/LACNIC delegated | 7.9+4.2+13+1+3 MB | 公约免费 |
| AWS ip-ranges.json | 2.6 MB | 公开 |
| Cloudflare ips-v4/v6 | 20 段 <1KB | 公开 |
| Fastly public-ip-list | 21 段 | 公开 |
| DB-IP Lite | ~15MB | CC BY 4.0（需署名） |

**许可证红线**：
- ❌ IP2Location LITE 是 **CC BY-SA**（传染性），内嵌会让衍生作品被迫开源
- ❌ CAIDA AS-relationships 是 **CC BY-NC**（禁商用），商业 SRC 服务踩雷
- ⚠️ MaxMind GeoLite2 需保留 attribution 字符串

**国内云厂商是坑**：阿里云/腾讯云/华为云**都没有官方 JSON 端点**（文档页是 SPA），
只能靠社区列表 + 人工季度复核。能自动更新的只有 AWS/Cloudflare/Fastly/Azure/GCP。

### 4.3 变更监控缺失

| 缺陷 | 位置 |
|---|---|
| DISAPPEARED 枚举有、实现无 | `monitor.py:115` 注释"需要 snapshot" |
| 抖动被当变更 | `watcher.py:268` 只做计数差 |
| 6 种 change_type 只填 1 种 | `watcher.py:301` 全填 `NEW_ASSET` |
| `body_hash` 字段有无模块填充 | 内容变更检测走不通 |

去抖方案：5 轮窗口，连续 3 次同状态 + 间隔 ≥2× 周期 → 判为稳定基线；
抖动写 `asset_probes` 留痕但**不入 asset_changes**。

---

## 5. 三个立即可做的下一步

按投入产出比排序：

### 5.1 给 37 条规则加置信度分级（P0，1 天）

**好消息**：`db/schema.sql` **已经有 `confidence INTEGER 0-100` 字段**，
但 `risk_score.py` 只用 `risk`，`correlation_engine.py` 不读 `confidence`——
两套口径完全没打通。**字段已存在，只需接线。**

每条 YAML 加 3 个字段：

```yaml
confidence: 0.85            # base_prior
evidence_strength: strong   # 信号强度
reproducibility: 2          # 至少 N 轮独立观测才报
```

### 5.2 补 3 个架构测试（P0，1 天）

抄 HunterX 的思路，不需要 2197 行：

1. `modules/` 不得 import `core.*` 内部实现
2. 新增 rule 文件必须有 `advice:` 字段
3. `correlation_engine` 不得直接 `import sqlite3`

### 5.3 证书 SAN 入库（P1，1-2 天）

`httpx_probe.py` 用 `ssl.get_server_certificate` 解析，把
`cert_sha256` / `cert_issuer_cn` / `cert_san[]` 写进 `sites` 表。

这补上归属判定最关键的维度，且**不依赖任何外部数据源**。
可直接移植 domain_hunter_pro 的 `CertInfo.java`（303 行）思路。

---

## 6. 许可合规结论

**MIT 收 Apache-2.0 代码合法**，4 项义务：

1. 保留文件顶部 `# SPDX-License-Identifier: Apache-2.0`（HunterX 已自带）
2. 新建 `THIRD_PARTY_NOTICES`
3. 修改过的文件标注 "Modified by arl-lite contributors"
4. 不得对 Apache-2.0 文件加额外限制

**AtlasX 闭源镜像不能碰**。

---

## 7. 未核实事项

以下信息来自 agent 调研，**我未亲自验证**：

- 各类 API 的免费额度数字（FOFA/Hunter/Quake 等）
- ICP 备案接口的反爬强度与法律边界
- 阿里云/腾讯云/华为云 IP 段的社区列表维护活跃度
- 各测绘平台的 ToS 商用条款细节
- EPSS/CVSS 的具体公式细节（引自 FIRST 官方文档，未读全文）
- crt.sh 的确切 QPS 阈值（仅实测到 429/502 现象）

**代码类结论（HunterX/arl-lite 内部）可信度高**——有文件行号锚点，
且关键指控我已亲自复核 4 条。
