from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .utils import _utc_now_iso

STORAGE_SCHEMA_VERSION = 1

class _Lock:
    def __init__(self, path: Path, stale_after_s: int = 600) -> None:
        self.path = path
        self.stale_after_s = stale_after_s

    def __enter__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        if self.path.exists():
            try:
                age = now - self.path.stat().st_mtime
                if age > self.stale_after_s:
                    self.path.unlink(missing_ok=True)
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

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass

def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)

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

def _metadata_path(repo: Path) -> Path:
    return repo / ".contextCLI" / "metadata.json"

def _artifacts_dir(repo: Path) -> Path:
    return repo / ".contextCLI" / "artifacts"

def _metrics_history_path(repo: Path) -> Path:
    return _artifacts_dir(repo) / "metrics_history.jsonl"

def _checkpoints_dir(repo: Path) -> Path:
    return repo / ".contextCLI" / "checkpoints"

def _backups_dir(repo: Path) -> Path:
    return repo / ".contextCLI" / "artifacts" / "backups"

def _lock_path(repo: Path) -> Path:
    return repo / ".contextCLI" / ".lock"

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

def load_metadata(repo: Path) -> dict[str, Any]:
    obj = _read_json(_metadata_path(repo), {})
    return obj if isinstance(obj, dict) else {}

def _metadata_template() -> dict[str, Any]:
    now = _utc_now_iso()
    return {
        "schema_version": STORAGE_SCHEMA_VERSION,
        "created_at": now,
        "updated_at": now,
    }

def _touch_metadata(repo: Path) -> None:
    current = load_metadata(repo)
    created_at = str(current.get("created_at", "") or _utc_now_iso())
    obj = {
        "schema_version": STORAGE_SCHEMA_VERSION,
        "created_at": created_at,
        "updated_at": _utc_now_iso(),
    }
    _atomic_write_json(_metadata_path(repo), obj)

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
    raw = [ln.rstrip("\n") for ln in (md or "").splitlines()]
    body = [ln.strip() for ln in raw if ln.strip()]

    out: list[str] = ["# Pointers"]
    for ln in body:
        if ln.startswith("#"):
            continue
        if not ln.startswith("- ["):
            continue
        if "](" not in ln or (") \u2014 " not in ln and ") - " not in ln and ") -- " not in ln):
            continue
        if len(ln) > 150:
            ln = ln[:147] + "..."
        out.append(ln)
        if len(out) - 1 >= max_lines:
            break

    return "\n".join(out) + "\n"

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

@dataclass(frozen=True)
class Checkpoint:
    path: Path
    task_id: str

    @staticmethod
    def create(repo: Path, cfg: Any, state: RepoState, note: Optional[str]) -> "Checkpoint":
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
