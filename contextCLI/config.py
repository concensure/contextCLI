from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .utils import _ENV_NAME_RE
from .storage import _config_path, _backup_file, _atomic_write_text, _backups_dir

SUPPORTED_PROVIDERS = {
    "openai_compatible",
    "together",
    "openrouter",
    "cerebras",
    "anthropic",
    "gemini",
    "ollama",
    "ollama_local",
}

_PROVIDER_DEFAULTS = {
    "openai_compatible": {
        "api_base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
    },
    "together": {
        "api_base_url": "https://api.together.xyz/v1",
        "api_key_env": "TOGETHER_API_KEY",
    },
    "openrouter": {
        "api_base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
    },
    "cerebras": {
        "api_base_url": "https://api.cerebras.ai/v1",
        "api_key_env": "CEREBRAS_API_KEY",
    },
    "anthropic": {
        "api_base_url": "https://api.anthropic.com/v1",
        "api_key_env": "ANTHROPIC_API_KEY",
    },
    "gemini": {
        "api_base_url": "https://generativelanguage.googleapis.com/v1beta/models",
        "api_key_env": "GEMINI_API_KEY",
    },
    "ollama": {
        "api_base_url": "",
        "api_key_env": "",
    },
    "ollama_local": {
        "api_base_url": "",
        "api_key_env": "",
    },
}

@dataclass(frozen=True)
class Config:
    max_pointer_lines: int = 200
    compaction_every_n_turns: int = 10
    summarizer_model: str = "claude-3-haiku"
    enable_pointers: bool = False
    load_env_file: bool = True
    capture_git_patch: bool = False
    redact_event_secrets: bool = True
    max_events_bytes: int = 5_000_000
    max_backup_files: int = 50

    api_provider: str = "openai_compatible"
    api_base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"

def _provider_default(name: str, field: str) -> str:
    return str(_PROVIDER_DEFAULTS.get(name, {}).get(field, ""))

def _default_config(enable_pointers: bool) -> Config:
    return Config(enable_pointers=enable_pointers)

def _with_overrides(cfg: Config, overrides: dict[str, Any]) -> Config:
    allowed = set(Config.__dataclass_fields__.keys())
    clean = {k: v for k, v in overrides.items() if k in allowed and v is not None}
    if "api_provider" in clean:
        clean["api_provider"] = str(clean["api_provider"]).strip().lower()
        if clean["api_provider"] not in SUPPORTED_PROVIDERS:
            allowed_providers = ", ".join(sorted(SUPPORTED_PROVIDERS))
            raise SystemExit(f"Unsupported provider `{clean['api_provider']}`. Use one of: {allowed_providers}")
        defaults = _PROVIDER_DEFAULTS[clean["api_provider"]]
        if "api_base_url" not in clean and cfg.api_base_url == _PROVIDER_DEFAULTS[cfg.api_provider]["api_base_url"]:
            clean["api_base_url"] = defaults["api_base_url"]
        if "api_key_env" not in clean and cfg.api_key_env == _PROVIDER_DEFAULTS[cfg.api_provider]["api_key_env"]:
            clean["api_key_env"] = defaults["api_key_env"]
    if "api_key_env" in clean:
        clean["api_key_env"] = str(clean["api_key_env"]).strip()
        if clean["api_key_env"] and not _ENV_NAME_RE.match(clean["api_key_env"]):
            raise SystemExit("api_key_env must be a valid environment variable name, for example OPENROUTER_API_KEY.")
    return replace(cfg, **clean)

def _render_config_toml(cfg: Config) -> str:
    return (
        "\n".join(
            [
                "# contextCLI configuration",
                f"max_pointer_lines = {cfg.max_pointer_lines}",
                f"compaction_every_n_turns = {cfg.compaction_every_n_turns}",
                f"max_events_bytes = {cfg.max_events_bytes}",
                f"max_backup_files = {cfg.max_backup_files}",
                f"load_env_file = {str(cfg.load_env_file).lower()}",
                f"capture_git_patch = {str(cfg.capture_git_patch).lower()}",
                f"redact_event_secrets = {str(cfg.redact_event_secrets).lower()}",
                "",
                "# When false, events are still recorded but pointer/state reflection is skipped.",
                f"enable_pointers = {str(cfg.enable_pointers).lower()}",
                "",
                '# Cheap summarizer model name (interpreted by your provider).',
                f"summarizer_model = \"{cfg.summarizer_model}\"",
                "",
                "# cheap model access (OpenAI-compatible Chat Completions)",
                f"api_provider = \"{cfg.api_provider}\"",
                f"api_base_url = \"{cfg.api_base_url}\"",
                f"api_key_env = \"{cfg.api_key_env}\"",
                "",
            ]
        )
        + "\n"
    )

def write_config(repo: Path, cfg: Config) -> None:
    p = _config_path(repo)
    if p.exists():
        _backup_file(p, _backups_dir(repo))
    _atomic_write_text(p, _render_config_toml(cfg))

def update_config(repo: Path, updates: dict[str, Any]) -> Config:
    cfg = _with_overrides(load_config(repo), updates)
    write_config(repo, cfg)
    return cfg

def load_config(repo: Path) -> Config:
    import tomllib

    data: dict[str, Any] = {}
    p = _config_path(repo)
    if p.exists():
        data = tomllib.loads(p.read_text(encoding="utf-8"))

    if "max_pointer_lines" not in data and "max_pointers" in data:
        data["max_pointer_lines"] = data.get("max_pointers")
    if "compaction_every_n_turns" not in data and "compaction_frequency" in data:
        data["compaction_every_n_turns"] = data.get("compaction_frequency")
    if "summarizer_model" not in data and "model_reflection" in data:
        data["summarizer_model"] = data.get("model_reflection")

    base = _default_config(enable_pointers=bool(data.get("enable_pointers", False)))
    return Config(
        max_pointer_lines=int(data.get("max_pointer_lines", base.max_pointer_lines)),
        compaction_every_n_turns=int(data.get("compaction_every_n_turns", base.compaction_every_n_turns)),
        summarizer_model=str(data.get("summarizer_model", base.summarizer_model)),
        enable_pointers=bool(data.get("enable_pointers", base.enable_pointers)),
        load_env_file=bool(data.get("load_env_file", base.load_env_file)),
        capture_git_patch=bool(data.get("capture_git_patch", base.capture_git_patch)),
        redact_event_secrets=bool(data.get("redact_event_secrets", base.redact_event_secrets)),
        max_events_bytes=int(data.get("max_events_bytes", base.max_events_bytes)),
        max_backup_files=int(data.get("max_backup_files", base.max_backup_files)),
        api_provider=str(data.get("api_provider", base.api_provider)),
        api_base_url=str(data.get("api_base_url", base.api_base_url)),
        api_key_env=str(data.get("api_key_env", base.api_key_env)),
    )
