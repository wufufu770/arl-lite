"""r99:通知**结构上一条都发不出去**,而 `notify test` 还报「sent OK」

## 实测复现(不是推测)

    dataclass 默认门限: high
    hit 有 severity 吗: False
    _notify_correlations 返回: '[i] notified 0/1 correlation(s) (1 failed)'
    上面那行有没有出现 [local webhook] ? 没出现 = 一条都没发出去

三处错叠在一起:

1. **默认门限高于所有发送方实际发的 severity。**
   `notify_task_done` 发 `info`(无错)/`medium`(有错),而默认门限 `high`;
   `notify_critical_finding` 取 `finding.get("severity", "info")`;
   `notify_correlation` 更绝 —— `CorrelationHit` **压根没有 severity 字段**,
   恒取默认 `"info"`。全都在门限之下。

2. **汇总行把「压根没发出去」说成「failed」。**
   用户看到 `(1 failed)` 会去查网络、查 token、查地址 —— 而真正的原因是门限。
   这和 r47「静默降级比降级本身更坏」、r97「清理命令凭空建出工作区还报 rc=0」
   是同一个病:**失败的谎比失败本身更贵**,它把人引向错误的排查方向。

3. **`notify test` 走的是另一个默认值。**
   `argparse --min-severity` 写的是 `"info"`,dataclass 写的是 `"high"`。
   于是同一个 URL、同一个 provider:

       arl-lite notify test         → min_severity=info → 「[+] sent OK」
       arl-lite correlate --notify  → min_severity=high → 一条都不发

   而 `notify test` 是用户**唯一**的验证手段。它说通了,用户就认为通知已配好。
   这是本轮最重的一条:**唯一的验证手段在撒谎。**

根因是同一个旋钮有两个默认值 —— 两处手抄,迟早漂(决策 #9)。

## 修法

- 等级表与默认门限各留**一处**定义,CLI 的 choices 从它派生,默认值读同一个常量。
- 汇总行区分 **skipped(门限挡下,没尝试)** 与 **failed(真试了没成)**。
- `watcher` 不再丢掉 `notify_*` 的返回值,并把三种结局分开记。

## 为什么要有第 2 节那几条「不许再分叉」

因为这个 bug 的形态就是**两个地方各写一个默认值然后各跑各的**。
只钉住「默认门限的值」不够 —— 下一个人照样可以在 argparse 里写回 `"high"`。
所以要钉的是「**这两处读的是同一个东西**」。
"""
from __future__ import annotations

import argparse
import logging

import pytest

from arl_lite.cli import _notify_correlations, build_parser
from arl_lite.core.correlation_engine import CorrelationHit
from arl_lite.notify import (
    WebhookConfig,
    notify_critical_finding,
    notify_task_done,
)
from arl_lite.notify.webhook import DEFAULT_MIN_SEVERITY, SEVERITY_ORDER, SEVERITY_RANK

RANK = SEVERITY_RANK


def _args(**kw) -> argparse.Namespace:
    base = dict(notify=True, webhook_url="http://notify.invalid/hook",
                webhook_provider="local", webhook=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _hit(risk: int = 9) -> CorrelationHit:
    """一条真实形状的关联命中

    注意它**没有 severity** —— 那正是问题的一半:`CorrelationHit` 的
    `to_dict()` 里根本没有这个键(实测键:advice / confidence* /
    description / evidence_preview / headline / matched_count / risk /
    rule_name / tags / target)。
    """
    return CorrelationHit(rule_name="exposed_admin_panel", risk=risk,
                          target={"host": "admin.example.com"},
                          headline="admin panel exposed", tags=["rce"])


def _sent_lines(caplog) -> list[str]:
    """`local` provider 真正「发出」时会打的那一行

    用它当「确实发出去了」的证据,而不是用返回值 —— 返回值在门限挡下时
    也是 False,分不出「没发」和「发了但失败」。
    """
    return [r.getMessage() for r in caplog.records
            if "[local webhook]" in r.getMessage()]


# ── 一、主判据:默认配置下,每条通知路径都真的发得出去 ──

def test_task_done_notification_can_actually_be_sent(caplog):
    """「任务完成」通知在**默认配置**下必须发得出去

    它固定发 `info`(无错)/`medium`(有错)。默认门限曾经是 `high`,
    于是这个函数在任何默认配置下都只能返回 False —— 它是一条
    **永远不会触发**的通知。
    """
    cfg = WebhookConfig(provider="local")
    with caplog.at_level(logging.INFO, logger="arl_lite.notify"):
        ok = notify_task_done(cfg, task_id=7, target="example.com",
                              found=12, duration_seconds=3.2)
    assert ok, (
        f"默认门限 {cfg.min_severity!r} 下「任务完成」通知发不出去 —— "
        f"它固定发 severity=info,结构上永远被挡")
    assert _sent_lines(caplog), "返回 True 但没有真发出过的痕迹"


def test_task_done_with_errors_can_also_be_sent(caplog):
    """有错误的那条也必须发得出去(`medium`)"""
    cfg = WebhookConfig(provider="local")
    with caplog.at_level(logging.INFO, logger="arl_lite.notify"):
        ok = notify_task_done(cfg, task_id=7, target="example.com", found=3,
                              duration_seconds=1.0, errors=["subfinder missing"])
    assert ok, f"默认门限下带错误的任务通知也发不出去:{cfg.min_severity!r}"
    assert _sent_lines(caplog)


def test_critical_finding_without_a_severity_field_can_be_sent(caplog):
    """finding **没有** severity 字段时也必须发得出去

    实测:finding dict 里没有 `severity` 是常态(那条路径就是
    `finding.get("severity", "info")`)。落进 `"info"` 之后,
    默认门限一高,critical finding 通知就永远不发 ——
    而用户恰恰最想收到 critical 的那一条。
    """
    cfg = WebhookConfig(provider="local")
    with caplog.at_level(logging.INFO, logger="arl_lite.notify"):
        ok = notify_critical_finding(
            cfg, {"title": "SQLi on /api", "description": "stack trace leaked"})
    assert ok, (
        f"没有 severity 字段的 finding 在默认门限 {cfg.min_severity!r} 下发不出去")
    assert _sent_lines(caplog)


def test_correlation_notification_can_actually_be_sent(caplog):
    """关联命中通知在默认配置下必须发得出去

    `CorrelationHit` 没有 severity 字段 → `notify_correlation` 恒取 `"info"`。
    这是 r99 那句「notified 0/1 (1 failed)」的直接来源。
    """
    with caplog.at_level(logging.INFO, logger="arl_lite.notify"):
        line = _notify_correlations(
            argparse.Namespace(notify=True, webhook_url="http://notify.invalid/h",
                               webhook_provider="local"), [_hit()], 1)
    assert _sent_lines(caplog), f"关联通知在默认配置下一条没发出去:{line!r}"
    assert "notified 1/1" in line, f"汇总行没反映真实结果:{line!r}"
    assert "failed" not in line, (
        f"真的发出去了,汇总行却说 failed —— 那又是一句假话:{line!r}")


# ── 二、不许再分叉:同一个旋钮只能有一个来源 ──

def test_cli_and_config_agree_on_the_default_severity():
    """`--min-severity` 的默认值必须**就是** `WebhookConfig` 的默认值

    这条直接对着 r99 的根因:原来 argparse 写 `"info"`、dataclass 写
    `"high"`,于是 `notify test` 说通了而真实通知一条不发。
    只钉住某一个值不够 —— 要钉住**两者读的是同一个东西**。
    """
    parser = build_parser()
    ns = parser.parse_args(["notify", "test"])
    assert ns.min_severity == DEFAULT_MIN_SEVERITY, (
        f"CLI 默认 {ns.min_severity!r} != DEFAULT_MIN_SEVERITY "
        f"{DEFAULT_MIN_SEVERITY!r} —— 两个地方各写各的,迟早漂(决策 #9)")
    assert WebhookConfig(provider="local").min_severity == DEFAULT_MIN_SEVERITY, (
        "WebhookConfig 的默认值也不等于那个常量 —— 第三处了")


def test_cli_severity_choices_are_derived_not_retyped():
    """`--min-severity` 的 choices 必须**由等级表派生**,不是手抄一份

    原来 choices 是字面量 `["info","low","medium","high","critical"]`,
    和 `SEVERITY_RANK` 各写各的。加一个等级要改两个地方,而忘了改的那处
    会让新等级**根本无法从 CLI 选中**。

    ## 为什么从 `--help` 读,不去摸 argparse 的私有属性

    首版写的是 `parser._subparsers._group_actions[0].choices["notify"]
    .choices["test"]` —— 那是 argparse 的内部结构,版本一换就碎。
    **判据自己碎掉,等于这条从来没守过**,而且它碎的时候报的是
    AttributeError,看起来像被测代码坏了,不像判据坏了(决策 #3)。

    `--help` 的输出是 argparse 承诺稳定的公开接口,而且它恰好就是
    用户能看到的那份清单 —— 用户在 `--help` 里看到的 choices 才是
    真正约束他的东西。
    """
    import contextlib
    import io

    from arl_lite.cli import main

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            main(["notify", "test", "--help"])
            raise AssertionError("`--help` 没有抛 SystemExit —— argparse 行为变了")
        except SystemExit as e:
            # r99:这里原来写的是 `except SystemExit: pass` —— 吞掉。
            # 那被 `test_swallowed_exceptions_are_accounted_for` 当成
            # 「接住了就当没事」的新增站点。
            #
            # 但它根本不该是「接住了当没事」:`--help` 抛 SystemExit **就是**
            # argparse 的正常契约,是这里要断言的事实。捕获它、断言它的退出码,
            # 「吞异常」就变成了「核对行为」—— 登记表里也就没有新增项。
            assert e.code == 0, f"`--help` 退出码是 {e.code!r},应为 0"
    help_text = buf.getvalue()
    assert help_text, "`notify test --help` 没有输出 —— 取法坏了"

    rendered = "{" + ",".join(SEVERITY_ORDER) + "}"
    assert rendered in help_text, (
        f"--help 里的 choices 不是等级表 {rendered},实际:{help_text!r} —— "
        f"又分叉了")
    assert f"默认 {SEVERITY_ORDER[0]}" in help_text, (
        f"--help 没说明默认值来自等级表头 {SEVERITY_ORDER[0]!r}:{help_text!r}")


def test_every_severity_rank_is_derived_from_the_one_list():
    """`SEVERITY_RANK` 必须由 `SEVERITY_ORDER` 推导,不能另写一张表

    ## 变异 M3 逮到的洞:值相等证明不了「是推导出来的」

    首版这条只断言 `SEVERITY_RANK == {name: i for i, name in
    enumerate(SEVERITY_ORDER)}`。而把 `SEVERITY_RANK` 换成**手写字典**
    `{"info": 0, "low": 1, ...}` 之后,两个字典**完全相等**,判据照样绿。

    真实后果不是当场错,是**以后**错:下一个人给 `SEVERITY_ORDER` 加一个
    等级,手写的 `SEVERITY_RANK` 不会跟着变,门限比较就按旧顺序判 ——
    而这条判据本该拦住它。**它守的是来源,不是当时的值。**

    所以这里查 AST:`SEVERITY_RANK` 的赋值右边不许是 `ast.Dict` 字面量。
    """
    assert SEVERITY_RANK == {name: i for i, name in enumerate(SEVERITY_ORDER)}, (
        "SEVERITY_RANK 与 SEVERITY_ORDER 不是一一对应的顺序表")
    assert sorted(SEVERITY_RANK) == sorted(SEVERITY_ORDER), "两边成员不一样"
    # 顺序本身是契约:门限比较是 `sev_rank >= min_rank`,顺序错了门限就反了
    assert RANK["info"] < RANK["low"] < RANK["medium"] < RANK["high"] < RANK["critical"]

    # ── 来源:必须是推导,不能是手写字典 ──
    import ast
    import pathlib

    import arl_lite.notify.webhook as wh
    src = pathlib.Path(wh.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    assigns = [n for n in tree.body
               if isinstance(n, ast.Assign) and len(n.targets) == 1
               and isinstance(n.targets[0], ast.Name)
               and n.targets[0].id == "SEVERITY_RANK"]
    assert assigns, "webhook.py 里找不到 SEVERITY_RANK 的赋值"

    kinds = {type(n.value).__name__ for n in assigns}
    assert "Dict" not in kinds, (
        f"SEVERITY_RANK 是手写字典({kinds})—— 值此刻是对的,但加等级时"
        f"它不会跟着 SEVERITY_ORDER 变,而门限比较会按旧顺序判错")
    assert "DictComp" in kinds or "Call" in kinds, (
        f"SEVERITY_RANK 既不是推导也不是字面量({kinds})—— 判据自己跟不上实现的变化")


# ── 三、汇总行不许把「没发」说成「failed」 ──

def test_below_the_threshold_is_reported_as_skipped_not_failed(caplog):
    """被门限挡下的必须说「没有尝试发送」,**不许**说 failed

    用户看到 `failed` 会去查网络、查 token、查地址 —— 而真正的原因是门限。
    把人引向错误的排查方向,比不说更贵。
    """
    cfg = WebhookConfig(provider="local", min_severity="critical")
    with caplog.at_level(logging.INFO, logger="arl_lite.notify"):
        line = _notify_correlations(
            argparse.Namespace(notify=True, webhook_url="",
                               webhook_provider="local", webhook=cfg),
            [_hit(risk=5)], 1)
    assert "failed" not in line, (
        f"被门限挡下的被说成 failed —— 用户会去查网络而不是查门限:{line!r}")
    assert "critical" in line, f"没说出门限是多少,用户不知道该改什么:{line!r}"
    assert not _sent_lines(caplog), "被挡下的不该真发出去"


def test_a_real_send_failure_is_still_counted_as_failed(monkeypatch):
    """**正控制**:真失败仍然要算 failed

    上一条把 skipped 和 failed 分开了,但如果分得太狠 —— 把所有 False
    都算成 skipped —— 那真故障又会重新变成沉默。所以必须有一条判据
    守住「真失败仍然是 failed」。
    """
    cfg = WebhookConfig(provider="local")           # 默认门限 info,不过滤

    def boom(*a, **kw):
        raise RuntimeError("模拟发送器炸了")

    monkeypatch.setattr("arl_lite.notify.notify_correlation", boom)
    line = _notify_correlations(
        argparse.Namespace(notify=True, webhook_url="",
                           webhook_provider="local", webhook=cfg),
        [_hit()], 1)

    assert "failed" in line, (
        f"真的抛异常了,汇总行却不说 failed —— 那又是一条沉默的故障:{line!r}")
    assert "min_severity" not in line, (
        f"真故障被说成「低于门限」,那是在给网络问题编一个配置借口:{line!r}")


def test_nothing_is_claimed_when_there_is_nothing_to_send():
    """**边界**:门限刚好卡住时,两段说明都不该出现

    `failed` 和「低于门限」是两件不同的事,都没有就不该提 ——
    凭空多一句解释,是在给一个没发生过的事编故事。
    """
    cfg = WebhookConfig(provider="local")
    line = _notify_correlations(
        argparse.Namespace(notify=True, webhook_url="",
                           webhook_provider="local", webhook=cfg), [], 0)
    assert "failed" not in line, f"空批次里出现了 failed:{line!r}"
    assert "min_severity" not in line, f"空批次里出现了门限说明:{line!r}"


# ── 四、`local` provider 不该因为没有 URL 被判死 ──

def test_a_local_provider_config_is_not_rejected_for_having_no_url():
    """注入合法的 `local` 配置不许被说成「没给 webhook url」

    `local` 只写日志、不发 HTTP,**本来就不需要 URL**。原来那里是
    无条件查 url,于是本地调试这条路每次都被判死,还报了一句
    和真实原因完全无关的话。
    """
    cfg = WebhookConfig(provider="local")
    assert cfg.url == "", "前置条件:local 配置确实没有 url"
    line = _notify_correlations(
        argparse.Namespace(notify=True, webhook_url="",
                           webhook_provider="local", webhook=cfg), [_hit()], 1)
    assert "no webhook url" not in line, (
        f"合法的 local 配置被判成「没给 url」:{line!r}")
    assert "notified 1/1" in line, f"local 配置下通知没发出去:{line!r}"


def test_a_non_local_provider_still_requires_a_url():
    """**正控制**:非 local 的仍然必须有 URL —— 上面那条不许放过头

    这是上一条的反向控制。只钉住「local 能过」而不钉住「非 local 不能过」,
    那条判据就能靠「全都不查 url」变绿。
    """
    from arl_lite.notify.webhook import is_valid_url

    cfg = WebhookConfig(url="http://x.invalid/h", provider="ntfy")
    assert is_valid_url(cfg.url)
    line = _notify_correlations(
        argparse.Namespace(notify=True, webhook_url="",
                           webhook_provider="ntfy", webhook=cfg), [_hit()], 1)
    assert "no webhook url" not in line, f"有 url 的反被判死:{line!r}"


# ── 五、`notify test` 必须还能用,而且不许给出假信心 ──

def test_notify_test_still_works_under_the_unified_default():
    """统一默认值之后,`notify test` 仍然发得出去

    这是 `notify test` 作为**唯一验证手段**的前提。它要是坏了,用户就
    再也没有任何办法确认通知配好了 —— 那比它撒谎还糟。
    """
    cfg = WebhookConfig(provider="local", min_severity=DEFAULT_MIN_SEVERITY)
    from arl_lite.notify import notify
    ok = notify(cfg, title="[ARL] Test notification", message="ping",
                severity="high", tags=["test_tube"])
    assert ok, f"统一后的默认门限下 notify test 发不出去:{cfg.min_severity!r}"


def test_notify_test_says_which_gate_it_used():
    """**正控制**:`notify test` 报的默认门限必须等于真实路径用的那个

    r99 之前 `notify test` 报 `min_severity=info`(argparse)而真实路径用
    `high`(dataclass)。这条把两者钉在一起。
    """
    from arl_lite.cli import main
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(["notify", "test", "--provider", "local"])
    out = buf.getvalue()
    assert rc == 0, f"notify test 失败了:{out!r}"
    assert f"min_severity={DEFAULT_MIN_SEVERITY}" in out, (
        f"notify test 报的默认门限({out!r})和真实路径用的 "
        f"{DEFAULT_MIN_SEVERITY!r} 对不上 —— 那正是 r99 的根因")


# ── 六、watch 那条路径:返回值不许再被丢掉 ──

def test_watcher_does_not_throw_away_the_notify_results():
    """`Watcher` 里那两处 `notify_*` 的返回值必须被**用上**

    原来两处都是裸调用,返回值整个丢掉。配上当时的默认门限,watch 每轮
    都「跑完了」,但一条通知没发,而用户从现象上分不清「没配 webhook」
    和「配了但一条没到」。

    ## 为什么用 AST 而不是跑一整轮 watch

    跑真的一轮 watch 要 Storage + 任务执行器,代价大、还依赖机器状态,
    而且它验的是「跑完之后日志长什么样」—— 慢且脆。
    这里只问一个结构问题:**结果被用了吗**。r97 那次已经吃过一次教训:
    「判据自己坏掉」报出来的错看起来像被测代码坏了(决策 #3)。
    """
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "arl_lite" / "core" / "watcher.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))

    discarded = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("notify_task_done", "notify_critical_finding")):
            continue
        # 丢弃的形态:它是一条**独立的**表达式语句(父节点就是 Expr)。
        for parent in ast.walk(tree):
            if (isinstance(parent, ast.Expr) and parent.value is node):
                discarded.append(f"{node.func.id} @ line {node.lineno}")

    assert not discarded, (
        f"这些 notify 调用把返回值整个丢掉了:{discarded} —— "
        f"发没发出去、还是被门限挡下,全都没人知道")


def test_watcher_records_whether_notifications_went_out():
    """**正控制**:watcher 必须留下一句「发了几条 / 挡下几条」

    上一条只保证「结果被用上了」。如果只是 `x = notify(...)` 然后丢掉
    `x`,结构上照样满足 —— 所以还要问一句:结果有没有**变成可读的信息**。
    判据只查它有没有那条记录(按 `log.` 调用 + 那两个计数的名字),
    不去匹配完整文案 —— 文案会变,结构不会。
    """
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "arl_lite" / "core" / "watcher.py"
    text = src.read_text(encoding="utf-8")
    tree = ast.parse(text)

    counts = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        # `sent = dropped = 0` 是**一个** Assign 带两个 target。
        # 首版只取 targets[0],于是 `sent` 没被看见,判据在**正确的代码**上
        # 报红 —— 又一次「判据自己坏掉看起来像实现坏了」(r93 那次栽过,
        # r97 的 argparse 私有结构那次也是同一个形状)。
        for tgt in node.targets:
            if isinstance(tgt, ast.Name):
                counts.add(tgt.id)
    assert {"sent", "dropped"} <= counts, (
        f"watcher 里没有 sent/dropped 两个计数(现有:{sorted(counts)}) —— "
        f"「发了几条、挡下几条」没有落到任何地方")

    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and isinstance(n.func.value, ast.Name)
               and n.func.value.id == "log"
               for n in ast.walk(tree)), (
        "watcher 里没有任何 log 调用 —— 计数就算有了也没人看得见")
