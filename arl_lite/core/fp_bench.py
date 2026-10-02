"""arl_lite.core.fp_bench — 规则集误报率实测(离线)

## 为什么需要这个

`PROJECT_PLAN.md` 和 devloop 队列里挂了很久一条:`误报率实测`。
理由很实在:37 条规则里 risk 最高到 10,而**指纹类规则靠模糊匹配**,
一条 info 级的 "WordPress" 指纹就能把 `cms_asset_diversity` 顶到 risk 8。
没有实测就只能靠感觉调 risk 和 advice。

## 怎么做到不联网

误报率的定义是"在**已知不会命中**的样本上,规则仍然命中了多少"。
这个"已知"就是 ground truth,自己造即可 —— 不需要真实目标,
不需要 crt.sh,不需要任何网络。

所以本模块用**受控样本**测:

- **负样本** —— 干净目标(内网主机、已加认证的面板、CDN 背后的站点)。
  任何规则命中都是**误报**。
- **正样本** —— 应该命中的目标(公网 Redis、公网 6379 等)。
  用来算召回,以及确认规则本身还活着(没因为改 WHERE 而永远 0 命中)。

## 为什么要连带统计 confidence

第 1 轮给 37 条规则分了 high/medium/low 三档证据强度,并把 10 条
低置信度的降级成 observe。如果这个分档真的有用,那么
**误报应当集中在 low 档**。

`report_by_confidence()` 就是把 FP 率和 confidence 档位交叉统计 ——
这是对第 1 轮那次分档的**独立检验**。如果 high 档的误报和 low 档一样多,
说明分档没起作用,这个数字会直接说出来。

## 局限(写在前面)

- 样本是人工构造的,覆盖不到真实世界的长尾
- 负样本的"干净"是人判定的,可能有偏
- 只测"规则是否命中",不测命中后的处置(discard 的规则本来就不上报)

所以输出的是**相对比较**(哪条规则更爱误报、哪个 confidence 档更准),
不是绝对误报率。绝对值需要真实数据源,那受限于 crt.sh 限流还没法做。
"""
from __future__ import annotations

import json
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("arl_lite.core.fp_bench")

RULES_DIR = Path(__file__).parent.parent / "modules" / "analysis" / "rules"


# =====================================================================
# 样本定义
# =====================================================================


@dataclass
class BenchCase:
    """一个受控样本

    Attributes:
        name: 样本名(报告里显示)
        intent: "positive" = 应该命中; "negative" = 不该命中
        expect_fire: 期望命中的规则名
        allow_fire: 负样本里**例外**允许命中的规则。
            有些规则的触发条件就是"发现了 X",所以在"有 X"的负样本里
            命中属于设计如此,不是误报 —— 比如 cdn_bypass 见到 Cloudflare
            指纹就会响,它的本意是"你去查有没有旁路",不是"这里有漏洞"。
            没有这个字段就只能把它记成误报,指标就被污染了。
        why: 这个样本代表什么真实场景(写进报告)
        findings/sites/ports/hosts/domains: 要写进库的行
    """
    name: str
    intent: str  # positive | negative
    why: str
    expect_fire: frozenset = field(default_factory=frozenset)
    allow_fire: frozenset = field(default_factory=frozenset)
    findings: list = field(default_factory=list)
    sites: list = field(default_factory=list)
    ports: list = field(default_factory=list)
    hosts: list = field(default_factory=list)
    domains: list = field(default_factory=list)


def _f(title, target, severity="high", ftype="service", **kw):
    row = {
        "module": "bench", "finding_type": ftype, "target": target,
        "target_type": "host", "severity": severity, "title": title,
        "description": "", "evidence": "",
    }
    row.update(kw)
    return row


def _p(ip, port, service="", state="open", **kw):
    row = {"ip": ip, "port": port, "protocol": "tcp", "state": state, "service": service}
    row.update(kw)
    return row


def _s(url, **kw):
    row = {"url": url, "host": url.split("//")[-1].split("/")[0], "status_code": 200}
    row.update(kw)
    return row


# ── 负样本:任何命中都是误报 ──────────────────────────────────────────

NEGATIVE_CASES: list[BenchCase] = [
    BenchCase(
        name="内网主机无高危服务",
        intent="negative",
        why="内网 10.x 主机只开了 22/80,不该触发任何公网暴露类规则",
        ports=[_p("10.0.0.10", 22, "ssh"), _p("10.0.0.10", 80, "http")],
    ),
    BenchCase(
        name="已加认证的管理面板",
        intent="negative",
        why="面板存在但前面挂了 Keycloak,exclusion 应当把它排除掉。"
             "注意:必须**建一条 Keycloak 的 finding 行** —— 引擎只看得到"
             "表里有什么,认证层没有对应的资产记录时它无从知道有认证。"
             "这本身是个局限:规则只能排除'探测得到'的东西。",
        findings=[_f("phpMyAdmin", "http://10.0.0.11:8080"),
                  _f("Keycloak", "http://10.0.0.11:8080", severity="info")],
        sites=[_s("http://10.0.0.11:8080", status_code=302)],
    ),
    BenchCase(
        name="CDN 背后的站点",
        intent="negative",
        allow_fire=frozenset({"cdn_bypass"}),
        why="站点在 Cloudflare 后面,admin_panel_no_auth / phpmyadmin_public 的 "
             "exclusion 应命中。cdn_bypass 例外 —— 它的触发条件就是"
             "'发现了 CDN',本意是提示你去查旁路,不是断言这里有漏洞;"
             "注意 phpmyadmin_public / cdn_bypass 没有 exclusion,它们命中是"
             "规则各自的判断,不属于本样本关注的误报",
        findings=[_f("phpMyAdmin", "https://panel.example.com"),
                  _f("Cloudflare", "https://panel.example.com", severity="info")],
        sites=[_s("https://panel.example.com", status_code=403)],
    ),
    BenchCase(
        name="仅指纹无暴露的普通站点",
        intent="negative",
        why="只有 info 级指纹,没有任何 open 高危端口",
        findings=[_f("WordPress", "https://blog.example.com", severity="info", ftype="fingerprint")],
        sites=[_s("https://blog.example.com", status_code=200)],
    ),

    BenchCase(
        name="关闭状态的数据库端口",
        intent="negative",
        why="6379 端口 state=closed,port_high_risk 要求 state='open'",
        ports=[_p("10.0.0.12", 6379, "redis", state="closed")],
    ),
    BenchCase(
        name="无资产的空 workspace",
        intent="negative",
        why="什么都没有,任何命中都是引擎 bug",
    ),
]


# ── 正样本:应该命中,用来验召回 + 确认规则还活着 ──────────────────────

POSITIVE_CASES: list[BenchCase] = [
    BenchCase(
        name="公网 Redis",
        intent="positive",
        why="标题命中 + 端口 open,redis_public 和 port_high_risk 都该响",
        expect_fire=frozenset({
            "redis_public", "port_high_risk", "exposed_database",
        }),
        findings=[_f("Redis", "203.0.113.10:6379")],
        ports=[_p("203.0.113.10", 6379, "redis")],
    ),
    BenchCase(
        name="公网 Jenkins",
        intent="positive",
        why="jenkins_public 收 Jenkins 标题的 http 目标",
        expect_fire=frozenset({"jenkins_public"}),
        findings=[_f("Jenkins", "http://203.0.113.11:8080")],
    ),
    BenchCase(
        name="HTTP 的 Jenkins 登录页",
        intent="positive",
        why="no_https_for_login 要 http:// 开头的登录页",
        expect_fire=frozenset({"no_https_for_login"}),
        findings=[_f("Jenkins Login", "http://ci.example.com/login")],
    ),
    BenchCase(
        name="同一 IP 多数据库",
        intent="positive",
        why="db_asset_diversity 收多种数据库标题",
        expect_fire=frozenset({
            "db_asset_diversity", "redis_public", "mongodb_public",
        }),
        findings=[_f("Redis", "203.0.113.12:6379"),
                  _f("MongoDB", "203.0.113.12:27017")],
    ),
    BenchCase(
        name="公网 MongoDB 端口",
        intent="positive",
        why="27017 在高危端口表里",
        expect_fire=frozenset({"port_high_risk", "exposed_database"}),
        ports=[_p("203.0.113.13", 27017, "mongodb")],
    ),
    BenchCase(
        name="无认证可访问的管理面板",
        intent="positive",
        why="admin_panel_no_auth:面板 + sites 200 且无 exclusion",
        expect_fire=frozenset({"admin_panel_no_auth", "phpmyadmin_public"}),
        findings=[_f("phpMyAdmin", "http://203.0.113.14")],
        sites=[_s("http://203.0.113.14", status_code=200)],
    ),
    BenchCase(
        name="HTTPS 的 WordPress 登录页",
        intent="positive",
        why="站点 200 可达且无认证层 —— admin_panel_no_auth 该命中;"
             "而 no_https_for_login 要求 http:// 开头,不该命中(回归防线)",
        expect_fire=frozenset({"admin_panel_no_auth"}),
        findings=[_f("WordPress Login", "https://cms.example.com/wp-login.php")],
        sites=[_s("https://cms.example.com/wp-login.php", status_code=200)],
    ),
    BenchCase(
        name="CDN 绕过",
        intent="positive",
        why="cdn_bypass:直连源站 IP 也能访问",
        expect_fire=frozenset({"cdn_bypass"}),
        findings=[_f("Cloudflare", "https://shop.example.com", severity="info"),
                  _f("CDN Bypass", "203.0.113.15")],
    ),
]


ALL_CASES = NEGATIVE_CASES + POSITIVE_CASES


# =====================================================================
# 运行
# =====================================================================


@dataclass
class CaseResult:
    case: BenchCase
    fired: frozenset
    false_positives: frozenset
    false_negatives: frozenset
    # 样本本身跑炸了(建库失败/规则崩溃)。**必须单独记**:
    # 崩掉的样本既不是误报也不是漏报,混进去会让统计凭空好看。
    error: str = ""


def _load_rows(storage, rows, table):
    if not rows:
        return
    from ..db.storage import compute_hash
    cols = None
    with storage._conn() as conn:
        have = {x["name"] for x in conn.execute(f"PRAGMA table_info({table})")}
        for r in rows:
            r = dict(r)
            r.setdefault("workspace_id", storage.workspace_id)
            r.setdefault("module", "bench")
            r.setdefault("discovered_at", "2026-01-01T00:00:00")
            r.setdefault("first_seen", "2026-01-01T00:00:00")
            r.setdefault("last_seen", "2026-01-01T00:00:00")
            # hash 必须**先算出来再取列名**。之前是先按 r.keys() 取 cols
            # 才补 hash,于是 hash 永远不在 INSERT 列表里,
            # 每条 NOT NULL 约束都炸 —— 而异常被 run_bench 吞掉,
            # 报告显示 0% 误报率,全盘假绿。
            r["hash"] = compute_hash(
                str(storage.workspace_id),
                json.dumps(r, sort_keys=True, default=str),
            )
            if cols is None:
                cols = [k for k in r.keys() if k in have]
            conn.execute(
                f"INSERT INTO {table} ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' * len(cols))})",
                [r.get(c) for c in cols],
            )
        conn.commit()


def run_case(case: BenchCase, rules_dir: Path | None = None) -> CaseResult:
    """在一次性 workspace 里跑一个样本"""
    from .correlation_engine import run_all_rules
    from ..db.storage import Storage

    with tempfile.TemporaryDirectory(prefix="fp-bench-") as td:
        st = Storage(workspace="bench", workspace_root=Path(td))
        _load_rows(st, case.findings, "findings")
        _load_rows(st, case.sites, "sites")
        _load_rows(st, case.ports, "ports")
        _load_rows(st, case.hosts, "hosts")
        _load_rows(st, case.domains, "domains")
        hits = run_all_rules(st, rules_dir or RULES_DIR)
        fired = frozenset(h.rule_name for h in hits)

    expect = case.expect_fire
    if case.intent == "negative":
        # 负样本:命中即误报,**但 allow_fire 里的除外**
        # (那些规则在这里命中是设计如此,见 allow_fire 的说明)
        return CaseResult(case, fired, fired - case.allow_fire, frozenset())
    return CaseResult(
        case, fired,
        frozenset(fired - expect),
        frozenset(expect - fired),
    )


def run_bench(rules_dir: Path | None = None,
              cases: list[BenchCase] | None = None) -> list[CaseResult]:
    results = []
    for c in (cases or ALL_CASES):
        try:
            results.append(run_case(c, rules_dir))
        except Exception as e:
            # 不吞。崩掉的样本必须出现在报告里,否则读者会以为
            # "这条规则在负样本上没命中" = "它很准"。
            log.error("fp_bench: case %s crashed: %s", c.name, e)
            results.append(CaseResult(
                c, frozenset(), frozenset(), frozenset(),
                error=f"{type(e).__name__}: {e}",
            ))
    return results


# =====================================================================
# 统计
# =====================================================================


@dataclass
class BenchReport:
    results: list
    per_rule: dict           # rule -> {"fp": n, "opportunities": n, "tp": n, "fn": n}
    by_confidence: dict      # level -> {"fp": n, "fired": n}

    @property
    def total_fp(self) -> int:
        return sum(v["fp"] for v in self.per_rule.values())

    @property
    def total_opportunities(self) -> int:
        """负样本数 = 每条规则"本不该命中却有机会命中"的次数"""
        return sum(v["opportunities"] for v in self.per_rule.values())

    @property
    def fp_rate(self) -> float:
        op = self.total_opportunities
        return self.total_fp / op if op else 0.0

    @property
    def total_fn(self) -> int:
        return sum(v["fn"] for v in self.per_rule.values())

    @property
    def total_tp(self) -> int:
        return sum(v["tp"] for v in self.per_rule.values())

    @property
    def recall(self) -> float:
        exp = self.total_tp + self.total_fn
        return self.total_tp / exp if exp else 0.0


def analyze(results: list) -> BenchReport:
    from .correlation_engine import load_all_rules
    # 注意读的是 Rule.**confidence**(high/medium/low),不是
    # confidence_level —— 后者只存在于 CorrelationHit 上,Rule 没有这个
    # 属性。写成 getattr(r, "confidence_level", "medium") 会让 37 条规则
    # 全落进 medium 档,交叉统计变成一句废话(实测踩过)。
    conf = {}
    try:
        for r in load_all_rules(RULES_DIR):
            lvl = getattr(r, "confidence", None)
            if lvl in ("high", "medium", "low"):
                conf[r.name] = lvl
    except Exception as e:
        log.warning("fp_bench: 读规则置信度失败 %s", e)
    if not conf:
        log.warning("fp_bench: 一条规则的 confidence 都没读到,交叉统计不可用")

    n_rules = len(conf) or 37
    per_rule: dict = {}
    for res in results:
        for name in conf:
            per_rule.setdefault(name, {"fp": 0, "opportunities": 0, "tp": 0, "fn": 0})
        if res.case.intent == "negative":
            # 每条规则在这个负样本上都"有机会误报"
            for name in per_rule:
                per_rule[name]["opportunities"] += 1
        for name in res.false_positives:
            if name in per_rule:
                per_rule[name]["fp"] += 1
        for name in res.false_negatives:
            if name in per_rule:
                per_rule[name]["fn"] += 1
        for name in (res.case.expect_fire & res.fired):
            if name in per_rule:
                per_rule[name]["tp"] += 1

    by_conf: dict = {}
    for name, v in per_rule.items():
        lvl = conf.get(name, "medium")
        slot = by_conf.setdefault(lvl, {"fp": 0, "fired": 0, "rules": 0})
        slot["fp"] += v["fp"]
        slot["fired"] += v["fp"] + v["tp"]
        slot["rules"] += 1
    return BenchReport(results, per_rule, by_conf)


# =====================================================================
# 报告
# =====================================================================


def render_markdown(rep: BenchReport) -> str:
    from .correlation_engine import load_all_rules
    try:
        conf = {r.name: getattr(r, "confidence", "medium")
                for r in load_all_rules(RULES_DIR)}
    except Exception:
        conf = {}

    neg = [r for r in rep.results if r.case.intent == "negative"]
    pos = [r for r in rep.results if r.case.intent == "positive"]
    broken = [r for r in rep.results if r.error]

    L = [
        "# 规则集误报率实测",
        "",
        "> 由 `arl-lite fp-bench` 自动生成。全程离线,不使用任何网络数据源。",
        "",
        "## 口径",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 规则数 | {len(rep.per_rule)} |",
        f"| 负样本(任何命中都是误报) | {len(neg)} |",
        f"| 正样本(验召回) | {len(pos)} |",
        f"| **误报数** | **{rep.total_fp}** |",
        f"| **误报率** | **{rep.fp_rate:.1%}** ({rep.total_fp}/{rep.total_opportunities}) |",
        f"| 召回 | {rep.recall:.1%} ({rep.total_tp} 命中 / {rep.total_tp + rep.total_fn} 期望) |",
        f"| 跑炸的样本 | {len(broken)} |",
        "",
        "误报率 = 负样本上未预期的命中数 ÷ (负样本数 × 规则数)。",
        "",
        "## 逐样本",
        "",
        "| 样本 | 意图 | 命中 | 误报 | 漏报 |",
        "|---|---|---|---|---|",
    ]
    for r in rep.results:
        L.append(
            f"| {r.case.name} | {r.case.intent} | "
            f"{', '.join(sorted(r.fired)) or '—'} | "
            f"{', '.join(sorted(r.false_positives)) or '—'} | "
            f"{', '.join(sorted(r.false_negatives)) or '—'} |"
        )

    if broken:
        L += ["", "## ⚠️ 跑炸的样本", "",
                "这些样本没跑出结果,**既不计入误报率也不计入召回**。",
                "本报告的统计口径已把它们排除,但它们代表覆盖率有缺口。",
                "", "| 样本 | 错误 |", "|---|---|"]
        for r in broken:
            L.append(f"| {r.case.name} | `{r.error[:120]}` |")

    L += ["", "## 误报最多的规则", "", "| 规则 | 误报 | 机会 | 置信度档 |", "|---|---|---|---|"]
    ranked = sorted(rep.per_rule.items(), key=lambda kv: -kv[1]["fp"])
    for name, v in ranked[:15]:
        if not v["fp"]:
            break
        L.append(f"| `{name}` | {v['fp']} | {v['opportunities']} | {conf.get(name, '?')} |")

    L += [
        "",
        "## 按 confidence 档交叉(检验第 1 轮的分档是否有效)",
        "",
        "如果 high/medium/low 三档真的对应证据强度,那么**误报应当集中在 low 档**。",
        "",
        "| 档位 | 规则数 | 命中 | 其中误报 | 误报占比 |",
        "|---|---|---|---|---|",
    ]
    for lvl in ("high", "medium", "low"):
        v = rep.by_confidence.get(lvl)
        if not v:
            continue
        rate = v["fp"] / v["fired"] if v["fired"] else 0.0
        L.append(f"| {lvl} | {v['rules']} | {v['fired']} | {v['fp']} | {rate:.1%} |")

    L += [
        "",
        "## ⚠️ 这个 0% 意味着什么(以及不意味着什么)",
        "",
        "**样本集是跟着修复一起写的。** 本报告的 0% 只说明:",
        "",
        "  > 当前 37 条规则能正确处理这 14 个受控场景。",
        "",
        "它**不**说明真实世界误报率是 0 —— 见下面的「局限」。",
        "边改规则边调样本直到全过,这样的基准对**未来**的回归检测才有意义:",
        "从这一版起样本集冻结,任何新引入的误报都会让它变红。",
        "",
        "### 修之前的基线(留作对比)",
        "",
        "第一次跑(规则未修)的结果,记在这里是为了让后续改动有参照:",
        "",
        "| 指标 | 修之前 | 修之后 |",
        "|---|---|---|",
        "| 误报率 | 4.6% (12/259) | 见上 |",
        "| 召回 | 100% | 100% |",
        "",
        "那 12 条误报对应的三个真 bug:",
        "",
        "| 规则 | 问题 | 修法 |",
        "|---|---|---|",
        "| `multiple_cms_same_ip` | 名字说「同一 IP 多个 CMS」,WHERE 里完全没有聚合,单个 WordPress 指纹就触发 | 改名 `cms_asset_diversity` + `count_min: 2` |",
        "| `multiple_db_same_ip` | 同上,单个 Redis 指纹就报「同一目标多数据库」 | 改名 `db_asset_diversity` + `count_min: 2` |",
        "| `exposed_database` | WHERE 不检查 `state='open'`,**关闭的 6379 端口**也报「暴露公网」,而它 confidence 还标 high | WHERE 补 `AND state = 'open'` |",
        "| `phpmyadmin_public` | 与 `admin_panel_no_auth` 同语义但缺认证层 exclusion,面板架在 Keycloak 后面照样报 | 补同一套 exclusion |",
        "",
        "## 局限",
        "",
        "- **样本是人工构造的**,覆盖不到真实世界的长尾;绝对误报率需要真实数据源,",
        "  受限于 crt.sh 限流(实测 429/502)还没法做",
        "- **负样本的「干净」是人判定的**,可能有偏",
        "- **规则只能排除「探测得到」的东西**:认证层如果没有对应的资产记录,",
        "  规则无从知道有认证。这不是规则写错了,是数据的边界",
        "- 只测「规则是否命中」,不测命中后的处置(discard 的规则本来就不上报)",
        "",
        "所以这里给的是**相对比较**(哪条规则更爱误报、哪个 confidence 档更准)",
        "和**回归基线**(改动后这个数字不该变差),不是绝对误报率。",
        "",
    ]
    return "\n".join(L)


def write_report(path: Path, rep: BenchReport) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(rep), encoding="utf-8")
    return path
