"""性能基线工具本身必须可信

## 为什么测测测的东西也要测

`arl-lite perf-bench` 存在的意义是回答"变慢了吗"。但一个**测错了的
基线比没有基线更坏**:它会持续给出"一切正常"的结论,而实际上它在量
一个跟真实负载无关的东西。

这轮开发中它已经骗过我两次:

1. 字段名按记忆填成 `name`/`fingerprint`,真实 schema 是 `domain`/
   `finding_type` —— 第一次跑就 IntegrityError,没测成
2. findings 的取值只有 388 种组合,small 档(2000 行)侥幸能过,
   **large 档(50000 行)直接 UNIQUE 约束失败** —— 而如果当时只有
   small 档,这个 bug 会安静地留在工具里,直到有人跑 large 才炸

第 2 条尤其要命:它意味着**小规模跑通不等于负载生成器正确**。

所以下面这些测试守的不是"跑得快",是"测的是不是真的"。
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from arl_lite.core.perf_bench import (
    SCALES,
    Phase,
    _rows,
    render_markdown,
    run_bench,
    run_repeated,
)

REPO = Path(__file__).parents[1]
TABLES = ("domains", "hosts", "ports", "findings", "sites")

# `_load()` 用 setdefault 统一补上的字段。它们是 NOT NULL,但不属于
# "负载形状"的一部分 —— 由写入层负责。断言生成器时必须排除,
# 否则会误报"生成器缺字段"。
_LOAD_PROVIDES = {
    "workspace_id", "hash", "module",
    "discovered_at", "first_seen", "last_seen",
}


# =====================================================================
# 负载生成器:形状必须和真实 schema 对得上
# =====================================================================


def test_generated_rows_satisfy_the_real_not_null_constraints(tmp_path):
    """合成数据必须能真的插进真实的表

    NOT NULL 约束是查 `PRAGMA table_info` 拿的,不是照记忆列的 ——
    照记忆列过一次(`name` 而不是 `domain`),直接 IntegrityError。
    """
    from arl_lite.db.storage import Storage
    from arl_lite.core.perf_bench import _load

    with tempfile.TemporaryDirectory() as td:
        st = Storage(workspace="t", workspace_root=Path(td))
        with st._conn() as conn:
            for t in TABLES:
                required = {
                    r["name"] for r in conn.execute(f"PRAGMA table_info({t})")
                    if r["notnull"]
                }
                for row in _rows(t, 50, st.workspace_id):
                    # 排除由 _load 统一补上的字段 —— 它们不是生成器的职责。
                    # 漏掉 module 就会误报:findings.module 是 NOT NULL,
                    # 但它由 _load 的 setdefault 提供。
                    missing = required - set(row.keys()) - _LOAD_PROVIDES
                    assert not missing, f"{t} 缺 NOT NULL 字段 {sorted(missing)}"
        # 真的插一遍 —— 约束满足与否,插了才算
        for t in TABLES:
            _load(st, t, _rows(t, 50, st.workspace_id))
        st.close()


def test_generated_rows_are_unique_enough_for_the_largest_scale():
    """每档的行数必须真的能插进去,不许撞 UNIQUE(workspace_id, hash)

    这是第 2 条教训的直接对策。`hash` 按整行内容算,`findings` 早期
    只有 4×97=388 种取值组合 —— small 档 2000 行能过是**侥幸**,
    large 档 50000 行立刻炸。

    所以这里不测 small 档,而是**直接测最大档的行数**:能在最大规模下
    不撞约束,小规模自然也行。反过来不成立。
    """
    from arl_lite.core.perf_bench import _load
    from arl_lite.db.storage import Storage

    biggest = max(SCALES.values(), key=lambda c: c["findings"])
    with tempfile.TemporaryDirectory() as td:
        st = Storage(workspace="u", workspace_root=Path(td))
        for t in TABLES:
            n = biggest[t]
            rows = _rows(t, n, st.workspace_id)
            assert len(rows) == n
            # 内容去重后必须仍然有 n 种 —— 全是重复行就等于没造数据
            import json
            uniq = {json.dumps(r, sort_keys=True) for r in rows}
            assert len(uniq) == n, (
                f"{t}: 造了 {n} 行但只有 {len(uniq)} 种不同内容 —— "
                f"这会撞 UNIQUE(hash),而且意味着负载形状不真实"
            )
        for t in TABLES:
            _load(st, t, _rows(t, biggest[t], st.workspace_id))
        st.close()


def test_port_distribution_stays_clustered_but_unique():
    """端口要保持聚集特性,否则测不出按 state/port 过滤的规则代价

    合成数据有两个相反的要求:内容要唯一(不撞 hash),但**分布**要
    贴近真实(端口聚集、state 混合)。全部唯一但均匀分布的话,
    "多条端口同 IP"这类规则的执行代价会完全测不出来。
    """
    rows = _rows("ports", 4000, 1)
    ports = [r["port"] for r in rows]
    uniq_ports = len(set(ports))
    assert uniq_ports <= 8, f"端口用了 {uniq_ports} 种,失去聚集特性"

    states = {r["state"] for r in rows}
    assert states == {"open", "closed", "filtered"}, (
        f"state 分布不真实:{states} —— 多条规则按 state='open' 过滤,"
        f"分布不真实就测不出它们的真实代价"
    )
    # 但 (ip, port) 必须唯一
    pairs = {(r["ip"], r["port"]) for r in rows}
    assert len(pairs) == len(rows), "(ip,port) 有重复,会撞 UNIQUE"


# =====================================================================
# 计时本身
# =====================================================================


def test_run_bench_measures_every_stage():
    """基线必须覆盖全部阶段 —— 少测一段就等于给了个残缺的数"""
    rep = run_bench("small")
    names = {p.name for p in rep.phases}
    for t in TABLES:
        assert f"write:{t}" in names, f"没测写入:{t}"
    assert "run_all_rules" in names, "没测规则引擎"
    assert "confidence.assess" in names, "没测置信度评估"
    assert "risk_score.compute_asset_risks" in names, "没测风险聚合"
    for p in rep.phases:
        assert p.seconds > 0, f"{p.name} 耗时为 0 —— 计时没生效"


def test_run_bench_leaves_no_untested_stage_note():
    """报告里不许留「XX 未测」

    早先 `score_findings` 名字猜错,报告里就多了一条
    「score_findings 未测: ImportError」。一条带 ImportError 的基线
    报告看起来还能用,实际上少了一段 —— 而读报告的人不会去数。
    """
    rep = run_bench("small")
    untested = [n for n in rep.notes if "未测" in n]
    assert not untested, f"有阶段没测到:{untested}"


def test_repeat_uses_the_median_not_the_mean():
    """多次测量取中位数 —— 均值会被 GC / 磁盘缓存的离群值拖走

    一次 GC 停顿就能把均值抬高 30%,而中位数不受影响。
    基线是要放进趋势线对比的,对离群值敏感就失去意义了。
    """
    rep = run_repeated("small", repeat=3)
    assert any("中位数" in n for n in rep.notes), "没记录用的是中位数"
    for p in rep.phases:
        assert p.seconds > 0


def test_throughput_is_derived_not_invented():
    """吞吐必须由 耗时/行数 算出来,不能是拍出来的"""
    p = Phase("x", seconds=2.0, rows=1000)
    assert p.rows_per_sec == 500.0
    # 0 行时不能除零
    assert Phase("y", seconds=1.0, rows=0).rows_per_sec == 0.0
    assert Phase("z", seconds=0.0, rows=100).rows_per_sec == 0.0


# =====================================================================
# 报告:必须写明它测不到什么
# =====================================================================


def test_report_states_what_it_cannot_measure():
    """基线报告必须声明局限,尤其是「不含网络」

    一个只写耗时不给局限的性能报告,会被当成"整个流程的耗时"。
    本项目的真实耗时里网络占大头(crt.sh 限流、目标站响应),
    把它算进引擎的数里,得到的是一个既不可复现也没法比较的数。
    """
    md = render_markdown(run_bench("small"))
    assert "局限" in md
    assert "不含网络" in md or "不含**网络" in md
    assert "下界" in md, "内存是下界(tracemalloc 不含 SQLite 页缓存),必须写明"


def test_report_never_claims_to_measure_the_network():
    """不许出现「全流程耗时」这种把网络算进去的说法"""
    md = render_markdown(run_bench("small"))
    for bad in ("全流程耗时", "端到端耗时", "真实耗时", "e2e"):
        assert bad not in md.lower(), f"报告里出现 {bad!r} —— 暗示包含了网络"


def test_peak_memory_is_reported_as_a_lower_bound():
    """tracemalloc 的峰值必须标成下界,不能当成 RSS

    tracemalloc 只跟踪 Python 对象分配,不含 SQLite 页缓存和解释器
    自身开销 —— 实测两者能差好几倍。把它当进程内存报出去,会让
    "2G 内存友好"这个项目定位看起来像被验证过,而它没有。
    """
    rep = run_bench("small")
    assert any("下界" in n for n in rep.notes), "内存没标下界"
    assert rep.peak_kb > 0


# =====================================================================
# 趋势表:算错方向,结论就反了
# =====================================================================


def test_trend_flags_superlinear_scaling():
    """超线性必须被标出来 —— 这正是基线唯一能回答的问题

    合成三个报告:第一个 1000 行耗时 1s,第二个 5000 行耗时 9s
    (数据 x5,耗时 x9,远超预期的 1.15 倍)。趋势表必须报警。
    """
    from arl_lite.core.perf_bench import PerfReport, render_trend

    def mk(rows, sec):
        r = PerfReport(scale=f"s{rows}", counts={"x": rows})
        r.phases = [Phase("write:x", sec, rows)]
        r.notes = []
        return r

    md = render_trend([mk(1000, 1.0), mk(5000, 9.0)])
    assert "超线性" in md, md
    # 判定必须带具体的倍数,不然读的人没法判断严重程度
    assert "⚠" in md


def test_trend_marks_linear_scaling_as_ok():
    """近线性必须标 ✓ —— 不能为了显得谨慎把正常也报警

    一个什么都报警的趋势表等于没报警。
    """
    from arl_lite.core.perf_bench import PerfReport, render_trend

    def mk(rows, sec):
        r = PerfReport(scale=f"s{rows}", counts={"x": rows})
        r.phases = [Phase("write:x", sec, rows)]
        r.notes = []
        return r

    md = render_trend([mk(1000, 1.0), mk(5000, 5.2)])
    assert "超线性" not in md
    assert "线性 ✓" in md, md


def test_trend_does_not_divide_by_zero_on_a_slow_run():
    """某一档耗时为 0 时不许炸

    边界防护:一次异常快或计时失灵的运行不该让整个基线工具崩掉 ——
    崩了就等于这轮基线白跑。
    """
    from arl_lite.core.perf_bench import PerfReport, render_trend

    def mk(rows, sec, name):
        r = PerfReport(scale=name, counts={"x": rows})
        r.phases = [Phase("write:x", sec, rows)]
        r.notes = []
        return r

    md = render_trend([mk(1000, 0.0, "a"), mk(5000, 5.0, "b")])
    assert "b" in md  # 没崩就算过
