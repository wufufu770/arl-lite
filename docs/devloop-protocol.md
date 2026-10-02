# arl-lite 自持迭代协议（Devloop Protocol）

> **状态**:活文档,跟随项目长期维护
> **生效版本**:v0.7.8 起
> **维护者**:项目所有者（单人）
> **最后审计**:见 `devloop/state.json` 的 `last_audit_round`

本文档定义 arl-lite 自我改进的**循环、门禁、退路与状态机**。它不是任务清单（任务在 `devloop/backlog.md`），而是约束任务如何被选、怎么被做、做到什么程度算完的元规则。

---

## 1. 为什么需要这个协议

### 1.1 一个没有门禁的自我改进循环会怎么失控

自我改进听起来很美,但放任自流的自我改进会稳定地走向以下五种坏结局。本协议存在的意义就是**给这五种失控装刹车**,不是为了学习如何为"持续改进"鼓掌。

| 失控模式 | 触发场景 | 后果 | 协议如何防 |
|---|---|---|---|
| **改 A 坏 B** | 只跑新代码的测试,不跑回归 | 老功能默默退化,直到下次生产事故才暴露 | `test_baseline` 门禁:不允许新增失败 |
| **门禁刷分** | 改门禁本身让指标变绿 | 防线被绕过,假装合规 | 门禁代码只增不删,变更必须走 IMPROVE 阶段 |
| **文档腐化** | 改代码不更新 `docs/` | 新人/未来的自己读到错误信息 | `doc_freshness` 门禁(非阻断但记入产物) |
| **重复造轮子** | 不读 backlog 就开新特性 | 解决已解决的问题 | `seed_if_empty()` 三层播种 |
| **越改越大** | 每轮都加新功能不删除旧的 | LOC 爆炸、2G VPS 跑不动 | `loc_budget` 门禁 |

### 1.2 为什么不能用"CI 跑过就行"代替本协议

- CI 只能保证**当下没坏**,不能保证**没漏坏**（如 pytest 缺 `asyncio_mode` 导致 9 个 async 测试被静默 skip,这正是 CI 跑过但实则漏判的例子）。
- CI 不能防止**架构腐化**（如 `import.cfrom` 出现、`risk_score.py` 越变越像不可读的胶水代码）。
- CI 不能保证**长期演进有方向**（缺待办可以快速漂移到"什么都做一点,什么都不精"）。

本协议是 CI 之上的**战略层**,CI 是战术层。

---

## 2. 项目铁律（约束集合）

任何循环、门禁、决策都不得违反这些。它们是**不可议的**。

| ID | 铁律 | 违反的代价 | 谁来检查 |
|---|---|---|---|
| **L1** | 零第三方 pip 依赖,纯 stdlib | 2G VPS 装不上、纯 stdlib 不可装 | `no_thirdparty_import` |
| **L2** | 3 态纪律:`{ok, data, error, error_type}`,6 类 error_type | 死源被假数据替换、报告失真 | 模块 review |
| **L3** | 死源可见:报告如实标出无出力的源 | 用户以为跑过实际没跑 | `correlation_engine` 输出契约 |
| **L4** | 零假数据:指纹/端口/CDN 结果天然不可信 | 误导用户决策 | `risk_score` 输出契约 + 外部研究 §3 |

### 2.1 L1 的特别说明

`pyproject.toml` 的 `[project.optional-dependencies]` 当前声明了 7 个可选包（typer/rich/httpx/pyyaml/apscheduler/openpyxl,以及 dev 组的 pytest）。这些是**已实现但未启用**的探针,实现初期为了证明"零依赖也能做"曾推翻原计划的依赖引入。**当前不应启用任何 optional 依赖**——除非新规加入本表 L1.5 且写明"为何这次必须破例"。

> **为什么不可议**:ark-lite 跟 ARL 灯塔的差异化卖点就是"2G 内存开箱即跑"。这个卖点倒了,项目定位就倒了。

---

## 3. 外部调研结论（设计依据）

这些结论来自项目外的代码直读与作者实测,**作为本协议的硬性约束依据**,不是建议。

### 3.1 结论表

| 来源 | 结论 | 在本协议中的引用 |
|---|---|---|
| Aabyss-Team/ARL + smicallef/spiderfoot 源码直读 | 模块基类设计 / SQL schema 列选择是项目质量的两大锚点 | 强制所有新模块继承 `BaseModule` 3 态契约 |
| HunterX 源码（26 个架构测试） | 架构测试是防架构腐化的有效手段 | §5 门禁体系引用此规模作参照 |
| AtlasX 作者实测 1000 份 SRC 报告 | `P(真漏洞 \| 已被标记为高危) = 76%` | §5 `rules_have_advice` 门禁,以及 §7 待办里"置信度字段消费"项 |
| Bayes 推导（基础率 1%,单次 FP 10% → 8.7%;要求 3 轮独立观测 → 99%） | 单一快照不可信,多轮观测才可信 | §9 落地路线的轮次优先级 |
| IP2Location LITE 许可证 CC BY-SA | 传染性,不允许合入仓库 | 红线,任何 PR 含此数据直接拒 |
| CAIDA 数据许可证 CC BY-NC | 禁商用 | 红线,与本项目 MIT 许可证不兼容 |

### 3.2 置信度四因子乘法模型（写入门禁的事实依据）

```
confidence = base_prior × signal_factor × cross_evidence × temporal_consistency
```

| 因子 | 含义 | 当前状态 | 期望状态 |
|---|---|---|---|
| `base_prior` | 指纹/规则本身的先验可信度 | 缺 | 第一轮必做 |
| `signal_factor` | 单信号强度（HTTP 200 / 证书签名 / 协议握手） | 缺 | 第一轮必做 |
| `cross_evidence` | 跨源交叉印证（同一资产被 ≥2 个 source 命中） | 缺 | 第二轮候选 |
| `temporal_consistency` | 多轮观察一致性（同一资产 ≥3 轮稳定出现） | 缺 | 第三轮候选 |

> **为什么是乘法不是加法**:任一因子为 0,confidence 必须为 0。这是"任一防线失守就降级"的安全语义,加法做不到。

> **为什么不是 AtlasX 的 76%**:那是 `P(真漏洞 \| 已被标记为高危)`,是**后验**。我们要的是工具自身的 `precision`,两者不能混。混淆这两个就会灾难性后果。

---

## 4. 循环的四个阶段与状态机

### 4.1 状态机总图

```
            ┌──── 任何门禁超时 / 崩溃 ────┐
            │                              │
            ▼                              │
   ┌─────────────┐   test_baseline 通过   ┌─────────────┐
   │   BUILD     ├──────────────────────►│    TEST     │
   │ (执行任务)  │                       │ (跑回归)    │
   └──────┬──────┘                       └──────┬──────┘
          │                                      │
          │ 测试数下降 / LOC 超预算                │ 全部通过
          │ / 连续 N 轮无净增量                    ▼
          │                              ┌─────────────┐
          │                              │  IMPROVE    │
          │                              │ (重写/重构) │
          │                              └──────┬──────┘
          │                                     │
          │                                     │ 提取通用模式
          │                                     ▼
          │                              ┌─────────────┐
          │                              │   PLAN     │
          │                              │ (生成下轮) │
          │                              └──────┬────┘
          │                                     │
          ▼                                     ▼
   ┌─────────────────────────────────────────────┐
   │  STATE 落盘(写 devloop/state.json,原子写)  │
   └─────────────────────┬───────────────────────┘
                         │
                         ▼
                   BUILD (下一轮)
```

### 4.2 阶段定义表

| 阶段 | 进入条件 | 做什么 | 退出条件 | 失败时怎么退 |
|---|---|---|---|---|
| **BUILD** | `backlog.md` 头部有未完成任务 | 执行一项任务,改代码,改文档,跑本地 smoke | 任务在 `backlog.md` 标为 done;或被 RETREAT 触发 | 进入 RETREAT(§6) |
| **TEST** | BUILD 退出后 | 跑 `make test` 全套 9 套（phase1-7 + edge + concurrency）;对照 `state.json.baseline` 比对失败数 | 失败数 ≤ baseline 失败数 | 进入 RETREAT |
| **IMPROVE** | TEST 通过 | 提取本轮发现的通用模式重写（如重复代码、参数传递冗长） | 重写后重跑 TEST 仍通过 | 进入 RETREAT |
| **PLAN** | IMPROVE 完成或未触发 | 从 backlog 取下一项,或调 `seed_if_empty()`(§7) | 新任务加入 backlog 头部 | 退到 BUILD |

### 4.3 进入条件的具体阈值

- **BUILD**:backlog 头项非空且不是 `[BLOCKED]` 标记。
- **TEST**:本轮代码有 git diff（无 diff 跳过 TEST 直接 PLAN）。
- **IMPROVE**:本轮 BUILD 改动的文件数 ≥ 3 且 LOC 净增 > 50 行。
- **PLAN**:任何时候 IMPROVE 退出后都必须执行。

> **为什么 IMPROVE 有 LOC 阈值**:小改动（< 50 行 / < 3 文件）通常不需要重构,触发 IMPROVE 反而是过度工程。

---

## 5. 门禁（Gate）体系

### 5.1 七道门禁总表

| ID | 门禁 | 防什么退化 | 阻断/非阻断 | 触发命令 |
|---|---|---|---|---|
| **G1** | `no_thirdparty_import` | L1 铁律被破坏 | **blocking** | `grep -rE "^import (typer|rich|httpx|pyyaml|apscheduler|openpyxl|litellm|textual)" arl_lite/` 应返回空 |
| **G2** | `test_baseline` | 回归（新增失败） | **blocking** | `make test` 失败数 ≤ `state.json.baseline.fail_count` |
| **G3** | `no_import_cycle` | 循环依赖导致的 import 地狱 | **blocking** | 自写 `tools/check_imports.py` 跑 `import` 图 DFS |
| **G4** | `loc_budget` | 无节制膨胀 | **blocking** | `find arl_lite -name "*.py" -exec cat {} + \| wc -l` ≤ 18,000 行 |
| **G5** | `rules_have_advice` | 规则失去可操作性 | **blocking** | 每条 YAML 规则必须有 `advice:` 非空字段 |
| **G6** | `prompt_injection_guard` | 安全防线回退 | **blocking** | `ai/prompts.py` 必须调用 `_sanitize` 处理 title/banner/whois/CN |
| **G7** | `doc_freshness` | 文档过期 | **非阻断** | `state.json.last_doc_audit_round` 与当前轮差 ≤ 5 |

### 5.2 blocking vs 非阻断的区分理由

- **blocking**:违反会让**铁律被破坏或当前质量不回来**。任何时候不允许绕过。
- **非阻断**:违反不会让铁律被破坏,但会随时间累积成大问题（如 `doc_freshness` 让 `PROJECT_PLAN.md` 完全过期,但代码本身不受影响）。

> **为什么 doc_freshness 非阻断**:文档写得烂不等于代码写得烂。阻断会让循环卡死在"写文档"上,而真正的问题是代码改动没让文档同步。设非阻断 + 轮次累积告警更合理。

### 5.3 门禁失败必须写进状态而不是被吞掉

每道门禁的失败结果必须写入 `devloop/state.json` 的 `gate_history` 数组,字段:

```json
{
  "round": 7,
  "gate_id": "G2",
  "status": "fail",
  "fail_count_delta": 3,
  "failed_tests": ["test_phase5.py::test_x", "..."],
  "action": "retreat_to_round_5",
  "timestamp": "2026-10-02T02:01:41+08:00"
}
```

> **为什么必须持久化**:被吞掉的失败会成为"传说"——某轮跑挂了但谁也不知道是哪轮、为什么、改的是状态。门禁历史是协议自己可审计的唯一来源。

### 5.4 门禁阈值与触发后处理

| 门禁 | 阈值 | 触发后默认动作 |
|---|---|---|
| G1 | 任意非空 import | 立即 REVERT 本轮全部改动（不动 BUILD）；记入 RETREAT 计数 |
| G2 | fail_count 比 baseline 增加 ≥ 1 | REVERT + 重新写一个最小失败用例 |
| G3 | 检测到环 | REVERT 该次重构 |
| G4 | `arl_lite/` 超过 baseline + 300 行 | 本轮记 `DONE_WITH_FAILURES`；见 5.5 的显式提升流程 |
| G5 | 任意规则缺 advice | REVERT 该规则的提交 |
| G6 | 任意 title 变量未 `_sanitize` | REVERT 该 prompt 修改 |
| G7 | 文档引用了实际未使用的依赖 | 仅记告警,不阻断 |

> **G4 为什么是「baseline + 容差」而不是一个绝对数字**:绝对行数(早期版本写的 18,000)
> 会把「仓库整体大小」和「本轮增量」混为一谈。前者由项目成熟度决定,后者才是迭代纪律
> 该管的东西。用 baseline 做地板、给固定容差,衡量的才是「这轮你到底写多了」。
>
> **协议自身也在这把尺子下**:`devloop/` 有独立红线,量的是**代码行**
> (扣掉 docstring / 注释 / 空行),当前 2,183,红线 2,400(硬编码,不可提升)。
> 它一旦比被它守护的代码涨得还快,就本末倒置了。
>
> **为什么量代码行而不是总行数**:初版量总行数,红线 3,200。到了第 9 轮
> 总行 3,189,只剩 11 行余量——但逐文件拆开是
> `2,096 代码 + 635 docstring + 149 注释 + 309 空行`,代码只占 66%。
> 这个库大量 docstring 记的是**"这个 bug 怎么发现的、为什么这么修"**。
> 删掉能腾地方,代价是下一个 agent 在同一个坑里再摔一次。
> 总行数仍然报出来供人参考。红线仍然**不可提升**——换了度量对象,
> 不等于松了约束。

### 5.5 Baseline 提升:门禁失败的第一类解法

#### 死路是怎么形成的

G4 这类门禁拿**版本库里的 baseline** 当参照。一旦某轮真的做完了实打实的新功能
(实例:第 5 轮新增 TLS 证书采集,净增 540 行),G4 必红。此时操作者只有两条路,
两条都不好:

| 路径 | 问题 |
|---|---|
| 手改 `devloop/baselines.json` | 历史里看不见是谁、何时、因为什么放宽的。与「偷偷放宽」只有操作习惯上的区别,没有机制上的区别。 |
| 硬拆模块把行数压回去 | 为了凑一个数字去扭曲代码结构,让门禁反过来支配设计。 |

第一条直接违反协议自己的不变式(门禁失败必须可见),第二条让门禁变质。
**两条都会让循环卡死或变质,而「永远有下一步」是本协议的第一目标。**

#### 三条硬规则

提升是一等操作,不是改配置文件:

```bash
arl-lite devloop accept <gate> --reason "为什么可以放宽"
arl-lite devloop promotions          # 看提升历史
```

1. **门禁必须正在失败** —— 已经绿的门禁不接受提升。
   这一条堵住「提前买预算」:否则操作者可以在每轮开始前先抬高阈值,门禁从此形同虚设。
2. **必须给理由,最少 10 字符** —— 没有理由的放宽就是偷偷放宽。
3. **只升不降,且只升 gate 自己点头的字段** —— 由 `Gate.promotable` /
   `Gate.promotable_fields` 逐个 opt-in,默认 `False`。

#### 哪些门禁可以被提升

| 门禁 | promotable | 可提升字段 | 理由 |
|---|---|---|---|
| `loc_budget` | ✅ | `total_loc` | 代码量预算本就是「随功能增长显式上移」的闸门 |
| `no_thirdparty_import` | ❌ | — | 恒为 0 的红线。把「当前有 5 处三方 import」写成新基准 = 把 bug 追认为正常 |
| `no_import_cycle` | ❌ | — | 同上 |
| `test_baseline` | ❌ | — | 抬高 allowed `failed` 数正是「偷偷放宽」的经典形态 |
| 其余 | ❌ | — | 安全的默认值:新加门禁忘了写 `promotable`,默认就是不可提升 |

`loc_budget` 的 `devloop_loc`(协议自身红线)**不在白名单里**,不能被
`total_loc` 的提升顺带捎上。

#### 留痕写进版本库,不是只落本地

提升记录追加到 `baselines.json` 的 `_promotions` 列表:

```json
"_promotions": [
  {
    "at": "2026-10-02T12:26:18",
    "changes": { "total_loc": { "from": 12246, "to": 12786 } },
    "gate": "loc_budget",
    "reason": "第5轮新增 TLS 证书采集(...),净增 540 行属实打实的新功能,不是膨胀",
    "round": 5
  }
]
```

**为什么不写进 `state.json`**:那份文件每机一份、被 `.gitignore` 排除,
那里的痕迹没有任何人 review 得到。写进 `baselines.json` 意味着每一条放宽
都会出现在 `git diff` 和 code review 里。

历史保留最近 50 条(只增不删)。只增不删是刻意的:能删掉旧记录的功能等于没有留痕。

#### 反向验证

`tests/test_devloop_accept.py` 的重点不是「accept 能用」,而是「该拒的全都拒了」。
17 个用例里 8 条是证伪用例:无理由、理由太短、门禁已绿、门禁不可提升、
字段不在白名单、数值变小、bool 冒充数字、`_promotions` 被误当门禁。

外加一条**变异测试**:把 accept 源码里的理由校验块整段删掉生成变异体,
断言变异体确实放行了无理由提升。

> 变异测试第一次只删「reason is required」那一行时,变异体**没有**放行——
> 因为紧跟着的「reason too short」把空串也拦了。这说明必填检查单独看是冗余的,
> 真正兜底的是长度检查。两道都留着是因为报错信息不同(「没给理由」vs「理由太短」),
> 但变异必须覆盖整个块,否则测的就不是约束本身。

---

## 6. 退路（Retreat）机制

### 6.1 触发条件

满足以下任一即触发退路:

1. **连续 N 轮无净增量**:`N = max(3, ceil(baseline_pass_rate * 5))`,目前 baseline 全绿故 N=3。
2. **测试数下降**:本轮 TEST 比上轮 TEST 的 pass_count 减少。
3. **LOC 超预算**:触发 G4 退路（不同于普通 G4 REVERT）。
4. **门禁历史连续 3 轮同一 G 失败**:说明该门禁设计本身有问题,需冻结该 G 并上升级到人工。

### 6.2 退到哪里

| 退路级别 | 触发条件 | 退到 |
|---|---|---|
| **R0 软退** | 单次 G1-G6 失败 | REVERT 本轮 git commit；`backlog.md` 该任务未标 done |
| **R1 硬退** | 连续 2 轮同一 G 失败 | REVERT 到 `state.json.last_all_green_round` 的 git ref；任务保留 |
| **R2 冻结** | 连续 4 轮同一 G 失败 | 冻结该 G（不要求生产代码满足），任务标 `[BLOCKED]`，等人工 |
| **R3 暂停** | `RETREAT_COUNT` 累加到 5（自项目起累计） | 暂停协议，人工 review |

### 6.3 为什么不自动 git reset

- **危险**:reset 会丢工作,如果 reset 后发现上周签的不是问题源头,工作已经没了。
- **更危险**:reset 不写状态,如果同一个人不读 git log 就跑下一轮,会重复做同样的事。
- **本协议的做法**:REVERT 用 `git revert`（保留历史）+ 写 `gate_history` 记录为何 REVERT。

> **核心原则**:**保留 commit 解释为什么**,而不是用 reset 抹掉为什么。

### 6.4 RETREAT_COUNT 的语义

- 每次进入 R1 即 `RETREAT_COUNT += 1`。
- 达到 5 触发 R3 暂停,需要人工确认是否调整门禁阈值或协议本身。
- `RETREAT_COUNT` 仅在**所有门禁连续 5 轮全绿**时才能 `--reset` 归零,防止"刷绿就忘痛"。

---

## 7. 待办队列永不枯竭（seed_if_empty）

人不可能一直盯着。协议必须自持。`seed_if_empty()` 在 backlog 为空时被调用,**绝不返回空**。

### 7.1 三层播种

```
def seed_if_empty(backlog: list, code_state: dict) -> list:
    if backlog:
        return backlog
    return (
        layer1_human_backlog()
        or layer2_code_derived()
        or layer3_long_horizon()
    )
```

| 层 | 名称 | 来源 | 何时使用 |
|---|---|---|---|
| **L1** | 人工 backlog | `devloop/backlog.md` 由人写入 | 永远优先 |
| **L2** | 代码现状自动推导 | 扫描代码 TODO/FIXME/XXX；grep `pass  # TODO`；扫描 `db/schema.sql` 中无消费者的字段 | L1 耗尽 |
| **L3** | 固定长期演进项 | `devloop/seed_long_horizon.json` 硬编码 | L2 也耗尽时,保底 |

### 7.3 L2 自动推导的扫描规则

| 扫描器 | 触发词 | 产出任务模板 |
|---|---|---|
| `scan_todo_comments` | `TODO\|FIXME\|XXX\|HACK` | "处理 `path:line` 注释：`{content}`" |
| `scan_orphan_schema` | grep schema.sql 中字段无任何 module 引用 | "为 `db/schema.sql:{line}` 字段 `confidence` 接入消费者" |
| `scan_disabled_tests` | pytest 中 `@pytest.mark.skip` | "评估 `tests/{file}::{func}` 是否可启用" |
| `scan_dead_imports` | `importlib.util.find_spec` 失败 | "清理 `path:line` 的死 import" |

### 7.4 为什么不允许 `return None`

> 单点项目跑完了"自动推导"循环回来了发现 backlog 为空,循环空转,协议就死了。L3 永远给硬编码任务保底,代价是这些任务可能永不完成——但**永有任务**比**循环停转**重要得多。

### 7.5 L3 长期演进项清单（当前硬编码）

| ID | 任务 | 为什么是 L3 |
|---|---|---|
| LH-1 | "为 `risk_score.py` 接入 `confidence` 字段消费" | 见 §3.2 |
| LH-2 | "为关联规则加 `confidence` 概念替代直接 `risk: 9/10`" | 见 §3.1 |
| LH-3 | "`check_filter_sql` 禁 `UNION`" | 安全防御 |
| LH-4 | "异常分类改为子类化而非字符串匹配" | 重构项,L4 才做 |
| LH-5 | "更新过期的 `docs/PROJECT_PLAN.md`" | 文档同步 |

### 7.6 不变式 #4 破过两次,都在同一处

「队列耗尽自动播种,永远有下一步」是协议的第一不变式。它破过两次,
而且两次都发生在第 8–9 轮这种"一切看起来很顺"的时候。

#### 第一次:PLAN 阶段跑在队列更新之前

`round()` 的阶段顺序是:

```
取待办 → BUILD → TEST → IMPROVE → PLAN → 更新队列状态
```

第 5 步 PLAN 执行时,当前条目还是 `in_progress`（第 6 步才标 done）。
`phase_plan` 看到 in_progress 就认为"还有活干"不播种;紧接着引擎把它标成
done,队列彻底空了 —— 而且**再没有任何代码路径会回来播种**。

实测:第 9 轮跑完队列变成 `0 pending / 8 done`,循环没有下一步了。

修法:新增 `ensure_next_step()`,在队列更新**之后**再查一次。
让"永远有下一步"成为整轮的**后置条件**,而不是轮中途的一次猜测。

#### 第二次:跳过已存在的 id,造出重复 id,循环原地空转

`seed_if_empty` 传给旋转逻辑的是 `active_ids`（只含 pending/in_progress）,
而 docstring 写的是"若已存在且处于 done/dropped 则加 `-r{round}` 后缀"。
**文档说一回事,代码做另一回事。**

后果是队列全 done 时 `active_ids` 为空 → 旋转不触发 → 补进来一批
**和已有条目完全同 id** 的新条目。而引擎所有按 id 定位的地方
（`mark_in_progress` / `mark_done` / `round()` 取待办）都是
`next(i for i in items if i.id == ...)`，永远命中**第一条**。于是引擎
反复翻转那条早已 done 的记录,新补的条目永远卡在 pending —— 原地空转。

实测:真实队列被搞成 **8 个 id 各出现两次,8 条全"活跃"**。
`devloop status` 只显示聚合计数,肉眼完全看不出来。

修法三层:
1. `existing_ids` 改成**全部**已有 id(不是只有活跃的)
2. 新增 `_disambiguate()`,在播种末尾统一保证 id 唯一 —— 三级策略里
   哪一层写错了都不至于产出重复 id
3. Tier 2/Tier 3 刻意传空集,让它们**总能提出**候选项。
   原来它们会 `if id in existing_ids: continue`,三个保底项全被跳过后
   `seed_if_empty()` 返回 0 —— **保底层不再是保底**

> **教训**:`seed_if_empty` 的返回值是协议"永远有下一步"的唯一保障。
> 任何以"跳过已存在项"为策略的写法,都是在悄悄拆掉这个保障。

#### 修复工具本身也会选错

`devloop repair` 保留同一 id 下"进度最靠前"的那条
（in_progress > pending > done > dropped）。这次它选错了:引擎误标的那份
是 done,脏数据是 pending,启发式把 7 项真做完的工作**退回成了待办**。

原因是启发式选不出真相 —— 两份副本都是错的时,没有哪份"更靠前"。
所以:
- 加了 `repair --dry-run`,改之前先把每份副本的
  `status/attempts/done_round/created_round/note` 打出来给人核对
- 真实数据最终按 `devloop history` 里的事实来源人工校正,
  而不是按启发式

**能让状态记录变错的工具,必须先能被检查,再能改。**

#### 第三次:只剩 in_progress 的软死局

队列**非空**(所以不播种),但 `next()` 只认 pending,选不出待办 →
整轮 `NOOP`,而且会一直 NOOP 下去。实测第 11 轮就这样空转。

`round()` 是同步的:轮次开头标 in_progress,跑到 TEST 阶段才落回
done 或 pending。**只要一轮正常跑完,就不该有残留**。一旦残留
(进程被 kill、手工改过状态、老实现有 bug)就没有自愈能力。

修法:`recover_stale_in_progress()` 在每轮开头的「取待办」之前调用,
此时队列里所有 in_progress 都是上一轮遗留的。

> **「有人在处理」这个状态不跨轮存活**,软死局才没有藏身之处。

#### 第四次:队列非空但全是鬼影 —— 不变式被满足成文字游戏

第 12 轮后队列 `9 pending / 7 done`,看起来很健康。逐条看:

```
item-14-c49ea6-r3-r1    给关联分析规则接上置信度    ← r1 已做
item-15-4dabda-r3-r1    补架构约束测试              ← r2 已做
check-filter-sql-union-17-r3-r1                        ← r3 已做
project-plan-md-19-r3-r1                               ← r4 已做
san-issuer-fingerprint-18-r3-r1                        ← r5 已做
storage-16-r3-r1                                       ← r7 已做
disappeared-20-r3-r1                                   ← r8 已做
```

**8 条待办全是已完成工作的重推导**,一件真活都没有。

根因:三层播种共用「撞上 id 就改名复活」这一种语义。
而 `backlog.md` 是人写的清单,引擎做完一条只在 `queue.json` 里标 done,
文件本身不删行 —— 播种再读它就又读到同一批,于是无限重新排队。

修法:**三层需要三种不同语义**。

| 层 | 语义 | 为什么 |
|---|---|---|
| Tier 1 backlog | **跳过**已完成的 | 人写下的待办做完了不该自己回来;想重做是显式动作 |
| Tier 2 现状推导 | **总是**提出 | 条件仍成立 = 活确实没干完(规则数仍 < 40) |
| Tier 3 长期项 | **总是**提出 | 本来就是「还会再来」的 L1..L5 |

#### 顺带修掉:id 不能依赖行号

`_slugify_id(title, idx)` 当时被喂的是**行号**。`backlog.md` 是人
手工维护的源文件,任何一处的增删都会让下方所有条目的 id 全变,
`queue.json` 里的 done 记录瞬间失效。

实测踩了两次:一次用 `~~删除线~~` 标完成(改了标题 → 换 id →
`done_ids` 完全拦不住,一次多造 7 条鬼影),一次在文件顶部加 6 行说明
(8 条 id 全变)。

改成 id 只由**标题**派生,序号只在**真出现同名条目**时使用。
人工源文件里的 id 必须是编辑无关的。

> 教训不止一个:测试辅助函数里**重新实现**了一遍 id 派生(按行号),
> 于是测试测的是平行实现而不是真实路径,一路假绿。改成直接调
> `_seed_from_backlog()` 走真实路径。

#### 顺带:done 必须标明是谁断言的

引擎的设计是「不自动改代码」。没有 `build_fn` 时它只看到
「门禁全绿」,看不到「活到底干了没有」。实测里「误报率实测」这条
待办被连标两次 done,而那两轮都没真的做它。

所以 `RoundRecord.completion_source` 记 `build_fn` / `operator`,
`devloop history` 给人工断言的轮次打 `[op]` 标记,轮次摘要里写明
"引擎只验证了门禁"。不区分这两种 done,状态文件就是在替执行者
背书它没做过的事。

---

## 8. 状态落盘（state.json）

### 8.1 文件路径与写时机

- **路径**:`devloop/state.json`（git tracked,以便代码状态通过 git 恢复协议状态）。
- **写时机**:每轮 PLAN 阶段结束时,以及任意门禁失败后立即写。

### 8.2 字段定义

```jsonc
{
  "schema_version": 1,
  "round": 7,                              // 当前轮次,自增
  "phase": "TEST",                         // 当前阶段
  "last_all_green_round": 5,               // 上次全绿轮次(供 R1 用)
  "retreat_count": 1,                      // 累计退路次数
  "baseline": {
    "fail_count": 0,                       // 当前允许的失败数
    "pass_count": 87,                      // 当前通过的测试数
    "loc": 13466,                          // 当前 LOC 快照
    "asof_round": 5                        // baseline 是在哪一轮确立的
  },
  "current_metrics": {                     // 上轮跑出来的指标
    "fail_count": 0,
    "pass_count": 87,
    "loc": 13510
  },
  "gate_history": [                        // 门禁历史,数组,新事件 push 末尾
  ],
  "backlog_snapshot": [                     // 上轮 PLAN 时的 backlog 头部 3 项
  ],
  "last_doc_audit_round": 5                // 用于 G7
}
```

### 8.3 原子写

```python
import json, tempfile, os

def save_state(state: dict, path: str) -> None:
    """原子写:写到 tmp 文件,os.replace 原子替换。"""
    dirpath = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=dirpath, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
```

> **为什么必须原子写**:崩溃恢复——若进程在写一半被 SIGKILL,非原子写会留下半个 JSON,下次启动协议读 state 就崩。原子写要么是旧 state 要么是新 state,不会中间态。

### 8.4 baseline 更新规则

- `baseline.fail_count` 只允许**在人工显式调整时**更新。
- 自动跑过的轮次失败数降低时,**不更新 baseline**,而是记入 `current_metrics` 等待人工 review 后再决定是否升 baseline。
- 这避免"通过刷分让 baseline 变松"。

---

## 9. 落地路线

### 9.1 第一轮（结合已知问题,优先级最高）

| 序号 | 任务 | 来源 | 涉及门禁 |
|---|---|---|---|
| 1.1 | `risk_score.py` 接入 `db/schema.sql` 的 `confidence` 字段 | LH-1 + §3.2 | G4 |
| 1.2 | 37 条规则全部加 `confidence:` 字段(初值 50),把 `risk_score` 公式改为 `risk × confidence / 100` | LH-2 | G5 |
| 1.3 | `check_filter_sql` 加 `UNION` 黑名单(报错而非 silent pass) | LH-3 | G6 (新增) |
| 1.4 | 删除过期 `docs/PROJECT_PLAN.md` 或改写为历史档 | LH-5 | G7 |

### 9.2 第二轮候选

| 序号 | 任务 | 来源 |
|---|---|---|
| 2.1 | 21 条依赖 `findings.title` 的规则改为"指纹匹配 + cross_evidence"双重门 | §3.2 |
| 2.2 | 异常分类改为子类化(`RateLimitError / AuthError / NetworkError / ...`) | LH-4 |
| 2.3 | 加 `cross_evidence` 因子(同资产被 ≥2 source 命中 → confidence × 1.5) | §3.2 |

### 9.3 第三轮候选

| 序号 | 任务 | 来源 |
|---|---|---|
| 3.1 | 加 `temporal_consistency` 因子(同资产 ≥3 轮稳定 → confidence × 1.3) | §3.2 |
| 3.2 | 写 1 个最小的 HunterX 风格架构测试 | §3.1 |

### 9.4 什么时候需要人工介入

| 触发 | 介入内容 |
|---|---|
| `RETREAT_COUNT` 累加到 5 | 人工判断是否调整门禁阈值或协议本身 |
| 同一 G 连续 4 轮失败 | 冻结该 G,人工决定是否废除 |
| baseline 调整请求 | 人工 review 后才能 `baseline.fail_count -= 1` |
| L1 铁律变更申请 | 边界情形,必须人工签字 |
| 新 optional 依赖启用申请 | 同上 |

---

## 10. 反模式清单

下列行为会破坏协议。任何发现即视为协议失效信号。

| 反模式 | 为什么坏 | 如何识别 |
|---|---|---|
| 为**让门禁变绿**而改门禁本身 | 防线失效 | `git log` 看是否某轮专门改门禁代码但任务说明无变更 |
| 往 backlog 塞永远做不完的大任务 | 让 L2、L3 永不触发,L1 也被稀释 | backlog 任务超过 3 轮未推进 |
| 跳过 TEST 直接进 IMPROVE | 改 A 坏 B 的经典路径 | `state.json.round_history` 看某轮 phase 序列缺 TEST |
| 修改 baseline 来掩盖回归 | 让 G2 永远绿 | `git diff state.json` 看是否仅 fail_count 变化 |
| 引入 `from X import Y` 其中 X 在 stdlib 之外 | L1 失守 | G1 |
| `git reset --hard` 来"快进" | 丢失 REVERT 历史 | `git reflog` 看是否有 reset |
| 把 `state.json` 加进 `.gitignore` | 协议状态无法跨机器恢复 | 看 `.gitignore` |
| 给门禁加 `if os.getenv("SKIP_GATE")` 绕过 | 防线失效 | grep `SKIP_GATE\|BYPASS_GATE` |
| 把 pytest 标记为 `@pytest.mark.skip` 而不修 | 假装通过,实则漏判 | grep `pytest.mark.skip` 数量突增 |
| 在 `prompt_injection_guard` 加 title 例外 | 安全防线回退 | G6 |

---

## 11. 附录

### 11.1 术语表

| 术语 | 含义 |
|---|---|
| **轮次（round）** | BUILD → TEST → IMPROVE → PLAN → 落盘 的完整序列 |
| **阶段（phase）** | 一个轮次中的四个子阶段 |
| **门禁（gate）** | 自动/半自动检查,违反即触发退路 |
| **退路（retreat）** | 协议遇到失败时的回退机制,有 R0-R3 四级 |
| **铁律（law）** | 不可议的项目设计约束,见 §2 |
| **baseline** | 测试通过的基线快照,见 §8.2 |
| **L1-L3 播种** | 见 §7.1,backlog 永不枯竭的三层来源 |
| **死源** | 数据源请求失败但被报告如实标出的状态 |

### 11.2 关键文件位置速查

| 文件 | 作用 |
|---|---|
| `devloop/baselines.json` | 门禁基准 + `_promotions` 提升留痕(进版本库) |
| `devloop/backlog.md` | 人工维护的任务清单 |
| `devloop/queue.json` | 运行时队列(每机一份,已 gitignore) |
| `arl_lite/devloop/` | 协议实现:state / protocol / gates / queue / accept / cli |
| `docs/devloop-protocol.md` | 本文档 |

### 11.3 命令速查

全部挂在主 CLI 下,`python3 -m arl_lite` 等价于安装后的 `arl-lite`:

| 命令 | 作用 |
|---|---|
| `arl-lite devloop status` | 当前轮次/阶段/门禁累计/队列统计/队首待办 |
| `arl-lite devloop test` | 只跑全部门禁,不记轮次(改完先验一下) |
| `arl-lite devloop test --gates loc_budget,test_baseline` | 只跑指定门禁 |
| `arl-lite devloop round` | 跑一整轮并落盘;门禁红则记 `DONE_WITH_FAILURES`,退出码 3 |
| `arl-lite devloop plan` | 只做规划,看下一步 |
| `arl-lite devloop history -n 10` | 最近 N 轮的通过/失败明细 |
| `arl-lite devloop add <id> "标题" -p 2` | 手动加待办 |
| `arl-lite devloop gate <name>` | 跑单个门禁并打印实测值 |
| `arl-lite devloop accept <name> --reason "..."` | 门禁红了且确认可放宽时,显式提升 baseline(见 5.5) |
| `arl-lite devloop promotions` | 看 baseline 提升历史(来自 `_promotions`) |
| `arl-lite devloop repair --dry-run` | 查队列里的重复 id(只看会怎么改) |
| `arl-lite devloop repair` | 修复重复 id |
| `arl-lite devloop unmark <id> --reason "..."` | 把误标的 done/dropped 改回 pending |

门禁也可以脱离主 CLI 单独跑:

```bash
python3 -m arl_lite.devloop.gates               # 全跑
python3 -m arl_lite.devloop.gates no_import_cycle   # 单跑
```

### 11.4 协议变更记录

| 日期 | 变更 |
|---|---|
| 2026-10-02 | 初版,基于 AtlasX/ARL/SpiderFoot 源码直读 + 项目已知问题清单 |
| 2026-10-02 | 协议落地为 `arl_lite/devloop/`(state/protocol/gates/queue/cli),纯 stdlib |
| 2026-10-02 | 修正 5.4:G4 阈值由「18,000 行绝对值」改为「baseline + 300 容差」——绝对值把仓库体量与单轮增量混为一谈 |
| 2026-10-02 | 新增 5.5:baseline 提升流程(`devloop accept`),解决 G4 失败后的死路 |
| 2026-10-02 | 修不变式 #4 的两处破裂(见 7.6):PLAN 阶段跑在队列更新之前;`seed_if_empty` 跳过已存在 id 导致重复 id 死循环 |

---

> **最后一句**:
> 协议的敌人不是缺陷,是**遗忘**。每跑完一轮,在 state.json 写一行;每季度读一次本文档,看哪些设计决策今天已经不适用了。能改即改。