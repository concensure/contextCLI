from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Optional

import typer

from .core import (
    Checkpoint,
    RepoState,
    load_dotenv,
    ensure_repo_initialized,
    events_count,
    init_repo,
    install_hooks,
    hook_status,
    load_config,
    load_current_context,
    latest_checkpoint_id,
    load_state,
    resume_prefix,
    run_auto_compaction,
    run_update_context,
    status_summary,
    doctor_report,
    uninstall_hooks,
    update_config,
    repair_repo,
    export_state_bundle,
    import_state_bundle,
)

app = typer.Typer(add_completion=False, invoke_without_command=True, no_args_is_help=False)
hooks_app = typer.Typer(help="Install or remove contextCLI hook templates.")
app.add_typer(hooks_app, name="hooks")


def _repo_opt(repo: Optional[Path]) -> Path:
    return repo or Path.cwd()


def _config_for_repo(repo: Path, *, no_dotenv: bool = False):
    cfg = load_config(repo)
    if cfg.load_env_file and not no_dotenv:
        load_dotenv(repo, {cfg.api_key_env})
    return cfg


@app.command("init")
def cmd_init(
    enable_pointers: Annotated[bool, typer.Option("--enable-pointers")] = False,
    install_git_hook: Annotated[bool, typer.Option("--install-git-hook")] = False,
    provider: Annotated[Optional[str], typer.Option("--provider", help="Provider name, for example openrouter or ollama.")] = None,
    model: Annotated[Optional[str], typer.Option("--model", help="Summarizer model name.")] = None,
    api_key_env: Annotated[Optional[str], typer.Option("--api-key-env", help="Environment variable that contains the API key.")] = None,
    api_base_url: Annotated[Optional[str], typer.Option("--api-base-url", help="Provider API base URL.")] = None,
    repo: Annotated[Optional[Path], typer.Option("--repo", exists=True, file_okay=False, dir_okay=True)] = None,
) -> None:
    """Create `.contextCLI/` and write default config + example git hook."""
    r = _repo_opt(repo)
    init_repo(
        r,
        enable_pointers=enable_pointers,
        install_git_hook=install_git_hook,
        config_overrides={
            "api_provider": provider,
            "summarizer_model": model,
            "api_key_env": api_key_env,
            "api_base_url": api_base_url,
        },
    )
    typer.echo("Initialized .contextCLI/")


@app.command("update-context")
def cmd_update_context(
    instruction: Annotated[str, typer.Option("--instruction", "-i")],
    force: Annotated[bool, typer.Option("--force")] = False,
    no_dotenv: Annotated[bool, typer.Option("--no-dotenv")] = False,
    repo: Annotated[Optional[Path], typer.Option("--repo", exists=True, file_okay=False, dir_okay=True)] = None,
) -> None:
    """Append an event and (optionally) run cheap-model reflection to update state/pointers."""
    r = _repo_opt(repo)
    ensure_repo_initialized(r)
    cfg = _config_for_repo(r, no_dotenv=no_dotenv)
    state = load_state(r)
    out = run_update_context(r, cfg, state, instruction=instruction, force=force)
    typer.echo(json.dumps(out, indent=2, sort_keys=True))


@app.command("auto-compaction")
def cmd_auto_compaction(
    force: Annotated[bool, typer.Option("--force")] = False,
    no_dotenv: Annotated[bool, typer.Option("--no-dotenv")] = False,
    repo: Annotated[Optional[Path], typer.Option("--repo", exists=True, file_okay=False, dir_okay=True)] = None,
) -> None:
    """Process recent events and update distilled files (used by hooks)."""
    r = _repo_opt(repo)
    ensure_repo_initialized(r)
    cfg = _config_for_repo(r, no_dotenv=no_dotenv)
    state = load_state(r)
    out = run_auto_compaction(r, cfg, state, force=force)
    typer.echo(json.dumps(out, indent=2, sort_keys=True))


@app.command("checkpoint")
def cmd_checkpoint(
    note: Annotated[Optional[str], typer.Option("--note")] = None,
    no_dotenv: Annotated[bool, typer.Option("--no-dotenv")] = False,
    repo: Annotated[Optional[Path], typer.Option("--repo", exists=True, file_okay=False, dir_okay=True)] = None,
) -> None:
    """Save a resumable snapshot under `.contextCLI/checkpoints/`."""
    r = _repo_opt(repo)
    ensure_repo_initialized(r)
    cfg = _config_for_repo(r, no_dotenv=no_dotenv)
    state = load_state(r)
    cp: Checkpoint = Checkpoint.create(r, cfg, state, note=note)
    typer.echo(cp.path.name)


@app.command("resume")
def cmd_resume(
    task_id: str,
    out: Annotated[Optional[Path], typer.Option("--out", dir_okay=False, writable=True)] = None,
    no_dotenv: Annotated[bool, typer.Option("--no-dotenv")] = False,
    repo: Annotated[Optional[Path], typer.Option("--repo", exists=True, file_okay=False, dir_okay=True)] = None,
) -> None:
    """Print (or write) a clean prefix: distilled state + pointers + optional checkpoint note."""
    r = _repo_opt(repo)
    ensure_repo_initialized(r)
    cfg = _config_for_repo(r, no_dotenv=no_dotenv)
    prefix = resume_prefix(r, cfg, task_id=task_id)
    if out is not None:
        out.write_text(prefix, encoding="utf-8")
        typer.echo(str(out))
    else:
        typer.echo(prefix)


@app.command("status")
def cmd_status(
    json_output: Annotated[bool, typer.Option("--json")] = False,
    no_dotenv: Annotated[bool, typer.Option("--no-dotenv")] = False,
    repo: Annotated[Optional[Path], typer.Option("--repo", exists=True, file_okay=False, dir_okay=True)] = None,
) -> None:
    """Show summary of current state and open items."""
    r = _repo_opt(repo)
    ensure_repo_initialized(r)
    _config_for_repo(r, no_dotenv=no_dotenv)
    state: RepoState = load_state(r)
    s = status_summary(state)
    s["events"] = events_count(r)
    s["current_context"] = load_current_context(r)
    s["latest_checkpoint_id"] = latest_checkpoint_id(r)
    if json_output:
        typer.echo(json.dumps(s, indent=2, sort_keys=True))
    else:
        summary = str((s["current_context"] or {}).get("summary", "")).strip()
        extra = f"repo: {str(r.resolve())}\nstate_dir: {str((r / '.contextCLI').resolve())}\n"
        extra += f"\nevents: {s['events']}"
        if s["latest_checkpoint_id"]:
            extra += f"\nlatest_checkpoint_id: {s['latest_checkpoint_id']}"
        if summary:
            extra += f"\nsummary: {summary}"
        typer.echo(extra.strip() + "\n\n" + s["text"])


def main() -> None:
    app()


@app.callback()
def _cb(
    ctx: typer.Context,
    version: Annotated[bool, typer.Option("--version", help="Print version and exit.")] = False,
) -> None:
    if version:
        from . import __version__

        typer.echo(__version__)
        raise typer.Exit(0)
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)


@app.command("config")
def cmd_config(
    no_dotenv: Annotated[bool, typer.Option("--no-dotenv")] = False,
    repo: Annotated[Optional[Path], typer.Option("--repo", exists=True, file_okay=False, dir_okay=True)] = None,
) -> None:
    """Print effective config (after back-compat normalization)."""
    r = _repo_opt(repo)
    ensure_repo_initialized(r)
    cfg = _config_for_repo(r, no_dotenv=no_dotenv)
    typer.echo(json.dumps(cfg.__dict__, indent=2, sort_keys=True))


@app.command("configure")
def cmd_configure(
    provider: Annotated[Optional[str], typer.Option("--provider", help="Provider name.")] = None,
    model: Annotated[Optional[str], typer.Option("--model", help="Summarizer model name.")] = None,
    api_key_env: Annotated[Optional[str], typer.Option("--api-key-env", help="Environment variable containing the API key.")] = None,
    api_base_url: Annotated[Optional[str], typer.Option("--api-base-url", help="Provider API base URL.")] = None,
    enable_pointers: Annotated[Optional[bool], typer.Option("--enable-pointers/--disable-pointers")] = None,
    load_env_file: Annotated[Optional[bool], typer.Option("--load-env-file/--no-load-env-file")] = None,
    capture_git_patch: Annotated[Optional[bool], typer.Option("--capture-git-patch/--no-capture-git-patch")] = None,
    redact_event_secrets: Annotated[Optional[bool], typer.Option("--redact-event-secrets/--no-redact-event-secrets")] = None,
    repo: Annotated[Optional[Path], typer.Option("--repo", exists=True, file_okay=False, dir_okay=True)] = None,
) -> None:
    """Update provider/model config without storing any API key value."""
    r = _repo_opt(repo)
    ensure_repo_initialized(r)
    cfg = update_config(
        r,
        {
            "api_provider": provider,
            "summarizer_model": model,
            "api_key_env": api_key_env,
            "api_base_url": api_base_url,
            "enable_pointers": enable_pointers,
            "load_env_file": load_env_file,
            "capture_git_patch": capture_git_patch,
            "redact_event_secrets": redact_event_secrets,
        },
    )
    typer.echo(json.dumps(cfg.__dict__, indent=2, sort_keys=True))


@app.command("doctor")
def cmd_doctor(
    repo: Annotated[Optional[Path], typer.Option("--repo", exists=True, file_okay=False, dir_okay=True)] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    no_dotenv: Annotated[bool, typer.Option("--no-dotenv")] = False,
) -> None:
    """Validate `.contextCLI/` and print actionable diagnostics."""
    r = _repo_opt(repo)
    ensure_repo_initialized(r)
    _config_for_repo(r, no_dotenv=no_dotenv)
    rep = doctor_report(r)
    if json_output:
        typer.echo(json.dumps(rep, indent=2, sort_keys=True))
    else:
        for ln in rep["lines"]:
            typer.echo(ln)


@app.command("repair")
def cmd_repair(
    repo: Annotated[Optional[Path], typer.Option("--repo", exists=True, file_okay=False, dir_okay=True)] = None,
    install_git_hook: Annotated[bool, typer.Option("--install-git-hook")] = False,
    clear_stale_lock: Annotated[bool, typer.Option("--clear-stale-lock")] = False,
) -> None:
    """Recreate missing contextCLI files and templates without resetting normal state."""
    r = _repo_opt(repo)
    out = repair_repo(r, install_git_hook=install_git_hook, clear_stale_lock=clear_stale_lock)
    typer.echo(json.dumps(out, indent=2, sort_keys=True))


@app.command("export-state")
def cmd_export_state(
    out: Annotated[Path, typer.Option("--out", dir_okay=False, writable=True)],
    include_checkpoints: Annotated[bool, typer.Option("--checkpoints/--no-checkpoints")] = True,
    no_dotenv: Annotated[bool, typer.Option("--no-dotenv")] = False,
    repo: Annotated[Optional[Path], typer.Option("--repo", exists=True, file_okay=False, dir_okay=True)] = None,
) -> None:
    """Export distilled context into a portable JSON bundle."""
    r = _repo_opt(repo)
    ensure_repo_initialized(r)
    cfg = _config_for_repo(r, no_dotenv=no_dotenv)
    bundle = export_state_bundle(r, cfg, include_checkpoints=include_checkpoints)
    out.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    typer.echo(str(out))


@app.command("import-state")
def cmd_import_state(
    bundle_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    replace_config: Annotated[bool, typer.Option("--replace-config/--keep-config")] = False,
    merge_checkpoints: Annotated[bool, typer.Option("--merge-checkpoints/--no-merge-checkpoints")] = True,
    repo: Annotated[Optional[Path], typer.Option("--repo", exists=True, file_okay=False, dir_okay=True)] = None,
) -> None:
    """Import a portable JSON bundle into a repo's `.contextCLI` state."""
    r = _repo_opt(repo)
    data = json.loads(bundle_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit("Import bundle must be a JSON object.")
    out = import_state_bundle(
        r,
        data,
        replace_config=replace_config,
        merge_checkpoints=merge_checkpoints,
    )
    typer.echo(json.dumps(out, indent=2, sort_keys=True))


@hooks_app.command("install")
def cmd_hooks_install(
    git_hook: Annotated[bool, typer.Option("--git-hook", help="Also install into .git/hooks/post-commit.")] = False,
    repo: Annotated[Optional[Path], typer.Option("--repo", exists=True, file_okay=False, dir_okay=True)] = None,
) -> None:
    """Install the example post-commit hook."""
    r = _repo_opt(repo)
    ensure_repo_initialized(r)
    for p in install_hooks(r, git_hook=git_hook):
        typer.echo(f"installed: {p}")


@hooks_app.command("uninstall")
def cmd_hooks_uninstall(
    git_hook: Annotated[bool, typer.Option("--git-hook", help="Also remove from .git/hooks/post-commit if contextCLI owns it.")] = False,
    repo: Annotated[Optional[Path], typer.Option("--repo", exists=True, file_okay=False, dir_okay=True)] = None,
) -> None:
    """Remove contextCLI post-commit hooks created by this tool."""
    r = _repo_opt(repo)
    ensure_repo_initialized(r)
    removed = uninstall_hooks(r, git_hook=git_hook)
    if not removed:
        typer.echo("no contextCLI hooks removed")
        return
    for p in removed:
        typer.echo(f"removed: {p}")


@hooks_app.command("status")
def cmd_hooks_status(
    repo: Annotated[Optional[Path], typer.Option("--repo", exists=True, file_okay=False, dir_okay=True)] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show whether Git is configured to run contextCLI hooks."""
    r = _repo_opt(repo)
    ensure_repo_initialized(r)
    hs = hook_status(r)
    if json_output:
        typer.echo(json.dumps(hs, indent=2, sort_keys=True))
        return
    typer.echo(f"repo: {r}")
    typer.echo(f"template_hook_exists: {hs['template_hook_exists']}")
    typer.echo(f"claude_hook_exists: {hs['claude_hook_exists']}")
    if not hs["is_git_repo"]:
        typer.echo("git: not a repository")
        return
    if hs["configured_hooks_path"]:
        typer.echo(f"git core.hooksPath: {hs['configured_hooks_path']}")
        typer.echo(f"using_repo_hooks_dir: {hs['using_repo_hooks_dir']}")
    else:
        typer.echo(f"direct_git_hook_exists: {hs['direct_git_hook_exists']}")
