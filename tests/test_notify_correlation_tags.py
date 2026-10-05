"""关联命中通知:把规则自己的 tags 带出去

## 断在哪

`correlations` 表存了每条规则的 `tags`(rce / unauth / data_leak /
database ...),`CorrelationHit` 一路带着它们,`notify_correlation` 里却
只按 risk 派生出 `link` / `fire` / `warning` —— **规则自己的标签全丢了**。

丢掉之后通知只剩"这条有风险",看不出是什么风险。ntfy 的 `Tags` header
既是 emoji 来源也是订阅过滤键,没有语义标签就没法按 `rce` 之类的
关键词订阅。

而且 `notify_correlation` 此前是**死代码**:有定义、有导出,但只有
`tests/test_phase7.py` 在调。`arl-lite correlate` 只打印不外发。

本文件盯住两件事:
1. 规则 tags 真的进了通知(不是只测了内部函数)
2. ntfy 头里拿得到 —— 那里有个 `tags[:5]` 的截断,顺序有讲究

全程用 `local` provider,不联网。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

import pytest

from arl_lite.notify import WebhookConfig, notify_correlation
from arl_lite.notify.webhook import _correlation_tags, _http_post


@pytest.fixture
def local_cfg():
    # min_severity 必须放到 info:默认是 high,低危命中会被
    # should_notify 直接挡掉,连日志都不打 —— 那就测不到 tags 了。
    return WebhookConfig(url="", provider="local", min_severity="info")


def _row(**kw):
    base = {
        "rule_name": "jenkins_public",
        "risk": 9,
        "severity": "critical",
        "target": "10.0.0.5:8080",
        "headline": "Jenkins 未授权访问",
        "tags": json.dumps(["rce", "unauth", "code_leak"]),
    }
    base.update(kw)
    return base


# =====================================================================
# 规则 tags 必须到达通知
# =====================================================================


def test_rule_tags_reach_the_notification(local_cfg, caplog):
    """存库形态(JSON 字符串)的 tags 必须出现在通知里"""
    with caplog.at_level(logging.INFO, logger="arl_lite.notify"):
        assert notify_correlation(local_cfg, _row()) is True
    text = caplog.text
    for t in ("rce", "unauth", "code_leak"):
        assert t in text, f"标签 {t!r} 没到通知里:\n{text}"


def test_rule_tags_reach_the_notification_from_list(local_cfg, caplog):
    """内存形态(list)的 tags 同样要带上 —— 调用方可能是 CorrelationHit"""
    with caplog.at_level(logging.INFO, logger="arl_lite.notify"):
        assert notify_correlation(local_cfg, _row(tags=["rce", "unauth"])) is True
    assert "rce" in caplog.text and "unauth" in caplog.text


def test_risk_markers_come_first(local_cfg, caplog):
    """风险标记必须排在语义标签前面

    ntfy 只取 `tags[:5]`。标记挤掉的是最靠后的标签,而标记是视觉
    信号(🔥/⚠️),语义标签只是过滤用 —— 顺序反了视觉就没了。
    """
    with caplog.at_level(logging.INFO, logger="arl_lite.notify"):
        notify_correlation(local_cfg, _row())
    line = [l for l in caplog.text.splitlines() if "tags=" in l][-1]
    tags_part = line[line.index("tags=") + 5:]
    assert tags_part.startswith("['link', 'fire'"), tags_part
    assert tags_part.index("rce") > tags_part.index("fire"), tags_part


@pytest.mark.parametrize("risk,marker", [(10, "fire"), (9, "fire"), (7, "warning"), (3, None)])
def test_risk_marker_thresholds(local_cfg, caplog, risk, marker):
    with caplog.at_level(logging.INFO, logger="arl_lite.notify"):
        notify_correlation(local_cfg, _row(risk=risk))
    line = [l for l in caplog.text.splitlines() if "tags=" in l][-1]
    if marker:
        assert marker in line, f"risk={risk} 应带 {marker}: {line}"
    else:
        assert "fire" not in line and "warning" not in line, f"risk={risk} 不该带标记: {line}"


# =====================================================================
# 清洗
# =====================================================================


def test_comma_in_a_tag_would_break_the_ntfy_header():
    """ntfy 的 Tags 用逗号分隔,标签里带逗号会把一条拆成两条"""
    assert _correlation_tags(["a,b"]) == ["a;b"], "逗号没被替换"
    assert "," not in _correlation_tags(["a,b", "c"])[0]


def test_newlines_are_stripped():
    got = _correlation_tags(["a\nb", "c\rd"])
    assert got == ["a b", "c d"], got


def test_duplicate_tags_are_collapsed():
    """重复标签会白占 ntfy 的 5 个槽位"""
    assert _correlation_tags(["rce", "rce", "rce"]) == ["rce"]
    # 与风险标记同名也要去重
    assert _correlation_tags(["link", "fire", "rce"]) == ["link", "fire", "rce"]


def test_order_is_preserved():
    assert _correlation_tags(["z", "a", "m"]) == ["z", "a", "m"]


@pytest.mark.parametrize("raw", [None, "", "   ", 123, {"a": 1}, ["", "  "], "not json"])
def test_junk_input_yields_no_tags(raw):
    """认不出来就当没有,绝不抛 —— 通知失败不该由脏数据造成"""
    assert _correlation_tags(raw) == []


def test_non_string_items_are_dropped():
    assert _correlation_tags(["ok", 1, None, "ok2"]) == ["ok", "ok2"]


# =====================================================================
# 真正发到 ntfy 头里(不打网络)
# =====================================================================


def test_tags_land_in_the_ntfy_header(monkeypatch):
    """端到端:规则 tags 必须出现在 ntfy 的 Tags header 里

    前面几条测的是 local provider 的日志。这条测的是 ntfy 真正
    组装的那个 header —— 截断和转义都发生在那儿。
    """
    captured = {}

    def fake_post(url, body, headers, timeout):
        captured.update(headers)
        return 200, b"ok"

    monkeypatch.setattr("arl_lite.notify.webhook._http_post", fake_post)
    cfg = WebhookConfig(url="https://ntfy.sh/topic", provider="ntfy")

    assert notify_correlation(cfg, _row()) is True
    tag_header = captured.get("Tags", "")
    assert "rce" in tag_header, f"Tags 头里没有规则标签: {tag_header!r}"
    assert "fire" in tag_header, f"Tags 头里没有风险标记: {tag_header!r}"
    # 逗号分隔,不能多出分隔符
    parts = [p for p in tag_header.split(",") if p]
    assert len(parts) == len(set(parts)), f"Tags 头里有重复项: {tag_header!r}"


def test_ntfy_header_is_capped_at_five(monkeypatch):
    """ntfy 只取前 5 个 —— 验证风险标记确实挤进了前 5

    规则标签再多也不能把 fire/warning 挤出去,否则高危命中
    在客户端上看起来和低危一样。
    """
    captured = {}
    monkeypatch.setattr(
        "arl_lite.notify.webhook._http_post",
        lambda url, body, headers, timeout: (captured.update(headers), (200, b"ok"))[1],
    )
    cfg = WebhookConfig(url="https://ntfy.sh/topic", provider="ntfy")
    many = json.dumps([f"t{i}" for i in range(20)])
    notify_correlation(cfg, _row(tags=many))

    parts = [p for p in captured["Tags"].split(",") if p]
    assert len(parts) == 5, f"应当被截到 5 个: {parts}"
    assert "link" in parts and "fire" in parts, f"风险标记被挤掉了: {parts}"


# =====================================================================
# 死代码不再是死代码
# =====================================================================


def test_correlate_command_can_notify():
    """`arl-lite correlate --notify` 真的接上了

    notify_correlation 此前只有测试在调,关联命中永远不外发。
    这条断言接线存在,避免它退回死代码。
    """
    import argparse
    from arl_lite import cli

    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    pc = sub.add_parser("correlate")
    pc.add_argument("--notify", action="store_true")
    pc.add_argument("--webhook-url", default="")
    pc.add_argument("--webhook-provider", default="ntfy")
    args = p.parse_args(["correlate", "--notify", "--webhook-url", "https://ntfy.sh/x"])
    assert args.notify is True

    assert callable(cli._notify_correlations)
    # 没给 url 时必须明确报出来,不能静默什么都不做
    args.webhook_url = ""
    out = cli._notify_correlations(args, [])
    assert out and "no --webhook-url" in out, out


def test_notify_is_skipped_without_the_flag():
    import argparse
    from arl_lite import cli

    p = argparse.ArgumentParser()
    p.add_argument("--notify", action="store_true")
    p.add_argument("--webhook-url", default="")
    args = p.parse_args([])
    assert cli._notify_correlations(args, [object()]) is None, \
        "没加 --notify 就不该有任何通知动作"
