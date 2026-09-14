"""arl_lite.ai.client

Mini LLM client — 零 pip 依赖,纯 stdlib (urllib + json + ssl)。

支持 4 个 provider(每个有各自的 chat completion 协议):
- OpenAI:POST {base_url}/chat/completions
- Anthropic:POST {base_url}/messages  + x-api-key + anthropic-version header
- Google:POST {base_url}/models/{model}:generateContent?key={api_key}
- Ollama:POST {base_url}/api/chat

设计:
- 不引入新 pip 依赖(urllib/SSL/socket 全 stdlib)
- 3 态返回:{ok, content, error}
- 60s timeout,失败不重试(用户主动)
- 不做 stream(单次 completion)
"""
from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import AIConfig, PROVIDERS, get_config

log = logging.getLogger("arl_lite.ai.client")


@dataclass
class CompletionResult:
    """LLM completion 返回 — 3 态"""
    ok: bool
    content: str = ""       # 生成的文本
    model: str = ""         # 实际用的 model
    provider: str = ""      # 实际用的 provider
    tokens_in: int = 0      # input tokens
    tokens_out: int = 0     # output tokens
    duration_ms: int = 0    # 耗时
    error: str = ""         # 错误信息
    error_type: str = ""    # network/auth/rate_limit/timeout/bad_request

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "content": self.content,
            "model": self.model,
            "provider": self.provider,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "error_type": self.error_type,
        }


# LLM 响应几 MB 封顶;无上限 read() 配合被劫持/恶意的 base_url 就是远程 OOM
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024


def _post_json(url: str, body: dict, headers: dict, timeout: int) -> dict:
    """POST JSON,返回 dict。失败抛 AIError"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            text = resp.read(_MAX_RESPONSE_BYTES).decode("utf-8", errors="replace")
            return json.loads(text)
    except urllib.error.HTTPError as e:
        # 读 body(可能 JSON 错误)
        try:
            err_body = e.read(_MAX_RESPONSE_BYTES).decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        if e.code == 401 or e.code == 403:
            raise AIAuthError(f"auth failed (HTTP {e.code}): {err_body[:200]}", status=e.code)
        if e.code == 429:
            raise AIRateLimitError(f"rate limited (HTTP 429): {err_body[:200]}", status=e.code)
        if e.code >= 500:
            raise AIServerError(f"server error (HTTP {e.code}): {err_body[:200]}", status=e.code)
        raise AIBadRequestError(f"HTTP {e.code}: {err_body[:200]}", status=e.code)
    except urllib.error.URLError as e:
        raise AINetworkError(f"network error: {e.reason}")
    except TimeoutError as e:
        raise AITimeoutError(f"timeout: {e}")
    except json.JSONDecodeError as e:
        raise AIBadRequestError(f"invalid JSON response: {e}")


# =========================
# Provider-specific completion
# =========================

def _complete_openai(cfg: AIConfig, system: str, user: str) -> CompletionResult:
    """OpenAI chat completion"""
    url = f"{cfg.base_url.rstrip('/')}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg.api_key}",
    }
    body = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
    }
    start = time.time()
    try:
        resp = _post_json(url, body, headers, cfg.timeout)
    except AIError as e:
        return CompletionResult(
            ok=False, provider=cfg.provider, model=cfg.model,
            error=str(e), error_type=e.kind,
            duration_ms=int((time.time() - start) * 1000),
        )
    duration = int((time.time() - start) * 1000)

    try:
        choice = resp["choices"][0]
        content = choice["message"]["content"]
        if not isinstance(content, str):
            # 部分网关回 content:null / dict,当成功会打印字面量 None
            raise TypeError(f"content is {type(content).__name__}, expected str")
    except (KeyError, IndexError, TypeError) as e:
        return CompletionResult(
            ok=False, provider=cfg.provider, model=cfg.model,
            error=f"unexpected response shape: {e}", error_type="bad_response",
            duration_ms=duration,
        )

    usage = resp.get("usage") or {}  # null/[] 在部分网关上真实出现
    return CompletionResult(
        ok=True, content=content, provider=cfg.provider, model=cfg.model,
        tokens_in=usage.get("prompt_tokens", 0) if isinstance(usage, dict) else 0,
        tokens_out=usage.get("completion_tokens", 0) if isinstance(usage, dict) else 0,
        duration_ms=duration,
    )


def _complete_anthropic(cfg: AIConfig, system: str, user: str) -> CompletionResult:
    """Anthropic messages API"""
    url = f"{cfg.base_url.rstrip('/')}/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": cfg.api_key,
        "anthropic-version": "2023-06-01",
    }
    body = {
        "model": cfg.model,
        "max_tokens": cfg.max_tokens,
        "temperature": cfg.temperature,
        "system": system,
        "messages": [
            {"role": "user", "content": user},
        ],
    }
    start = time.time()
    try:
        resp = _post_json(url, body, headers, cfg.timeout)
    except AIError as e:
        return CompletionResult(
            ok=False, provider=cfg.provider, model=cfg.model,
            error=str(e), error_type=e.kind,
            duration_ms=int((time.time() - start) * 1000),
        )
    duration = int((time.time() - start) * 1000)

    try:
        content = resp["content"][0]["text"]
        if not isinstance(content, str):
            raise TypeError(f"content is {type(content).__name__}, expected str")
    except (KeyError, IndexError, TypeError) as e:
        return CompletionResult(
            ok=False, provider=cfg.provider, model=cfg.model,
            error=f"unexpected response shape: {e}", error_type="bad_response",
            duration_ms=duration,
        )

    usage = resp.get("usage") or {}
    return CompletionResult(
        ok=True, content=content, provider=cfg.provider, model=cfg.model,
        tokens_in=usage.get("input_tokens", 0) if isinstance(usage, dict) else 0,
        tokens_out=usage.get("output_tokens", 0) if isinstance(usage, dict) else 0,
        duration_ms=duration,
    )


def _complete_google(cfg: AIConfig, system: str, user: str) -> CompletionResult:
    """Google Gemini generateContent API(key 走 header,不进 URL)"""
    url = f"{cfg.base_url.rstrip('/')}/models/{cfg.model}:generateContent"
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": cfg.api_key,
    }
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": cfg.temperature,
            "maxOutputTokens": cfg.max_tokens,
        },
    }
    start = time.time()
    try:
        resp = _post_json(url, body, headers, cfg.timeout)
    except AIError as e:
        return CompletionResult(
            ok=False, provider=cfg.provider, model=cfg.model,
            error=str(e), error_type=e.kind,
            duration_ms=int((time.time() - start) * 1000),
        )
    duration = int((time.time() - start) * 1000)

    try:
        content = resp["candidates"][0]["content"]["parts"][0]["text"]
        if not isinstance(content, str):
            raise TypeError(f"content is {type(content).__name__}, expected str")
    except (KeyError, IndexError, TypeError) as e:
        return CompletionResult(
            ok=False, provider=cfg.provider, model=cfg.model,
            error=f"unexpected response shape: {e}", error_type="bad_response",
            duration_ms=duration,
        )

    usage = resp.get("usageMetadata") or {}
    return CompletionResult(
        ok=True, content=content, provider=cfg.provider, model=cfg.model,
        tokens_in=usage.get("promptTokenCount", 0) if isinstance(usage, dict) else 0,
        tokens_out=usage.get("candidatesTokenCount", 0) if isinstance(usage, dict) else 0,
        duration_ms=duration,
    )


def _complete_ollama(cfg: AIConfig, system: str, user: str) -> CompletionResult:
    """Ollama /api/chat"""
    url = f"{cfg.base_url.rstrip('/')}/api/chat"
    headers = {"Content-Type": "application/json"}
    body = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {
            "temperature": cfg.temperature,
            "num_predict": cfg.max_tokens,
        },
    }
    start = time.time()
    try:
        resp = _post_json(url, body, headers, cfg.timeout)
    except AIError as e:
        return CompletionResult(
            ok=False, provider=cfg.provider, model=cfg.model,
            error=str(e), error_type=e.kind,
            duration_ms=int((time.time() - start) * 1000),
        )
    duration = int((time.time() - start) * 1000)

    try:
        content = resp["message"]["content"]
        if not isinstance(content, str):
            raise TypeError(f"content is {type(content).__name__}, expected str")
    except (KeyError, TypeError) as e:
        return CompletionResult(
            ok=False, provider=cfg.provider, model=cfg.model,
            error=f"unexpected response shape: {e}", error_type="bad_response",
            duration_ms=duration,
        )

    # Ollama 报 token 计数在 prompt_eval_count / eval_count
    return CompletionResult(
        ok=True, content=content, provider=cfg.provider, model=cfg.model,
        tokens_in=resp.get("prompt_eval_count", 0),
        tokens_out=resp.get("eval_count", 0),
        duration_ms=duration,
    )


# =========================
# Exceptions
# =========================

class AIError(Exception):
    """AI 错误基类"""
    kind: str = "error"

    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


class AINetworkError(AIError):
    kind = "network"


class AITimeoutError(AIError):
    kind = "timeout"


class AIRateLimitError(AIError):
    kind = "rate_limit"


class AIAuthError(AIError):
    kind = "auth"


class AIServerError(AIError):
    kind = "server"


class AIBadRequestError(AIError):
    kind = "bad_request"


# =========================
# Public API
# =========================

_PROVIDERS = {
    "openai": _complete_openai,
    "anthropic": _complete_anthropic,
    "google": _complete_google,
    "ollama": _complete_ollama,
}


def complete(
    system: str,
    user: str,
    config: AIConfig | None = None,
    provider: str | None = None,
) -> CompletionResult:
    """调用 LLM completion。

    Args:
        system: system prompt
        user: user prompt
        config: 直接给 AIConfig(覆盖默认查找)
        provider: 强制 provider name(必须存在,不会 fallback)
    """
    if config is None:
        # 严格模式:如果 provider 指定,必须显式存在
        if provider is not None:
            if provider not in PROVIDERS:
                return CompletionResult(
                    ok=False, error=f"unknown provider: {provider}", error_type="unsupported"
                )
            cfg = get_config(provider, strict=True)
            if cfg is None:
                return CompletionResult(
                    ok=False, provider=provider,
                    error=f"provider '{provider}' not configured", error_type="not_configured"
                )
            config = cfg
        else:
            config = get_config(None)
    if config is None:
        return CompletionResult(
            ok=False, error="no AI config found", error_type="not_configured"
        )
    if not config.is_configured():
        return CompletionResult(
            ok=False, provider=config.provider, model=config.model,
            error=f"provider '{config.provider}' not configured (missing API key)",
            error_type="not_configured",
        )

    fn = _PROVIDERS.get(config.provider)
    if fn is None:
        return CompletionResult(
            ok=False, provider=config.provider, model=config.model,
            error=f"unsupported provider: {config.provider}",
            error_type="unsupported",
        )

    log.debug(f"calling {config.provider}/{config.model} (timeout={config.timeout}s)")
    return fn(config, system, user)


def is_configured(provider: str | None = None) -> bool:
    """检查是否已配置(可调用)"""
    cfg = get_config(provider)
    return cfg is not None and cfg.is_configured()
