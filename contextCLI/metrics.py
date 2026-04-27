from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .utils import _utc_now_iso
from .storage import (
    _events_path,
    _checkpoints_dir,
    _read_json,
    _pointers_path,
    _state_path,
    _metrics_history_path,
    _atomic_write_text,
    _load_recent_events,
)
from .assembler import resume_prefix

def _estimate_tokens(text: str) -> int:
    stripped = text.strip()
    if not stripped:
        return 0
    return max(1, (len(stripped) + 3) // 4)

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

def metrics_report(repo: Path, cfg: Any, *, recent_event_limit: int = 20, task_id: str = "latest") -> dict[str, Any]:
    resume_text = resume_prefix(repo, cfg, task_id=task_id)
    recent_events = _load_recent_events(repo, limit=recent_event_limit)
    raw_recent_text = json.dumps(recent_events, indent=2, sort_keys=True) if recent_events else ""
    pointers_text = _pointers_path(repo).read_text(encoding="utf-8") if _pointers_path(repo).exists() else ""
    pointer_lines = [ln for ln in pointers_text.splitlines() if ln.startswith("- [")]
    checkpoints = sorted(_checkpoints_dir(repo).glob("*.json"), key=lambda p: p.name)

    distilled_tokens = _estimate_tokens(resume_text)
    raw_recent_tokens = _estimate_tokens(raw_recent_text)
    savings_tokens = max(0, raw_recent_tokens - distilled_tokens)
    savings_pct = 0.0 if raw_recent_tokens <= 0 else round((savings_tokens / raw_recent_tokens) * 100.0, 1)
    is_smaller = raw_recent_tokens <= 0 or distilled_tokens <= raw_recent_tokens
    ratio = 0.0 if raw_recent_tokens <= 0 else round(distilled_tokens / raw_recent_tokens, 3)
    recommendation = (
        "No recent events yet; token-savings comparison becomes meaningful after more activity."
        if raw_recent_tokens <= 0
        else (
            "Distilled resume context is smaller than the raw recent-event baseline."
            if is_smaller
            else "Distilled resume context is larger than the raw recent-event baseline; refresh compaction or reduce pointer volume."
        )
    )

    return {
        "assumptions": {
            "token_estimate_method": "chars_div_4",
            "recent_event_limit": recent_event_limit,
            "comparison": "distilled resume context vs raw recent events JSON",
        },
        "counts": {
            "events": events_count(repo),
            "recent_events_used": len(recent_events),
            "checkpoints": len(checkpoints),
            "pointer_lines": len(pointer_lines),
        },
        "sizes": {
            "resume_chars": len(resume_text),
            "resume_tokens_est": distilled_tokens,
            "raw_recent_events_chars": len(raw_recent_text),
            "raw_recent_events_tokens_est": raw_recent_tokens,
            "working_state_bytes": _state_path(repo).stat().st_size if _state_path(repo).exists() else 0,
            "pointers_bytes": _pointers_path(repo).stat().st_size if _pointers_path(repo).exists() else 0,
            "events_bytes": _events_path(repo).stat().st_size if _events_path(repo).exists() else 0,
        },
        "savings_estimate": {
            "tokens_saved_vs_raw_recent_events": savings_tokens,
            "percent_saved_vs_raw_recent_events": savings_pct,
        },
        "efficiency": {
            "resume_smaller_than_raw_recent_events": is_smaller,
            "resume_to_raw_recent_events_ratio": ratio,
            "recommendation": recommendation,
        },
        "latest_checkpoint_id": latest_checkpoint_id(repo),
    }

def record_metrics_snapshot(repo: Path, report: dict[str, Any], *, keep_last: int = 50) -> dict[str, Any]:
    snapshot = {
        "ts": _utc_now_iso(),
        "counts": report.get("counts", {}),
        "sizes": report.get("sizes", {}),
        "savings_estimate": report.get("savings_estimate", {}),
        "efficiency": report.get("efficiency", {}),
    }
    path = _metrics_history_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[str] = []
    if path.exists():
        try:
            existing = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except OSError:
            existing = []
    existing.append(json.dumps(snapshot, sort_keys=True))
    if keep_last > 0:
        existing = existing[-keep_last:]
    _atomic_write_text(path, "\n".join(existing) + ("\n" if existing else ""))
    return snapshot

def load_metrics_history(repo: Path, *, limit: int = 10) -> list[dict[str, Any]]:
    path = _metrics_history_path(repo)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return []
    for ln in lines[-limit:]:
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out

def metrics_history_report(repo: Path, *, limit: int = 10) -> dict[str, Any]:
    history = load_metrics_history(repo, limit=limit)
    latest = history[-1] if history else {}
    previous = history[-2] if len(history) >= 2 else {}

    latest_resume = int(((latest.get("sizes") or {}).get("resume_tokens_est", 0)) or 0) if isinstance(latest, dict) else 0
    latest_saved = int(((latest.get("savings_estimate") or {}).get("tokens_saved_vs_raw_recent_events", 0)) or 0) if isinstance(latest, dict) else 0
    prev_resume = int(((previous.get("sizes") or {}).get("resume_tokens_est", 0)) or 0) if isinstance(previous, dict) else 0
    prev_saved = int(((previous.get("savings_estimate") or {}).get("tokens_saved_vs_raw_recent_events", 0)) or 0) if isinstance(previous, dict) else 0
    has_baseline = len(history) >= 2

    if not has_baseline:
        trend = "baseline_only"
        recommendation = "Record another snapshot later to see whether context efficiency is improving or regressing."
        delta_resume = 0
        delta_saved = 0
    else:
        delta_resume = latest_resume - prev_resume
        delta_saved = latest_saved - prev_saved
        if delta_saved > 0 or (delta_saved == 0 and delta_resume < 0):
            trend = "improving"
            recommendation = "Recent snapshot indicates better token efficiency than the previous one."
        elif delta_saved < 0 or (delta_saved == 0 and delta_resume > 0):
            trend = "regressing"
            recommendation = "Recent snapshot indicates worse token efficiency than the previous one."
        else:
            trend = "stable"
            recommendation = "Recent snapshot is materially unchanged from the previous one."

    return {
        "count": len(history),
        "entries": history,
        "delta": {
            "resume_tokens_est": delta_resume,
            "tokens_saved_vs_raw_recent_events": delta_saved,
        },
        "trend": {
            "has_baseline": has_baseline,
            "direction": trend,
            "recommendation": recommendation,
        },
    }

def metrics_markdown_report(
    repo: Path,
    cfg: Any,
    *,
    recent_event_limit: int = 20,
    history_limit: int = 10,
    task_id: str = "latest",
) -> str:
    current = metrics_report(repo, cfg, recent_event_limit=recent_event_limit, task_id=task_id)
    history = metrics_history_report(repo, limit=history_limit)
    lines: list[str] = []
    lines.append("# contextCLI Metrics Report")
    lines.append("")
    lines.append(f"- Repo: {repo}")
    lines.append(f"- Comparison: {current['assumptions']['comparison']}")
    lines.append(f"- Token estimate: {current['assumptions']['token_estimate_method']}")
    lines.append(f"- Recent event limit: {current['assumptions']['recent_event_limit']}")
    lines.append("")
    lines.append("## Current")
    lines.append("")
    lines.append(f"- Resume tokens est: {current['sizes']['resume_tokens_est']}")
    lines.append(f"- Raw recent events tokens est: {current['sizes']['raw_recent_events_tokens_est']}")
    lines.append(f"- Tokens saved vs raw recent events: {current['savings_estimate']['tokens_saved_vs_raw_recent_events']}")
    lines.append(f"- Percent saved vs raw recent events: {current['savings_estimate']['percent_saved_vs_raw_recent_events']}%")
    lines.append(f"- Resume/raw ratio: {current['efficiency']['resume_to_raw_recent_events_ratio']}")
    lines.append(f"- Resume smaller than raw baseline: {current['efficiency']['resume_smaller_than_raw_recent_events']}")
    lines.append(f"- Recommendation: {current['efficiency']['recommendation']}")
    lines.append("")
    lines.append("## Counts")
    lines.append("")
    lines.append(f"- Events: {current['counts']['events']}")
    lines.append(f"- Recent events used: {current['counts']['recent_events_used']}")
    lines.append(f"- Checkpoints: {current['counts']['checkpoints']}")
    lines.append(f"- Pointer lines: {current['counts']['pointer_lines']}")
    lines.append("")
    lines.append("## Trend")
    lines.append("")
    lines.append(f"- History count: {history['count']}")
    lines.append(f"- Direction: {history['trend']['direction']}")
    lines.append(f"- Delta resume tokens est: {history['delta']['resume_tokens_est']}")
    lines.append(f"- Delta tokens saved vs raw recent events: {history['delta']['tokens_saved_vs_raw_recent_events']}")
    lines.append(f"- Recommendation: {history['trend']['recommendation']}")
    if history["entries"]:
        lines.append("")
        lines.append("## Recent Snapshots")
        lines.append("")
        for entry in history["entries"]:
            ts = str(entry.get("ts", ""))
            sizes = entry.get("sizes") or {}
            savings = entry.get("savings_estimate") or {}
            efficiency = entry.get("efficiency") or {}
            lines.append(
                f"- {ts}: resume={sizes.get('resume_tokens_est', 0)}, "
                f"saved={savings.get('tokens_saved_vs_raw_recent_events', 0)}, "
                f"smaller={efficiency.get('resume_smaller_than_raw_recent_events', False)}"
            )
    return "\n".join(lines).rstrip() + "\n"
