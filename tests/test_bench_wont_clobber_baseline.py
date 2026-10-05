"""r62:两个 bench 默认不许覆盖版本库里已提交的性能基线。

## 起因(实测,不是读代码猜的)

`arl-lite perf-bench` 早先的默认输出路径是 `docs/PERF_BASELINE.md`,
`arl-lite fp-bench` 是 `docs/FP_RATE.md` —— **两个都是 `git ls-files` 查得到的
已提交文件**。实测跑一次 `arl-lite perf-bench --scale small`,
`git diff docs/PERF_BASELINE.md` 显示已提交的 medium 基线(36000 行、合计 7.590s)
整表被换成 small 的一次(7200 行),退出码 0,一句提示都没有。

最要命的是这条命令是工具**自己在 docstring 里让人跑的**
(`arl-lite perf-bench --scale small   # 快速冒烟`)。照文档走就毁基线,
而 `git diff` 出来是一张看起来完全正常的表格 —— 数字、耗时、吞吐一应俱全。

## 为什么安全默认值取「不写」

写完再警告等于已经毁了 —— 顺序不能反,和 r53 那次「`max(0)` 把丢失夹成 0」
是同一个形状。所以这里默认**不写**、说清怎么才能写、退出码非 0,
但**测量结果照常打在 stdout**:毁的是文件,不是信息。

## 判据量的是行为,不是结构

`cmd_perf_bench` 是在 `chdir` 之外的进程里跑的真实命令,它的「写不写」完全
可测,所以不写 AST 判据(r14:行为能测出来的别假装测不出)。唯一的结构性事实
是「默认路径确实存在且非空」—— 也就是「确实有一份基线值得保护」,
这条用文件本身验,不用 git。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import pytest

from arl_lite.cli import cmd_fp_bench, cmd_perf_bench
from arl_lite.core import fp_bench as fb
from arl_lite.core import perf_bench as pb

REPO = Path(__file__).resolve().parent.parent
PERF_BASELINE = REPO / "docs" / "PERF_BASELINE.md"
FP_RATE = REPO / "docs" / "FP_RATE.md"


def _canned_perf(scale: str = "small") -> pb.PerfReport:
    """一份形状真的、但跑得零成本的测量结果。

    走真的 `render_markdown`/`write_report`,所以判据量的仍是**真写盘行为**,
    只是把「跑 7200 行」这一步换掉了。
    """
    return pb.PerfReport(
        scale=scale,
        counts={"domains": 200, "hosts": 2000, "findings": 2000},
        phases=[pb.Phase(name="run_all_rules", seconds=1.5, rows=4200, peak_kb=900)],
        hits=11,
        rules=37,
        env={"python": "3.12.3"},
    )


@pytest.fixture
def perf_run(monkeypatch, tmp_path):
    """在 tmp 里当 CWD 跑 cmd_perf_bench,免得真动仓库里的基线。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "PERF_BASELINE.md").write_text(
        "# 性能基线\n\n规模档:**medium** · Python 3.12.3 · Linux x86_64\n\n## 分阶段耗时\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pb, "run_repeated", lambda scale, repeat: _canned_perf(scale))
    return tmp_path


def _args(**kw):
    base = dict(out="", in_place=False, scale="small", repeat=1)
    base.update(kw)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------- 事实前提


def test_the_default_paths_are_real_files_worth_protecting():
    """默认路径确实存在且非空 —— 没有基线的话这条判据守的是空气。"""
    for p in (PERF_BASELINE, FP_RATE):
        assert p.is_file() and p.stat().st_size > 0, f"{p} 不存在或是空的"


# ---------------------------------------------------------------- 核心:默认不覆盖


def test_perf_bench_does_not_clobber_the_baseline_by_default(perf_run, capsys):
    before = (perf_run / "docs" / "PERF_BASELINE.md").read_bytes()
    rc = cmd_perf_bench(_args())
    after = (perf_run / "docs" / "PERF_BASELINE.md").read_bytes()
    assert after == before, "默认跑一次就把已提交的基线改了"
    assert rc != 0, f"没写文件却报成功(rc={rc}),用户以为基线已经刷新了"


def test_refusing_to_write_still_gives_you_the_numbers(perf_run, capsys):
    """测量本身没白做:数字必须还在 stdout 上。"""
    cmd_perf_bench(_args())
    out = capsys.readouterr().out
    assert "run_all_rules" in out and "37 条规则" in out, f"不写文件就不该把测量也吞掉:{out!r}"


def test_it_says_how_to_actually_write(perf_run, capsys):
    """拒绝必须说清两条出路,否则用户只会以为命令坏了。"""
    cmd_perf_bench(_args())
    err = capsys.readouterr().err
    assert "--out" in err and "--in-place" in err, f"没说清怎么才能写:{err!r}"


def test_fp_bench_does_not_clobber_fp_rate_by_default(monkeypatch, tmp_path, capsys):
    """fp-bench 是同一个毛病,别只修一半。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "FP_RATE.md").write_text("# 规则集误报率实测\n", encoding="utf-8")
    monkeypatch.setattr(fb, "run_bench", list)
    monkeypatch.setattr(fb, "analyze", lambda r: fb.BenchReport(
        results=[], per_rule={}, by_confidence={}))
    before = (tmp_path / "docs" / "FP_RATE.md").read_bytes()
    rc = cmd_fp_bench(argparse.Namespace(out="", in_place=False))
    assert (tmp_path / "docs" / "FP_RATE.md").read_bytes() == before, "默认覆盖了 FP_RATE.md"
    assert rc != 0, f"没写却报成功(rc={rc})"


# ---------------------------------------------------------------- 显式要写


def test_in_place_writes_the_baseline(perf_run):
    rc = cmd_perf_bench(_args(in_place=True))
    text = (perf_run / "docs" / "PERF_BASELINE.md").read_text(encoding="utf-8")
    assert "规模档:**small**" in text, f"--in-place 没写成:{text[:200]!r}"
    assert rc == 0


def test_out_writes_elsewhere_and_leaves_the_baseline_alone(perf_run):
    target = perf_run / "scratch" / "p.md"
    before = (perf_run / "docs" / "PERF_BASELINE.md").read_bytes()
    rc = cmd_perf_bench(_args(out=str(target)))
    assert target.is_file() and "规模档:**small**" in target.read_text(encoding="utf-8")
    assert (perf_run / "docs" / "PERF_BASELINE.md").read_bytes() == before
    assert rc == 0


def test_scale_mismatch_is_called_out_in_place(perf_run, capsys):
    """medium 基线被 small 覆盖 —— 数字不可比,必须喊出来。

    只警告不拦:有人可能就是想换档重设基线(比如团队固定跑 small)。
    拦死会逼人改去手写文件,那更糟。
    """
    rc = cmd_perf_bench(_args(in_place=True, scale="small"))
    err = capsys.readouterr().err
    assert rc == 0, "换档不该直接失败"
    assert "medium" in err and "small" in err and "不可比" in err, f"换档没说清不可比:{err!r}"


def test_same_scale_does_not_warn(perf_run, capsys, monkeypatch):
    """同档重跑是正常基线维护,不该每次都喊。"""
    monkeypatch.setattr(pb, "run_repeated", lambda scale, repeat: _canned_perf("medium"))
    rc = cmd_perf_bench(_args(in_place=True, scale="medium"))
    assert rc == 0
    assert "不可比" not in capsys.readouterr().err, "同档重跑不该警告"


# ---------------------------------------------------------------- 端到端:真跑一次


@pytest.mark.slow
def test_the_documented_smoke_test_actually_leaves_the_baseline_alone(tmp_path):
    """r62 的起因就是 docstring 里那条「快速冒烟」命令。

    真起一个子进程跑文档里写的 `arl-lite perf-bench --scale small`,比字节。

    ## 这里踩过一次坑,值得写下来

    首版这条在**真仓库根目录**里跑。变异测试 M1 恰恰是把「默认不写」改回
    「默认直接写」的那个变异 —— 于是这条判据在实现坏掉时,忠实地执行了
    写盘,把**它自己守护的** `docs/PERF_BASELINE.md` 从 medium 换成了 small。
    判据红是对的,可它红的方式是先把基线毁了:下一轮再跑就是拿一个坏基线
    当「前」状态,真正的 bug 反而被掩盖了。

    判据也得有**爆炸半径**。修法是把 CWD 换成临时目录、里面放一个哨兵
    `docs/PERF_BASELINE.md` —— 默认路径是相对 CWD 的(`_P("docs/...")`),
    所以验的是**同一条代码路径**,但砸的是临时文件。
    """
    import hashlib

    docs = tmp_path / "docs"
    docs.mkdir()
    baseline = docs / "PERF_BASELINE.md"
    baseline.write_text("# 性能基线\n\n规模档:**medium** · Python 3.12.3\n", encoding="utf-8")
    before = hashlib.md5(baseline.read_bytes()).hexdigest()

    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(REPO))
    # cwd 用 tmp:默认路径 `docs/PERF_BASELINE.md` 是**相对 CWD** 的,
    # 所以验的还是同一条代码路径,但砸的是临时文件。
    # PYTHONPATH 指向真仓库,`python3 -m arl_lite.cli` 才找得到模块。
    repo_before = hashlib.md5(PERF_BASELINE.read_bytes()).hexdigest()
    p = subprocess.run(
        [sys.executable, "-B", "-m", "arl_lite.cli", "perf-bench",
         "--scale", "small", "--repeat", "1"],
        capture_output=True, text=True, cwd=tmp_path, env=env, timeout=900)

    after = hashlib.md5(baseline.read_bytes()).hexdigest()
    assert p.returncode != 0, f"默认跑法居然报成功(rc={p.returncode})"
    assert after == before, (
        f"文档里那条冒烟命令把基线改了:{before} → {after}\n"
        f"这正是 r62 要修的东西"
    )
    # 判据自己也不许有爆炸半径:上面那轮真的起过子进程,
    # 仓库里那份基线必须一个字节都没动。
    assert hashlib.md5(PERF_BASELINE.read_bytes()).hexdigest() == repo_before, (
        "判据自己在跑的过程中改了仓库里的基线 —— 那它守护的东西已经不可信了"
    )
