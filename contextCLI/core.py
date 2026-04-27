from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

from .summarizer import cheap_reflect
from .storage import (
    _Lock,
    _atomic_write_text,
    _atomic_write_json,
    _read_json,
    _metadata_path,
    _metadata_template,
    _touch_metadata,
    _state_path,
    _events_path,
    _pointers_path,
    _current_context_path,
    _artifacts_dir,
    _checkpoints_dir,
    _backups_dir,
    _lock_path,
    _backup_file,
    RepoState,
    Checkpoint,
    load_metadata,
    load_current_context,
    load_state,
    _write_state,
    _write_current_context,
    _write_pointers,
    _normalize_pointers,
    _write_gitignore_defaults,
    _write_env_example,
    _load_recent_events,
    STORAGE_SCHEMA_VERSION,
)
from .config import (
    Config,
    _default_config,
    _with_overrides,
    _config_path,
    load_config,
    write_config,
    update_config,
    _render_config_toml,
)
from .hooks import (
    install_hooks,
    uninstall_hooks,
    hook_status,
    configure_hooks_path,
)
from .metrics import (
    events_count,
    latest_checkpoint_id,
    load_metrics_history,
    metrics_report,
    metrics_history_report,
    record_metrics_snapshot,
    metrics_markdown_report,
)
from .assembler import (
    resume_prefix,
    status_summary,
)
from .doctor import (
    doctor_report,
    doctor_exit_ok,
    validate_provider,
    _validate_checkpoint_snapshot,
)
from .utils import (
    _utc_now_iso,
    _git_branch,
    _git_patch_head,
    _git_diff_head,
    _redact_obj,
    _hash_text,
    load_dotenv,
    redact_text,
)

def ensure_repo_initialized(repo: Path) -> None:
    if not (repo / ".contextCLI").exists():
        raise SystemExit("Missing .contextCLI/. Run `contextCLI init` first.")

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
        text = cfg_path.read_text(encoding="utf-8")
        if "max_pointers" in text or "compaction_frequency" in text or "model_reflection" in text:
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
        if "enable_pointers" in cfg_path.read_text(encoding="utf-8") and enable_pointers:
            t2 = cfg_path.read_text(encoding="utf-8")
            if "enable_pointers = false" in t2:
                _backup_file(cfg_path, _backups_dir(repo))
                _atomic_write_text(cfg_path, t2.replace("enable_pointers = false", "enable_pointers = true"))

    if config_overrides:
        update_config(repo, config_overrides)

    for p, default in [
        (_metadata_path(repo), _metadata_template()),
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
        (_metadata_path(repo), _metadata_template()),
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

    _touch_metadata(repo)
    return {"ok": True, "actions": actions}

def migrate_repo(repo: Path) -> dict[str, Any]:
    actions: list[str] = []
    repair_repo(repo)
    meta = load_metadata(repo)
    version = int(meta.get("schema_version", 0) or 0)
    if version != STORAGE_SCHEMA_VERSION:
        _touch_metadata(repo)
        actions.append(f"updated metadata schema_version to {STORAGE_SCHEMA_VERSION}")
    else:
        _touch_metadata(repo)
        actions.append("refreshed .contextCLI/metadata.json")
    return {"ok": True, "actions": actions, "schema_version": STORAGE_SCHEMA_VERSION}

def export_state_bundle(repo: Path, cfg: Config, *, include_checkpoints: bool = True, redact: bool = True) -> dict[str, Any]:
    from .utils import redact_text
    working_state = _read_json(_state_path(repo), {})
    current_context = load_current_context(repo)
    pointers_md = _normalize_pointers(
        _pointers_path(repo).read_text(encoding="utf-8") if _pointers_path(repo).exists() else "",
        cfg.max_pointer_lines,
    )
    if redact:
        working_state = _redact_obj(working_state)
        current_context = _redact_obj(current_context)
        pointers_md = redact_text(pointers_md)
    bundle: dict[str, Any] = {
        "schema_version": 1,
        "storage_schema_version": STORAGE_SCHEMA_VERSION,
        "exported_at": _utc_now_iso(),
        "metadata": load_metadata(repo),
        "working_state": working_state,
        "current_context": current_context,
        "pointers_md": pointers_md,
        "config": cfg.__dict__,
    }
    if include_checkpoints:
        checkpoints: list[dict[str, Any]] = []
        for cp in sorted(_checkpoints_dir(repo).glob("*.json"), key=lambda p: p.name):
            snap = _read_json(cp, None)
            if isinstance(snap, dict):
                if redact:
                    snap = _redact_obj(snap)
                checkpoints.append(snap)
        bundle["checkpoints"] = checkpoints
    return bundle

def import_state_bundle(
    repo: Path,
    bundle: dict[str, Any],
    *,
    replace_config: bool = False,
    merge_checkpoints: bool = True,
) -> dict[str, Any]:
    actions: list[str] = []
    repair_repo(repo)
    ensure_repo_initialized(repo)

    schema_version = int(bundle.get("schema_version", 0) or 0)
    if schema_version != 1:
        raise SystemExit(f"Unsupported export schema_version `{schema_version}`.")
    storage_schema = int(bundle.get("storage_schema_version", STORAGE_SCHEMA_VERSION) or STORAGE_SCHEMA_VERSION)
    if storage_schema > STORAGE_SCHEMA_VERSION:
        raise SystemExit(
            f"Import bundle requires storage schema `{storage_schema}`, but this build supports `{STORAGE_SCHEMA_VERSION}`."
        )

    working_state = bundle.get("working_state")
    current_context = bundle.get("current_context")
    pointers_md = bundle.get("pointers_md")
    if not isinstance(working_state, dict):
        raise SystemExit("Import bundle is missing a valid `working_state` object.")
    if current_context is not None and not isinstance(current_context, dict):
        raise SystemExit("Import bundle has invalid `current_context`.")
    if pointers_md is not None and not isinstance(pointers_md, str):
        raise SystemExit("Import bundle has invalid `pointers_md`.")

    cfg = load_config(repo)
    state = RepoState.from_json(working_state)
    _write_state(repo, state, max_backup_files=cfg.max_backup_files)
    actions.append("updated .contextCLI/working_state.json")

    if isinstance(current_context, dict):
        _write_current_context(repo, current_context, max_backup_files=cfg.max_backup_files)
        actions.append("updated .contextCLI/current_context.json")

    if isinstance(pointers_md, str):
        _write_pointers(
            repo,
            _normalize_pointers(pointers_md, cfg.max_pointer_lines),
            max_backup_files=cfg.max_backup_files,
        )
        actions.append("updated .contextCLI/pointers.md")

    imported_metadata = bundle.get("metadata")
    if isinstance(imported_metadata, dict):
        created_at = str(imported_metadata.get("created_at", "") or _utc_now_iso())
        _atomic_write_json(
            _metadata_path(repo),
            {
                "schema_version": STORAGE_SCHEMA_VERSION,
                "created_at": created_at,
                "updated_at": _utc_now_iso(),
            },
        )
        actions.append("updated .contextCLI/metadata.json")
    else:
        _touch_metadata(repo)

    imported_config = bundle.get("config")
    if replace_config and isinstance(imported_config, dict):
        allowed = {k: v for k, v in imported_config.items() if k in Config.__dataclass_fields__}
        new_cfg = _with_overrides(cfg, allowed)
        write_config(repo, new_cfg)
        actions.append("updated .contextCLI/config.toml")
        cfg = new_cfg

    if merge_checkpoints:
        checkpoints = bundle.get("checkpoints")
        if checkpoints is not None and not isinstance(checkpoints, list):
            raise SystemExit("Import bundle has invalid `checkpoints`.")
        imported = 0
        for snap in checkpoints or []:
            ok, msg = _validate_checkpoint_snapshot(snap)
            if not ok:
                raise SystemExit(f"Import bundle has invalid checkpoint: {msg}.")
            task_id = str(snap.get("task_id", "")).strip()
            cp_path = _checkpoints_dir(repo) / f"{task_id}.json"
            if cp_path.exists():
                continue
            _atomic_write_json(cp_path, snap)
            imported += 1
        if imported:
            actions.append(f"imported {imported} checkpoints")

    return {"ok": True, "actions": actions}

def _record_event(
    repo: Path,
    kind: str,
    payload: dict[str, Any],
    *,
    max_events_bytes: int,
    redact: bool,
) -> dict[str, Any]:
    from .storage import _append_jsonl
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

def _compaction(repo: Path, cfg: Config, state: RepoState, reason: str) -> dict[str, Any]:
    recent = _load_recent_events(repo, limit=20)
    current_state = _read_json(_state_path(repo), {})
    current_pointers = _pointers_path(repo).read_text(encoding="utf-8") if _pointers_path(repo).exists() else ""

    api_key = os.environ.get(cfg.api_key_env, "")
    if not api_key:
        print(
            f"[contextCLI] WARNING: env var {cfg.api_key_env!r} is not set — "
            "pointer reflection skipped. Set the variable and commit again.",
            file=sys.stderr,
        )
        return {"reason": reason, "reflected": False, "error": f"{cfg.api_key_env} not set"}

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

    if not reflection.get("_reflected"):
        ctx = reflection.get("current_context") or {}
        risks = ctx.get("risks") if isinstance(ctx, dict) else []
        err_detail = "; ".join(str(r) for r in risks) if risks else "unknown error"
        print(
            f"[contextCLI] WARNING: reflection failed ({err_detail}). "
            "Check your api_provider/api_key_env/summarizer_model in .contextCLI/config.toml.",
            file=sys.stderr,
        )

    new_state = reflection.get("working_state")
    if isinstance(new_state, dict):
        merged = state.to_json()
        if isinstance(new_state.get("open_items"), list):
            merged["open_items"] = list(new_state.get("open_items") or [])
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
