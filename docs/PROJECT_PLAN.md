# arl-lite 项目构建方案

> **目标**:在 2h2g 单人服务器上,做出一个 CLI/TUI 形态的"灯塔 ARL 降级增强版",覆盖 ARL 13 项核心能力,集成 AI 增强(自然语言入口、结果分析、自动报告、MCP 接入)。

> ⚠️ **本文档已与 `COMPETITOR_ANALYSIS.md` 同步**:基于 Aabyss-Team/ARL(⭐2069)+ smicallef/spiderfoot master 源码直读的判决,补了 6 个增量点(BaseModule 3 态、Storage 5 列、SIGTERM、Scope 授权、14 源并列、新增关联分析模块)。

---

## 0. 项目定位

### 一句话

> 借鉴 SpiderFoot 模块基类 + bbot Preset 编排 + Recon-ng Workspace,自己用 Python 写一个 2G 内存友好的、CLI/TUI 形态的、AI 增强版的资产侦察工具。

### 跟 ARL 灯塔的关系

| 维度 | ARL 灯塔 | arl-lite |
|---|---|---|
| 形态 | Web(Vue + Flask) | CLI + TUI(Typer + Textual) |
| 部署 | Docker Compose(5+ 容器) | 单进程(pip install) |
| 存储 | MongoDB + ES + RabbitMQ | SQLite + APScheduler |
| 内存峰值 | 3-4G | < 200M(峰值 1G) |
| 核心能力 | 13 项 | 13 项(完全覆盖) |
| AI 增强 | 无(2025 版加了 MCP) | 原生集成(LiteLLM) |
| MCP | 仅 Web 端集成 | 同时支持 server + client |

---

## 1. 设计原则

**5 条死规矩**:

1. **能 SQL 就不 ES,能 SQLite 就不 MongoDB** — 数据能塞进单文件就单文件
2. **能 subprocess 就不常驻进程** — nmap/subfinder 用完即走
3. **能同步就不异步,必须异步时限制并发** — 2G 机器跑不了 asyncio.gather(*无限个)
4. **AI 增强只在 5 个边界点加,执行层不碰** — nmap 还是 nmap,不加 AI
5. **单人用 = 一个人能维护 = 代码不超 3000 行** — 不为"未来扩展"过度设计

**新增 5 条从竞品源码提炼的死规矩**(基于 `COMPETITOR_ANALYSIS.md` 反面教材):

6. **失败与真空必须可区分** — 每源返回 `{ok, data, error}` 三态,**禁吞错** (ARL 同款坑)
7. **监控必须真 diff,不是定期重扫** — `first_seen` / `last_seen` + 变更事件表(ARL site_monitor 是假监控)
8. **schema 纪律 Phase 1 定死** — 学 ARL 用 Mongo 就没纪律,换 SQLite 必须先定列 + 迁移文件
9. **每源死源状态可见** — 报告如实标哪些源本轮没出力,**零假数据纪律**
10. **Scope 文件带授权窗口期** — `scope.yaml` 含授权凭证号 + 时间窗,合规先于功能

---

## 2. 技术栈

### 核心依赖(全部 pip 装)

| 用途 | 库 | 内存影响 |
|---|---|---|
| CLI 框架 | `typer>=0.9` | < 5M |
| TUI 框架 | `textual>=0.50` | ~30M(空闲) |
| 终端美化 | `rich>=13` | (textual 自带) |
| HTTP 客户端 | `httpx>=0.25` | < 5M(空载) |
| 异步运行时 | `asyncio`(内置) | < 5M |
| 调度器 | `apscheduler>=3.10` | < 10M |
| 持久化 | `sqlite3`(内置) | 0M(系统自带) |
| 配置 | `pyyaml>=6` | < 5M |
| LLM 抽象 | `litellm>=1.50` | < 10M(空载) |
| MCP 协议 | `mcp>=1.0`(官方 SDK) | < 10M |
| 导出 | `openpyxl>=3.1`(Excel) | < 20M(按需加载) |
| 进程查找 | `shutil`(内置) | 0M |

### 外部工具(apt + go install 装)

```bash
# 系统包(apt)
nmap                  # 端口扫描 + 服务识别
massdns               # DNS 爆破
ripgrep               # 文本搜索(在 arl-lite 内置)

# Go 工具(go install)
subfinder             # 子域枚举(ProjectDiscovery)
httpx                 # 站点探测(ProjectDiscovery)
nuclei                # 漏洞扫描(ProjectDiscovery)
katana                # Web 爬虫(ProjectDiscovery)

# Python 工具(pipx)
ehole                 # 指纹识别(国内 CMS 强)
```

### 内存总账(2G 服务器)

```
系统占用:           ~250M
Python 运行时:        ~30M
arl-lite 进程(空闲):  ~100M  (TUI + APScheduler)
外部工具(按需):
  - nmap 运行时峰值:  +300M
  - httpx 运行时:     +50M
  - nuclei 运行时:    +200M(可降到 50M)
  - Ollama 7B 量化:   +800M(可选)
────────────────────────────────────
峰值:                ~1.7G ✅
日常空闲:            ~400M ✅
```

---

## 3. 项目骨架(完整目录树)

```
arl-lite/
├── pyproject.toml              # 包定义 + 依赖
├── README.md                   # 入口文档
├── LICENSE                     # MIT
├── Makefile                    # 常用命令快捷方式
├── install.sh                  # 一键安装脚本
│
├── arl_lite/                   # 主包
│   ├── __init__.py            # 版本号
│   ├── __main__.py            # python -m arl_lite
│   │
│   ├── cli.py                 # Typer CLI 入口
│   ├── tui.py                 # Textual TUI 入口
│   ├── config.py              # 配置加载(YAML)
│   │
│   ├── core/                  # 核心引擎
│   │   ├── __init__.py
│   │   ├── base_module.py    # 模块基类(借鉴 SpiderFoot)
│   │   ├── workspace.py      # workspace 管理
│   │   ├── task.py           # 任务调度
│   │   ├── scheduler.py      # APScheduler 封装
│   │   ├── agent.py          # AI Agent Loop(借鉴 OpenClaude)
│   │   ├── state.py          # 全局状态
│   │   └── logger.py         # 日志(rich)
│   │
│   ├── db/                    # 数据层
│   │   ├── __init__.py
│   │   ├── schema.sql        # SQLite schema + FTS5
│   │   ├── storage.py        # 存储封装
│   │   ├── migrations.py     # 迁移管理
│   │   └── seed.py           # 指纹库种子数据
│   │
│   ├── modules/               # 安全模块(借鉴 Recon-ng 命名)
│   │   ├── __init__.py
│   │   │
│   │   ├── recon/            # 主动/被动侦察
│   │   │   ├── __init__.py
│   │   │   ├── domains-hosts/        # 域 → 主机
│   │   │   │   ├── subfinder.py
│   │   │   │   ├── crtsh.py
│   │   │   │   ├── massdns_brute.py
│   │   │   │   └── rapiddns.py
│   │   │   ├── hosts-ports/          # 主机 → 端口
│   │   │   │   ├── nmap.py
│   │   │   │   └── naabu.py
│   │   │   ├── hosts-services/       # 主机 → 服务
│   │   │   │   ├── httpx.py
│   │   │   │   ├── ehole.py
│   │   │   │   └── nmap_service.py
│   │   │   └── hosts-vulns/          # 主机 → 漏洞
│   │   │       └── nuclei.py
│   │   │
│   │   ├── discovery/        # 主动发现
│   │   │   ├── info-disclosure/      # 信息泄漏
│   │   │   │   ├── dirsearch.py
│   │   │   │   └── nuclei_leak.py
│   │   │   ├── takeovers/            # 子域接管
│   │   │   │   └── nuclei_takeover.py
│   │   │   └── cidr/                 # C 段扫描
│   │   │       └── nmap_cidr.py
│   │   │
│   │   ├── monitoring/        # 周期任务
│   │   │   ├── diff.py               # 资产 diff
│   │   │   ├── site_change.py        # 站点变化
│   │   │   └── github_monitor.py     # GitHub 监控
│   │   │
│   │   ├── reporting/         # 报告输出
│   │   │   ├── csv_exporter.py
│   │   │   ├── json_exporter.py
│   │   │   ├── excel_exporter.py
│   │   │   └── html_reporter.py
│   │   │
│   │   └── registry.py        # 模块自动发现 + 注册
│   │
│   ├── ai/                    # AI 集成层
│   │   ├── __init__.py
│   │   ├── llm.py             # LiteLLM 封装
│   │   ├── router.py          # Agent routing
│   │   ├── tools_spec.py      # Tool schemas
│   │   ├── analyzer.py        # 结果智能分析
│   │   ├── reporter.py        # AI 报告生成
│   │   ├── explainer.py       # 漏洞解释
│   │   ├── nl_parser.py       # 自然语言 → 任务
│   │   ├── prompts/           # Prompt 模板
│   │   │   ├── analyze.txt
│   │   │   ├── report.txt
│   │   │   └── explain.txt
│   │   └── mcp_server.py      # arl-lite 变 MCP server
│   │
│   ├── integrations/          # 外部工具适配
│   │   ├── __init__.py
│   │   ├── subfinder.py       # subprocess 调 subfinder
│   │   ├── nmap.py            # subprocess 调 nmap
│   │   ├── httpx.py           # subprocess 调 httpx
│   │   ├── nuclei.py          # subprocess 调 nuclei
│   │   ├── ehole.py           # subprocess 调 ehole
│   │   ├── crtsh.py           # httpx 调 crt.sh API
│   │   └── tool_checker.py    # 工具可用性检查
│   │
│   ├── tui/                   # TUI 组件
│   │   ├── __init__.py
│   │   ├── app.py             # Textual App
│   │   ├── screens/
│   │   │   ├── dashboard.py   # 仪表盘
│   │   │   ├── assets.py      # 资产浏览
│   │   │   ├── tasks.py       # 任务管理
│   │   │   ├── monitor.py     # 监控中心
│   │   │   └── ai_chat.py     # AI 对话
│   │   └── widgets.py
│   │
│   └── utils/                 # 工具函数
│       ├── __init__.py
│       ├── cidr.py            # CIDR 解析
│       ├── dns.py             # DNS 查询
│       ├── validators.py      # 验证器
│       └── formatters.py      # 输出格式化
│
├── presets/                   # 任务策略(借鉴 bbot Preset)
│   ├── subdomain-enum.yml
│   ├── port-scan.yml
│   ├── full-recon.yml
│   ├── web-fingerprint.yml
│   └── vuln-scan.yml
│
├── fingerprints/              # 指纹库(精简版)
│   ├── web_fingerprints.json  # 1k 条核心指纹
│   └── nuclei_overrides.yml   # nuclei 模板覆盖
│
├── tests/                     # 测试
│   ├── test_storage.py
│   ├── test_subfinder.py
│   ├── test_nmap.py
│   └── test_agent.py
│
└── scripts/                   # 运维脚本
    ├── install_deps.sh        # 装外部工具
    ├── seed_fingerprints.py   # 灌指纹库
    └── reset_db.sh            # 重置数据库
```

---

## 4. 核心抽象(6 个)

### 抽象 1:`BaseModule`(模块基类)

**位置**:`arl_lite/core/base_module.py`
**借鉴**:SpiderFoot `sflib.SFLib`
**作用**:所有安全模块的父类,统一接口

```python
from abc import ABC, abstractmethod
from typing import Any, Optional
from dataclasses import dataclass, field

@dataclass
class ModuleResult:
    """模块执行结果(统一格式)"""
    success: bool
    target: str
    found: int
    duration_seconds: float
    errors: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

class BaseModule(ABC):
    """所有 arl-lite 模块的基类"""

    # 元信息(子类必须覆盖)
    name: str = ""
    category: str = ""            # recon/domains-hosts, discovery/info-disclosure...
    description: str = ""
    author: str = "arl-lite"
    version: str = "1.0"

    # 输入/输出声明
    input_type: str = ""          # "domain" | "host" | "ip" | "url"
    output_type: str = ""         # 同上

    # 依赖工具
    required_tools: list = field(default_factory=list)  # ["subfinder", "nmap"]

    # 配置(子类可扩展)
    config: dict = field(default_factory=dict)

    def __init__(self, task_id: int = 0, workspace: str = "default"):
        self.task_id = task_id
        self.workspace = workspace

    def pre_check(self) -> tuple[bool, str]:
        """运行前检查:工具是否齐全"""
        from ..integrations.tool_checker import check_tools
        ok, msg = check_tools(self.required_tools)
        if not ok:
            return False, f"工具缺失: {msg}"
        return True, ""

    @abstractmethod
    async def run(self, target: str, **kwargs) -> ModuleResult:
        """核心逻辑,子类必须实现"""
        pass

    def post_process(self, result: ModuleResult) -> ModuleResult:
        """后处理钩子(默认 noop)"""
        return result

    # 数据写入快捷方法
    def add_domain(self, domain: str, source: str = None):
        from ..db import storage
        storage.add_domain(self.task_id, domain, source or self.name)

    def add_host(self, host: str, ip: str = None):
        from ..db import storage
        storage.add_host(self.task_id, host, ip)

    def add_port(self, ip: str, port: int, service: str = None, banner: str = None):
        from ..db import storage
        storage.add_port(ip, port, service, banner)

    def add_site(self, url: str, host: str, ip: str, port: int,
                 scheme: str, title: str, status_code: int, tech: str = None):
        from ..db import storage
        storage.add_site(self.task_id, url, host, ip, port, scheme, title, status_code, tech)

    def add_finding(self, finding_type: str, target: str, data: dict,
                    severity: str = "info"):
        from ..db import storage
        storage.add_finding(self.task_id, self.name, finding_type, target, data, severity)
```

### 抽象 2:`Storage`(存储层)

**位置**:`arl_lite/db/storage.py`
**借鉴**:SpiderFoot SQLite + FTS5
**作用**:所有数据访问的统一入口

```python
# 核心表(arl_lite/db/schema.sql)
#
# CREATE TABLE tasks (
#   id INTEGER PRIMARY KEY,
#   name TEXT, target TEXT, modules TEXT,  -- modules 是 JSON 数组
#   status TEXT, created_at TIMESTAMP, finished_at TIMESTAMP
# );
#
# CREATE TABLE domains (
#   id INTEGER PRIMARY KEY, task_id INTEGER, domain TEXT UNIQUE,
#   source TEXT, resolved_ip TEXT, discovered_at TIMESTAMP
# );
#
# CREATE TABLE hosts (
#   id INTEGER PRIMARY KEY, task_id INTEGER, host TEXT, ip TEXT,
#   discovered_at TIMESTAMP
# );
#
# CREATE TABLE ports (
#   id INTEGER PRIMARY KEY, ip TEXT, port INTEGER,
#   service TEXT, version TEXT, banner TEXT,
#   UNIQUE(ip, port)
# );
#
# CREATE TABLE sites (
#   id INTEGER PRIMARY KEY, task_id INTEGER, url TEXT UNIQUE,
#   host TEXT, ip TEXT, port INTEGER, scheme TEXT,
#   title TEXT, status_code INTEGER, content_length INTEGER,
#   tech TEXT, body_hash TEXT, discovered_at TIMESTAMP
# );
#
# CREATE TABLE findings (
#   id INTEGER PRIMARY KEY, task_id INTEGER, module TEXT,
#   type TEXT, target TEXT, data TEXT, severity TEXT,
#   discovered_at TIMESTAMP
# );
#
# CREATE TABLE monitors (
#   id INTEGER PRIMARY KEY, target TEXT, interval_seconds INTEGER,
#   last_run TIMESTAMP, enabled INTEGER DEFAULT 1
# );
#
# CREATE TABLE schedules (
#   id INTEGER PRIMARY KEY, name TEXT, cron TEXT, task_config TEXT, enabled INTEGER
# );
#
# CREATE VIRTUAL TABLE sites_fts USING fts5(
#   url, title, tech, content='sites', content_rowid='id'
# );
```

### 抽象 3:`Workspace`(工作空间)

**位置**:`arl_lite/core/workspace.py`
**借鉴**:Recon-ng workspace
**作用**:每个目标一个独立数据库,任务互不污染

```python
# 工作空间目录: ~/.arl-lite/workspaces/<name>/
#   ├── data.db          # SQLite
#   ├── logs/            # 任务日志
#   ├── results/         # 原始输出(nmap XML 等)
#   └── config.yaml      # 该 workspace 的配置
```

### 抽象 4:`Preset`(任务策略)

**位置**:`presets/*.yml`
**借鉴**:bbot Preset + Osmedeus Workflow
**作用**:把 ARL 灯塔的 22 个开关变成 YAML

```yaml
# presets/full-recon.yml
name: full-recon
description: 完整资产侦察流程
flags: [recon]
include:
  - subdomain-enum
  - port-scan
  - web-fingerprint
modules:
  subfinder:
    timeout: 30
  nmap:
    top_ports: 1000
    service_detect: true
  httpx:
    threads: 50
  ehole: {}
output:
  format: [csv, json, html]
  notify: []
```

### 抽象 5:`Agent`(AI 代理)

**位置**:`arl_lite/core/agent.py`
**借鉴**:OpenClaude QueryEngine(精简版)
**作用**:自然语言 → 任务编排

```python
class Agent:
    """轻量 AI Agent Loop"""

    def __init__(self, llm: "LLM", tools: list[dict]):
        self.llm = llm
        self.tools = tools

    async def run(self, user_input: str) -> str:
        """自然语言 → 工具调用 → 总结"""
        messages = [{"role": "user", "content": user_input}]

        for i in range(5):  # 最多 5 轮工具调用
            response = await self.llm.chat(messages, tools=self.tools)

            if response.finish_reason == "stop":
                return response.content

            if response.finish_reason == "tool_calls":
                # 执行工具调用
                for tool_call in response.tool_calls:
                    result = await self._execute_tool(tool_call)
                    messages.append({"role": "tool", "content": result})
```

### 抽象 6:`MCPServer`(MCP 协议)

**位置**:`arl_lite/ai/mcp_server.py`
**作用**:让 arl-lite 变成可被 Cursor/Claude 调用的 MCP server

```python
# 暴露 4 个工具给 AI agent:
#   arl_scan_subdomains(domain)
#   arl_scan_ports(target)
#   arl_query_assets(filter)
#   arl_run_nuclei(target, severity)
```

---

## 5. 数据流

### 单次任务流程

```
用户输入(CLI / 自然语言 / Preset)
   ↓
[1] 加载 Workspace,创建 Task 记录
   ↓
[2] Task 解析为 Module 列表(可串行/并行)
   ↓
[3] APScheduler 调度执行
   ↓
[4] 每个 Module:
      pre_check() → 验证工具
      run(target) → 调用外部工具
      post_process() → 解析输出
      写入 SQLite(storage.add_xxx)
   ↓
[5] 任务完成,触发 AI 分析(可选)
   ↓
[6] 生成报告 + 通知
```

### 周期监控流程

```
APScheduler cron 触发
   ↓
读取 monitors 表,过滤 enabled=1
   ↓
对每个 monitor:
   - 跑对应模块
   - 与上次结果 diff
   - 新增资产 → 通知(可选 AI 解读)
   - 更新 last_run
```

### AI 增强流程(自然语言入口)

```
用户: "帮我扫 example.com 的子域,跑高危 nuclei"
   ↓
Agent.run(user_input)
   ↓
LiteLLM → 返回 tool_calls:
   - arl_scan_subdomains(domain="example.com")
   - arl_run_nuclei(target=<subdomains>, severity="high,critical")
   ↓
执行工具调用,收集结果
   ↓
LiteLLM → 总结:"扫到 47 个子域,其中 12 个有高危漏洞,最严重的是..."
   ↓
返回给用户(CLI 输出 / TUI 展示 / MCP 响应)
```

---

## 6. 功能矩阵(对比 ARL 灯塔 13 项)

| 编号 | ARL 能力 | arl-lite 实现 | 工具/库 | 内存影响 |
|---|---|---|---|---|
| 1 | 域名资产发现 | `SubfinderModule` | subfinder | +50M |
| 2 | 字典爆破子域 | `MassdnsBruteModule` | massdns | +30M |
| 3 | crt.sh 证书查询 | `CrtshModule`(直接 API) | httpx | +10M |
| 4 | IP/IP 段资产 | `NmapCIDRModule` | nmap | +300M |
| 5 | 端口扫描 | `NmapPortModule` | nmap | +300M |
| 6 | 服务识别 | `NmapServiceModule` | nmap -sV | +300M |
| 7 | Web 站点指纹 | `HttpxModule` + `EholeModule` | httpx + ehole | +50M |
| 8 | 站点爬虫 | `KatanaModule` | katana | +100M |
| 9 | 文件泄漏 | `DirsearchModule` | 字典 + httpx | +30M |
| 10 | nuclei PoC | `NucleiModule`(按需) | nuclei | +200M(可降) |
| 11 | GitHub 监控 | `GitHubMonitorModule` | httpx + API | +10M |
| 12 | 资产监控 + diff | `DiffModule` | SQLite 对比 | +5M |
| 13 | 计划任务 | APScheduler cron | apscheduler | +10M |
| ➕ | **AI 增强**(原 ARL 没有) | LiteLLM + MCP | litellm | +10M(空载) |
| ➕ | **自然语言入口** | Agent Loop | litellm | 同上 |
| ➕ | **MCP server**(可被 AI 调用) | 官方 MCP SDK | mcp | +10M |

**结论**:13 项核心能力 100% 覆盖,加 3 项 AI 增强。

---

## 7. 实施路线图(5 个阶段)

### Phase 1:MVP 跑通(目标 1-2 天,~600 行)

**目标**:`arl-lite run -t example.com` 跑出子域名

**交付物**:
- 项目骨架(目录 + 配置文件)
- `core/base_module.py`(BaseModule)
- `db/schema.sql` + `db/storage.py`(SQLite)
- `integrations/subfinder.py`(subprocess 包装)
- `integrations/crtsh.py`(httpx 调 API)
- `modules/recon/domains-hosts/subfinder.py`
- `modules/recon/domains-hosts/crtsh.py`
- `cli.py`(Typer 跑 `arl-lite run -t xxx`)
- `install.sh`(一键装依赖)
- `pyproject.toml` + `README.md`

**验收**:
```bash
arl-lite run -t example.com --modules subfinder,crtsh
# 看到子域列表
arl-lite query domains --workspace default
# 看到所有结果
```

### Phase 2:端口扫描 + 服务识别(目标 +1-2 天,~1200 行)

**目标**:`arl-lite run -t example.com --full` 跑出子域 + 端口 + 服务

**交付物**:
- `integrations/nmap.py`(subprocess + XML 解析)
- `modules/recon/hosts-ports/nmap.py`
- `modules/recon/hosts-services/nmap_service.py`
- `modules/recon/hosts-services/httpx.py`
- `core/task.py`(多模块串行调度)
- `presets/port-scan.yml`
- `presets/full-recon.yml`

**验收**:
```bash
arl-lite run -t example.com --preset full-recon
# 跑完后:
arl-lite query sites --workspace example.com --filter "port:443"
arl-lite export csv --workspace example.com
```

### Phase 3:TUI + 监控(目标 +1-2 天,~1800 行)

**目标**:能交互、能监控、能 diff

**交付物**:
- `tui/app.py`(Textual 主界面)
- `tui/screens/{dashboard,assets,tasks,monitor}.py`
- `core/scheduler.py`(APScheduler 集成)
- `modules/monitoring/diff.py`
- `modules/monitoring/site_change.py`
- `core/workspace.py`(workspace 切换)
- `cli.py` 补全 `monitor` 子命令

**验收**:
```bash
arl-lite tui
# 进 TUI,能浏览资产、看任务、配置监控

arl-lite monitor add --target example.com --interval 24h
arl-lite monitor diff --target example.com
# 看到新增资产
```

### Phase 4:AI 增强(目标 +1-2 天,~2400 行)

**目标**:自然语言入口 + 智能分析 + 报告生成

**交付物**:
- `ai/llm.py`(LiteLLM 封装)
- `ai/router.py`(agent routing)
- `ai/tools_spec.py`(Tool schemas)
- `ai/analyzer.py`(结果智能分析)
- `ai/reporter.py`(报告生成)
- `ai/explainer.py`(漏洞解释)
- `ai/nl_parser.py`(自然语言 → 任务)
- `ai/prompts/*.txt`(prompt 模板)
- `core/agent.py`(Agent Loop)
- `config/ai-config.yaml`(provider 配置)

**验收**:
```bash
# 自然语言入口
arl-lite ask "扫 example.com 的子域,标出高价值的"
# AI 返回子域列表 + 标注

# 智能分析
arl-lite analyze --workspace example.com
# AI 总结:8 个高优先级、12 个中优先级

# 报告生成
arl-lite report --workspace example.com --format html --audience boss
# 生成给老板看的报告

# 漏洞解释
arl-lite explain CVE-2024-1234
# 中文解释 + 复现步骤
```

### Phase 5:漏洞扫描 + MCP + 完善(目标 +1-2 天,~3000 行)

**目标**:补 nuclei、补 MCP、文档化

**交付物**:
- `integrations/nuclei.py`(nuclei 集成)
- `modules/recon/hosts-vulns/nuclei.py`
- `modules/discovery/info-disclosure/dirsearch.py`
- `modules/monitoring/github_monitor.py`
- `ai/mcp_server.py`(arl-lite 变 MCP server)
- `modules/registry.py`(自动发现)
- 完整文档、测试、Docker(可选)

**验收**:
```bash
# nuclei 集成
arl-lite nuclei -t urls.txt --severity high,critical
# 看到 PoC 结果

# MCP server 启动
arl-lite mcp-serve
# 在 Cursor 里能调 arl-lite 的工具

# GitHub 监控
arl-lite monitor add --type github --keyword "example.com"
# 监控 GitHub 上的代码泄漏
```

**总规模**:**~3000 行 Python + ~500 行 YAML + 200 行 SQL**。

---

## 8. 关键代码示例

### CLI 入口(`cli.py`)

```python
import typer
from pathlib import Path
from rich.console import Console

app = typer.Typer(help="arl-lite: 灯塔 ARL 降级增强版,2G 内存友好的资产侦察工具")
console = Console()

@app.command()
def run(
    target: str = typer.Option(..., "-t", "--target", help="目标(域名/IP/URL)"),
    preset: str = typer.Option(None, "-p", "--preset", help="任务策略"),
    modules: str = typer.Option(None, "-m", "--modules", help="模块列表(逗号分隔)"),
    workspace: str = typer.Option("default", "-w", "--workspace"),
    diff: bool = typer.Option(False, "--diff", help="与上次结果对比"),
):
    """运行扫描任务"""
    from .core.task import TaskRunner
    runner = TaskRunner(workspace=workspace)
    runner.run(target=target, preset=preset, modules=modules, diff=diff)

@app.command()
def tui():
    """启动 TUI 界面"""
    from .tui import run_tui
    run_tui()

@app.command()
def ask(
    question: str = typer.Argument(..., help="自然语言问题"),
):
    """AI 自然语言入口"""
    from .ai import run_ask
    run_ask(question)

@app.command()
def monitor(
    action: str = typer.Argument(..., help="add/list/diff/remove"),
    target: str = typer.Option(None, "-t"),
    interval: str = typer.Option("24h", "-i", "--interval"),
):
    """监控管理"""
    from .core.scheduler import run_monitor_cmd
    run_monitor_cmd(action, target, interval)

@app.command()
def query(
    table: str = typer.Argument(..., help="domains/hosts/ports/sites/findings"),
    workspace: str = typer.Option("default", "-w"),
    filter: str = typer.Option(None, "-f", "--filter"),
    limit: int = typer.Option(50, "-l", "--limit"),
):
    """查询资产"""
    from .db.storage import Storage
    s = Storage(workspace=workspace)
    results = s.query(table, filter=filter, limit=limit)
    # Rich 表格输出...

if __name__ == "__main__":
    app()
```

### 一个完整的 Module(子域枚举)

```python
# arl_lite/modules/recon/domains-hosts/subfinder.py
import asyncio
import json
import tempfile
from pathlib import Path
from ....core.base_module import BaseModule, ModuleResult
from ....integrations.subfinder import run_subfinder

class SubfinderModule(BaseModule):
    name = "subfinder"
    category = "recon/domains-hosts"
    description = "通过 subfinder 枚举子域名(聚合多个被动数据源)"
    input_type = "domain"
    output_type = "domain"
    required_tools = ["subfinder"]

    async def run(self, target: str, **kwargs) -> ModuleResult:
        # 1. 调用 subfinder
        subs, errors = await run_subfinder(target, timeout=30)
        # 2. 入库
        for sub in subs:
            self.add_domain(sub, source="subfinder")
        # 3. 返回结果
        return ModuleResult(
            success=len(errors) == 0,
            target=target,
            found=len(subs),
            duration_seconds=...,
            errors=errors,
            metadata={"source": "subfinder", "sample": subs[:10]}
        )
```

### AI 入口(自然语言 → 任务)

```python
# arl_lite/ai/nl_parser.py
import litellm
from .llm import LLM
from .tools_spec import TOOL_SPECS

SYSTEM_PROMPT = """你是 arl-lite 的 AI 助手,负责把用户的自然语言指令转成工具调用。

可用工具:
- arl_scan_subdomains(domain): 扫描子域
- arl_scan_ports(target): 扫描端口
- arl_run_nuclei(target, severity): 跑 nuclei PoC
- arl_query_assets(filter): 查询已有资产

根据用户指令,选择并调用合适的工具,1 个或多个。"""

async def run_ask(question: str, workspace: str = "default"):
    llm = LLM()  # LiteLLM 包装

    # Step 1: LLM 选工具
    response = await llm.chat(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question}
        ],
        tools=TOOL_SPECS
    )

    # Step 2: 执行工具
    results = []
    for tool_call in response.tool_calls:
        result = await execute_tool(tool_call, workspace=workspace)
        results.append(result)

    # Step 3: LLM 总结
    summary = await llm.chat(
        messages=[
            {"role": "system", "content": "请用简洁的中文总结扫描结果,突出重点。"},
            {"role": "user", "content": f"原始问题:{question}\n扫描结果:{results}"}
        ]
    )

    print(summary.content)
```

---

## 9. 部署方案

### 一键安装脚本(`install.sh`)

```bash
#!/bin/bash
set -e

echo "==> 1. 安装系统依赖"
sudo apt update
sudo apt install -y nmap massdns python3-pip ripgrep

echo "==> 2. 安装 Go 工具"
go install -v github.com/projectdiscovery/subfinder/v2/cmd/subfinder@latest
go install -v github.com/projectdiscovery/httpx/cmd/httpx@latest
go install -v github.com/projectdiscovery/nuclei/v3/cmd/nuclei@latest
go install -v github.com/projectdiscovery/katana/cmd/katana@latest
export PATH=$PATH:~/go/bin

echo "==> 3. 安装 Python 工具"
pipx install ehole 2>/dev/null || pip install --user ehole

echo "==> 4. 安装 arl-lite"
pip install -e .

echo "==> 5. 初始化数据库"
arl-lite init

echo "==> 6. 验证"
arl-lite --version
arl-lite tools check
```

### 系统要求

| 项 | 最低 | 推荐 |
|---|---|---|
| 系统 | Ubuntu 20.04+ / Debian 11+ | Ubuntu 22.04 |
| Python | 3.10 | 3.11+ |
| 内存 | 1G(纯工具) | 2G(完整 + TUI) |
| 磁盘 | 1G | 5G(含 nuclei 模板) |
| 网络 | 能访问公网 | 稳定带宽 |

---

## 10. 命令速查(用户视角)

```bash
# 跑扫描
arl-lite run -t example.com
arl-lite run -t example.com --preset full-recon
arl-lite run -t example.com --modules subfinder,nmap
arl-lite run -t 1.2.3.0/24 --modules nmap_cidr

# 查询资产
arl-lite query domains
arl-lite query ports --filter "port:443"
arl-lite query sites --filter "title:后台"
arl-lite export json --workspace example.com > result.json

# TUI
arl-lite tui

# 监控
arl-lite monitor add -t example.com -i 24h
arl-lite monitor diff -t example.com
arl-lite monitor list

# AI 入口
arl-lite ask "扫 example.com 的子域,标出高价值的"
arl-lite analyze -w example.com
arl-lite explain CVE-2024-1234
arl-lite report -w example.com --format html

# 配置
arl-lite config set ai.provider ollama
arl-lite config set ai.model qwen2.5-coder:7b
arl-lite config set fofa.token xxxxx
arl-lite config show

# 工具管理
arl-lite tools check
arl-lite tools install

# MCP
arl-lite mcp-serve
```

---

## 11. 风险与权衡

| 决策 | 选择 | 替代方案 | 理由 |
|---|---|---|---|
| 不 fork SpiderFoot | 自己写 | fork cbxss/spiderfoot | 许可清晰、代码精简(3000 行 vs 19k stars) |
| 不用 MongoDB | SQLite | 保留 Mongo | 2G 内存不够,SQLite FTS5 够用 |
| 不用 masscan | 纯 nmap | 加 masscan | masscan 瞬时吃 500M+,对小目标杀鸡用牛刀 |
| 不用 Web UI | CLI + TUI | 保留 Web | 2G 跑 Flask 吃力,Textual TUI 完全够用 |
| 不用 Elasticsearch | SQLite FTS5 | 保留 ES | ES 1G 起步,小规模全文搜索不需要 |
| 不用 Celery | APScheduler | 保留 Celery | 单机场景 APScheduler 足够,Celery 重型 |
| TUI 优先 Web | Textual | 保留 Web UI | 单人用 TUI 体验更好,响应更快 |
| **Source 3 态返回** | `{ok, data, error}` | 吞错返回空 | **禁 ARL 同款坑**(COMPETITOR_ANALYSIS #1) |
| **SIGTERM 优雅停止** | 必须实现 | 让进程被强杀 | **生产化分水岭**(抄 ARL celerytask.py) |
| **Scope 授权窗口期** | `scope.yaml` + 检查 | 不检查直接扫 | 合规先于功能,无授权凭证不执行 |
| **真 diff 而非定期重扫** | first_seen/last_seen + 变更事件表 | 定期重扫结果对比 | ARL 假监控是反面教材 |
| **指纹直接复用** | ARL 1917 条 + Wappalyzer 扩展 | 自写规则库 | 省 500 行 |
| **关联分析抄 SF** | 30 条规则 + 50 行执行器 | 不做 | 原文方案漏,补上 |
| **AI Phase 1 只做 1 个边界点** | 分析归纳 | 5 个一起做 | 同类无 AI 先例,自证纪律 |

---

## 12. 验收标准(Phase 1 跑通)

完成后,在 2G 服务器上执行:

```bash
# 1. 安装
./install.sh

# 2. 跑一个任务
arl-lite run -t example.com --modules subfinder,crtsh
# 看到:
#   [+] subfinder: 47 subdomains found
#   [+] crtsh: 23 subdomains found
#   Total: 52 unique subdomains

# 3. 查询
arl-lite query domains --workspace default --limit 10
# 表格展示 10 个子域

# 4. 导出
arl-lite export json --workspace default > assets.json
# 拿到 JSON

# 5. 内存
ps aux | grep arl-lite | grep -v grep
# RSS < 200M ✅
```

跑通即可进入 Phase 2。

---

## 13. 后续路线(Phase 5 之后)

- **Phase 6**:Web UI(可选,FastAPI + htmx,1G 内存能跑)
- **Phase 7**:插件市场(社区模块)
- **Phase 8**:分布式(AWS Lambda 调用 nmap)
- **Phase 9**:漏洞知识库 + AI 自动写 PoC

---

**总计**:5 个阶段,~3000 行 Python,目标 2 周出 v1.0。

---

## 14. 竞品源码对照增量(2026-09-06)

> **来源**:Aabyss-Team/ARL(⭐2069)+ smicallef/spiderfoot master 源码直读
> **详细分析**:`docs/COMPETITOR_ANALYSIS.md`
> **原则**:文档叙事不算数,只认代码锚点;同类踩的坑显式规避,可白嫖资产直接复用

### 14.1 增量总览(6 项,源自竞品源码)

| # | 增量点 | 来源 | 优先级 |
|---|---|---|---|
| 1 | BaseModule 改 3 态返回 `{ok, data, error}` | 规避 ARL `DNSQueryBase.query()` 吞错坑 | **P0 / Phase 1** |
| 2 | Storage schema 加 5 列:`hash` / `confidence` / `risk` / `source_hash` / `first_seen` / `last_seen` | SF `db.py:74` 列设计 + 监控 diff 地基 | **P0 / Phase 1** |
| 3 | **SIGTERM 优雅停止** + 任务状态回写 STOP + end_time | 抄 ARL `celerytask.py` `sigterm_handler` | **P0 / Phase 1**(生产化分水岭) |
| 4 | **Scope 文件**带授权凭证号 + 窗口期 | 规避 ARL scope 只有采集语义无授权语义 | **P1 / Phase 2** |
| 5 | 子域信源改 **14 源并列** + 死源状态可见 | 抄 ARL `dns_query_plugin/` 信源冗余哲学 | **P1 / Phase 2** |
| 6 | 指纹**直接复用 ARL 1917 条**(webapp.json)+ Wappalyzer 扩展 | 抄 ARL 现成资产,引擎 ~300 行 | **P1 / Phase 2** |
| ➕ | **⭐ 新增关联分析模块**(原方案盲区) | 抄 SF `correlations/*.yaml` 30 条 + 50 行执行器 | **P2 / Phase 3** |
| 7 | AI Phase 1 只做 1 个边界点验证(分析归纳) | 同类产品无 AI 先例,自证纪律 | **P2 / Phase 3** |

### 14.2 增量 #1:BaseModule 3 态返回(禁吞错)

**反面教材**:ARL `dns_query_plugin/base.py` 的 `query()` 写了 `except Exception → return []`——失败与真空不可区分。**arl-lite 显式规避**:

```python
# arl_lite/core/base_module.py
from typing import Generic, TypeVar
from dataclasses import dataclass

T = TypeVar("T")

@dataclass
class SourceResult(Generic[T]):
    """每源统一返回格式——三态"""
    ok: bool                        # True=有数据,False=无数据(可能是失败)
    data: list[T]                   # 实际数据
    error: str | None = None        # 失败原因,ok=False 时必须有值
    source: str = ""                # 数据来源标识
    duration: float = 0.0           # 耗时

    def __bool__(self) -> bool:
        """让 `if result:` 判断是否有数据(不是判断 ok)"""
        return self.ok and len(self.data) > 0

# 子类用法(crtsh.py)
class CrtshModule(BaseModule):
    async def run(self, target: str) -> SourceResult[Domain]:
        try:
            subs = await self._fetch_from_crtsh(target)
            return SourceResult(ok=True, data=subs, source="crtsh")
        except httpx.TimeoutException as e:
            return SourceResult(ok=False, data=[], error=f"timeout: {e}", source="crtsh")
        except Exception as e:
            return SourceResult(ok=False, data=[], error=f"unknown: {e}", source="crtsh")
```

**纪律**:禁止 `except: pass` / `except: return []` / `except: return None`。任何吞错必须改 `SourceResult(ok=False, error=...)`。

### 14.3 增量 #2:Storage schema 加 5 列

**抄 SF `db.py:74`** 的列设计 + 补监控 diff 缺的两列:

```sql
-- arl_lite/db/schema.sql
-- 核心 5 表(每表都带 workspace_id + hash 去重)

CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,             -- 增量 #3: workspace 隔离
    task_id INTEGER,
    url TEXT NOT NULL,
    host TEXT,
    ip TEXT,
    port INTEGER,
    scheme TEXT,
    title TEXT,
    status_code INTEGER,
    content_length INTEGER,
    tech TEXT,                                 -- 指纹结果(JSON)
    -- 增量 #2: 抄 SF 5 列 + 补 2 列
    hash TEXT NOT NULL,                        -- 去重键(host+port+url 的 sha256)
    confidence INTEGER DEFAULT 50,             -- 0-100,抄 SF
    risk INTEGER DEFAULT 0,                    -- 0-10,抄 SF
    source_hash TEXT,                          -- 溯源链:指向产生此资产的源
    module TEXT,                               -- 产生者
    false_positive INTEGER DEFAULT 0,          -- 误报标记
    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,  -- 增量 #5:真 diff 的基石
    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(workspace_id, hash)
);

CREATE INDEX IF NOT EXISTS idx_sites_hash ON sites(hash);
CREATE INDEX IF NOT EXISTS idx_sites_ws_hash ON sites(workspace_id, hash);
CREATE INDEX IF NOT EXISTS idx_sites_first_seen ON sites(workspace_id, first_seen);
CREATE INDEX IF NOT EXISTS idx_sites_risk ON sites(risk DESC);

-- 变更事件表(增量 #5 的真 diff)
CREATE TABLE IF NOT EXISTS asset_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    asset_hash TEXT NOT NULL,
    change_type TEXT NOT NULL,        -- NEW_ASSET / DISAPPEARED / TITLE_CHANGED / TECH_CHANGED / FINGERPRINT_CHANGED
    before_value TEXT,                -- JSON,变更前快照
    after_value TEXT,                 -- JSON,变更后快照
    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_changes_ws_type ON asset_changes(workspace_id, change_type, detected_at);
```

**纪律**:
- `hash` 列必填(每个资产算 sha256,跨任务跨周期去重)
- `first_seen` 永不更新,`last_seen` 每次见到就 UPDATE
- 所有表带 `workspace_id` 索引

### 14.4 增量 #3:SIGTERM 优雅停止(抄 ARL `celerytask.py`)

**核心代码**(~80 行):

```python
# arl_lite/core/signal_handler.py
import signal
import sys
import atexit

class GracefulShutdown:
    """SIGTERM 优雅停止——任务状态回写 + 清理"""

    def __init__(self, task_runner: "TaskRunner"):
        self.task_runner = task_runner
        self.shutdown_requested = False
        signal.signal(signal.SIGTERM, self._handle)
        signal.signal(signal.SIGINT, self._handle)
        atexit.register(self._cleanup)

    def _handle(self, signum, frame):
        if self.shutdown_requested:
            # 第二次信号,强制退出
            print("\n[!] Force exit on second signal", file=sys.stderr)
            sys.exit(1)
        self.shutdown_requested = True
        print(f"\n[*] Caught signal {signum}, graceful shutdown...", file=sys.stderr)
        # 关键:回写状态(抄 ARL)
        self.task_runner.mark_stopped()
        # 通知当前任务停止(每个 module 轮询这个标志)
        self.task_runner.request_stop()

    def _cleanup(self):
        # 关 DB 连接、释放资源
        self.task_runner.cleanup()

# TaskRunner 里
def mark_stopped(self):
    """抄 ARL celerytask.py:回写任务状态"""
    with self.db.conn() as conn:
        conn.execute(
            "UPDATE tasks SET status='STOPPED', end_time=CURRENT_TIMESTAMP WHERE id=?",
            (self.task_id,)
        )
```

**BaseModule 里每个 module 都要 check**:

```python
async def run(self, target):
    if self._stop_checker():
        raise StopIteration("SIGTERM received")
    # ... 实际工作
```

### 14.5 增量 #4:Scope 文件带授权凭证号 + 窗口期

**反面教材**:ARL scope 只有"采集范围"语义,无"授权"语义。**arl-lite 显式补**:

```yaml
# ~/.arl-lite/scope.yaml
authorization:
  ticket: "PENTEST-2026-001"           # 授权凭证号
  issued_by: "甲方安全部"
  issued_at: "2026-09-01"
  valid_from: "2026-09-01T00:00:00Z"   # 窗口期开始
  valid_until: "2026-12-31T23:59:59Z"  # 窗口期结束
  scope:                                # 采集范围
    domains:
      - "example.com"
      - "*.example.com"
    ips:
      - "203.0.113.0/24"
    excluded:
      - "production.example.com"        # 白名单:生产环境不扫
  contact: "security@example.com"        # 应急联系人
```

**使用方式**:
```bash
arl-lite run -t example.com --scope ~/.arl-lite/scope.yaml
# 检查授权窗口期:过期 / 未到 / 范围外 → 拒绝执行 + 报错
```

### 14.6 增量 #5:子域 14 源并列 + 死源可见

**抄 ARL `dns_query_plugin/` 14 源架构**(信源冗余 > 单源重试)+ 加死源状态:

```python
# arl_lite/modules/recon/domains-hosts/source_status.py
@dataclass
class SourceStatus:
    """每源本轮状态(零假数据纪律)"""
    name: str                    # "crtsh"
    enabled: bool                # 用户是否启用
    ok: bool                    # 本轮是否成功
    found: int                   # 本轮发现数量
    duration: float             # 耗时
    error: str | None           # 失败原因
    rate_limit_hit: bool         # 是否触发限速

# 任务报告里必带这一段:
# == 子域信源状态(本轮) ==
# ✓ crtsh: 23 个
# ✓ rapiddns: 12 个
# ✗ securitytrails: 401 Unauthorized(token 过期或未配置)
# ⊘ hunter: 跳过(token 未配置)
# ✓ certspotter: 5 个
```

**14 源清单**:
1. `subfinder`(主动)
2. `crtsh`(被动,免费)
3. `rapiddns`(被动,免费)
4. `certspotter`(被动,免费)
5. `chaos`(被动,ProjectDiscovery 限速)
6. `virustotal`(被动,限速)
7. `alienvault`(被动,免费)
8. `securitytrails`(被动,token)
9. `fofa`(被动,token 国内主力)
10. `hunter`(被动,token 国内主力)
11. `quake`(被动,token 国内主力)
12. `zoomeye`(被动,token)
13. `passivetotal`(被动,token)
14. `massdns_brute`(主动字典爆破)

### 14.7 增量 #6:指纹直接复用 ARL 1917 条

**白嫖方式**(~节省 500 行):

```bash
# 装 arl 仓库时顺带拷
curl -L https://raw.githubusercontent.com/Aabyss-Team/ARL/master/app/dicts/webapp.json \
  -o arl_lite/fingerprints/webapp.json
# 657KB,1917 条 Wappalyzer 兼容规则
```

**指纹引擎**(只 ~300 行):

```python
# arl_lite/modules/recon/hosts-services/fingerprint.py
import json
from pathlib import Path
from ....core.base_module import BaseModule

class FingerprintModule(BaseModule):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 启动时加载指纹库(ARL 1917 条 + 未来扩展 Wappalyzer)
        rules_path = Path(__file__).parent.parent.parent / "fingerprints" / "webapp.json"
        self.rules = json.loads(rules_path.read_text())["apps"]

    async def match(self, response: httpx.Response) -> list[dict]:
        """三路匹配:headers / html / title"""
        hits = []
        for app_name, rule in self.rules.items():
            # headers 匹配
            for h_pattern in rule.get("headers", {}).values():
                if h_pattern.lower() in str(response.headers).lower():
                    hits.append({"app": app_name, "method": "headers", "confidence": 90})
            # html 匹配
            for html_pattern in rule.get("html", []):
                if html_pattern.lower() in response.text.lower():
                    hits.append({"app": app_name, "method": "html", "confidence": 80})
            # title 匹配
            for title_pattern in rule.get("title", []):
                if title_pattern.lower() in response.text[:2000].lower():
                    hits.append({"app": app_name, "method": "title", "confidence": 70})
        return hits
```

### 14.8 增量 #⭐:关联分析模块(原方案盲区,Phase 3 新增)

**抄 SF `correlations/*.yaml` 30 条规则**(声明式 collect → aggregate → headline):

```yaml
# arl_lite/modules/analysis/rules/exposed_storage.yml
# 云存储桶开放检测
name: exposed_storage
description: 检测 S3 / OSS / Azure Blob 桶是否对外开放
collect:
  - sites where tech matches ".*storage.*" or ".*bucket.*"
  - sites where path matches "/\.(s3|oss|bucket)"
  - sites where body_hash matches "Index of /"
aggregate:
  field: site_id
headline: "存储桶疑似开放: {url}"
risk: 8
```

```yaml
# arl_lite/modules/analysis/rules/exposed_database.yml
name: exposed_database
description: 检测数据库服务是否暴露在公网
collect:
  - ports where port in [3306, 5432, 6379, 27017, 9200, 1433, 5984, 2379]
  - sites where ip in (select ip from ports where service matches ".*(mysql|postgres|redis|mongo|elastic|mssql|couch|etcd).*")
aggregate:
  field: ip
headline: "数据库服务暴露在公网: {ip}:{port} ({service})"
risk: 9
```

**执行器**(~50 行):

```python
# arl_lite/modules/analysis/correlation_engine.py
import yaml
import re
from pathlib import Path

class CorrelationEngine:
    def __init__(self, db, rules_dir: Path):
        self.db = db
        self.rules = self._load_rules(rules_dir)

    def run_all(self, workspace_id: int) -> list[dict]:
        """对所有规则执行一遍,返回 findings"""
        findings = []
        for rule in self.rules:
            for hit in self._execute(rule, workspace_id):
                findings.append({**hit, "rule": rule["name"]})
        return findings

    def _execute(self, rule: dict, workspace_id: int) -> list[dict]:
        # collect 是 SQL WHERE 列表,逐条跑
        for condition in rule.get("collect", []):
            # 把声明式条件转 SQL(简化版:用正则匹配 field=value)
            # 实际是:解析 "sites where tech matches '.*storage.*'"
            results = self.db.query_by_pattern(workspace_id, condition)
            for r in results:
                yield {
                    "headline": rule["headline"].format(**r),
                    "risk": rule["risk"],
                    "asset": r,
                }
```

**30 条规则直接移植**(从 SF `correlations/*.yaml` 抄):
- exposed_storage / exposed_database / exposed_admin / exposed_vpn
- email_breach / password_leak / api_key_exposed
- outlier_domain / outlier_ip / outlier_port
- same_ip_multiple_domains / same_domain_multiple_ips
- suspicious_tld / suspicious_registrar / suspicious_asn
- ...(共 30 条)

**这是「AI 增强输出」之外最便宜的洞察层**——纯声明式规则,零 LLM 调用成本。

### 14.9 增量 #7:AI Phase 1 只做 1 个边界点

**原 5 个 AI 边界点**:
1. 入口(自然语言)
2. 调度(智能编排)
3. 分析(结果智能分析)
4. 输出(报告生成)
5. MCP(被 AI 调用)

**判决**:Phase 1 只做 #3「分析归纳」1 个边界点,验证 LLM 在「子域/指纹结果归纳摘要」上是否真有用,有效再铺其余 4 个。

```bash
# Phase 3 的第一个 AI 命令
arl-lite analyze -w example.com
# 输出示例(LLM 归纳):
# 本次扫描共发现 47 个子域、12 个 Web 站点、3 个高危服务。
# 重点关注:
#   - admin.example.com:管理后台,Nginx 1.18,无 WAF
#   - test-api.example.com:测试 API,暴露 Swagger UI
#   - jenkins.example.com:CI/CD,Jenkins 2.300,可能存在未授权访问
```

### 14.10 调整后的 Phase 任务清单

**Phase 1 MVP**(增量版,目标 1-2 天,~700 行):
- BaseModule + **3 态返回** ✅
- Storage schema + **5 列** ✅
- subfinder + crtsh 模块(用 SourceResult)✅
- **SIGTERM 优雅停止** ✅
- CLI `arl-lite run -t xxx` 跑通

**Phase 2**(目标 +1-2 天,~1300 行):
- 14 源子域并列(抄 ARL)+ 死源状态可见 ✅
- 指纹 1917 条复用 + Wappalyzer 扩展 ✅
- nmap / httpx / ehole 模块
- Scope 文件 + 授权窗口期检查 ✅
- `presets/full-recon.yml`

**Phase 3**(目标 +1-2 天,~2000 行):
- Textual TUI
- **关联分析模块**(30 条规则 + 50 行执行器)✅ ⭐
- 真 diff(`first_seen` / `last_seen` + 变更事件表)✅
- **AI「分析归纳」1 个边界点**(验证用)✅

**Phase 4**(目标 +1-2 天,~2700 行):
- AI 其余 4 边界点(入口/调度/报告/MCP)
- GitHub 监控
- nuclei 集成
- 报告生成

**Phase 5**(目标 +1-2 天,~3000 行):
- MCP server(被 Cursor/Claude 调用)
- 插件机制
- 文档 + 测试
- Docker(可选)

---

### 14.11 可白嫖资产清单(合计 ~1MB 规则资产,省 800+ 行)

| 资产 | 来源 | 用法 |
|---|---|---|
| **1917 条指纹规则** | ARL `app/dicts/webapp.json` | 指纹引擎数据层直接载入 |
| **30 条关联规则** | SF `correlations/*.yaml` | 关联执行器数据层直接移植 |
| **14 个信源插件清单 + 清洗逻辑** | ARL `dns_query_plugin/` | 插件骨架 + normalize 逻辑参考 |
| **SQLite 列设计** | SF `db.py:74` | schema 直接参考 |
| **黑名单降噪列表** | ARL `dicts/black_asset_site.txt` | 监控降噪直接用 |
| **Wappalyzer 超集** | Wappalyzer 官方 JSON(MIT) | 指纹扩展数据源 |

---

### 14.12 反面教材清单(显式规避)

| 坑 | 谁踩的 | arl-lite 规避 |
|---|---|---|
| **吞错返回空列表** | ARL `DNSQueryBase.query()` | 每源返回 `{ok, data, error}` 三态 |
| **无 diff 语义的「定期重扫」冒充监控** | ARL site monitor | first_seen/last_seen + 变更事件表 |
| **scope 只有采集语义无授权语义** | ARL | `scope.yaml` 加**授权凭证号 + 窗口期** |
| **Mongo schema-free 换 SQLite 后无纪律** | ARL | Phase 1 定死 schema + 迁移文件 |
| **全事件总线复杂度** | SF | 只取 produces 声明,不做总线 |

