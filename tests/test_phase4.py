"""arl-lite Phase 4 测试 — AI 集成层

测试覆盖:
1. Config CRUD + env 覆盖 + path traversal 防护
2. Client 3 态返回 + 各 provider 协议(mock)
3. 5 个 ai 命令在 fallback 模式下正常输出
4. CLI 5 个 ai 子命令入口
5. 边界:空输入 / 越界 / 不存在的 id
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))


def ok(msg: str) -> None:
    print(f"  \033[32m✓\033[0m {msg}")


def fail(msg: str) -> None:
    print(f"  \033[31m✗\033[0m {msg}")
    raise AssertionError(msg)


# =========================
# 1. Config
# =========================

def test_config():
    print("\n[1] Config CRUD + 路径安全 + env 覆盖")

    from arl_lite.ai.config import (
        AIConfig, CONFIG_PATH, PROVIDERS, get_config, load_config,
        reset_config, save_config, set_config,
    )

    # 临时 HOME 隔离
    with tempfile.TemporaryDirectory() as home:
        os.environ["HOME"] = home
        from arl_lite.ai import config as cfg_mod
        cfg_mod.CONFIG_PATH = Path(home) / ".arl-lite" / "config.json"

        # 1.1 默认无 config
        reset_config(None)
        cfg = get_config()
        if cfg is None:
            ok("默认无 config → get_config() 返回 None")
        else:
            fail(f"期望 None,得到 {cfg.provider}")

        # 1.2 set + get
        c = set_config("openai", api_key="sk-test-1234567890", model="gpt-4o")
        if c.provider == "openai" and c.api_key == "sk-test-1234567890":
            ok("set openai api_key + model")
        else:
            fail(f"set 失败:{c}")

        c2 = set_config("ollama", base_url="http://localhost:11434", model="llama3")
        if c2.is_configured() and c2.model == "llama3":
            ok("ollama 不用 api_key 也能配置")
        else:
            fail(f"ollama 配置失败:{c2}")

        # 1.3 配置文件 mode 0600
        mode = cfg_mod.CONFIG_PATH.stat().st_mode & 0o777
        if mode == 0o600:
            ok(f"config file mode 0600")
        else:
            fail(f"config file mode 应该是 0600,实际 {oct(mode)}")

        # 1.4 路径穿越
        try:
            set_config("../evil", api_key="x")
            fail("set_config 没拦路径穿越")
        except ValueError as e:
            ok(f"set_config 路径穿越: {e}")

        # 1.5 未知 provider
        try:
            set_config("unknown", api_key="x")
            fail("set_config 没拦未知 provider")
        except ValueError as e:
            ok(f"set_config 未知 provider: {e}")

        # 1.6 env 覆盖
        os.environ["ARL_AI_OPENAI_API_KEY"] = "sk-from-env"
        # 重新加载
        configs = load_config()
        if configs["openai"].api_key == "sk-from-env":
            ok("env 覆盖 file")
        else:
            fail(f"env 覆盖失败:{configs['openai'].api_key}")
        del os.environ["ARL_AI_OPENAI_API_KEY"]

        # 1.7 PROVIDERS 包含 4 个
        if set(PROVIDERS) == {"openai", "anthropic", "google", "ollama"}:
            ok("PROVIDERS = 4 个")
        else:
            fail(f"PROVIDERS 不对:{PROVIDERS}")


# =========================
# 2. Client (mock 不真打 API)
# =========================

def test_client():
    print("\n[2] Client 3 态返回 + mock 协议")

    from arl_lite.ai.client import (
        AIError, AINetworkError, AITimeoutError, AIRateLimitError,
        AIBadRequestError, CompletionResult, _complete_openai,
        _complete_anthropic, _complete_google, _complete_ollama,
    )
    from arl_lite.ai.config import AIConfig

    # 2.1 CompletionResult dataclass
    r = CompletionResult(ok=True, content="hi", model="m", provider="openai")
    d = r.to_dict()
    if d["ok"] and d["content"] == "hi" and d["model"] == "m":
        ok("CompletionResult.to_dict")
    else:
        fail(f"to_dict 错:{d}")

    # 2.2 OpenAI mock — 正常响应
    cfg = AIConfig(provider="openai", api_key="sk-test")
    fake_resp = {
        "choices": [{"message": {"content": "hello"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    with patch("arl_lite.ai.client._post_json", return_value=fake_resp):
        result = _complete_openai(cfg, "system", "user")
    if result.ok and result.content == "hello" and result.tokens_in == 10 and result.tokens_out == 5:
        ok("openai mock: 正常响应")
    else:
        fail(f"openai mock: {result.to_dict()}")

    # 2.3 OpenAI mock — 网络错误
    with patch("arl_lite.ai.client._post_json", side_effect=AINetworkError("conn refused")):
        result = _complete_openai(cfg, "system", "user")
    if not result.ok and result.error_type == "network":
        ok("openai mock: 网络错误")
    else:
        fail(f"openai 网络错:{result.to_dict()}")

    # 2.4 OpenAI mock — auth 失败(401)
    with patch("arl_lite.ai.client._post_json", side_effect=AIBadRequestError("auth", status=401)):
        result = _complete_openai(cfg, "system", "user")
    if not result.ok and result.error_type == "bad_request":
        ok("openai mock: 401 auth")
    else:
        fail(f"openai 401:{result.to_dict()}")

    # 2.5 OpenAI mock — rate limit (429)
    with patch("arl_lite.ai.client._post_json", side_effect=AIRateLimitError("rate", status=429)):
        result = _complete_openai(cfg, "system", "user")
    if not result.ok and result.error_type == "rate_limit":
        ok("openai mock: 429 rate limit")
    else:
        fail(f"openai 429:{result.to_dict()}")

    # 2.6 Anthropic mock
    cfg_a = AIConfig(provider="anthropic", api_key="sk-ant-test")
    fake_a = {
        "content": [{"text": "anthropic reply"}],
        "usage": {"input_tokens": 8, "output_tokens": 4},
    }
    with patch("arl_lite.ai.client._post_json", return_value=fake_a):
        result = _complete_anthropic(cfg_a, "sys", "user")
    if result.ok and result.content == "anthropic reply" and result.tokens_in == 8:
        ok("anthropic mock")
    else:
        fail(f"anthropic:{result.to_dict()}")

    # 2.7 Google mock
    cfg_g = AIConfig(provider="google", api_key="goog-test")
    fake_g = {
        "candidates": [{"content": {"parts": [{"text": "google reply"}]}}],
        "usageMetadata": {"promptTokenCount": 6, "candidatesTokenCount": 3},
    }
    with patch("arl_lite.ai.client._post_json", return_value=fake_g):
        result = _complete_google(cfg_g, "sys", "user")
    if result.ok and result.content == "google reply" and result.tokens_in == 6:
        ok("google mock")
    else:
        fail(f"google:{result.to_dict()}")

    # 2.8 Ollama mock
    cfg_o = AIConfig(provider="ollama", base_url="http://localhost:11434")
    fake_o = {
        "message": {"content": "ollama reply"},
        "prompt_eval_count": 12,
        "eval_count": 7,
    }
    with patch("arl_lite.ai.client._post_json", return_value=fake_o):
        result = _complete_ollama(cfg_o, "sys", "user")
    if result.ok and result.content == "ollama reply" and result.tokens_in == 12:
        ok("ollama mock")
    else:
        fail(f"ollama:{result.to_dict()}")

    # 2.9 response 异常 shape
    with patch("arl_lite.ai.client._post_json", return_value={"unexpected": "shape"}):
        result = _complete_openai(cfg, "sys", "user")
    if not result.ok and result.error_type == "bad_response":
        ok("openai mock: 异常 shape")
    else:
        fail(f"openai 异常 shape:{result.to_dict()}")


# =========================
# 3. fallback 模板(无 AI 配置)
# =========================

def test_fallback():
    print("\n[3] Fallback 模板(无 AI 配置)")

    from arl_lite.ai.prompts import (
        fallback_ask, fallback_report, fallback_explain, fallback_suggest,
        fallback_fix, prepare_data_for_ask, prepare_data_for_report,
        prepare_data_for_suggest, to_json,
    )

    # 3.1 ask
    out = fallback_ask("哪些子域", {"domains": ["a.com", "b.com"]})
    if "[离线模式]" in out and "a.com" in out:
        ok("fallback_ask: 子域问题")
    else:
        fail(f"fallback_ask:{out}")

    out2 = fallback_ask("端口情况", {"ports": [1, 2, 3]})
    if "3 个端口" in out2:
        ok("fallback_ask: 端口问题")
    else:
        fail(f"fallback_ask 端口:{out2}")

    out3 = fallback_ask("我爱你", {"domains": []})
    if "[离线模式]" in out3:
        ok("fallback_ask: 不认识的问题(兜底)")
    else:
        fail(f"fallback_ask 未知:{out3}")

    # 3.2 report
    data = {
        "summary": {"total_correlations": 10, "by_level": {"critical": 2, "high": 3, "medium": 4, "low": 1}, "max_risk": 9, "unique_targets": 5},
        "risks": [{"target": "1.1.1.1", "risk_score": 10, "risk_level": "critical"}],
    }
    out = fallback_report(data)
    if "安全报告" in out and "critical" in out and "1.1.1.1" in out:
        ok("fallback_report: 模板报告")
    else:
        fail(f"fallback_report:{out[:200]}")

    # 3.3 explain
    corr = {"rule_name": "exposed_database", "headline": "DB exposed", "risk": 9, "advice": "fix it"}
    out = fallback_explain(corr)
    if "exposed_database" in out and "DB exposed" in out and "fix it" in out:
        ok("fallback_explain: 关联")
    else:
        fail(f"fallback_explain:{out}")

    # 3.4 suggest
    out = fallback_suggest({"domains": ["x.com"], "ports": [], "findings": []})
    if "[离线模式]" in out and "建议" in out:
        ok("fallback_suggest: 子域少+无 port")
    else:
        fail(f"fallback_suggest:{out}")

    # 3.5 fix
    f = {"finding_type": "exposed_port", "target": "1.1.1.1:22", "title": "SSH open", "severity": "high"}
    out = fallback_fix(f)
    if "SSH open" in out and "P2" in out:
        ok("fallback_fix")
    else:
        fail(f"fallback_fix:{out}")

    # 3.6 to_json 安全
    s = to_json({"a": 1, "b": [1, 2, 3]})
    if isinstance(s, str) and '"a": 1' in s:
        ok("to_json 序列化")
    else:
        fail(f"to_json:{s}")


# =========================
# 4. 5 个 ai 命令(无 AI 配置)
# =========================

def test_commands():
    print("\n[4] 5 个 ai 命令(fallback)")

    from arl_lite.db.storage import Storage
    from arl_lite.ai.commands import (
        cmd_ai_ask, cmd_ai_report, cmd_ai_explain, cmd_ai_suggest,
        cmd_ai_fix, cmd_ai_config_get, cmd_ai_config_list,
    )
    from arl_lite.ai import config as cfg_mod

    with tempfile.TemporaryDirectory() as home:
        os.environ["HOME"] = home
        cfg_mod.CONFIG_PATH = Path(home) / ".arl-lite" / "config.json"
        cfg_mod.reset_config(None)

        # 准备一个 workspace
        ws = tempfile.mkdtemp()
        s = Storage("p4", ws)
        s.create_task(s.workspace_id, "x.com")
        for sub in ["www", "mail", "admin"]:
            s.add_host(task_id=1, host=f"{sub}.x.com", source="crtsh", confidence=0.8)
        s.add_port(task_id=1, host="1.1.1.1", port=3306, state="open", service="mysql")
        s.add_port(task_id=1, host="1.1.1.1", port=22, state="open", service="ssh")
        s.add_finding(
            task_id=1, target="1.1.1.1:3306", finding_type="exposed_port",
            title="MySQL on 3306", description="public mysql", evidence="banner:5.7",
            severity="high",
        )

        # 跑关联 + 保存
        from arl_lite.core.correlation_engine import run_all_rules, save_correlations
        rules_dir = str(Path(__file__).parent.parent) + "/arl_lite/modules/analysis/rules"
        hits = run_all_rules(s, rules_dir)
        save_correlations(s, hits)

        # 4.1 ask(无 AI → fallback)
        args = type("Args", (), {"question": "哪些端口暴露", "workspace": "p4"})()
        rc = cmd_ai_ask(args, storage=s)
        if rc == 0:
            ok("ai ask (无 AI → fallback) exit=0")
        else:
            fail(f"ai ask exit={rc}")

        # 4.2 ask 空 question
        args = type("Args", (), {"question": "", "workspace": "p4"})()
        rc = cmd_ai_ask(args, storage=s)
        if rc == 2:
            ok("ai ask 空 question exit=2")
        else:
            fail(f"ai ask 空 exit={rc}")

        # 4.3 report
        args = type("Args", (), {"workspace": "p4"})()
        rc = cmd_ai_report(args, storage=s)
        if rc == 0:
            ok("ai report (无 AI → fallback)")
        else:
            fail(f"ai report exit={rc}")

        # 4.4 explain
        args = type("Args", (), {"corr_id": "1", "workspace": "p4"})()
        rc = cmd_ai_explain(args, storage=s)
        if rc == 0:
            ok("ai explain corr_id=1")
        else:
            fail(f"ai explain exit={rc}")

        # 4.5 explain 越界 corr_id
        args = type("Args", (), {"corr_id": "99999", "workspace": "p4"})()
        rc = cmd_ai_explain(args, storage=s)
        if rc == 2:
            ok("ai explain 越界 exit=2")
        else:
            fail(f"ai explain 越界 exit={rc}")

        args = type("Args", (), {"corr_id": "abc", "workspace": "p4"})()
        rc = cmd_ai_explain(args, storage=s)
        if rc == 2:
            ok("ai explain 非数字 exit=2")
        else:
            fail(f"ai explain 非数字 exit={rc}")

        # 4.6 suggest
        args = type("Args", (), {"workspace": "p4"})()
        rc = cmd_ai_suggest(args, storage=s)
        if rc == 0:
            ok("ai suggest")
        else:
            fail(f"ai suggest exit={rc}")

        # 4.7 fix
        args = type("Args", (), {"finding_id": "1", "workspace": "p4"})()
        rc = cmd_ai_fix(args, storage=s)
        if rc == 0:
            ok("ai fix")
        else:
            fail(f"ai fix exit={rc}")

        # 4.8 fix 越界
        args = type("Args", (), {"finding_id": "99999", "workspace": "p4"})()
        rc = cmd_ai_fix(args, storage=s)
        if rc == 2:
            ok("ai fix 越界 exit=2")
        else:
            fail(f"ai fix 越界 exit={rc}")


# =========================
# 5. CLI 子命令入口
# =========================

def test_cli():
    print("\n[5] CLI ai 子命令")

    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent), "HOME": tempfile.mkdtemp()}

    cmds = [
        "ai config list",
        "ai config get",
        "ai config get openai",
        "ai config get invalid_provider",
        "ai config reset",
        "ai config reset openai",
        "ai ask '哪些子域'",
        "ai report",
        "ai suggest",
        "ai explain 1",
        "ai fix 1",
    ]
    for cmd in cmds:
        r = subprocess.run(
            ["python3", "-m", "arl_lite"] + cmd.split(),
            cwd=str(Path(__file__).parent.parent), env=env,
            capture_output=True, text=True, timeout=10,
        )
        # 几乎所有都应该 exit=0
        if r.returncode == 0 or r.returncode == 2:
            ok(f"'{cmd[:40]}': exit={r.returncode}")
        else:
            fail(f"'{cmd[:40]}': exit={r.returncode} | {r.stderr[:100]}")


# =========================
# 6. AI 路径中(用 mock 模拟成功)
# =========================

def test_ai_path():
    print("\n[6] AI 调用成功路径(mock)")

    from arl_lite.db.storage import Storage
    from arl_lite.ai.config import set_config
    from arl_lite.ai import config as cfg_mod
    from arl_lite.ai.commands import cmd_ai_ask, cmd_ai_report
    from arl_lite.ai.client import complete, CompletionResult

    with tempfile.TemporaryDirectory() as home:
        os.environ["HOME"] = home
        cfg_mod.CONFIG_PATH = Path(home) / ".arl-lite" / "config.json"
        cfg_mod.reset_config(None)
        set_config("openai", api_key="sk-test-1234567890", model="gpt-4o")

        ws = tempfile.mkdtemp()
        s = Storage("p4mock", ws)
        s.create_task(s.workspace_id, "x.com")

        # mock complete
        fake_result = CompletionResult(
            ok=True, content="AI 回复:风险集中在 1.1.1.1",
            provider="openai", model="gpt-4o",
            tokens_in=20, tokens_out=10, duration_ms=500,
        )
        with patch("arl_lite.ai.commands.complete", return_value=fake_result):
            args = type("Args", (), {"question": "风险集中在哪", "workspace": "p4mock", "provider": None})()
            rc = cmd_ai_ask(args, storage=s)
        if rc == 0:
            ok("ai ask with AI 成功")
        else:
            fail(f"ai ask with AI exit={rc}")

        with patch("arl_lite.ai.commands.complete", return_value=fake_result):
            args = type("Args", (), {"workspace": "p4mock", "provider": None})()
            rc = cmd_ai_report(args, storage=s)
        if rc == 0:
            ok("ai report with AI 成功")
        else:
            fail(f"ai report with AI exit={rc}")


# =========================
# 7. AI 失败路径
# =========================

def test_ai_failure():
    print("\n[7] AI 调用失败路径(fallback 到模板)")

    from arl_lite.db.storage import Storage
    from arl_lite.ai.config import set_config
    from arl_lite.ai import config as cfg_mod
    from arl_lite.ai.commands import cmd_ai_ask
    from arl_lite.ai.client import CompletionResult

    with tempfile.TemporaryDirectory() as home:
        os.environ["HOME"] = home
        cfg_mod.CONFIG_PATH = Path(home) / ".arl-lite" / "config.json"
        cfg_mod.reset_config(None)
        set_config("openai", api_key="sk-test")

        ws = tempfile.mkdtemp()
        s = Storage("p4fail", ws)
        s.create_task(s.workspace_id, "x.com")
        s.add_host(task_id=1, host="a.com", source="test", confidence=0.5)

        # mock 失败
        fake_fail = CompletionResult(
            ok=False, error="connection refused", error_type="network",
            provider="openai", model="gpt-4o",
        )
        with patch("arl_lite.ai.commands.complete", return_value=fake_fail):
            args = type("Args", (), {"question": "有哪些子域", "workspace": "p4fail", "provider": None})()
            rc = cmd_ai_ask(args, storage=s)
        # 失败 → fallback → exit=0(fallback 输出了)
        if rc == 0:
            ok("ai ask 失败 → fallback exit=0")
        else:
            fail(f"ai ask 失败 exit={rc} (应=0)")


# =========================
# 8. Bug regression — Phase 4 审计发现的 bug
# =========================

def test_bug_regression():
    """Phase 4 审计发现的 8 个 bug 的 regression 测试"""

    from arl_lite.db.storage import Storage
    from arl_lite.ai.config import (
        AIConfig, set_config, get_config, PROVIDERS, reset_config,
    )
    from arl_lite.ai import config as cfg_mod
    from arl_lite.ai.client import (
        _complete_openai, complete, CompletionResult,
    )
    from arl_lite.ai.commands import (
        cmd_ai_ask, cmd_ai_report, cmd_ai_explain, cmd_ai_suggest,
        cmd_ai_fix, cmd_ai_config_set,
    )

    with tempfile.TemporaryDirectory() as home:
        os.environ["HOME"] = home
        cfg_mod.CONFIG_PATH = Path(home) / ".arl-lite" / "config.json"
        cfg_mod.reset_config(None)

        # Bug 1: temperature 越界
        try:
            AIConfig(provider="openai", api_key="sk", temperature=-0.5)
            fail("Bug 1: temperature=-0.5 接受")
        except ValueError:
            ok("Bug 1: temperature=-0.5 ValueError")
        try:
            AIConfig(provider="openai", api_key="sk", temperature=2.5)
            fail("Bug 1b: temperature=2.5 接受")
        except ValueError:
            ok("Bug 1b: temperature=2.5 ValueError")

        # Bug 2: max_tokens 越界
        try:
            AIConfig(provider="openai", api_key="sk", max_tokens=0)
            fail("Bug 2: max_tokens=0 接受")
        except ValueError:
            ok("Bug 2: max_tokens=0 ValueError")
        try:
            AIConfig(provider="openai", api_key="sk", max_tokens=-1)
            fail("Bug 2b: max_tokens=-1 接受")
        except ValueError:
            ok("Bug 2b: max_tokens=-1 ValueError")
        try:
            AIConfig(provider="openai", api_key="sk", max_tokens=999999)
            fail("Bug 2c: max_tokens=999999 接受")
        except ValueError:
            ok("Bug 2c: max_tokens=999999 ValueError")

        # Bug 3: timeout 越界
        try:
            AIConfig(provider="openai", api_key="sk", timeout=0)
            fail("Bug 3: timeout=0 接受")
        except ValueError:
            ok("Bug 3: timeout=0 ValueError")
        try:
            AIConfig(provider="openai", api_key="sk", timeout=99999)
            fail("Bug 3b: timeout=99999 接受")
        except ValueError:
            ok("Bug 3b: timeout=99999 ValueError")

        # Bug 4: set_config 越界静默接受
        try:
            set_config("openai", api_key="sk", temperature=-1.0)
            fail("Bug 4: set_config temperature=-1.0 接受")
        except ValueError:
            ok("Bug 4: set_config temperature=-1.0 ValueError")
        try:
            set_config("openai", api_key="sk", max_tokens=0)
            fail("Bug 4b: set_config max_tokens=0 接受")
        except ValueError:
            ok("Bug 4b: set_config max_tokens=0 ValueError")

        # Bug 5: config 嵌套类型错误
        cfg_mod.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        cfg_mod.CONFIG_PATH.write_text('{"openai": "not a dict"}', encoding="utf-8")
        configs = cfg_mod.load_config()
        if isinstance(configs, dict):
            ok("Bug 5: 嵌套 str 不 crash")
        else:
            fail(f"Bug 5: {configs}")

        # Bug 6: bad JSON
        cfg_mod.CONFIG_PATH.write_text("not json", encoding="utf-8")
        try:
            cfg_mod.load_config()
            ok("Bug 6: 坏 JSON 不 crash")
        except Exception as e:
            fail(f"Bug 6: 坏 JSON 报 {e}")

        # Bug 7: complete(provider="nonexistent") strict
        set_config("openai", api_key="sk-test-1234")
        r = complete("s", "u", provider="nonexistent")
        if not r.ok and r.error_type == "unsupported":
            ok("Bug 7: complete(provider=nonexistent) → unsupported")
        else:
            fail(f"Bug 7: {r.to_dict()}")

        # Bug 7b: complete(provider="anthropic") 但 anthropic 没配
        r = complete("s", "u", provider="anthropic")
        if not r.ok and r.error_type == "not_configured":
            ok("Bug 7b: complete(provider=anthropic 没配) → not_configured")
        else:
            fail(f"Bug 7b: {r.to_dict()}")

        # Bug 8: 5 个命令 --provider bad 拒绝
        cfg_mod.reset_config(None)
        ws = tempfile.mkdtemp()
        s = Storage("p4reg", ws)
        s.create_task(s.workspace_id, "x.com")
        s.add_host(task_id=1, host="a.com", source="t")
        s.add_port(task_id=1, host="1.1.1.1", port=80)
        s.add_finding(task_id=1, target="1.1.1.1:80", finding_type="x", title="x", severity="info")
        from arl_lite.core.correlation_engine import run_all_rules, save_correlations
        hits = run_all_rules(s, str(Path(__file__).parent.parent) + "/arl_lite/modules/analysis/rules")
        save_correlations(s, hits)

        for name, fn, args in [
            ("ai ask", cmd_ai_ask, {"question": "x", "workspace": "p4reg", "provider": "bad"}),
            ("ai report", cmd_ai_report, {"workspace": "p4reg", "provider": "bad"}),
            ("ai suggest", cmd_ai_suggest, {"workspace": "p4reg", "provider": "bad"}),
            ("ai explain", cmd_ai_explain, {"corr_id": "1", "workspace": "p4reg", "provider": "bad"}),
            ("ai fix", cmd_ai_fix, {"finding_id": "1", "workspace": "p4reg", "provider": "bad"}),
        ]:
            args_obj = type("A", (), args)()
            rc = fn(args_obj, storage=s)
            if rc == 2:
                ok(f"Bug 8: {name} --provider bad → exit=2")
            else:
                fail(f"Bug 8: {name} --provider bad exit={rc}")

        # Bug 9: ai ask 空 question
        args = type("A", (), {"question": "", "workspace": "p4reg", "provider": None})()
        rc = cmd_ai_ask(args, storage=s)
        if rc == 2:
            ok("Bug 9: ai ask 空 question → exit=2")
        else:
            fail(f"Bug 9: ai ask 空 exit={rc}")

        # Bug 10: ai ask question 全部空白
        args = type("A", (), {"question": "   ", "workspace": "p4reg", "provider": None})()
        rc = cmd_ai_ask(args, storage=s)
        if rc == 2:
            ok("Bug 10: ai ask 空白 question → exit=2")
        else:
            fail(f"Bug 10: ai ask 空白 exit={rc}")

        # Bug 11: ai explain 负数
        args = type("A", (), {"corr_id": "-1", "workspace": "p4reg", "provider": None})()
        rc = cmd_ai_explain(args, storage=s)
        if rc == 2:
            ok("Bug 11: ai explain corr_id=-1 → exit=2")
        else:
            fail(f"Bug 11: ai explain -1 exit={rc}")

        # Bug 12: ai fix 0
        args = type("A", (), {"finding_id": "0", "workspace": "p4reg", "provider": None})()
        rc = cmd_ai_fix(args, storage=s)
        if rc == 2:
            ok("Bug 12: ai fix finding_id=0 → exit=2")
        else:
            fail(f"Bug 12: ai fix 0 exit={rc}")


# =========================
# runner
# =========================

def main():
    print("=" * 60)
    print("Phase 4 AI 集成测试")
    print("=" * 60)
    test_config()
    test_client()
    test_fallback()
    test_commands()
    test_cli()
    test_ai_path()
    test_ai_failure()
    test_bug_regression()
    print()
    print("=" * 60)
    print("\033[32mALL PHASE 4 TESTS PASSED\033[0m")
    print("=" * 60)


if __name__ == "__main__":
    main()
