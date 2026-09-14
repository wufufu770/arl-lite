"""arl_lite.ai.commands

5 个 AI 子命令实现:
- ai ask "<question>"   — 自然语言查询
- ai report [-w]        — 自动生成报告
- ai explain <corr_id>  — 解释一条关联
- ai suggest [-w]       — 建议下一步扫描
- ai fix <finding_id>   — 修复建议
- ai config <...>       — 配置管理

设计:
- 不破坏执行层(只读数据,不改 db)
- 无 AI 配置时用 fallback(模板化输出,不假装有 AI)
- 3 态返回:成功输出 / 部分成功(无 AI 时) / 失败
"""
from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

from .client import complete
from .config import (
    CONFIG_PATH,
    PROVIDERS,
    get_config,
    reset_config,
    set_config,
)
from .prompts import (
    ASK_SYSTEM,
    ASK_USER_TEMPLATE,
    EXPLAIN_SYSTEM,
    EXPLAIN_USER_TEMPLATE,
    FIX_SYSTEM,
    FIX_USER_TEMPLATE,
    REPORT_SYSTEM,
    REPORT_USER_TEMPLATE,
    SUGGEST_SYSTEM,
    SUGGEST_USER_TEMPLATE,
    fallback_ask,
    fallback_explain,
    fallback_fix,
    fallback_report,
    fallback_suggest,
    prepare_data_for_ask,
    prepare_data_for_report,
    prepare_data_for_suggest,
    to_json,
)

if TYPE_CHECKING:
    from ..db.storage import Storage

log = logging.getLogger("arl_lite.ai.commands")


# =========================
# ai ask
# =========================

def cmd_ai_ask(args, storage: "Storage | None" = None) -> int:
    """自然语言查询"""
    question = args.question
    if not question or not question.strip():
        print("[!] question must not be empty", file=sys.stderr)
        return 2

    provider_arg = getattr(args, "provider", None)
    # 显式指定 provider 时,先验证存在(避免 fallback)
    if provider_arg is not None:
        from .config import PROVIDERS
        if provider_arg not in PROVIDERS:
            print(f"[!] unknown provider: {provider_arg!r} (choose from {PROVIDERS})", file=sys.stderr)
            return 2

    if storage is None:
        from ..db.storage import Storage
        storage = Storage(workspace=args.workspace)

    data = prepare_data_for_ask(storage, question)
    data_json = to_json(data)

    cfg = get_config(provider_arg)
    if cfg is None or not cfg.is_configured():
        # fallback
        print(f"[i] AI not configured (run: arl-lite ai config set <provider> --api-key ...)")
        print(f"[i] using offline fallback:\n")
        print(fallback_ask(question, data))
        return 0

    result = complete(
        system=ASK_SYSTEM,
        user=ASK_USER_TEMPLATE.format(question=question, data_json=data_json),
        config=cfg,
    )

    if not result.ok:
        print(f"[!] AI call failed: {result.error_type}: {result.error}", file=sys.stderr)
        print(f"[i] using offline fallback:\n")
        print(fallback_ask(question, data))
        return 1

    print(f"[i] {result.provider}/{result.model} ({result.duration_ms}ms, "
          f"tokens: in={result.tokens_in} out={result.tokens_out})")
    print()
    print(result.content)
    return 0


# =========================
# ai report
# =========================

def cmd_ai_report(args, storage: "Storage | None" = None) -> int:
    """生成侦察报告"""
    provider_arg = getattr(args, "provider", None)
    if provider_arg is not None and provider_arg not in PROVIDERS:
        print(f"[!] unknown provider: {provider_arg!r} (choose from {PROVIDERS})", file=sys.stderr)
        return 2
    if storage is None:
        from ..db.storage import Storage
        storage = Storage(workspace=args.workspace)

    data = prepare_data_for_report(storage)
    data_json = to_json(data)

    cfg = get_config(getattr(args, "provider", None))
    if cfg is None or not cfg.is_configured():
        print(f"[i] AI not configured (run: arl-lite ai config set <provider> --api-key ...)")
        print(f"[i] using offline fallback:\n")
        print(fallback_report(data))
        return 0

    result = complete(
        system=REPORT_SYSTEM,
        user=REPORT_USER_TEMPLATE.format(data_json=data_json),
        config=cfg,
    )

    if not result.ok:
        print(f"[!] AI call failed: {result.error_type}: {result.error}", file=sys.stderr)
        print(f"[i] using offline fallback:\n")
        print(fallback_report(data))
        return 1

    print(f"# arl-lite 安全报告(workspace: {args.workspace})\n")
    print(f"> 由 {result.provider}/{result.model} 生成,{result.duration_ms}ms, "
          f"tokens: in={result.tokens_in} out={result.tokens_out}\n")
    print(result.content)
    return 0


# =========================
# ai explain
# =========================

def cmd_ai_explain(args, storage: "Storage | None" = None) -> int:
    """解释一条关联"""
    if not args.corr_id or not str(args.corr_id).isdigit() or int(args.corr_id) <= 0:
        print(f"[!] corr_id must be a positive integer (got {args.corr_id!r})", file=sys.stderr)
        return 2

    provider_arg = getattr(args, "provider", None)
    if provider_arg is not None and provider_arg not in PROVIDERS:
        print(f"[!] unknown provider: {provider_arg!r} (choose from {PROVIDERS})", file=sys.stderr)
        return 2

    if storage is None:
        from ..db.storage import Storage
        storage = Storage(workspace=args.workspace)

    corr_id = int(args.corr_id)
    rows = storage.query("correlations", limit=10000)
    corr = None
    for r in rows:
        if r.get("id") == corr_id:
            corr = r
            break
    if corr is None:
        # 找不到指定 ID:列出可用 ID 给提示
        available_ids = [r.get("id") for r in rows if r.get("id") is not None]
        available_ids.sort(reverse=True)
        if available_ids:
            print(f"[!] correlation #{corr_id} not found. available: {available_ids[:10]}", file=sys.stderr)
        else:
            print(f"[!] no correlations in workspace {args.workspace} (run arl-lite correlate first)", file=sys.stderr)
        return 2

    data_json = to_json({
        "rule_name": corr.get("rule_name"),
        "target": corr.get("target"),
        "target_type": corr.get("target_type"),
        "risk": corr.get("risk"),
        "headline": corr.get("headline"),
        "advice": corr.get("advice"),
        "tags": corr.get("tags"),
    })

    cfg = get_config(getattr(args, "provider", None))
    if cfg is None or not cfg.is_configured():
        print(f"[i] AI not configured")
        print(fallback_explain(corr))
        return 0

    result = complete(
        system=EXPLAIN_SYSTEM,
        user=EXPLAIN_USER_TEMPLATE.format(data_json=data_json),
        config=cfg,
    )

    if not result.ok:
        print(f"[!] AI call failed: {result.error_type}: {result.error}", file=sys.stderr)
        print(fallback_explain(corr))
        return 1

    print(f"# 关联 #{corr_id}: {corr.get('rule_name')} (risk={corr.get('risk')})\n")
    print(f"> {corr.get('headline', '')}\n")
    print(result.content)
    return 0


# =========================
# ai suggest
# =========================

def cmd_ai_suggest(args, storage: "Storage | None" = None) -> int:
    """建议下一步扫描"""
    provider_arg = getattr(args, "provider", None)
    if provider_arg is not None and provider_arg not in PROVIDERS:
        print(f"[!] unknown provider: {provider_arg!r} (choose from {PROVIDERS})", file=sys.stderr)
        return 2
    if storage is None:
        from ..db.storage import Storage
        storage = Storage(workspace=args.workspace)

    data = prepare_data_for_suggest(storage)
    data_json = to_json(data)

    cfg = get_config(getattr(args, "provider", None))
    if cfg is None or not cfg.is_configured():
        print(f"[i] AI not configured")
        print(fallback_suggest(data))
        return 0

    result = complete(
        system=SUGGEST_SYSTEM,
        user=SUGGEST_USER_TEMPLATE.format(data_json=data_json),
        config=cfg,
    )

    if not result.ok:
        print(f"[!] AI call failed: {result.error_type}: {result.error}", file=sys.stderr)
        print(fallback_suggest(data))
        return 1

    print(f"# 扫描建议 (workspace: {args.workspace})\n")
    print(f"> 由 {result.provider}/{result.model} 生成\n")
    print(result.content)
    return 0


# =========================
# ai fix
# =========================

def cmd_ai_fix(args, storage: "Storage | None" = None) -> int:
    """修复建议"""
    if not args.finding_id or not str(args.finding_id).isdigit() or int(args.finding_id) <= 0:
        print(f"[!] finding_id must be a positive integer (got {args.finding_id!r})", file=sys.stderr)
        return 2

    provider_arg = getattr(args, "provider", None)
    if provider_arg is not None and provider_arg not in PROVIDERS:
        print(f"[!] unknown provider: {provider_arg!r} (choose from {PROVIDERS})", file=sys.stderr)
        return 2

    if storage is None:
        from ..db.storage import Storage
        storage = Storage(workspace=args.workspace)

    finding_id = int(args.finding_id)
    rows = storage.query("findings", limit=10000)
    finding = None
    for r in rows:
        if r.get("id") == finding_id:
            finding = r
            break
    if finding is None:
        print(f"[!] finding #{finding_id} not found in workspace {args.workspace}", file=sys.stderr)
        return 2

    data_json = to_json({
        "finding_type": finding.get("finding_type"),
        "title": finding.get("title"),
        "target": finding.get("target"),
        "target_type": finding.get("target_type"),
        "severity": finding.get("severity"),
        "description": finding.get("description"),
        "evidence": finding.get("evidence"),
        "cve": finding.get("cve"),
    })

    cfg = get_config(getattr(args, "provider", None))
    if cfg is None or not cfg.is_configured():
        print(f"[i] AI not configured")
        print(fallback_fix(finding))
        return 0

    result = complete(
        system=FIX_SYSTEM,
        user=FIX_USER_TEMPLATE.format(data_json=data_json),
        config=cfg,
    )

    if not result.ok:
        print(f"[!] AI call failed: {result.error_type}: {result.error}", file=sys.stderr)
        print(fallback_fix(finding))
        return 1

    print(f"# Finding #{finding_id}: {finding.get('title')} ({finding.get('severity')})\n")
    print(f"> 目标:{finding.get('target')}\n")
    print(result.content)
    return 0


# =========================
# ai config
# =========================

def cmd_ai_config_set(args) -> int:
    """设置 provider 配置"""
    if args.provider not in PROVIDERS:
        print(f"[!] provider must be one of {PROVIDERS}", file=sys.stderr)
        return 2

    kwargs: dict = {}
    if args.api_key:
        kwargs["api_key"] = args.api_key
    if args.base_url:
        kwargs["base_url"] = args.base_url
    if args.model:
        kwargs["model"] = args.model
    if args.temperature is not None:
        kwargs["temperature"] = args.temperature
    if args.max_tokens is not None:
        kwargs["max_tokens"] = args.max_tokens

    if not kwargs:
        print("[!] at least one option required: --api-key / --base-url / --model / --temperature / --max-tokens",
              file=sys.stderr)
        return 2

    try:
        cfg = set_config(args.provider, **kwargs)
    except ValueError as e:
        print(f"[!] {e}", file=sys.stderr)
        return 2

    print(f"[+] {args.provider} config updated:")
    print(f"  base_url: {cfg.base_url}")
    print(f"  model:    {cfg.model}")
    print(f"  temp:     {cfg.temperature}")
    print(f"  max_tok:  {cfg.max_tokens}")
    if cfg.api_key:
        print(f"  api_key:  {cfg.api_key[:6]}***{cfg.api_key[-4:] if len(cfg.api_key) > 10 else ''}  (hidden)")
    print(f"[+] saved to {CONFIG_PATH}")
    return 0


def cmd_ai_config_get(args) -> int:
    """获取 provider 配置"""
    from .config import load_config
    configs = load_config()
    if args.provider:
        if args.provider not in PROVIDERS:
            print(f"[!] provider must be one of {PROVIDERS}", file=sys.stderr)
            return 2
        providers = [args.provider]
    else:
        providers = list(PROVIDERS)

    for p in providers:
        cfg = configs.get(p)
        if cfg is None:
            print(f"  {p}: <not configured>")
            continue
        configured = "✓" if cfg.is_configured() else "✗"
        print(f"  {p}: [{configured}]")
        print(f"    base_url: {cfg.base_url}")
        print(f"    model:    {cfg.model}")
        print(f"    temp:     {cfg.temperature}")
        print(f"    max_tok:  {cfg.max_tokens}")
        if cfg.api_key:
            masked = f"{cfg.api_key[:6]}***{cfg.api_key[-4:]}" if len(cfg.api_key) > 10 else "***"
            print(f"    api_key:  {masked}")
    return 0


def cmd_ai_config_list(args) -> int:
    """列出所有 provider + 当前活动 provider"""
    from .config import load_config
    configs = load_config()
    print(f"[i] config file: {CONFIG_PATH}")
    print(f"[i] supported providers: {', '.join(PROVIDERS)}")
    print()
    active = get_config()
    active_name = active.provider if active else "none (will use offline fallback)"
    print(f"[i] currently active: {active_name}\n")

    # 显示所有 4 个 provider(已配置的 + 未配置的)
    for p in PROVIDERS:
        cfg = configs.get(p)
        mark = "★" if active and p == active.provider else " "
        if cfg is None:
            print(f"  {mark} {p}: <not set>")
        else:
            configured = "ready" if cfg.is_configured() else "missing api_key"
            print(f"  {mark} {p}: {configured}  (model={cfg.model})")
    return 0


def cmd_ai_config_reset(args) -> int:
    """重置 provider 配置"""
    if args.provider:
        if args.provider not in PROVIDERS:
            print(f"[!] provider must be one of {PROVIDERS}", file=sys.stderr)
            return 2
        reset_config(args.provider)
        print(f"[+] {args.provider} config reset")
    else:
        reset_config(None)
        print(f"[+] all AI config reset (deleted {CONFIG_PATH})")
    return 0


def cmd_ai_config_test(args) -> int:
    """测试当前 provider 是否可用"""
    cfg = get_config(getattr(args, "provider", None))
    if cfg is None:
        print("[!] no config found", file=sys.stderr)
        return 2
    if not cfg.is_configured():
        print(f"[!] {cfg.provider} not configured (missing api key)", file=sys.stderr)
        return 2

    print(f"[i] testing {cfg.provider}/{cfg.model} @ {cfg.base_url} ...")
    result = complete(
        system="You are a test assistant. Reply with: OK",
        user="test",
        config=cfg,
    )
    if result.ok:
        print(f"[+] {cfg.provider}/{cfg.model} OK ({result.duration_ms}ms, "
              f"tokens: in={result.tokens_in} out={result.tokens_out})")
        print(f"[+] response: {result.content[:100]}")
        return 0
    else:
        print(f"[!] {cfg.provider}/{cfg.model} FAILED: {result.error_type}: {result.error}")
        return 1
