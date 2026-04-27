from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .summarizer import cheap_reflect
from .utils import _find_secret_paths, _has_secret_pattern, _ENV_NAME_RE
from .storage import (
    _read_json,
    _pointers_path,
    _checkpoints_dir,
    _lock_path,
    _metadata_path,
    _events_path,
    _state_path,
    _current_context_path,
    _artifacts_dir,
    load_metadata,
    STORAGE_SCHEMA_VERSION,
)
from .config import SUPPORTED_PROVIDERS, _provider_default, load_config
from .hooks import hook_status
from .metrics import latest_checkpoint_id

def doctor_exit_ok(report: dict[str, Any], *, strict: bool = False) -> bool:
    if not bool(report.get("ok", False)):
        return False
    if not strict:
        return True
    summary = report.get("summary")
    if not isinstance(summary, dict):
        return bool(report.get("ok", False))
    return int(summary.get("warnings", 0) or 0) == 0

def validate_provider(repo: Path, cfg: Any) -> dict[str, Any]:
    api_key = os.environ.get(cfg.api_key_env, "")
    result = cheap_reflect(
        provider=cfg.api_provider,
        base_url=cfg.api_base_url,
        api_key=api_key,
        model=cfg.summarizer_model,
        recent_events=[],
        working_state={},
        pointers_md="# Pointers\n",
        max_pointer_lines=cfg.max_pointer_lines,
        reason="provider-validation",
        timeout_s=20.0,
    )
    reflected = bool(result.get("_reflected", False))
    current_context = result.get("current_context") if isinstance(result.get("current_context"), dict) else {}
    risks = current_context.get("risks") if isinstance(current_context, dict) else []
    summary = str(current_context.get("summary", "") if isinstance(current_context, dict) else "")
    return {
        "ok": reflected,
        "provider": cfg.api_provider,
        "model": cfg.summarizer_model,
        "base_url": cfg.api_base_url,
        "api_key_env": cfg.api_key_env,
        "api_key_present": bool(api_key) if cfg.api_provider not in ("ollama", "ollama_local") else True,
        "summary": summary,
        "risks": risks if isinstance(risks, list) else [],
    }

def _checkpoint_health(repo: Path, task_id: str) -> tuple[bool, str]:
    cp = _checkpoints_dir(repo) / f"{task_id}.json"
    if not cp.exists():
        return False, "checkpoint: latest checkpoint missing from disk"
    try:
        snap = json.loads(cp.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError as e:
        return False, f"checkpoint: latest checkpoint has bad JSON: {e}"
    ok, msg = _validate_checkpoint_snapshot(snap, expected_task_id=task_id)
    if not ok:
        return False, f"checkpoint: {msg}"
    return True, f"checkpoint: latest checkpoint {task_id} is readable"

def _validate_checkpoint_snapshot(snap: Any, *, expected_task_id: str = "") -> tuple[bool, str]:
    if not isinstance(snap, dict):
        return False, "checkpoint is not a JSON object"
    task_id = str(snap.get("task_id", "")).strip()
    if not task_id:
        return False, "checkpoint is missing task_id"
    if expected_task_id and task_id != expected_task_id:
        return False, "latest checkpoint task_id does not match filename"
    if "working_state" not in snap or not isinstance(snap.get("working_state"), dict):
        return False, "checkpoint is missing working_state"
    if "pointers_md" not in snap or not isinstance(snap.get("pointers_md"), str):
        return False, "checkpoint is missing pointers_md"
    current_context = snap.get("current_context")
    if current_context is not None and not isinstance(current_context, dict):
        return False, "checkpoint has invalid current_context"
    note = snap.get("note")
    if note is not None and not isinstance(note, str):
        return False, "checkpoint has invalid note"
    return True, ""

def doctor_report(repo: Path) -> dict[str, Any]:
    root = repo / ".contextCLI"
    gitignore = repo / ".gitignore"
    env_example = repo / ".env.example"
    required_files = [
        root / "config.toml",
        root / "metadata.json",
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
    issues: list[dict[str, str]] = []
    error_count = 0
    warning_count = 0

    def add_issue(code: str, message: str, *, severity: str = "error", fix_command: str = "") -> None:
        nonlocal error_count, warning_count
        issue = {"code": code, "message": message, "severity": severity}
        if fix_command:
            issue["fix_command"] = fix_command
        issues.append(issue)
        if severity == "error":
            error_count += 1
        else:
            warning_count += 1

    lines.append(f"repo: {repo}")
    lines.append(f"state_dir: {root}")

    lock = _lock_path(repo)
    if lock.exists():
        try:
            info = lock.read_text(encoding="utf-8").strip()
        except OSError:
            info = ""
        msg = f"LOCK: present ({info or 'unreadable'})"
        lines.append(msg)
        add_issue("lock_present", msg, fix_command="contextCLI repair --clear-stale-lock")

    missing_files = [str(p.relative_to(repo)) for p in required_files if not p.exists()]
    missing_dirs = [str(p.relative_to(repo)) for p in required_dirs if not p.exists()]
    if missing_files:
        lines.append("MISSING files:")
        lines.extend([f"- {p}" for p in missing_files])
        add_issue("missing_files", "Missing required .contextCLI files.", fix_command="contextCLI repair")
    if missing_dirs:
        lines.append("MISSING dirs:")
        lines.extend([f"- {p}" for p in missing_dirs])
        add_issue("missing_dirs", "Missing required .contextCLI directories.", fix_command="contextCLI repair")

    if not env_example.exists():
        msg = "env: .env.example missing (run `contextCLI init` to restore the template)"
        lines.append(msg)
        add_issue("missing_env_example", msg, fix_command="contextCLI repair")
    else:
        try:
            env_example_text = env_example.read_text(encoding="utf-8")
            if "OLLAMA_BASE_URL=" not in env_example_text:
                msg = "env: .env.example does not mention OLLAMA_BASE_URL"
                lines.append(msg)
                add_issue("env_example_incomplete", msg, fix_command="contextCLI repair")
        except OSError as e:
            msg = f"env: cannot read .env.example: {type(e).__name__}: {e}"
            lines.append(msg)
            add_issue("env_example_unreadable", msg)

    try:
        gitignore_text = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
        if ".contextCLI/" not in gitignore_text:
            msg = "gitignore: missing .contextCLI/ entry"
            lines.append(msg)
            add_issue("gitignore_missing_contextcli", msg, fix_command="contextCLI repair")
        if "\n.env\n" not in "\n" + gitignore_text + "\n":
            msg = "gitignore: missing .env entry"
            lines.append(msg)
            add_issue("gitignore_missing_env", msg, fix_command="contextCLI repair")
    except OSError as e:
        msg = f"gitignore: unreadable: {type(e).__name__}: {e}"
        lines.append(msg)
        add_issue("gitignore_unreadable", msg)

    for p in [root / "metadata.json", root / "working_state.json", root / "current_context.json"]:
        if not p.exists():
            continue
        try:
            json.loads(p.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as e:
            msg = f"BAD JSON: {p.name}: {e}"
            lines.append(msg)
            add_issue("bad_json", msg, fix_command="contextCLI repair")

    meta = load_metadata(repo) if (root / "metadata.json").exists() else {}
    if meta:
        meta_version = int(meta.get("schema_version", 0) or 0)
        lines.append(f"metadata: schema_version={meta_version}")
        if meta_version != STORAGE_SCHEMA_VERSION:
            msg = f"metadata: unsupported schema_version {meta_version} (expected {STORAGE_SCHEMA_VERSION}); run `contextCLI migrate`"
            lines.append(msg)
            add_issue("metadata_schema_mismatch", msg, fix_command="contextCLI migrate")
    secret_hits: list[str] = []
    for path, obj in [
        ("working_state", _read_json(root / "working_state.json", {})),
        ("current_context", _read_json(root / "current_context.json", {})),
    ]:
        secret_hits.extend(_find_secret_paths(obj, path))
    pointers_p = root / "pointers.md"
    if pointers_p.exists():
        try:
            pointers_text = pointers_p.read_text(encoding="utf-8")
            if _has_secret_pattern(pointers_text):
                secret_hits.append("pointers.md")
        except OSError:
            pass
    latest_cp_id = latest_checkpoint_id(repo)
    if latest_cp_id:
        latest_cp = _read_json(_checkpoints_dir(repo) / f"{latest_cp_id}.json", {})
        secret_hits.extend(_find_secret_paths(latest_cp, f"checkpoints.{latest_cp_id}"))
    if secret_hits:
        lines.append("secrets: possible secret-like values found in persisted state:")
        for hit in secret_hits[:5]:
            lines.append(f"- {hit}")
        if len(secret_hits) > 5:
            lines.append(f"- ... ({len(secret_hits) - 5} more)")
        add_issue("possible_secret_leakage", "Possible secret-like values found in persisted state.")

    try:
        cfg = load_config(repo)
        lines.append(f"config: enable_pointers={cfg.enable_pointers} model={cfg.summarizer_model} provider={cfg.api_provider}")
        lines.append(f"config: max_pointer_lines={cfg.max_pointer_lines} max_events_bytes={cfg.max_events_bytes} max_backup_files={cfg.max_backup_files}")
        if cfg.api_provider not in SUPPORTED_PROVIDERS:
            msg = f"BAD provider: {cfg.api_provider}"
            lines.append(msg)
            add_issue("bad_provider", msg)
        if cfg.api_key_env and not _ENV_NAME_RE.match(cfg.api_key_env):
            msg = f"BAD api_key_env: {cfg.api_key_env}"
            lines.append(msg)
            add_issue("bad_api_key_env", msg)
        expected_base = _provider_default(cfg.api_provider, "api_base_url")
        expected_key_env = _provider_default(cfg.api_provider, "api_key_env")
        if cfg.api_provider in {"ollama", "ollama_local"}:
            if not cfg.api_base_url and not os.environ.get("OLLAMA_BASE_URL", ""):
                lines.append("provider: ollama endpoint not set (configure api_base_url or OLLAMA_BASE_URL)")
            if cfg.api_key_env:
                lines.append("provider: ollama does not usually need api_key_env")
        else:
            if not cfg.api_base_url:
                msg = "provider: api_base_url is empty"
                lines.append(msg)
                add_issue("missing_api_base_url", msg)
            elif expected_base and cfg.api_base_url != expected_base:
                lines.append(f"provider: custom api_base_url in use ({cfg.api_base_url})")
            if not cfg.api_key_env:
                msg = "provider: api_key_env is empty"
                lines.append(msg)
                add_issue("missing_api_key_env", msg)
            elif expected_key_env and cfg.api_key_env != expected_key_env:
                lines.append(f"provider: custom api_key_env in use ({cfg.api_key_env})")
        if cfg.enable_pointers:
            key = os.environ.get(cfg.api_key_env, "")
            if cfg.api_provider in ("ollama", "ollama_local"):
                lines.append("auth: ollama selected (no API key required)")
            elif not key:
                msg = f"auth: missing env var {cfg.api_key_env} (reflection will no-op)"
                lines.append(msg)
                fix = f"auth: add {cfg.api_key_env} to .env or your shell environment"
                lines.append(fix)
                add_issue("missing_api_key", msg, severity="warning", fix_command=fix)
    except Exception as e:
        msg = f"BAD config.toml: {type(e).__name__}: {e}"
        lines.append(msg)
        add_issue("bad_config_toml", msg, fix_command="contextCLI repair")
        cfg = None

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
                msg = f"POINTERS: too many lines ({len(pointer_lines)} > {cfg.max_pointer_lines})"
                lines.append(msg)
                add_issue("too_many_pointers", msg)
            if too_long:
                msg = f"POINTERS: {len(too_long)} lines exceed 150 chars"
                lines.append(msg)
                add_issue("pointer_lines_too_long", msg)
            if malformed:
                msg = f"POINTERS: {len(malformed)} malformed lines (expected '- [label](ref) -- desc')"
                lines.append(msg)
                add_issue("malformed_pointers", msg)
        except OSError as e:
            msg = f"POINTERS: unreadable: {type(e).__name__}: {e}"
            lines.append(msg)
            add_issue("pointers_unreadable", msg)

    events_p = root / "events.jsonl"
    if events_p.exists():
        try:
            sz = events_p.stat().st_size
            lines.append(f"events.jsonl: {sz} bytes")
            if cfg is not None and cfg.max_events_bytes > 0 and sz > cfg.max_events_bytes:
                msg = f"EVENTS: exceeds max_events_bytes ({sz} > {cfg.max_events_bytes})"
                lines.append(msg)
                add_issue("events_file_too_large", msg)
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
            add_issue("bad_checkpoint", cp_line, fix_command="contextCLI repair")
    else:
        lines.append("checkpoint: none created yet")

    hs = hook_status(repo)
    if hs["template_hook_exists"]:
        lines.append("hook: hooks/post-commit present")
    else:
        lines.append("hook: hooks/post-commit missing (run `contextCLI init`)")
        lines.append("hook: fix with `contextCLI hooks install`")
        add_issue("missing_post_commit_hook", "hook: hooks/post-commit missing", fix_command="contextCLI hooks install")
    if hs["claude_hook_exists"]:
        lines.append("hook: hooks/claude-end-of-turn.sh present")
    else:
        lines.append("hook: hooks/claude-end-of-turn.sh missing (run `contextCLI init`)")
        lines.append("hook: fix with `contextCLI hooks install`")
        add_issue("missing_claude_hook", "hook: hooks/claude-end-of-turn.sh missing", fix_command="contextCLI hooks install")

    if not hs["is_git_repo"]:
        lines.append("hook: not a git repository")
    else:
        if hs["configured_hooks_path"]:
            lines.append(f"hook: git core.hooksPath={hs['configured_hooks_path']}")
            if hs["using_repo_hooks_dir"]:
                lines.append("hook: git is configured to use hooks/post-commit")
            elif hs["using_direct_git_hooks_dir"] and hs["direct_git_hook_exists"]:
                lines.append("hook: git is configured to use .git/hooks/post-commit")
            else:
                lines.append("hook: git uses a different hooks directory")
                lines.append("hook: fix with `contextCLI hooks wire --repo-hooks` or `contextCLI hooks wire --git-hooks`")
                add_issue("unexpected_hooks_path", "hook: git uses a different hooks directory", fix_command="contextCLI hooks wire --repo-hooks")
        elif hs["direct_git_hook_exists"]:
            lines.append("hook: .git/hooks/post-commit installed")
        else:
            lines.append("hook: git is not yet wired to run contextCLI automatically")
            lines.append("hook: fix with `contextCLI hooks wire --repo-hooks`")
            add_issue("hooks_not_wired", "hook: git is not yet wired to run contextCLI automatically", fix_command="contextCLI hooks wire --repo-hooks")

    ok = error_count == 0
    return {
        "ok": ok,
        "lines": lines,
        "issues": issues,
        "summary": {
            "errors": error_count,
            "warnings": warning_count,
            "issue_count": len(issues),
        },
    }
