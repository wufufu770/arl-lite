# devloop 待办清单（人工追加）

格式：`- [P0..P3] kind: title | detail | verify`

kind 取值：`change` / `test` / `doc` / `research` / `refactor`

这条清单是循环的**长期燃料**。队列耗尽时 `seed_if_empty()` 会先读这里；
这里空了才会退回固定的长期演进项（`queue.py` 的 `_FALLBACK`）。

> **第 16 轮起，原来夹在中间的那层「扫代码现状自动推导」已整层删除。**
> 它的五条检查逐条实测，**没有一条能产出一件真活**：三条阈值早就过了
> 永不触发，一条提不出东西，还有一条（对齐 PROJECT_PLAN.md）
> **它自己的验收条件此刻就已通过**，于是永远完不成却一直挂在队头。
> 逐条数据见 `arl_lite/devloop/queue.py` 的 `REMOVED_TIER2_WHY`，
> 原因分析见 `docs/devloop-protocol.md` 7.8 节。
>
> **所以「目标数字」请写在这里，不要指望自动推导。** 「规则数要到 40」
> 「数据源要到 12」这类阈值是**决定**，不是从仓库现状推出来的事实，
> 而一个没人拥有的数字永远不会被更新 —— 要么永不触发，要么逼着人
> 为了凑数而干活。
>
> 写在这里的条目要注意一件事：**`verify` 此刻不能已经通过**。
> 验收条件恒真的待办是假活（没人能"重新做一遍"一个已经为真的条件）。
> 周期性任务请用带时间边界的判据，参考 `queue._FRESH_WITHIN()`。

---

## 已排期

<!-- 播种按 id 跳过已完成的条目,所以下面这些"已完成"的行不会再被捡回来。
     保留它们是为了让"这活做过、怎么做的"在源文件里也留个痕。
     **注意:标题不要改** —— id 是从标题派生的(`_slugify_id`),
     改标题等于换一个 id,`done` 记录拦不住,立刻变成一条新待办。
     完成标记写进 detail 段。 -->

- [P1] change: 给关联分析规则接上置信度 | ✅ r1 完成 · 新建 core/confidence.py 四因子乘法模型,37 条规则分档 | python3 -m arl_lite.devloop gate rules_have_advice
- [P1] change: 补架构约束测试 | ✅ r2 完成 · 11 条约束全部做反向验证 | python3 -m pytest tests/test_architecture.py
- [P2] change: storage 异常分类去字符串化 | ✅ r7 完成 · 改用 sqlite_errorcode 精确判定;实测 NOT NULL 误判成重复会静默丢数据 | python3 -m pytest tests/test_db_errors.py
- [P2] change: check_filter_sql 补 UNION 禁令 | ✅ r3 完成 · 实测 `1=1) UNION SELECT ...` 确能跨表读数据 | python3 -m pytest tests/test_sql_injection.py
- [P2] change: 证书 SAN/issuer/fingerprint 入库 | ✅ r5/r6 完成 · 自研 DER 解析(零依赖 + 必须采自签站) | python3 -m pytest tests/test_tls_cert.py
- [P2] doc: 重写 PROJECT_PLAN.md | ✅ r4 完成 · 按实现现状重写;门禁首次 0 warned | python3 -m arl_lite.devloop gate doc_freshness
- [P3] change: 实现 DISAPPEARED 变更类型 | ✅ r8 完成 · 用 first_seen/last_seen 判断,不需要 snapshot 表 | python3 -m pytest tests/test_monitor_disappeared.py
- [P3] research: 误报率实测 | ✅ r14 完成 · 离线受控样本(14 个),并借此修掉 4 个规则真 bug;产出 docs/FP_RATE.md,`arl-lite fp-bench` 可复跑 | python3 -m arl_lite fp-bench

## 候选（待细化）

- 离线内嵌 RIPE/APNIC/ARIN delegated 数据做 IP→ASN 归属（约 16MB，公约免费）
  - 注意：IP2Location LITE 是 CC BY-SA（传染性）、CAIDA 是 CC BY-NC（禁商用），**都不能内嵌**
- CDN/云厂商 IP 段清单（AWS/Cloudflare/Fastly 公开可自动更新；阿里云/腾讯云/华为云**无官方 JSON 端点**，只能人工季度复核）
- 变更监控去抖 + 基线学习（5 轮窗口，连续 3 次同状态判为稳定基线）
