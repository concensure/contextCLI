from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*([^\s'\"`]+)"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)([A-Za-z0-9._~+/=-]+)"),
    re.compile(r"(sk-[A-Za-z0-9_-]{16,})"),
    re.compile(r"(-----BEGIN [A-Z ]*PRIVATE" + r" KEY-----.*?-----END [A-Z ]*PRIVATE" + r" KEY-----)", re.S),
]

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

def _make_executable(path: Path) -> None:
    try:
        mode = path.stat().st_mode
        path.chmod(mode | 0o111)
    except OSError:
        pass

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

def redact_text(text: str) -> str:
    redacted = text
    for pat in _SECRET_PATTERNS:
        def repl(match: re.Match[str]) -> str:
            if len(match.groups()) >= 2:
                return f"{match.group(1)}[REDACTED]"
            return "[REDACTED]"

        redacted = pat.sub(repl, redacted)
    return redacted

def _has_secret_pattern(text: str) -> bool:
    return any(pat.search(text) for pat in _SECRET_PATTERNS)

def _find_secret_paths(obj: Any, prefix: str, *, limit: int = 5) -> list[str]:
    hits: list[str] = []

    def walk(value: Any, path: str) -> None:
        if len(hits) >= limit:
            return
        if isinstance(value, str):
            if _has_secret_pattern(value):
                hits.append(path)
            return
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, f"{path}.{key}")
                if len(hits) >= limit:
                    return
            return
        if isinstance(value, list):
            for idx, child in enumerate(value):
                walk(child, f"{path}[{idx}]")
                if len(hits) >= limit:
                    return

    walk(obj, prefix)
    return hits

def _redact_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {k: _redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_obj(v) for v in obj]
    return obj

def _hash_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]
