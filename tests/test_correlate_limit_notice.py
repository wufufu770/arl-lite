"""r58:`correlate` 的 `--limit` 静默截断,连**通知**都只推了一半

## 实测的退化路径(不是推测)

关联引擎返回 60 条命中,`arl-lite correlate` 什么都不给(`--limit` 默认 50):

    [+] 60 hits, saved/refreshed 60 in 0.00s      ← 这是**引擎命中总数**
    ... 列出 50 行 ...
    [i] notified 50/50 correlation(s)             ← 这是**截断后的数**

两行数字挨着出现,谁也没解释差在哪。用户看到「60 hits」和「notified 50/50」,
合理推断是「60 条命中,全推了」—— 实际有 10 条既没列出也没推。

首行那个 `60 hits` 不是显示数,是 `run_all_rules` 的返回值长度,它在
`min_risk` 过滤和 `limit` 截断**之前**就打印了。这和 r50 那条
「两个数字并排出现却没人解释差在哪」是同一个形状。

## 通知这一侧比显示更严重

显示不全,用户自己还能调大 `--limit` 再看一遍。而**通知是发给别人的**:
收通知的人没有终端输出可看,不知道少推了什么,也不会追问。所以
「推的条数有上界」可以接受,「那个上界看起来像全部」不行。

汇总行原来写的是 `notified {ok}/{len(hits)}`,而 `hits` 是**截断后**的,
所以它不是沉默,是**主动说谎**:60 条命中推了 50 条,它说「50/50」。

## 顺便抓到共用文案的一个泄漏(r56 建的那套)

`--limit` 截断提示是 r56 抽出来的共用文案,里面写着
「只显示了**最新** 50 条 …… 机器消费用 `--json`」。而 `correlate`:

- 是按 **risk 降序**取的(高风险在前),不是「最新」
- **根本没有 `--json`**

共用文案照抄到不适用的语境上,就是给用户一条不存在的出路(r58 的教训
和 r52 的 `last_count` 同源:名字/文案承诺的东西,得和承载它的东西对得上)。
所以「怎么取全」这句改成可覆盖的,correlate 传自己的说法。
"""
from __future__ import annotations

import ast
import contextlib
import io
import pathlib
from types import SimpleNamespace as NS

import pytest

from arl_lite.cli import _limit_notice_text, main

REPO = pathlib.Path(__file__).resolve().parents[1]
HITS = 60


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # 必须先建出工作区:`cmd_correlate` 开头就是 `_ensure_workspace_exists`,
    # 不建的话它 return 1,后面每一行都不会跑 —— 而输出里一个错都看不出来,
    # 只表现为「列了 0 行」(首版就栽在这,以为是自己写错了匹配)。
    from arl_lite.db.storage import Storage
    Storage(workspace="default")
    return tmp_path, monkeypatch


def _hit(i):
    return NS(rule_name=f"r{i}", risk=9, target=f"1.2.3.{i}", target_type="ip",
              headline=f"hit-{i}", tags=["rce"], advice="fix", confidence=80,
              matched_assets=[], matched_count=1, evidence="", asset_type="ip",
              asset_hash=f"h{i}", task_id=0)


@pytest.fixture
def fake_engine(monkeypatch):
    """替掉引擎和 webhook,让 60 条命中真的走完一整轮

    只换「数据从哪来」和「通知发去哪」,`cmd_correlate` 本身的每一行都真跑。
    """
    import arl_lite.core.correlation_engine as ce
    import arl_lite.notify as notify_mod
    sent: list = []
    monkeypatch.setattr(ce, "run_all_rules",
                        lambda storage, rules_dir: [_hit(i) for i in range(HITS)])
    monkeypatch.setattr(ce, "save_correlations", lambda storage, h: len(h))
    monkeypatch.setattr(notify_mod, "notify_correlation",
                        lambda cfg, payload: (sent.append(payload), True)[1])
    return sent


def _run(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        main(["correlate", *argv])
    return buf.getvalue()


def _lines(out, needle):
    return [l for l in out.splitlines() if needle in l]


# ── 一、主判据:截断了要说 ──

def test_limit_truncation_is_stated(ws, fake_engine):
    """60 条命中只列 50 条 → 必须出现「50 of 60」"""
    out = _run()
    listed = len([l for l in out.splitlines() if l.strip().startswith("[9] r")])
    assert listed == 50, f"应该列 50 行,实际 {listed}"
    n = _lines(out, "of 60")
    assert n, f"没说被截断:\n{out[-400:]}"
    assert "correlation(s)" in n[0], f"量词不对:{n[0]}"


def test_no_truncation_stays_quiet(ws, fake_engine):
    """`--limit 1000` 时不喊截断 —— 喊多了就不灵了"""
    out = _run("--limit", "1000")
    assert len([l for l in out.splitlines() if l.strip().startswith("[9] r")]) == HITS
    assert not _lines(out, " of 60"), f"没截断却喊了:\n{out[-300:]}"


def test_exactly_at_the_limit_is_not_called_truncated(ws, fake_engine):
    """正好等于 limit 不是截断"""
    out = _run("--limit", str(HITS))
    assert not _lines(out, " of 60")


# ── 二、基准是「过了 min_risk 的数」,不是引擎命中总数 ──

def test_min_risk_filter_is_not_reported_as_truncation(ws, fake_engine,
                                                        monkeypatch):
    """`--min-risk` 是用户**主动**要求的筛选,不是截断,不该混进「少了多少」

    60 条命中、limit 50、min_risk 10 把它们全滤掉 → 剩下 0 条,
    「共 0 条,只显示了 0 条」是废话;而说「of 60」会把**用户自己的筛选**
    说成工具截断了 —— 用户会以为该修 limit,其实该调 min_risk。
    """
    out = _run("--limit", "50", "--min-risk", "10")
    assert "of 60" not in out, (
        f"把 min_risk 过滤说成截断了:\n{out[-300:]}")
    assert _lines(out, "no correlations found"), (
        f"全都该被滤掉,却还在输出:\n{out[-300:]}")


# ── 三、通知:推的条数有上界可以,看起来像「全部」不行 ──

def test_notify_summary_does_not_claim_everything_was_sent(ws, fake_engine):
    """汇总行不许说「50/50」—— 那是在说谎

    60 条命中推 50 条,原来的 `notified {ok}/{len(hits)}` 分母是截断后的数,
    于是显示「notified 50/50 correlation(s)」,看起来像全部都推了。
    """
    out = _run("--notify", "--webhook-url", "http://x")
    summary = _lines(out, "notified")
    assert summary, f"没看到通知汇总行:\n{out[-400:]}"
    line = summary[0]
    # `notified {ok}/{len(hits)}` 这半句**保留** —— `ok/len(hits)` 的
    # 语义是「成功推了几条 / 尝试推了几条」,它本身没错,错的是**只**
    # 说这半句。所以判据不是「不许出现 50/50」,而是「50/50 后面必须
    # 跟着澄清」—— 首版写成前者,过严,把一句正确的补充也判成了谎。
    head, _, tail = line.partition("correlation(s)")
    assert tail.strip(), (
        f"汇总行只有 `notified 50/50` 就结束了 —— 那就是在说谎:\n{line}")
    assert "60" in tail and "10" in tail, (
        f"没说清共多少、剩多少没推:\n{line}")


def test_notify_actually_sends_only_the_shown_rows(ws, fake_engine):
    """推的条数确实是 50 —— 上界生效了,但上面那条判据保证它被说出来"""
    _run("--notify", "--webhook-url", "http://x")
    assert len(fake_engine) == 50, (
        f"实际推送 {len(fake_engine)} 条 —— 上界要么没生效,要么被改成了别的数")


def test_notify_summary_is_plain_when_nothing_was_cut(ws, fake_engine):
    """没截断时汇总行保持原样,不加多余的话"""
    out = _run("--limit", "1000", "--notify", "--webhook-url", "http://x")
    line = _lines(out, "notified")[0]
    assert f"{HITS}/{HITS}" in line
    assert "没有推" not in line, f"没截断却说有没推的:\n{line}"


def test_notify_without_flag_sends_nothing(ws, fake_engine):
    """没有 `--notify` 就一条都不推(既有行为,别顺手改坏)"""
    out = _run()
    assert fake_engine == []
    assert not _lines(out, "notified")


# ── 四、共用文案不许泄漏到不适用的语境(r58 顺手抓到的) ──

def test_notice_does_not_claim_the_rows_are_the_newest():
    """不能说「最新 N 条」—— `correlate` 是按 risk 降序,不是按时间

    `monitor changes` / `query` 走 `ORDER BY id DESC`,说「最新」成立;
    `correlate` 是 `hits.sort(key=lambda h: -h.risk)`,高风险在前。
    共用文案照抄「最新」,就是在给 `correlate` 的用户一个错误的顺序预期。
    """
    t = _limit_notice_text(50, 60, 50, "correlation",
                           how="要全看就调大 --limit")
    assert "最新" not in t, f"共用文案说了「最新」:{t}"


def test_notice_must_not_advertise_a_json_flag_a_command_does_not_have():
    """默认那句「机器消费用 `--json`」不许出现在 `correlate` 的输出里

    `correlate` 没有 `--json` 参数 —— 照抄过去就是给用户一条不存在的出路。
    """
    out = _run()
    assert "--json" not in out, (
        f"提示里推荐了一个不存在的选项:\n{_lines(out, 'of 60')}")
    # 而 monitor changes / query 那些真的有 json 的,默认那句还在
    assert "--json" in _limit_notice_text(50, 200, 50), (
        "默认文案丢了「--json」—— 那些命令确实有它")


def test_correlate_advice_mentions_where_the_full_set_lives(ws, fake_engine):
    """`correlate` 的建议要说清「全都在哪」—— 它没有 json 可退

    首版这条**漏了 `fake_engine` fixture**:引擎于是跑真的 37 条规则、
    打在空库上,一条命中都没有,输出里自然没有 "of 60" —— 报成
    IndexError,看着像断言写错了,其实是场景根本没造出来。
    """
    out = _run()
    line = _lines(out, "of 60")[0]
    assert "correlations" in line, (
        f"没说全量在哪、怎么取:\n{line}")


# ── 五、结构:截断和提示各只在���里 ──

def test_the_limit_slice_lives_in_one_place_with_the_total_already_known():
    """`matched_total` 必须在 `hits[:limit]` **之前**取

    反过来写就等于又回到「只有一个数」的老路:截断之后再取总数,
    拿到的永远是 `len(hits)`,提示永远打不出来。
    """
    tree = ast.parse((REPO / "arl_lite" / "cli.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "cmd_correlate")
    body = ast.unparse(fn)
    i_total = body.index("matched_total = len(hits)")
    i_slice = body.index("hits = hits[:args.limit]")
    assert i_total < i_slice, (
        "matched_total 取在截断之后 —— 拿到的永远是 len(hits),"
        "提示永远打不出来")


def test_notify_receives_the_pretotal():
    """`_notify_correlations` 必须拿到 matched_total,不能只有截断后的 hits

    少传这个参数的话,汇总行的分母就退回截断后的数,回到「50/50」。
    """
    tree = ast.parse((REPO / "arl_lite" / "cli.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "cmd_correlate")
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_notify_correlations"]
    assert len(calls) == 1
    names = {a.id for a in calls[0].args if isinstance(a, ast.Name)}
    assert "matched_total" in names, (
        f"_notify_correlations 收到的是 {sorted(names)} —— "
        f"没有截断前的总数,汇总行就会说「50/50」")
