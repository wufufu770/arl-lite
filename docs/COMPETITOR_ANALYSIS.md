# arl-lite 竞品源码对照调查(2026-09-06)

> **信源**:Aabyss-Team/ARL(⭐2069,官方删库后最热镜像)+ smicallef/spiderfoot master 源码直读
> **方法**:每个功能点拉同类产品的真实源码(基类/建表语句/规则文件),给「可抄/不可抄」判决
> **原则**:文档叙事不算数,只认代码锚点

---

## 一、总判决表

| # | 方案功能点 | 同类构建方式(源码锚点) | 可借鉴判决 |
|---|---|---|---|
| 1 | BaseModule 抽象 | ARL 三层基类(BaseThread→DNSQueryBase)+ SF SpiderFootPlugin(562 行) | **混抄**:ARL 的「插件纯信源+清洗集中」+ SF 的 produces 类型声明;**禁抄** ARL 吞错 |
| 2 | Storage SQLite+FTS5 | SF SQLite 事件表(db.py:74)+ ARL Mongo schema-free | **SF 列设计是参考答案**:hash 去重 + confidence/risk + 溯源链 |
| 3 | Workspace | SF scan_instance 外键 + ARL scope_id 聚合 | 全表带 `workspace_id` 一列搞定,抄 |
| 4 | Preset | SF scan_config(模块集合+选项覆盖) | 抄,够用 |
| 5 | 周期监控 diff | ARL 极薄(无 first_seen/last_seen 语义)+ SF 无 | **同类全弱项 = 差异化机会,自建** |
| 6 | 任务调度 | ARL Celery 双队列+SIGTERM 优雅停止 | APScheduler 正确;**必抄 SIGTERM 状态回写** |
| 7 | 子域信源 | ARL 14 插件并列,无单源重试 | 抄 14 源架构(信源冗余>单源重试)+ **补死源可见** |
| 8 | 指纹识别 | ARL webapp.json 1917 条(Wappalyzer 兼容+fofa_rule) | **直接复用资产,引擎 ~300 行** |
| 9 | 关联分析(方案盲区) | SF correlations/*.yaml 30 条声明式规则 | **方案没规划这条——建议新增,YAML+50 行执行器** |
| 10 | MCP + AI 5 边界点 | 同类产品均无 AI 先例 | **无对标可抄**,按自证纪律走(读重写门+scope) |

---

## 二、逐项源码证据

### 1. BaseModule —— ARL 三层 + SF 声明式路由

**ARL 构建**(app/services/):

- `BaseThread`:semaphore 并发 + 异常分级四层(RequestException 静默 / etree 解析静默 / Exception 记日志 / BaseException 上抛终止)+ 守护线程
- `DNSQueryBase`(信源插件契约):`init_key(**kwargs)` + `sub_domains(target)` 两个钩子;**清洗全部集中在基类 query()**:去 `*.` 通配 / lowercase / 后缀匹配 / `.target` 校验 / `DOMAIN_MAX_LEN` 长度上限——插件只管拉数,不管合法性的分层非常干净
- ⚠️ **反面教材**:`query()` 里 `except Exception → return []`——**失败与真空不可区分**(ARL 也有这个坑)

**SF 构建**(spiderfoot/plugin.py 562 行):

- **声明式数据流路由**:`watchedEvents()`(我消费什么事件类型)+ `producedEvents()`(我产出什么类型)——调度器据此把事件路由给模块,形成类型化流水线
- `_priority`(监听者执行顺序)、`checkForStop()`(协作式中断——KILL 熔断同类)、`handleEvent(event)` 唯一业务钩子
- 事件对象(event.py 303 行)自带 `confidence` / `visibility` / `risk` 三评分 + `sourceEvent` 溯源指针

**arl-lite 判决**:抄 ARL 的「清洗集中基类」+ SF 的「produces 类型声明」(用于模块依赖排序和 Preset 校验);不要抄 SF 的全事件总线(无限扇出,3000 行装不下),也不要 ARL 的吞错。

### 2. Storage —— SF 的列设计是 SQLite 资产库参考答案

**SF tbl_scan_results 实际建表**(db.py:74):

```sql
scan_instance_id / hash(去重键) / type(事件类型外键) / generated(时间戳) /
confidence(0-100) / visibility / risk(0-10) / module(产生者) /
data / false_positive(误报标记) / source_event_hash(溯源链,ROOT 为根)
```

8 个索引覆盖 scan_id / type / hash / module / source_hash 五维查询。

**arl-lite 判决**:**SQLite 表直接抄这五列思想**:hash 去重键 + type + confidence + risk + source_hash + module + first_seen/last_seen(补 SF 没有的监控 diff 地基)+ FTS5 只挂文本列。

**ARL 侧证明**:`BaseInfo` 极薄(纯 json dump)+ Mongo schema-free 兜底 = 换 SQLite 后没有纪律就失血——**schema 纪律必须 Phase 1 定死**。

### 3. Workspace —— 两种模型兼容

- SF:`scan_instance(guid)` 外键挂日志/结果/关联全部数据
- ARL:`asset_scope(scope_id)` 聚合资产视图

**arl-lite 判决**:所有表加一列 `workspace_id` + 一张 workspace 表,成本一列,回报是数据隔离 + 监控 diff 按工作区分组。

### 4. Preset —— SF scan_config 模型够用

preset = 模块 ID 集合 + 选项覆盖(dict)。

ARL 的 `task_data` 状态机字段(status/celery_id/service)参考其 `WAITING→RUNNING→STOP/STOPPED` 流转,但单机不需要 celery_id。

### 5. 周期监控 diff —— 同类全是弱项,这是超越点

**ARL 的站点监控**(helpers/asset_site_monitor.py 49 行):scheduler 定时提交「资产站点更新」job → 复用采集流程 + `black_asset_site.txt` 前缀黑名单降噪。

- ❌ **没有** first_seen/last_seen 显式语义
- ❌ **没有** 字段级 diff
- ❌ **没有** 变更集视图
- 实质是「定期重扫」冒充「监控」

**SF 完全没有周期概念**(一次 scan 为生命周期)。

**arl-lite 判决**:**这是同类产品公认的洞**。做真 diff 需要:
- `last_seen` 惰性更新
- upsert 计数
- 变更事件表(新增资产/消失资产/指纹变化/标题变化分类)
- ARL 黑名单前缀过滤直接抄(低成本降噪)

### 6. 调度 —— 必抄 ARL 的 SIGTERM 优雅停止

**ARL celerytask.py**:`sigterm_handler` 捕获信号 → 按 celery_id 回写任务状态 `STOP` + `end_time` → `exit_gracefully`。队列分离(ASSET_TASK/GITHUB_TASK routing key)思想映射到 APScheduler 双 executor。

**arl-lite 判决**:
- APScheduler 选型维持
- **SIGTERM→状态回写→清理**这个序列是任务类工具生产化分水岭,**80 行内实现,Phase 1 必带**

### 7. 子域信源 —— ARL 的 14 插件并列架构

`dns_query_plugin/` 全目录:`crtsh` / `certspotter` / `fofa` / `hunter_qax` / `quake_360` / `rapiddns` / `securitytrails` / `virustotal` / `zoomeye` / `chaos` / `alienvault` / `passivetotal` + `dns_query` 基类

**哲学**:**信源冗余 > 单源重试**(crt.sh 挂了不影响整体,因为 14 源并列)。单源本身极薄:`crtsh.py` 45 行无重试无超时(timeout 元组 `(30.1, 50.1)` 是唯一防线)。

**arl-lite 判决**:
- 抄 14 源并列架构
- 免费源先行(crtsh/rapiddns/certspotter/chaos/virustotal 限速)
- fofa/hunter/quake 三家 token 化(国内 SRC 语境主力)
- 方案原定的 subfinder subprocess 路线可保留作**主动源**
- **超出 ARL 的点**:每源带超时 + **死源状态可见**(报告如实标注哪些源本轮没出力——**零假数据纪律**)

### 8. 指纹 —— 1917 条现成资产

**ARL webapp.json(657KB)**:Wappalyzer 兼容格式(`cats` 分类 / `headers` / `html` / `title` 匹配)+ `fofa_rule` 交叉验证字段。

SF 生态的 Wappalyzer `technologies.json` 数万条可作超集。

**arl-lite 判决**:
- 指纹引擎 = headers + html + title 三路匹配 ~300 行
- **直接复用 1917 条规则**——省掉自写规则库的 500 行
- APScheduler 定期全量重扫时做指纹缓存(ARL 有 `fingerprint_cache.py` 先例可参考)

### 9. 关联分析 —— 方案盲区,SF 送 30 条现成规则

`correlations/*.yaml`:声明式(`collect` 条件组 → `aggregation field` → `headline` 模板 → `risk` 等级)。

代表规则:云桶开放 / 数据库暴露 / email 多泄漏源 / 离群域名 / 离群 IP / 同域多恶意源。

**arl-lite 判决**:
- **建议新增此功能点**(原方案 13 项能力对照里没有)
- 执行器 ~50 行(`collect=SQL WHERE` 组合)
- **30 条规则直接移植**
- 在资产侦察语境(找薄弱面)这些规则的命中率天然高
- 是「AI 增强输出」之外**最便宜的洞察层**

### 10. MCP + AI 5 边界点 —— 无对标,自证纪律

ARL/SF 源码 grep 无任何 AI/LLM 模块(SF 命中的全是 email 关联规则)。

MCP python-sdk 的 FastMCP 装饰器形态(单文件 server)符合 3000 行预算。

**arl-lite 判决**:
- 原创设计没有生态验证,风险自担
- **建议 Phase 1 只做深「分析归纳」一个边界点**(子域/指纹结果的 LLM 归纳摘要),验证有效再铺其余 4 点
- MCP 工具面维持上轮定的**读重写门**纪律

---

## 三、可白嫖资产清单(合计 ~1MB 规则资产,省 800+ 行)

| 资产 | 来源 | 用法 |
|---|---|---|
| **1917 条指纹规则** | ARL `app/dicts/webapp.json` | 指纹引擎数据层直接载入 |
| **30 条关联规则** | SF `correlations/*.yaml` | 关联执行器数据层直接移植 |
| **14 个信源插件清单 + 清洗逻辑** | ARL `dns_query_plugin/` | 插件骨架 + normalize 逻辑参考 |
| **SQLite 列设计** | SF `db.py:74` | schema 直接参考 |
| **黑名单降噪列表** | ARL `dicts/black_asset_site.txt` | 监控降噪直接用 |
| **Wappalyzer 超集** | Wappalyzer 官方 JSON(MIT) | 指纹扩展数据源 |

---

## 四、反面教材清单(同类产品踩过的坑,arl-lite 要显式规避)

| 坑 | 谁踩的 | arl-lite 规避 |
|---|---|---|
| **吞错返回空列表**(失败与真空不可区分) | ARL `DNSQueryBase.query()` | 每源返回 `{ok, data, error}` 三态 |
| **无 diff 语义的「定期重扫」冒充监控** | ARL site monitor | first_seen / last_seen + 变更事件表 |
| **scope 只有采集语义无授权语义** | ARL(无窗口/凭证概念) | `scope.yaml` 加**授权凭证号 + 窗口期** |
| **Mongo schema-free 换 SQLite 后无纪律** | ARL | Phase 1 定死 schema + 迁移文件 |
| **全事件总线复杂度** | SF | 只取 produces 声明,不做总线 |

---

## 五、对原方案的增量

| 增量项 | 来自 | 优先级 |
|---|---|---|
| 1. BaseModule 改为 3 态返回 `{ok, data, error}` | 反面教材 #1 | **P0 - Phase 1** |
| 2. Storage schema 加 5 列:`hash` / `confidence` / `risk` / `source_hash` / `first_seen` / `last_seen` | 判决 #2 #5 | **P0 - Phase 1** |
| 3. SIGTERM 优雅停止 + 任务状态回写 | 判决 #6 | **P0 - Phase 1**(生产化分水岭) |
| 4. Scope 文件加授权凭证号 + 窗口期管理 | 反面教材 #3 | **P1 - Phase 2** |
| 5. 子域信源改为 14 源并列 + 死源状态可见 | 判决 #7 | **P1 - Phase 2** |
| 6. 指纹直接复用 ARL 1917 条 + Wappalyzer 扩展 | 判决 #8 | **P1 - Phase 2** |
| 7. **新增关联分析模块**(原方案没有) | 判决 #9 | **P2 - Phase 3** |
| 8. AI Phase 1 只做 1 个边界点验证(分析归纳) | 判决 #10 | **P2 - Phase 3** |
