from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .summarizer import cheap_reflect

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

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
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


def _provider_default(name: str, field: str) -> str:
    return str(_PROVIDER_DEFAULTS.get(name, {}).get(field, ""))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def load_dotenv(repo: Path, allowed_keys: set[str]) -> None:
    """
    Minimal `.env` loader (stdlib-only).
    Loads only explicitly allowed KEY=VALUE lines from `<repo>/.env`.
    """
    if not allowed_keys:
        return
    env_path = repo / ".env"
    if not env_path.exists():
        return
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'").strip('"')
            if not k or k not in allowed_keys:
                continue
            os.environ.setdefault(k, v)
    except OSError:
        return

class _Lock:
    def __init__(self, path: Path, stale_after_s: int = 600) -> None:
        self.path = path
        self.stale_after_s = stale_after_s

    def __enter__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        # If a previous lock was left behind, clear it after a grace period.
        if self.path.exists():
            try:
                age = now - self.path.stat().st_mtime
                if age > self.stale_after_s:
                    self.path.unlink(missing_ok=True)  # py3.11+
            except OSError:
                pass
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(
                    fd,
                    (
                        json.dumps(
                            {"pid": os.getpid(), "created_at": _utc_now_iso()},
                            sort_keys=True,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
            finally:
                os.close(fd)
        except FileExistsError:
            info = ""
            try:
                info = self.path.read_text(encoding="utf-8").strip()
            except OSError:
                info = ""
            msg = "contextCLI is busy (lock present). Try again shortly."
            if info:
                msg += f" lock={info}"
            raise SystemExit(msg)

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _make_executable(path: Path) -> None:
    try:
        mode = path.stat().st_mode
        path.chmod(mode | 0o111)
    except OSError:
        pass


def _atomic_write_json(path: Path, obj: Any) -> None:
    _atomic_write_text(path, json.dumps(obj, indent=2, sort_keys=True) + "\n")


def _backup_file(path: Path, backups_dir: Path) -> None:
    if not path.exists():
        return
    backups_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = backups_dir / f"{path.name}.{ts}.bak"
    shutil.copy2(path, dst)


def _prune_backups(backups_dir: Path, max_files: int) -> None:
    if max_files <= 0 or not backups_dir.exists():
        return
    try:
        files = sorted(backups_dir.glob("*.bak"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for p in files[max_files:]:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _rotate_events_if_needed(repo: Path, events_path: Path, max_bytes: int) -> None:
    if max_bytes <= 0 or not events_path.exists():
        return
    try:
        if events_path.stat().st_size < max_bytes:
            return
    except OSError:
        return
    artifacts = _artifacts_dir(repo)
    artifacts.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = artifacts / f"events.{ts}.jsonl"
    try:
        shutil.move(str(events_path), str(dst))
    except OSError:
        return
    # Start a fresh file.
    try:
        events_path.write_text("", encoding="utf-8")
    except OSError:
        pass


def _append_jsonl(repo: Path, path: Path, obj: Any, *, max_events_bytes: int) -> None:
    _rotate_events_if_needed(repo, path, max_events_bytes)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj, sort_keys=True))
        f.write("\n")


def _sh(cmd: list[str], cwd: Path) -> str:
    try:
        p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=False)
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        if out and err:
            return out + "\n" + err
        return out or err
    except FileNotFoundError:
        return ""

def _git_branch(cwd: Path) -> str:
    if not (cwd / ".git").exists():
        return ""
    return _sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)


def _git_hooks_path(cwd: Path) -> str:
    if not (cwd / ".git").exists():
        return ""
    return _sh(["git", "config", "--get", "core.hooksPath"], cwd=cwd)


def _git_diff_head(cwd: Path) -> str:
    if not (cwd / ".git").exists():
        return ""
    return _sh(["git", "show", "--stat", "--no-color", "--pretty=medium", "HEAD"], cwd=cwd)


def _git_patch_head(cwd: Path) -> str:
    if not (cwd / ".git").exists():
        return ""
    return _sh(["git", "show", "--stat", "--patch", "--no-color", "--pretty=medium", "HEAD"], cwd=cwd)


_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*([^\s'\"`]+)"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)([A-Za-z0-9._~+/=-]+)"),
    re.compile(r"(sk-[A-Za-z0-9_-]{16,})"),
    re.compile(r"(-----BEGIN [A-Z ]*PRIVATE" + r" KEY-----.*?-----END [A-Z ]*PRIVATE" + r" KEY-----)", re.S),
]


def redact_text(text: str) -> str:
    redacted = text
    for pat in _SECRET_PATTERNS:
        def repl(match: re.Match[str]) -> str:
            if len(match.groups()) >= 2:
                return f"{match.group(1)}[REDACTED]"
            return "[REDACTED]"

        redacted = pat.sub(repl, redacted)
    return redacted


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

    # Cheap model access:
    # `openai_compatible` means "POST {base_url}/chat/completions" with OPENAI_API_KEY.
    api_provider: str = "openai_compatible"
    api_base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"


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


def _config_path(repo: Path) -> Path:
    return repo / ".contextCLI" / "config.toml"


def _state_path(repo: Path) -> Path:
    return repo / ".contextCLI" / "working_state.json"


def _events_path(repo: Path) -> Path:
    return repo / ".contextCLI" / "events.jsonl"


def _pointers_path(repo: Path) -> Path:
    return repo / ".contextCLI" / "pointers.md"


def _current_context_path(repo: Path) -> Path:
    return repo / ".contextCLI" / "current_context.json"


def _artifacts_dir(repo: Path) -> Path:
    return repo / ".contextCLI" / "artifacts"


def _checkpoints_dir(repo: Path) -> Path:
    return repo / ".contextCLI" / "checkpoints"


def _backups_dir(repo: Path) -> Path:
    return repo / ".contextCLI" / "artifacts" / "backups"

def _lock_path(repo: Path) -> Path:
    return repo / ".contextCLI" / ".lock"


def ensure_repo_initialized(repo: Path) -> None:
    if not (repo / ".contextCLI").exists():
        raise SystemExit("Missing .contextCLI/. Run `contextCLI init` first.")


def _write_gitignore_defaults(repo: Path) -> bool:
    gitignore = repo / ".gitignore"
    try:
        existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
        if ".contextCLI/" in existing and "\n.env\n" in "\n" + existing + "\n":
            return False
        sep = "" if not existing or existing.endswith("\n") else "\n"
        add = ""
        if ".contextCLI/" not in existing:
            add += ".contextCLI/\n"
        if "\n.env\n" not in "\n" + existing + "\n":
            add += ".env\n"
        _atomic_write_text(gitignore, existing + sep + add)
        return True
    except OSError:
        return False


def _write_env_example(repo: Path) -> bool:
    env_example = repo / ".env.example"
    if env_example.exists():
        return False
    try:
        env_example.write_text(
            "\n".join(
                [
                    "# Copy to `.env` and fill values. Never commit `.env`.",
                    "# OpenAI-compatible providers (OpenAI/Together/OpenRouter/Cerebras):",
                    "OPENAI_API_KEY" + "=",
                    "TOGETHER_API_KEY" + "=",
                    "OPENROUTER_API_KEY" + "=",
                    "CEREBRAS_API_KEY" + "=",
                    "",
                    "# Anthropic:",
                    "ANTHROPIC_API_KEY" + "=",
                    "",
                    "# Gemini:",
                    "GEMINI_API_KEY" + "=",
                    "",
                    "# Ollama is local and typically needs no key.",
                    "# Set this only if Ollama is not using the default local endpoint.",
                    "OLLAMA_BASE_URL" + "=",
                    "",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return True
    except OSError:
        return False


def hook_script() -> str:
    return "\n".join(
        [
            "#!/usr/bin/env sh",
            "set -eu",
            "",
            'cd "$(git rev-parse --show-toplevel)"',
            "",
            "# Fail-open so commits are never blocked by reflection/provider issues.",
            "contextCLI auto-compaction || true",
            "",
        ]
    )


def claude_end_of_turn_script() -> str:
    return "\n".join(
        [
            "#!/usr/bin/env sh",
            "set -eu",
            "",
            "# Optional Claude Code end-of-turn hook.",
            "# Run this from a repository initialized with `contextCLI init`.",
            "# The instruction can be passed as the first argument or via CONTEXTCLI_INSTRUCTION.",
            "",
            'instruction="${1:-${CONTEXTCLI_INSTRUCTION:-Claude turn completed}}"',
            "",
            "contextCLI update-context --instruction \"$instruction\" || true",
            "",
        ]
    )


def install_hooks(repo: Path, *, git_hook: bool = False) -> list[str]:
    installed: list[str] = []
    hooks_dir = repo / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    post_commit = hooks_dir / "post-commit"
    _atomic_write_text(post_commit, hook_script())
    _make_executable(post_commit)
    installed.append(str(post_commit))
    claude_hook = hooks_dir / "claude-end-of-turn.sh"
    _atomic_write_text(claude_hook, claude_end_of_turn_script())
    _make_executable(claude_hook)
    installed.append(str(claude_hook))

    if git_hook and (repo / ".git" / "hooks").exists():
        dst = repo / ".git" / "hooks" / "post-commit"
        _atomic_write_text(dst, hook_script())
        _make_executable(dst)
        installed.append(str(dst))
    return installed


def uninstall_hooks(repo: Path, *, git_hook: bool = False) -> list[str]:
    removed: list[str] = []
    targets = [repo / "hooks" / "post-commit", repo / "hooks" / "claude-end-of-turn.sh"]
    if git_hook:
        targets.append(repo / ".git" / "hooks" / "post-commit")
    for p in targets:
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        owns_post_commit = "contextCLI auto-compaction" in text
        owns_claude_hook = "contextCLI update-context --instruction \"$instruction\" || true" in text
        if not owns_post_commit and not owns_claude_hook:
            continue
        p.unlink()
        removed.append(str(p))
    return removed


def hook_status(repo: Path) -> dict[str, Any]:
    hooks_dir = repo / "hooks"
    template_hook = hooks_dir / "post-commit"
    claude_hook = hooks_dir / "claude-end-of-turn.sh"
    git_dir = repo / ".git"
    direct_git_hook = git_dir / "hooks" / "post-commit"
    configured_hooks_path = _git_hooks_path(repo).strip()

    if configured_hooks_path:
        hooks_path_resolved = (repo / configured_hooks_path).resolve()
    else:
        hooks_path_resolved = None

    using_repo_hooks_dir = hooks_path_resolved == hooks_dir.resolve() if hooks_path_resolved else False

    return {
        "is_git_repo": git_dir.exists(),
        "template_hook_exists": template_hook.exists(),
        "claude_hook_exists": claude_hook.exists(),
        "direct_git_hook_exists": direct_git_hook.exists(),
        "configured_hooks_path": configured_hooks_path,
        "using_repo_hooks_dir": using_repo_hooks_dir,
        "template_hook_path": str(template_hook),
        "claude_hook_path": str(claude_hook),
        "direct_git_hook_path": str(direct_git_hook),
    }


def init_repo(
    repo: Path,
    enable_pointers: bool,
    *,
    install_git_hook: bool = False,
    config_overrides: Optional[dict[str, Any]] = None,
) -> None:
    root = repo / ".contextCLI"
    (root / "artifacts" / "topics").mkdir(parents=True, exist_ok=True)
    (root / "checkpoints").mkdir(parents=True, exist_ok=True)
    # Migration: older scaffold accidentally created `.contextCLI/topics` at root.
    stray_topics = root / "topics"
    if stray_topics.exists() and stray_topics.is_dir():
        dst = root / "artifacts" / "topics"
        dst.mkdir(parents=True, exist_ok=True)
        for child in stray_topics.iterdir():
            target = dst / child.name
            if target.exists():
                continue
            try:
                child.replace(target)
            except OSError:
                pass
        try:
            stray_topics.rmdir()
        except OSError:
            pass

    cfg_path = _config_path(repo)
    cfg = _with_overrides(_default_config(enable_pointers), config_overrides or {})
    if not cfg_path.exists():
        _atomic_write_text(cfg_path, _render_config_toml(cfg))
    else:
        # If an older config exists, keep user edits. Only add missing keys and
        # normalize known old key names.
        text = cfg_path.read_text(encoding="utf-8")
        if "max_pointers" in text or "compaction_frequency" in text or "model_reflection" in text:
            # Non-destructive: append our new keys if they aren't present.
            updated = text
            if "max_pointer_lines" not in text:
                updated += f"\nmax_pointer_lines = {cfg.max_pointer_lines}\n"
            if "compaction_every_n_turns" not in text:
                updated += f"compaction_every_n_turns = {cfg.compaction_every_n_turns}\n"
            if "summarizer_model" not in text:
                updated += f"summarizer_model = \"{cfg.summarizer_model}\"\n"
            if "max_events_bytes" not in text:
                updated += f"max_events_bytes = {cfg.max_events_bytes}\n"
            if "max_backup_files" not in text:
                updated += f"max_backup_files = {cfg.max_backup_files}\n"
            if "load_env_file" not in text:
                updated += f"load_env_file = {str(cfg.load_env_file).lower()}\n"
            if "capture_git_patch" not in text:
                updated += f"capture_git_patch = {str(cfg.capture_git_patch).lower()}\n"
            if "redact_event_secrets" not in text:
                updated += f"redact_event_secrets = {str(cfg.redact_event_secrets).lower()}\n"
            if "enable_pointers" not in text:
                updated += f"enable_pointers = {str(cfg.enable_pointers).lower()}\n"
            if "api_provider" not in text:
                updated += f"api_provider = \"{cfg.api_provider}\"\n"
            if "api_base_url" not in text:
                updated += f"api_base_url = \"{cfg.api_base_url}\"\n"
            if "api_key_env" not in text:
                updated += f"api_key_env = \"{cfg.api_key_env}\"\n"
            if updated != text:
                _backup_file(cfg_path, _backups_dir(repo))
                _atomic_write_text(cfg_path, updated.strip() + "\n")
        # If user explicitly passed enable_pointers, ensure config reflects it.
        if "enable_pointers" in cfg_path.read_text(encoding="utf-8") and enable_pointers:
            t2 = cfg_path.read_text(encoding="utf-8")
            # naive but safe: replace a single-line `enable_pointers = false` if present.
            if "enable_pointers = false" in t2:
                _backup_file(cfg_path, _backups_dir(repo))
                _atomic_write_text(cfg_path, t2.replace("enable_pointers = false", "enable_pointers = true"))

    if config_overrides:
        update_config(repo, config_overrides)

    for p, default in [
        (_events_path(repo), None),
        (_state_path(repo), {}),
        (_pointers_path(repo), "# Pointers\n"),
        (_current_context_path(repo), {}),
    ]:
        if p.exists():
            continue
        if p.suffix == ".json":
            _atomic_write_json(p, default)
        elif p.suffix == ".md":
            _atomic_write_text(p, str(default))
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("", encoding="utf-8")

    install_hooks(repo, git_hook=False)

    if install_git_hook and (repo / ".git" / "hooks").exists():
        install_hooks(repo, git_hook=True)

    _write_gitignore_defaults(repo)
    _write_env_example(repo)


def repair_repo(repo: Path, *, install_git_hook: bool = False, clear_stale_lock: bool = False) -> dict[str, Any]:
    actions: list[str] = []
    root = repo / ".contextCLI"
    root.mkdir(parents=True, exist_ok=True)
    (root / "artifacts" / "topics").mkdir(parents=True, exist_ok=True)
    (root / "checkpoints").mkdir(parents=True, exist_ok=True)

    cfg_path = _config_path(repo)
    if not cfg_path.exists():
        write_config(repo, _default_config(enable_pointers=False))
        actions.append("created .contextCLI/config.toml")

    for p, default in [
        (_events_path(repo), ""),
        (_state_path(repo), {}),
        (_pointers_path(repo), "# Pointers\n"),
        (_current_context_path(repo), {}),
    ]:
        if p.exists():
            continue
        if p.suffix == ".json":
            _atomic_write_json(p, default)
        else:
            _atomic_write_text(p, str(default))
        actions.append(f"created {p.relative_to(repo)}")

    installed = install_hooks(repo, git_hook=install_git_hook)
    for path in installed:
        actions.append(f"installed {Path(path).relative_to(repo)}")

    if _write_gitignore_defaults(repo):
        actions.append("updated .gitignore")
    if _write_env_example(repo):
        actions.append("created .env.example")

    lock = _lock_path(repo)
    if clear_stale_lock and lock.exists():
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            age = 0
        if age > 600:
            try:
                lock.unlink(missing_ok=True)
                actions.append("removed stale .contextCLI/.lock")
            except OSError:
                actions.append("failed to remove stale .contextCLI/.lock")

    return {"ok": True, "actions": actions}


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
    # TOML parsing with stdlib (py>=3.11).
    import tomllib

    data: dict[str, Any] = {}
    p = _config_path(repo)
    if p.exists():
        data = tomllib.loads(p.read_text(encoding="utf-8"))

    # Back-compat with earlier scaffold keys.
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


@dataclass
class RepoState:
    turns: int = 0
    last_compaction_turn: int = 0
    open_items: list[str] = None  # type: ignore[assignment]
    last_updated_at: str = ""

    @staticmethod
    def from_json(obj: dict[str, Any]) -> "RepoState":
        return RepoState(
            turns=int(obj.get("turns", 0)),
            last_compaction_turn=int(obj.get("last_compaction_turn", 0)),
            open_items=list(obj.get("open_items", []) or []),
            last_updated_at=str(obj.get("last_updated_at", "")),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "turns": self.turns,
            "last_compaction_turn": self.last_compaction_turn,
            "open_items": self.open_items,
            "last_updated_at": self.last_updated_at,
        }


def load_state(repo: Path) -> RepoState:
    return RepoState.from_json(_read_json(_state_path(repo), {}))


def load_current_context(repo: Path) -> dict[str, Any]:
    obj = _read_json(_current_context_path(repo), {})
    return obj if isinstance(obj, dict) else {}


def _write_state(repo: Path, state: RepoState, *, max_backup_files: int) -> None:
    p = _state_path(repo)
    _backup_file(p, _backups_dir(repo))
    _prune_backups(_backups_dir(repo), max_backup_files)
    state.last_updated_at = _utc_now_iso()
    _atomic_write_json(p, state.to_json())


def _write_current_context(repo: Path, obj: dict[str, Any], *, max_backup_files: int) -> None:
    p = _current_context_path(repo)
    _backup_file(p, _backups_dir(repo))
    _prune_backups(_backups_dir(repo), max_backup_files)
    _atomic_write_json(p, obj)


def _write_pointers(repo: Path, pointers_md: str, *, max_backup_files: int) -> None:
    p = _pointers_path(repo)
    _backup_file(p, _backups_dir(repo))
    _prune_backups(_backups_dir(repo), max_backup_files)
    _atomic_write_text(p, pointers_md.rstrip() + "\n")


def _normalize_pointers(md: str, max_lines: int) -> str:
    # Keep only well-formed pointer lines. This lets the cheap model be a bit messy
    # without breaking downstream consumers.
    raw = [ln.rstrip("\n") for ln in (md or "").splitlines()]
    body = [ln.strip() for ln in raw if ln.strip()]

    out: list[str] = ["# Pointers"]
    for ln in body:
        if ln.startswith("#"):
            continue
        if not ln.startswith("- ["):
            continue
        # Accept either an em-dash separator or a simple dash separator.
        if "](" not in ln or (") \u2014 " not in ln and ") - " not in ln and ") -- " not in ln):
            continue
        if len(ln) > 150:
            ln = ln[:147] + "..."
        out.append(ln)
        if len(out) - 1 >= max_lines:
            break

    return "\n".join(out) + "\n"


def _hash_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


def _redact_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {k: _redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_obj(v) for v in obj]
    return obj


def _record_event(
    repo: Path,
    kind: str,
    payload: dict[str, Any],
    *,
    max_events_bytes: int,
    redact: bool,
) -> dict[str, Any]:
    safe_payload = _redact_obj(payload) if redact else payload
    ev = {
        "ts": _utc_now_iso(),
        "kind": kind,
        "payload": {
            **safe_payload,
            "repo": str(repo),
            "git_branch": _git_branch(repo),
        },
    }
    _append_jsonl(repo, _events_path(repo), ev, max_events_bytes=max_events_bytes)
    return ev


def run_update_context(repo: Path, cfg: Config, state: RepoState, instruction: str, *, force: bool = False) -> dict[str, Any]:
    with _Lock(_lock_path(repo)):
        state.turns += 1
        ev = _record_event(
            repo,
            "instruction",
            {"instruction": instruction},
            max_events_bytes=cfg.max_events_bytes,
            redact=cfg.redact_event_secrets,
        )
        _write_state(repo, state, max_backup_files=cfg.max_backup_files)

        ran = False
        # Opportunistically run compaction when enabled; still respect frequency to avoid churn.
        should = cfg.enable_pointers and (
            force or (state.turns - state.last_compaction_turn) >= cfg.compaction_every_n_turns
        )
        if should:
            ran = True
            out = _compaction(repo, cfg, state, reason="update-context")
        else:
            out = {"message": "event recorded; compaction skipped"}

        return {"event": ev, "turns": state.turns, "compaction_ran": ran, "result": out}


def run_auto_compaction(repo: Path, cfg: Config, state: RepoState, *, force: bool = False) -> dict[str, Any]:
    with _Lock(_lock_path(repo)):
        state.turns += 1
        diff = _git_patch_head(repo) if cfg.capture_git_patch else _git_diff_head(repo)
        ev_kind = "git_post_commit" if diff else "auto_compaction"
        ev = _record_event(
            repo,
            ev_kind,
            {"diff": diff[:200_000], "diff_hash": _hash_text(diff) if diff else ""},
            max_events_bytes=cfg.max_events_bytes,
            redact=cfg.redact_event_secrets,
        )
        _write_state(repo, state, max_backup_files=cfg.max_backup_files)

        should = cfg.enable_pointers and (
            force or (state.turns - state.last_compaction_turn) >= cfg.compaction_every_n_turns
        )
        if not should:
            return {"event": ev, "message": "skipped (pointers disabled or compaction frequency not reached)"}

        out = _compaction(repo, cfg, state, reason="auto-compaction")
        return {"event": ev, "result": out}


def _load_recent_events(repo: Path, limit: int = 20) -> list[dict[str, Any]]:
    p = _events_path(repo)
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8").splitlines()
    out: list[dict[str, Any]] = []
    for ln in lines[-limit:]:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def _compaction(repo: Path, cfg: Config, state: RepoState, reason: str) -> dict[str, Any]:
    recent = _load_recent_events(repo, limit=20)
    current_state = _read_json(_state_path(repo), {})
    current_pointers = _pointers_path(repo).read_text(encoding="utf-8") if _pointers_path(repo).exists() else ""

    api_key = os.environ.get(cfg.api_key_env, "")
    reflection = cheap_reflect(
        provider=cfg.api_provider,
        base_url=cfg.api_base_url,
        api_key=api_key,
        model=cfg.summarizer_model,
        recent_events=recent,
        working_state=current_state,
        pointers_md=current_pointers,
        max_pointer_lines=cfg.max_pointer_lines,
        reason=reason,
    )

    # Update distilled files (all optional; keep robust against partial outputs).
    # Never let the model clobber `turns`/`last_compaction_turn`.
    new_state = reflection.get("working_state")
    if isinstance(new_state, dict):
        merged = state.to_json()
        if isinstance(new_state.get("open_items"), list):
            merged["open_items"] = list(new_state.get("open_items") or [])
        # allow model to set additional keys, but keep our counters.
        for k, v in new_state.items():
            if k in ("turns", "last_compaction_turn", "last_updated_at"):
                continue
            merged[k] = v
        _write_state(repo, RepoState.from_json(merged), max_backup_files=cfg.max_backup_files)
    else:
        _write_state(repo, state, max_backup_files=cfg.max_backup_files)

    new_context = reflection.get("current_context")
    if isinstance(new_context, dict):
        _write_current_context(repo, new_context, max_backup_files=cfg.max_backup_files)

    pointers_md = reflection.get("pointers_md")
    if isinstance(pointers_md, str):
        _write_pointers(
            repo,
            _normalize_pointers(pointers_md, cfg.max_pointer_lines),
            max_backup_files=cfg.max_backup_files,
        )

    # Optional topic files.
    topics = reflection.get("topics") or []
    if isinstance(topics, list):
        topics_dir = _artifacts_dir(repo) / "topics"
        topics_dir.mkdir(parents=True, exist_ok=True)
        for t in topics:
            if not isinstance(t, dict):
                continue
            name = str(t.get("name", "")).strip()
            content = str(t.get("content", "")).strip()
            if not name or not content:
                continue
            safe = "".join(ch for ch in name if ch.isalnum() or ch in ("-", "_")).strip("_-")
            if not safe:
                continue
            _atomic_write_text(topics_dir / f"{safe}.md", content.rstrip() + "\n")

    state.last_compaction_turn = state.turns
    _write_state(repo, state, max_backup_files=cfg.max_backup_files)
    return {"reason": reason, "reflected": bool(reflection.get("_reflected", False))}


@dataclass(frozen=True)
class Checkpoint:
    path: Path
    task_id: str

    @staticmethod
    def create(repo: Path, cfg: Config, state: RepoState, note: Optional[str]) -> "Checkpoint":
        # Task id is time-based to avoid collisions.
        task_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        cp_dir = _checkpoints_dir(repo)
        cp_dir.mkdir(parents=True, exist_ok=True)
        cp_path = cp_dir / f"{task_id}.json"

        snapshot = {
            "task_id": task_id,
            "ts": _utc_now_iso(),
            "note": note or "",
            "working_state": _read_json(_state_path(repo), {}),
            "pointers_md": _pointers_path(repo).read_text(encoding="utf-8") if _pointers_path(repo).exists() else "",
            "current_context": _read_json(_current_context_path(repo), {}),
            "config": cfg.__dict__,
        }
        _atomic_write_json(cp_path, snapshot)
        return Checkpoint(path=cp_path, task_id=task_id)


def resume_prefix(repo: Path, cfg: Config, task_id: str) -> str:
    if task_id == "latest":
        cps = sorted(_checkpoints_dir(repo).glob("*.json"), key=lambda p: p.name)
        if cps:
            task_id = cps[-1].stem
    cp = _checkpoints_dir(repo) / f"{task_id}.json"
    snap = _read_json(cp, {})
    working = _read_json(_state_path(repo), {})
    pointers = _pointers_path(repo).read_text(encoding="utf-8") if _pointers_path(repo).exists() else ""

    if isinstance(snap, dict) and snap.get("task_id") == task_id:
        working = snap.get("working_state", working) or working
        pointers = snap.get("pointers_md", pointers) or pointers
        note = str(snap.get("note", "") or "").strip()
    else:
        note = ""

    pointers = _normalize_pointers(pointers, cfg.max_pointer_lines)

    parts = []
    parts.append("## contextCLI: distilled working state")
    parts.append(json.dumps(working, indent=2, sort_keys=True))
    if note:
        parts.append("## contextCLI: checkpoint note")
        parts.append(note)
    parts.append("## contextCLI: pointers")
    parts.append(pointers.strip())
    return "\n\n".join(parts).strip() + "\n"


def status_summary(state: RepoState) -> dict[str, Any]:
    open_items = state.open_items or []
    lines = []
    lines.append(f"turns: {state.turns}")
    lines.append(f"last_compaction_turn: {state.last_compaction_turn}")
    if state.last_updated_at:
        lines.append(f"last_updated_at: {state.last_updated_at}")
    if open_items:
        lines.append("")
        lines.append("open_items:")
        for it in open_items[:20]:
            lines.append(f"- {it}")
        if len(open_items) > 20:
            lines.append(f"... ({len(open_items) - 20} more)")
    return {"turns": state.turns, "open_items": open_items, "text": "\n".join(lines)}


def events_count(repo: Path) -> int:
    p = _events_path(repo)
    if not p.exists():
        return 0
    n = 0
    with p.open("r", encoding="utf-8") as f:
        for ln in f:
            if ln.strip():
                n += 1
    return n


def latest_checkpoint_id(repo: Path) -> str:
    cps = sorted(_checkpoints_dir(repo).glob("*.json"), key=lambda p: p.name)
    if not cps:
        return ""
    return cps[-1].stem


def _checkpoint_health(repo: Path, task_id: str) -> tuple[bool, str]:
    cp = _checkpoints_dir(repo) / f"{task_id}.json"
    if not cp.exists():
        return False, "checkpoint: latest checkpoint missing from disk"
    try:
        snap = json.loads(cp.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError as e:
        return False, f"checkpoint: latest checkpoint has bad JSON: {e}"
    if not isinstance(snap, dict):
        return False, "checkpoint: latest checkpoint is not a JSON object"
    if str(snap.get("task_id", "")) != task_id:
        return False, "checkpoint: latest checkpoint task_id does not match filename"
    if "working_state" not in snap or "pointers_md" not in snap:
        return False, "checkpoint: latest checkpoint is missing required fields"
    return True, f"checkpoint: latest checkpoint {task_id} is readable"


def doctor_report(repo: Path) -> dict[str, Any]:
    root = repo / ".contextCLI"
    gitignore = repo / ".gitignore"
    env_example = repo / ".env.example"
    required_files = [
        root / "config.toml",
        root / "events.jsonl",
        root / "working_state.json",
        root / "pointers.md",
        root / "current_context.json",
    ]
    required_dirs = [
        root / "artifacts",
        root / "artifacts" / "topics",
        root / "checkpoints",
    ]

    lines: list[str] = []
    ok = True

    lines.append(f"repo: {repo}")
    lines.append(f"state_dir: {root}")

    lock = _lock_path(repo)
    if lock.exists():
        ok = False
        try:
            info = lock.read_text(encoding="utf-8").strip()
        except OSError:
            info = ""
        lines.append(f"LOCK: present ({info or 'unreadable'})")

    missing_files = [str(p.relative_to(repo)) for p in required_files if not p.exists()]
    missing_dirs = [str(p.relative_to(repo)) for p in required_dirs if not p.exists()]
    if missing_files:
        ok = False
        lines.append("MISSING files:")
        lines.extend([f"- {p}" for p in missing_files])
    if missing_dirs:
        ok = False
        lines.append("MISSING dirs:")
        lines.extend([f"- {p}" for p in missing_dirs])

    if not env_example.exists():
        lines.append("env: .env.example missing (run `contextCLI init` to restore the template)")
    else:
        try:
            env_example_text = env_example.read_text(encoding="utf-8")
            if "OLLAMA_BASE_URL=" not in env_example_text:
                lines.append("env: .env.example does not mention OLLAMA_BASE_URL")
        except OSError as e:
            ok = False
            lines.append(f"env: cannot read .env.example: {type(e).__name__}: {e}")

    try:
        gitignore_text = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
        if ".contextCLI/" not in gitignore_text:
            ok = False
            lines.append("gitignore: missing .contextCLI/ entry")
        if "\n.env\n" not in "\n" + gitignore_text + "\n":
            ok = False
            lines.append("gitignore: missing .env entry")
    except OSError as e:
        ok = False
        lines.append(f"gitignore: unreadable: {type(e).__name__}: {e}")

    # Parse JSON.
    for p in [root / "working_state.json", root / "current_context.json"]:
        if not p.exists():
            continue
        try:
            json.loads(p.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as e:
            ok = False
            lines.append(f"BAD JSON: {p.name}: {e}")

    # Config parse.
    try:
        cfg = load_config(repo)
        lines.append(f"config: enable_pointers={cfg.enable_pointers} model={cfg.summarizer_model} provider={cfg.api_provider}")
        lines.append(f"config: max_pointer_lines={cfg.max_pointer_lines} max_events_bytes={cfg.max_events_bytes} max_backup_files={cfg.max_backup_files}")
        if cfg.api_provider not in SUPPORTED_PROVIDERS:
            ok = False
            lines.append(f"BAD provider: {cfg.api_provider}")
        if cfg.api_key_env and not _ENV_NAME_RE.match(cfg.api_key_env):
            ok = False
            lines.append(f"BAD api_key_env: {cfg.api_key_env}")
        expected_base = _provider_default(cfg.api_provider, "api_base_url")
        expected_key_env = _provider_default(cfg.api_provider, "api_key_env")
        if cfg.api_provider in {"ollama", "ollama_local"}:
            if not cfg.api_base_url and not os.environ.get("OLLAMA_BASE_URL", ""):
                lines.append("provider: ollama endpoint not set (configure api_base_url or OLLAMA_BASE_URL)")
            if cfg.api_key_env:
                lines.append("provider: ollama does not usually need api_key_env")
        else:
            if not cfg.api_base_url:
                ok = False
                lines.append("provider: api_base_url is empty")
            elif expected_base and cfg.api_base_url != expected_base:
                lines.append(f"provider: custom api_base_url in use ({cfg.api_base_url})")
            if not cfg.api_key_env:
                ok = False
                lines.append("provider: api_key_env is empty")
            elif expected_key_env and cfg.api_key_env != expected_key_env:
                lines.append(f"provider: custom api_key_env in use ({cfg.api_key_env})")
        if cfg.enable_pointers:
            key = os.environ.get(cfg.api_key_env, "")
            if cfg.api_provider in ("ollama", "ollama_local"):
                lines.append("auth: ollama selected (no API key required)")
            elif not key:
                lines.append(f"auth: missing env var {cfg.api_key_env} (reflection will no-op)")
    except Exception as e:
        ok = False
        lines.append(f"BAD config.toml: {type(e).__name__}: {e}")
        cfg = None  # type: ignore[assignment]

    # Validate pointers.md shape.
    pointers_p = root / "pointers.md"
    if pointers_p.exists():
        try:
            txt = pointers_p.read_text(encoding="utf-8")
            plines = [ln.rstrip("\n") for ln in txt.splitlines() if ln.strip()]
            pointer_lines = [ln for ln in plines if ln.startswith("- [")]
            too_long = [ln for ln in pointer_lines if len(ln) > 150]
            malformed = [
                ln
                for ln in pointer_lines
                if "](" not in ln or (") \u2014 " not in ln and ") - " not in ln and ") -- " not in ln)
            ]
            if cfg is not None and len(pointer_lines) > cfg.max_pointer_lines:
                ok = False
                lines.append(f"POINTERS: too many lines ({len(pointer_lines)} > {cfg.max_pointer_lines})")
            if too_long:
                ok = False
                lines.append(f"POINTERS: {len(too_long)} lines exceed 150 chars")
            if malformed:
                ok = False
                lines.append(f"POINTERS: {len(malformed)} malformed lines (expected '- [label](ref) -- desc')")
        except OSError as e:
            ok = False
            lines.append(f"POINTERS: unreadable: {type(e).__name__}: {e}")

    # Events size / rotation hint.
    events_p = root / "events.jsonl"
    if events_p.exists():
        try:
            sz = events_p.stat().st_size
            lines.append(f"events.jsonl: {sz} bytes")
            if cfg is not None and cfg.max_events_bytes > 0 and sz > cfg.max_events_bytes:
                ok = False
                lines.append(f"EVENTS: exceeds max_events_bytes ({sz} > {cfg.max_events_bytes})")
        except OSError:
            pass
        try:
            rotated = sorted((root / "artifacts").glob("events.*.jsonl"))
            if rotated:
                lines.append(f"events rotated: {len(rotated)} files")
        except OSError:
            pass

    latest_cp = latest_checkpoint_id(repo)
    if latest_cp:
        cp_ok, cp_line = _checkpoint_health(repo, latest_cp)
        lines.append(cp_line)
        if not cp_ok:
            ok = False
    else:
        lines.append("checkpoint: none created yet")

    hs = hook_status(repo)
    if hs["template_hook_exists"]:
        lines.append("hook: hooks/post-commit present")
    else:
        lines.append("hook: hooks/post-commit missing (run `contextCLI init`)")
    if hs["claude_hook_exists"]:
        lines.append("hook: hooks/claude-end-of-turn.sh present")
    else:
        lines.append("hook: hooks/claude-end-of-turn.sh missing (run `contextCLI init`)")

    if not hs["is_git_repo"]:
        lines.append("hook: not a git repository")
    else:
        if hs["configured_hooks_path"]:
            lines.append(f"hook: git core.hooksPath={hs['configured_hooks_path']}")
            if hs["using_repo_hooks_dir"]:
                lines.append("hook: git is configured to use hooks/post-commit")
            else:
                lines.append("hook: git uses a different hooks directory")
        elif hs["direct_git_hook_exists"]:
            lines.append("hook: .git/hooks/post-commit installed")
        else:
            lines.append("hook: git is not yet wired to run contextCLI automatically")

    return {"ok": ok, "lines": lines}
