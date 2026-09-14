"""arl_lite.ai.config

AI provider / API key 配置管理。

- 配置文件:`~/.arl-lite/config.json`(mode 0600,只 owner 可读写)
- 支持 4 个 provider:openai / anthropic / google / ollama
- 4 个子命令:ai config set / get / list / reset
- 兼容环境变量(优先级:env > config)
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("arl_lite.ai.config")

CONFIG_PATH = Path.home() / ".arl-lite" / "config.json"

# 支持的 provider
PROVIDERS = ("openai", "anthropic", "google", "ollama")

# 默认 base URL(可被 config 覆盖)
DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta",
    "ollama": "http://127.0.0.1:11434",
}

# 默认 model
DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-sonnet-20241022",
    "google": "gemini-1.5-flash",
    "ollama": "qwen2.5:7b",
}


@dataclass
class AIConfig:
    """AI 配置(单一 provider 的设置)"""
    provider: str  # openai/anthropic/google/ollama
    api_key: str = ""  # ollama 不用
    base_url: str = ""
    model: str = ""
    temperature: float = 0.3
    max_tokens: int = 1024
    timeout: int = 60  # 秒
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.provider not in PROVIDERS:
            raise ValueError(f"unknown provider: {self.provider} (choose from {PROVIDERS})")
        if not self.base_url:
            self.base_url = DEFAULT_BASE_URLS[self.provider]
        if not self.model:
            self.model = DEFAULT_MODELS[self.provider]
        self._validate_base_url()
        # 校验数值范围
        # OpenAI spec 0-2,Anthropic 0-1;宽容到 0-2
        if not (0.0 <= self.temperature <= 2.0):
            raise ValueError(f"temperature must be 0..2 (got {self.temperature})")
        # max_tokens 各家不同(4o=16384,claude=8192,gemini=8192),宽容到 1-200000
        if not (1 <= self.max_tokens <= 200000):
            raise ValueError(f"max_tokens must be 1..200000 (got {self.max_tokens})")
        if self.timeout < 1 or self.timeout > 3600:
            raise ValueError(f"timeout must be 1..3600 (got {self.timeout})")

    def _validate_base_url(self) -> None:
        """base_url 校验:scheme 白名单,拒绝内嵌凭据。

        api_key 会被放进发往 base_url 的请求(Authorization 头 / key 参数),
        一个被诱导设置的 base_url 就能把 key 整体外带,所以至少挡住
        明显不合法的值;非本机 http 明文发 key 打 warning。
        """
        from urllib.parse import urlparse
        parsed = urlparse(self.base_url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"base_url scheme must be http(s): {self.base_url!r}")
        if not parsed.hostname:
            raise ValueError(f"base_url must include a host: {self.base_url!r}")
        if parsed.username or parsed.password:
            raise ValueError("base_url must not embed credentials")
        host = parsed.hostname.lower()
        local = host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(".local")
        if parsed.scheme == "http" and not local and self.api_key:
            log.warning(
                f"provider '{self.provider}': api_key will be sent over plain http "
                f"to {host} (non-local); use https unless this is intentional"
            )

    def is_configured(self) -> bool:
        """是否已配置(ollama 不需要 key)

        api_key 必须:
        - 非空字符串
        - 非纯空白
        - 长度 >= 8(避免 'sk-xx' 之类占位)
        - 不能包含 'your-' 'placeholder' 'xxx' 之类
        """
        if self.provider == "ollama":
            return True
        if not isinstance(self.api_key, str):
            return False
        key = self.api_key.strip()
        if len(key) < 8:
            return False
        low = key.lower()
        if any(bad in low for bad in ("your-", "placeholder", "xxx", "changeme")):
            return False
        return True

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "api_key": self.api_key,
            "base_url": self.base_url,
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout": self.timeout,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AIConfig":
        return cls(
            provider=d.get("provider", "openai"),
            api_key=d.get("api_key", ""),
            base_url=d.get("base_url", ""),
            model=d.get("model", ""),
            temperature=d.get("temperature", 0.3),
            max_tokens=d.get("max_tokens", 1024),
            timeout=d.get("timeout", 60),
        )


def _ensure_config_dir() -> Path:
    """确保 config 目录存在,mode 0700"""
    config_dir = CONFIG_PATH.parent
    config_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(config_dir, 0o700)
    except OSError:
        pass
    return config_dir


def load_config() -> dict[str, AIConfig]:
    """加载所有 provider 配置

    返回: { provider_name: AIConfig }
    优先级:env var > config file
    行为:只返回 file/env 显式配置过的 provider,未配置的不出现
    """
    configs: dict[str, AIConfig] = {}
    file_data: dict = {}

    # 1. 读 config file
    if CONFIG_PATH.exists():
        try:
            file_data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            # 必须是 dict(不是 list / str / 数字)
            if not isinstance(file_data, dict):
                log.warning(f"config root must be dict (got {type(file_data).__name__}), using empty")
                file_data = {}
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            log.warning(f"failed to read config {CONFIG_PATH}: {e}")
            file_data = {}

    # 2. 收集 file 中显式配置的 provider
    configured = set(file_data.keys())

    # 3. 也检查 env
    for provider in PROVIDERS:
        if os.environ.get(f"ARL_AI_{provider.upper()}_API_KEY"):
            configured.add(provider)
        if os.environ.get(f"ARL_AI_{provider.upper()}_BASE_URL"):
            configured.add(provider)
        if os.environ.get(f"ARL_AI_{provider.upper()}_MODEL"):
            configured.add(provider)

    # 4. 为每个显式配置的 provider 构造 config
    for provider in configured:
        if provider not in PROVIDERS:
            continue
        provider_data = file_data.get(provider, {})
        # 必须是 dict(防止 file 里是 string/list)
        if not isinstance(provider_data, dict):
            log.warning(f"config for {provider} must be dict (got {type(provider_data).__name__}), using empty")
            provider_data = {}
        # env 覆盖
        env_key = os.environ.get(f"ARL_AI_{provider.upper()}_API_KEY", "")
        env_base = os.environ.get(f"ARL_AI_{provider.upper()}_BASE_URL", "")
        env_model = os.environ.get(f"ARL_AI_{provider.upper()}_MODEL", "")

        if env_key:
            provider_data["api_key"] = env_key
        if env_base:
            provider_data["base_url"] = env_base
        if env_model:
            provider_data["model"] = env_model

        provider_data["provider"] = provider
        try:
            configs[provider] = AIConfig.from_dict(provider_data)
        except (ValueError, TypeError) as e:
            log.warning(f"skip invalid config for {provider}: {e}")

    return configs


def save_config(configs: dict[str, AIConfig]) -> None:
    """保存所有 provider 配置(覆盖)

    文件 mode 0600(只有 owner 可读写)。用 O_CREAT|0600 直接建文件,
    避免"先 0644 落盘再 chmod"窗口期内 key 被其他本地用户读到。
    """
    _ensure_config_dir()
    data = {p: cfg.to_dict() for p, cfg in configs.items()}
    payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    log.info(f"config saved: {CONFIG_PATH}")


def get_config(provider: str | None = None, *, strict: bool = False) -> AIConfig | None:
    """获取单个 provider 配置

    优先级:ARL_AI_PROVIDER env > provider 参数 > 第一个已配置的
    返回 None 表示没配置(调用方应 fallback)

    Args:
        provider: 指定 provider name
        strict: 严格模式 — provider 不存在时返回 None,不 fallback
    """
    configs = load_config()
    if provider is None:
        provider = os.environ.get("ARL_AI_PROVIDER", "")
    if provider and provider in configs:
        return configs[provider]
    # provider 显式但不存在
    if provider and strict:
        return None
    # provider 显式但不存在,非 strict — fallback
    if provider:
        if provider == "ollama":
            # ollama 不需要 key,零配置也应可用(走默认 base_url),
            # 否则用户会看到误导性的 "AI not configured" 而非真实连接错误
            return configs.get("ollama") or AIConfig(provider="ollama")
        # 有 provider 但没 config,看 configs 里有没有已配的
        for cfg in configs.values():
            if cfg.is_configured():
                return cfg
        return None
    # 没 provider — fallback 到第一个已配置的
    for cfg in configs.values():
        if cfg.is_configured():
            return cfg
    return None


def set_config(provider: str, **kwargs: Any) -> AIConfig:
    """设置单个 provider 配置

    用法:set_config("openai", api_key="sk-xxx", model="gpt-4o")
    """
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    configs = load_config()
    existing = configs.get(provider, AIConfig(provider=provider))
    # 覆盖字段
    for k, v in kwargs.items():
        if hasattr(existing, k):
            setattr(existing, k, v)
    # 显式重新校验(因为 setattr 绕过 __post_init__)
    # base_url 也要补验:api_key 会发往 base_url,被诱导设置的值等于 key 外带
    if not (0.0 <= existing.temperature <= 2.0):
        raise ValueError(f"temperature must be 0..2 (got {existing.temperature})")
    if not (1 <= existing.max_tokens <= 200000):
        raise ValueError(f"max_tokens must be 1..200000 (got {existing.max_tokens})")
    if existing.timeout < 1 or existing.timeout > 3600:
        raise ValueError(f"timeout must be 1..3600 (got {existing.timeout})")
    existing._validate_base_url()
    configs[provider] = existing
    save_config(configs)
    return existing


def reset_config(provider: str | None = None) -> None:
    """重置配置(provider 或全部)"""
    if provider:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            data.pop(provider, None)
            fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"))
            finally:
                os.close(fd)
    else:
        if CONFIG_PATH.exists():
            CONFIG_PATH.unlink()
