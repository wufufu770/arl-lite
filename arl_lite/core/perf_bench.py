"""arl_lite.core.perf_bench — 核心流水线性能基线(纯 stdlib)

## 测什么,不测什么

这个工具测的是 **arl-lite 自己算得动的部分**:存储写入、规则引擎、
置信度/风险打分、关联引擎。

**它不测网络。** 子域名枚举和 HTTP 探测的真实耗时由 crt.sh / 目标站
的响应决定,与本项目代码无关 —— 而且本项目实测 crt.sh 限流严重
(429/502),拿它当基准得到的数字既不可复现也没有比较意义。

所以基线里的数字是**下界**:真实一次侦察 = 本基线 + 网络往返。
把它们混在一起报,就成了一个没人能复现、也没人敢拿来比较的数。

这一点和 `fp_bench` 的纪律一致:**能测的测准,测不了的写明测不了。**

## 为什么要有基线

没有基线时,任何"变慢了"都是猜。有了基线,下一轮改动能回答一个
具体问题:37 条规则跑 1 万行数据要多久?加一条规则的边际成本是多少?
写入路径有没有出现 N+1?

## 用法

    arl-lite perf-bench                  # 跑标准规模,**只测不写**,数字打在 stdout
    arl-lite perf-bench --scale small   # 快速冒烟(档位是 small/medium/large)
    arl-lite perf-bench --repeat 3       # 取 3 次的中位数,抗抖动
    arl-lite perf-bench --out /tmp/p.md  # 写到别处
    arl-lite perf-bench --in-place       # 确定要覆盖已提交的性能基线时才用

默认**不写** `docs/PERF_BASELINE.md`:那是版本库里已提交的基线,
一次 `--scale small` 冒烟就足以把 medium 档的整表换掉,而且退出码是 0。
"""
from __future__ import annotations

import gc
import json
import logging
import statistics
import tempfile
import time
import tracemalloc
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

RULES_DIR = Path(__file__).parent.parent / "modules" / "analysis" / "rules"

# 规模档位。刻意取二的幂,方便看出"是不是线性"——
# 2x 数据不是 2x 时间,就说明有非线性成分(索引缺失/N+1)。
SCALES: dict[str, dict[str, int]] = {
    "small":  {"domains": 200,    "hosts": 2000,   "ports": 2000,  "findings": 2000,   "sites": 1000},
    "medium": {"domains": 1000,   "hosts": 10000,  "ports": 10000, "findings": 10000,  "sites": 5000},
    "large":  {"domains": 5000,   "hosts": 50000,  "ports": 50000, "findings": 50000,  "sites": 25000},
}

FIXED_TS = "2026-01-01T00:00:00"


# ─── 负载生成 ──────────────────────────────────────────────────────────────
def _rows(table: str, n: int, wid: int) -> list[dict]:
    """生成 n 行**形状真实**的记录

    形状很重要:字段分布照抄真实资产的形态(端口号聚集、状态混合、
    URL 带路径),而不是 `row_0/row_1/...`。用假形状测出来的数只能
    衡量"SQLite 插 N 行",测不出"查这种数据要多久"。

    字段名取自真实 schema 的 NOT NULL 约束(直接查 `PRAGMA table_info`
    核过,不是照记忆写的)。早先按 `name`/`fingerprint` 想当然地填,
    第一次跑就 `NOT NULL constraint failed: domains.domain`。

    ## 字段取值必须让组合数**匹配行数**,否则会撞 UNIQUE(hash)

    `hash` 是按整行内容算的,`(workspace_id, hash)` 上有 UNIQUE 约束。
    早先 findings 的取值只有 `4 种 type × 97 个 target = 388` 种组合,
    于是 small 档(2000 行)侥幸能过,large 档(50000 行)直接
    `UNIQUE constraint failed: findings.workspace_id, findings.hash`。

    这说明合成数据的**形状不够真实** —— 真实的 finding 不会两万条
    挤在 388 种内容上。修法不是绕过约束(比如加随机后缀),那会让
    测出来的"唯一值分布"失真;而是让每行真正带自己的身份
    (`i % n` 这类取模会让同余的行再次撞车,所以这里用 `i` 本身)。
    """
    if table == "domains":
        return [
            {"domain": f"sub{i}.example{i % 97}.com",
             "source": "bench", "resolved_ip": f"10.{i % 255}.{(i // 255) % 255}.{i % 254 + 1}"}
            for i in range(n)
        ]
    if table == "hosts":
        return [
            {"host": f"h{i}.example{i % 97}.com",
             "ip": f"10.{i % 255}.{(i // 255) % 255}.{i % 254 + 1}",
             "asn": None, "geo_country": "US" if i % 3 == 0 else None}
            for i in range(n)
        ]
    if table == "ports":
        # 端口号刻意聚集在少数几个上,并混合 state ——
        # 规则里有多条按 state='open' 过滤,分布不真实就测不出真实代价。
        # 唯一性靠 (ip, port):真实场景下同一 ip:port 只该有一条。
        common = [80, 443, 22, 8080, 3306, 6379, 9200, 27017]
        rows = []
        for i in range(n):
            # 让 (ip, port) 的组合数 >= n,同时保持端口聚集特性:
            # 第 i 行用「前 i 个 port 轮转」,port 出现频率仍高度不均
            p = common[i % len(common)]
            ip = f"10.{(i // len(common)) % 250 + 1}.{(i // (len(common) * 250)) % 250}.{i % 254 + 1}"
            rows.append({"ip": ip, "port": p,
                         "service": f"svc{i % 7}",
                         "state": ["open", "closed", "filtered"][i % 3],
                         "protocol": "tcp"})
        return rows
    if table == "findings":
        # 每行带自己的 i —— 用取模会让同余行内容相同、直接撞 hash
        return [
            {"finding_type": ["service", "header", "cert", "cve"][i % 4],
             "target": f"h{i}.example{i % 97}.com",
             "target_type": "host",
             "severity": ["high", "medium", "low", "info"][i % 4],
             "title": f"bench finding {i}",
             "description": f"synthetic-{i}", "evidence": f"ev-{i}"}
            for i in range(n)
        ]
    if table == "sites":
        # 唯一性靠 url 的路径段带 i;同时状态码混合,贴近真实分布
        return [
            {"url": f"https://h{i}.example{i % 97}.com/p{i}",
             "host": f"h{i}.example{i % 97}.com",
             "ip": f"10.{(i // 3) % 250 + 1}.{(i // 750) % 250}.{i % 254 + 1}",
             "port": [80, 443, 8080][i % 3], "scheme": "https",
             "title": f"page {i % 60}", "status_code": [200, 301, 404, 500][i % 4],
             "tech": "nginx", "server": "nginx/1.24"}
            for i in range(n)
        ]
    raise ValueError(f"unknown table: {table}")


def _load(storage, table: str, rows: list[dict]) -> None:
    """写入 —— 走真实 Storage 的 hash 计算,不绕过约束"""
    from ..db.storage import compute_hash

    with storage._conn() as conn:
        have = {x["name"] for x in conn.execute(f"PRAGMA table_info({table})")}
        cols = None
        for r in rows:
            r = dict(r)
            r.setdefault("workspace_id", storage.workspace_id)
            r.setdefault("module", "perf")
            r.setdefault("discovered_at", FIXED_TS)
            r.setdefault("first_seen", FIXED_TS)
            r.setdefault("last_seen", FIXED_TS)
            r["hash"] = compute_hash(
                str(storage.workspace_id),
                json.dumps(r, sort_keys=True, default=str),
            )
            if cols is None:
                cols = [k for k in r.keys() if k in have]
                missing = [k for k in r.keys() if k not in have]
                if missing:
                    log.debug("perf_bench: %s 跳过不存在的列 %s", table, missing)
            vals = [r.get(c) for c in cols]
            conn.execute(
                f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                vals,
            )
        conn.commit()


# ─── 单阶段计时 ────────────────────────────────────────────────────────────
@dataclass
class Phase:
    name: str
    seconds: float
    rows: int = 0
    note: str = ""
    # 峰值内存(kB)。tracemalloc 只跟踪 Python 分配,不含 SQLite 页缓存,
    # 所以它**低估**真实占用 —— 见 PerfReport.notes 的说明。
    peak_kb: int = 0

    @property
    def rows_per_sec(self) -> float:
        return self.rows / self.seconds if self.seconds > 0 and self.rows else 0.0


def _timed(fn, *a, **kw) -> tuple[float, object]:
    gc.collect()
    tracemalloc.start()
    t0 = time.perf_counter()
    try:
        out = fn(*a, **kw)
    finally:
        dt = time.perf_counter() - t0
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    return dt, out, peak // 1024


@dataclass
class PerfReport:
    scale: str
    counts: dict[str, int]
    phases: list[Phase] = field(default_factory=list)
    hits: int = 0
    rules: int = 0
    env: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(self.counts.values())

    @property
    def total_seconds(self) -> float:
        return sum(p.seconds for p in self.phases)

    @property
    def peak_kb(self) -> int:
        return max((p.peak_kb for p in self.phases), default=0)


def run_bench(scale: str = "medium",
              rules_dir: Path | None = None) -> PerfReport:
    """跑一遍基线。返回报告(不写文件)。"""
    from .correlation_engine import run_all_rules
    from ..db.storage import Storage

    counts = SCALES.get(scale)
    if counts is None:
        raise ValueError(f"unknown scale {scale!r}; pick from {sorted(SCALES)}")

    rep = PerfReport(scale=scale, counts=dict(counts))
    with tempfile.TemporaryDirectory(prefix="perf-bench-") as td:
        st = Storage(workspace="perf", workspace_root=Path(td))

        for table, n in counts.items():
            rows = _rows(table, n, st.workspace_id)
            dt, _, peak = _timed(_load, st, table, rows)
            rep.phases.append(Phase(f"write:{table}", dt, n, "", peak))
            del rows

        rules = list((rules_dir or RULES_DIR).glob("*.yml"))
        rep.rules = len(rules)

        dt, hits, peak = _timed(run_all_rules, st, rules_dir or RULES_DIR)
        rep.hits = len(hits)
        rep.phases.append(Phase(
            "run_all_rules", dt, rep.total_rows,
            f"{len(rules)} 条规则 / {len(hits)} 个命中", peak,
        ))

        # 打分路径:两个都测,它们性质不同 ——
        #   assess() 按**规则命中**逐条评估,是 O(命中数)
        #   compute_asset_risks() 从 correlations 聚合,是 O(命中数 × 关联数)
        # 后者最容易悄悄退化成 O(n²),所以单独计时。
        # 函数名不是猜的:早先按 `score_findings` 试,ImportError,
        # 基线报告里留下一条「未测」。改用真实的 API。
        from .confidence import assess
        dt, _, peak = _timed(
            lambda: [assess("bench_rule", "medium",
                             has_cross_ref=True, cross_ref_satisfied=(i % 2 == 0))
                     for i in range(rep.hits or 1000)]
        )
        rep.phases.append(Phase(
            "confidence.assess", dt, rep.hits or 1000,
            f"逐条评估 {rep.hits or 1000} 个命中", peak,
        ))

        from .risk_score import compute_asset_risks
        dt, _, peak = _timed(compute_asset_risks, st)
        rep.phases.append(Phase(
            "risk_score.compute_asset_risks", dt, rep.hits,
            f"从 {rep.hits} 个命中聚合", peak,
        ))

        st.close()

    import platform
    import sys
    rep.env = {
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.machine()}",
    }
    rep.notes.append(
        "tracemalloc 只跟踪 Python 对象分配,**不含** SQLite 页缓存与 "
        "解释器自身开销,所以峰值内存是**下界**,不是进程真实 RSS"
    )
    rep.notes.append(
        "本基线**不含网络**。子域名枚举与 HTTP 探测的耗时由 crt.sh / "
        "目标站决定,与本项目代码无关,且 crt.sh 实测限流严重(429/502),"
        "拿它当基准不可复现"
    )
    return rep


def run_repeated(scale: str = "medium", repeat: int = 3,
                 rules_dir: Path | None = None) -> PerfReport:
    """跑 repeat 次取中位数 —— 单次测量会被 GC / 磁盘缓存干扰"""
    runs = [run_bench(scale, rules_dir) for _ in range(max(1, repeat))]
    last = runs[-1]
    by_name: dict[str, list[float]] = {}
    peaks: dict[str, int] = {}
    for r in runs:
        for p in r.phases:
            by_name.setdefault(p.name, []).append(p.seconds)
            peaks[p.name] = max(peaks.get(p.name, 0), p.peak_kb)
    last.phases = [
        Phase(n, statistics.median(v), 0, "", peaks.get(n, 0))
        for n, v in by_name.items()
    ]
    # rows 需要重新填,单次跑里丢掉了
    for p in last.phases:
        if p.name.startswith("write:"):
            p.rows = last.counts.get(p.name.split(":", 1)[1], 0)
        elif p.name == "run_all_rules":
            p.rows = last.total_rows
            p.note = f"{last.rules} 条规则 / {last.hits} 个命中"
        elif p.name == "confidence.assess":
            p.rows = last.hits
            p.note = f"逐条评估 {last.hits} 个命中"
        elif p.name == "risk_score.compute_asset_risks":
            p.rows = last.hits
            p.note = f"从 {last.hits} 个命中聚合"
    last.notes.append(f"耗时取 {repeat} 次的中位数(单次会被 GC 和磁盘缓存干扰)")
    return last


# ─── 渲染 ──────────────────────────────────────────────────────────────────
def render_markdown(rep: PerfReport) -> str:
    L: list[str] = []
    L.append("# 性能基线\n")
    L.append(f"规模档:**{rep.scale}** · "
             f"Python {rep.env.get('python','?')} · {rep.env.get('platform','?')}\n")
    L.append(f"数据量:{rep.total_rows} 行"
             f"({', '.join(f'{k}={v}' for k, v in rep.counts.items())})\n")

    L.append("## 分阶段耗时\n")
    L.append("| 阶段 | 行数 | 耗时(s) | 吞吐(行/s) | 峰值内存(kB) | 备注 |")
    L.append("|---|---:|---:|---:|---:|---|")
    for p in sorted(rep.phases, key=lambda x: -x.seconds):
        rps = f"{p.rows_per_sec:,.0f}" if p.rows_per_sec else "-"
        L.append(f"| `{p.name}` | {p.rows:,} | {p.seconds:.3f} | {rps} | "
                 f"{p.peak_kb:,} | {p.note} |")
    # 合计行:防零。计时失灵不该让整个报告崩掉 —— 崩了就等于这轮白跑。
    if rep.total_seconds > 0:
        L.append(f"| **合计** | **{rep.total_rows:,}** | **{rep.total_seconds:.3f}** | "
                 f"**{rep.total_rows / rep.total_seconds:,.0f}** | "
                 f"**{rep.peak_kb:,}** | |")
        L.append("")

    L.append("## 结论\n")
    L.append(f"- **{rep.total_rows:,} 行全流水线 {rep.total_seconds:.2f}s**"
             f"(不含网络)")
    L.append(f"- **{rep.rules} 条规则**跑完 {rep.total_rows:,} 行,"
             f"命中 {rep.hits} 个")
    L.append(f"- 写入与规则引擎的耗时占比见上表 —— **写入占比高就该先查索引,"
             f"规则占比高就该先看规则复杂度**")
    L.append(f"- 峰值内存 {rep.peak_kb / 1024:.1f} MB(下界,见下方说明)\n")

    if rep.notes:
        L.append("## 局限\n")
        for n in rep.notes:
            L.append(f"- {n}")
        L.append("")
    return "\n".join(x for x in L if x is not None)


def write_report(path: Path, rep: PerfReport) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(rep), encoding="utf-8")
    return path


def render_trend(reports: list[PerfReport]) -> str:
    """三档对比 —— **趋势**比单点有用

    单个规模的数字只说明"现在多快";三档连起来才能回答
    "有没有非线性成分"。规则引擎一旦有人写了不带索引的聚合,
    表现就是 large 档的耗时/行数比 small 档明显更高。

    所以这里算的不是"快不快",而是"放大 5 倍数据,耗时涨几倍"。
    数据 x5 而耗时 x3.4 是**次线性**(摊薄了固定开销);
    x5.4 就值得看一眼是不是多了个全表扫。
    """
    L = ["# 性能基线:三档趋势\n"]
    L.append("| 规模 | 总行数 | 合计(s) | 吞吐(行/s) | 规则引擎(s) | 峰值(MB) |")
    L.append("|---|---:|---:|---:|---:|---:|")
    for r in reports:
        rr = next((p.seconds for p in r.phases if p.name == "run_all_rules"), 0.0)
        # 吞吐要防零:一次异常快或计时失灵的运行会让 total_seconds=0,
        # 而基线工具崩掉 = 这一轮白跑。显示 "-" 而不是炸。
        tput = f"{r.total_rows / r.total_seconds:,.0f}" if r.total_seconds > 0 else "-"
        L.append(f"| {r.scale} | {r.total_rows:,} | {r.total_seconds:.2f} | "
                 f"{tput} | {rr:.2f} | {r.peak_kb / 1024:.1f} |")
    L.append("")
    L.append("## 线性度\n")
    L.append("相邻档数据量约 x5。耗时涨得比 5 倍多,就要怀疑有全表扫或 N+1。\n")
    L.append("| 区间 | 数据 | 合计耗时 | 判定 |")
    L.append("|---|---:|---:|---|")
    for a, b in zip(reports, reports[1:]):
        if b.total_seconds <= 0 or a.total_seconds <= 0:
            continue
        dr = b.total_rows / a.total_rows
        dt = b.total_seconds / a.total_seconds
        if dt > dr * 1.15:
            verdict = f"**超线性**({dt / dr:.2f}x 预期)⚠"
        elif dt < dr * 0.7:
            verdict = f"次线性({dt / dr:.2f}x 预期,固定开销被摊薄)"
        else:
            verdict = "线性 ✓"
        L.append(f"| {a.scale} → {b.scale} | x{dr:.1f} | x{dt:.2f} | {verdict} |")
    L.append("")
    return "\n".join(L)

