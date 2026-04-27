from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .storage import (
    _checkpoints_dir,
    _read_json,
    _state_path,
    _pointers_path,
    _normalize_pointers,
)

def resume_prefix(repo: Path, cfg: Any, task_id: str) -> str:
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

def status_summary(state: Any) -> dict[str, Any]:
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
