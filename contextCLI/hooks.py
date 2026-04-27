from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .utils import _sh, _git_hooks_path, _make_executable
from .storage import _atomic_write_text

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
    git_hooks_dir = git_dir / "hooks"
    direct_git_hook = git_dir / "hooks" / "post-commit"
    configured_hooks_path = _git_hooks_path(repo).strip()

    if configured_hooks_path:
        hooks_path_resolved = (repo / configured_hooks_path).resolve()
    else:
        hooks_path_resolved = None

    using_repo_hooks_dir = hooks_path_resolved == hooks_dir.resolve() if hooks_path_resolved else False
    using_direct_git_hooks_dir = hooks_path_resolved == git_hooks_dir.resolve() if hooks_path_resolved else False

    return {
        "is_git_repo": git_dir.exists(),
        "template_hook_exists": template_hook.exists(),
        "claude_hook_exists": claude_hook.exists(),
        "direct_git_hook_exists": direct_git_hook.exists(),
        "configured_hooks_path": configured_hooks_path,
        "using_repo_hooks_dir": using_repo_hooks_dir,
        "using_direct_git_hooks_dir": using_direct_git_hooks_dir,
        "template_hook_path": str(template_hook),
        "claude_hook_path": str(claude_hook),
        "direct_git_hook_path": str(direct_git_hook),
    }

def configure_hooks_path(repo: Path, *, use_repo_hooks_dir: bool) -> dict[str, Any]:
    if not (repo / ".git").exists():
        raise SystemExit("Cannot configure git hooks path: target is not a git repository.")
    value = "hooks" if use_repo_hooks_dir else ".git/hooks"
    try:
        p = subprocess.run(
            ["git", "config", "core.hooksPath", value],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as e:
        raise SystemExit("Cannot configure git hooks path: git is not installed or not on PATH.") from e
    if p.returncode != 0:
        detail = (p.stderr or p.stdout or "").strip()
        msg = "Cannot configure git hooks path."
        if detail:
            msg += f" {detail}"
        raise SystemExit(msg)
    return {"ok": True, "hooks_path": value}
