# 规则集误报率实测

> 由 `arl-lite fp-bench` 自动生成。全程离线,不使用任何网络数据源。

## 口径

| 项 | 值 |
|---|---|
| 规则数 | 37 |
| 负样本(任何命中都是误报) | 6 |
| 正样本(验召回) | 8 |
| **误报数** | **0** |
| **误报率** | **0.0%** (0/222) |
| 召回 | 100.0% (14 命中 / 14 期望) |
| 跑炸的样本 | 0 |

误报率 = 负样本上未预期的命中数 ÷ (负样本数 × 规则数)。

## 逐样本

| 样本 | 意图 | 命中 | 误报 | 漏报 |
|---|---|---|---|---|
| 内网主机无高危服务 | negative | — | — | — |
| 已加认证的管理面板 | negative | — | — | — |
| CDN 背后的站点 | negative | cdn_bypass | — | — |
| 仅指纹无暴露的普通站点 | negative | — | — | — |
| 关闭状态的数据库端口 | negative | — | — | — |
| 无资产的空 workspace | negative | — | — | — |
| 公网 Redis | positive | exposed_database, port_high_risk, redis_public | — | — |
| 公网 Jenkins | positive | jenkins_public | — | — |
| HTTP 的 Jenkins 登录页 | positive | no_https_for_login | — | — |
| 同一 IP 多数据库 | positive | db_asset_diversity, mongodb_public, redis_public | — | — |
| 公网 MongoDB 端口 | positive | exposed_database, port_high_risk | — | — |
| 无认证可访问的管理面板 | positive | admin_panel_no_auth, phpmyadmin_public | — | — |
| HTTPS 的 WordPress 登录页 | positive | admin_panel_no_auth | — | — |
| CDN 绕过 | positive | cdn_bypass | — | — |

## 误报最多的规则

| 规则 | 误报 | 机会 | 置信度档 |
|---|---|---|---|

## 按 confidence 档交叉(检验第 1 轮的分档是否有效)

如果 high/medium/low 三档真的对应证据强度,那么**误报应当集中在 low 档**。

| 档位 | 规则数 | 命中 | 其中误报 | 误报占比 |
|---|---|---|---|---|
| high | 13 | 6 | 0 | 0.0% |
| medium | 14 | 5 | 0 | 0.0% |
| low | 10 | 3 | 0 | 0.0% |

## ⚠️ 这个 0% 意味着什么(以及不意味着什么)

**样本集是跟着修复一起写的。** 本报告的 0% 只说明:

  > 当前 37 条规则能正确处理这 14 个受控场景。

它**不**说明真实世界误报率是 0 —— 见下面的「局限」。
边改规则边调样本直到全过,这样的基准对**未来**的回归检测才有意义:
从这一版起样本集冻结,任何新引入的误报都会让它变红。

### 修之前的基线(留作对比)

第一次跑(规则未修)的结果,记在这里是为了让后续改动有参照:

| 指标 | 修之前 | 修之后 |
|---|---|---|
| 误报率 | 4.6% (12/259) | 见上 |
| 召回 | 100% | 100% |

那 12 条误报对应的三个真 bug:

| 规则 | 问题 | 修法 |
|---|---|---|
| `multiple_cms_same_ip` | 名字说「同一 IP 多个 CMS」,WHERE 里完全没有聚合,单个 WordPress 指纹就触发 | 改名 `cms_asset_diversity` + `count_min: 2` |
| `multiple_db_same_ip` | 同上,单个 Redis 指纹就报「同一目标多数据库」 | 改名 `db_asset_diversity` + `count_min: 2` |
| `exposed_database` | WHERE 不检查 `state='open'`,**关闭的 6379 端口**也报「暴露公网」,而它 confidence 还标 high | WHERE 补 `AND state = 'open'` |
| `phpmyadmin_public` | 与 `admin_panel_no_auth` 同语义但缺认证层 exclusion,面板架在 Keycloak 后面照样报 | 补同一套 exclusion |

## 局限

- **样本是人工构造的**,覆盖不到真实世界的长尾;绝对误报率需要真实数据源,
  受限于 crt.sh 限流(实测 429/502)还没法做
- **负样本的「干净」是人判定的**,可能有偏
- **规则只能排除「探测得到」的东西**:认证层如果没有对应的资产记录,
  规则无从知道有认证。这不是规则写错了,是数据的边界
- 只测「规则是否命中」,不测命中后的处置(discard 的规则本来就不上报)

所以这里给的是**相对比较**(哪条规则更爱误报、哪个 confidence 档更准)
和**回归基线**(改动后这个数字不该变差),不是绝对误报率。
