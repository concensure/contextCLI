from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Optional

import typer

from .utils import load_dotenv
from .storage import (
    Checkpoint,
    RepoState,
    load_state,
    load_current_context,
)
from .config import (
    load_config,
    update_config,
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
from .hooks import (
    install_hooks,
    uninstall_hooks,
    hook_status,
    configure_hooks_path,
)
from .assembler import (
    resume_prefix,
    status_summary,
)
from .doctor import (
    doctor_report,
    doctor_exit_ok,
    validate_provider,
)
from .core import (
    ensure_repo_initialized,
    init_repo,
    run_auto_compaction,
    run_update_context,
    repair_repo,
    export_state_bundle,
    import_state_bundle,
    migrate_repo,
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


@app.command("metrics")
def cmd_metrics(
    json_output: Annotated[bool, typer.Option("--json")] = False,
    recent_event_limit: Annotated[int, typer.Option("--recent-event-limit", min=1, max=500)] = 20,
    task_id: Annotated[str, typer.Option("--task-id")] = "latest",
    record: Annotated[bool, typer.Option("--record/--no-record")] = False,
    no_dotenv: Annotated[bool, typer.Option("--no-dotenv")] = False,
    repo: Annotated[Optional[Path], typer.Option("--repo", exists=True, file_okay=False, dir_okay=True)] = None,
) -> None:
    """Show objective context size metrics and an estimated token-savings comparison."""
    r = _repo_opt(repo)
    ensure_repo_initialized(r)
    cfg = _config_for_repo(r, no_dotenv=no_dotenv)
    rep = metrics_report(r, cfg, recent_event_limit=recent_event_limit, task_id=task_id)
    if record:
        rep["recorded_snapshot"] = record_metrics_snapshot(r, rep)
    if json_output:
        typer.echo(json.dumps(rep, indent=2, sort_keys=True))
        return
    typer.echo(f"repo: {r}")
    typer.echo(f"comparison: {rep['assumptions']['comparison']}")
    typer.echo(f"assumption: ~1 token per 4 chars, recent_event_limit={rep['assumptions']['recent_event_limit']}")
    typer.echo("")
    typer.echo(f"resume_tokens_est: {rep['sizes']['resume_tokens_est']}")
    typer.echo(f"raw_recent_events_tokens_est: {rep['sizes']['raw_recent_events_tokens_est']}")
    typer.echo(f"tokens_saved_vs_raw_recent_events: {rep['savings_estimate']['tokens_saved_vs_raw_recent_events']}")
    typer.echo(f"percent_saved_vs_raw_recent_events: {rep['savings_estimate']['percent_saved_vs_raw_recent_events']}%")
    typer.echo(f"resume_to_raw_recent_events_ratio: {rep['efficiency']['resume_to_raw_recent_events_ratio']}")
    typer.echo(f"resume_smaller_than_raw_recent_events: {rep['efficiency']['resume_smaller_than_raw_recent_events']}")
    typer.echo(f"recommendation: {rep['efficiency']['recommendation']}")
    typer.echo("")
    typer.echo(f"events: {rep['counts']['events']}")
    typer.echo(f"recent_events_used: {rep['counts']['recent_events_used']}")
    typer.echo(f"checkpoints: {rep['counts']['checkpoints']}")
    typer.echo(f"pointer_lines: {rep['counts']['pointer_lines']}")
    if record:
        typer.echo("metrics_snapshot_recorded: true")


@app.command("metrics-history")
def cmd_metrics_history(
    json_output: Annotated[bool, typer.Option("--json")] = False,
    limit: Annotated[int, typer.Option("--limit", min=1, max=200)] = 10,
    repo: Annotated[Optional[Path], typer.Option("--repo", exists=True, file_okay=False, dir_okay=True)] = None,
) -> None:
    """Show recent recorded metrics snapshots and the delta from the previous snapshot."""
    r = _repo_opt(repo)
    ensure_repo_initialized(r)
    rep = metrics_history_report(r, limit=limit)
    if json_output:
        typer.echo(json.dumps(rep, indent=2, sort_keys=True))
        return
    typer.echo(f"repo: {r}")
    typer.echo(f"history_count: {rep['count']}")
    typer.echo(f"trend_direction: {rep['trend']['direction']}")
    typer.echo(f"delta_resume_tokens_est: {rep['delta']['resume_tokens_est']}")
    typer.echo(f"delta_tokens_saved_vs_raw_recent_events: {rep['delta']['tokens_saved_vs_raw_recent_events']}")
    typer.echo(f"recommendation: {rep['trend']['recommendation']}")
    if not rep["entries"]:
        typer.echo("No metrics snapshots recorded yet.")
        return
    typer.echo("")
    for entry in rep["entries"]:
        ts = str(entry.get("ts", ""))
        sizes = entry.get("sizes") or {}
        savings = entry.get("savings_estimate") or {}
        efficiency = entry.get("efficiency") or {}
        typer.echo(
            f"{ts} | resume_tokens_est={sizes.get('resume_tokens_est', 0)} | "
            f"saved={savings.get('tokens_saved_vs_raw_recent_events', 0)} | "
            f"smaller={efficiency.get('resume_smaller_than_raw_recent_events', False)}"
        )


@app.command("metrics-report")
def cmd_metrics_report(
    out: Annotated[Optional[Path], typer.Option("--out", dir_okay=False, writable=True)] = None,
    recent_event_limit: Annotated[int, typer.Option("--recent-event-limit", min=1, max=500)] = 20,
    history_limit: Annotated[int, typer.Option("--history-limit", min=1, max=200)] = 10,
    task_id: Annotated[str, typer.Option("--task-id")] = "latest",
    no_dotenv: Annotated[bool, typer.Option("--no-dotenv")] = False,
    repo: Annotated[Optional[Path], typer.Option("--repo", exists=True, file_okay=False, dir_okay=True)] = None,
) -> None:
    """Generate a markdown report for current metrics and recent trend history."""
    r = _repo_opt(repo)
    ensure_repo_initialized(r)
    cfg = _config_for_repo(r, no_dotenv=no_dotenv)
    report = metrics_markdown_report(
        r,
        cfg,
        recent_event_limit=recent_event_limit,
        history_limit=history_limit,
        task_id=task_id,
    )
    if out is not None:
        out.write_text(report, encoding="utf-8")
        typer.echo(str(out))
    else:
        typer.echo(report)


@app.command("validate-provider")
def cmd_validate_provider(
    json_output: Annotated[bool, typer.Option("--json")] = False,
    no_dotenv: Annotated[bool, typer.Option("--no-dotenv")] = False,
    repo: Annotated[Optional[Path], typer.Option("--repo", exists=True, file_okay=False, dir_okay=True)] = None,
) -> None:
    """Check whether the configured provider/model/key can complete a minimal reflection call."""
    r = _repo_opt(repo)
    ensure_repo_initialized(r)
    cfg = _config_for_repo(r, no_dotenv=no_dotenv)
    rep = validate_provider(r, cfg)
    if json_output:
        typer.echo(json.dumps(rep, indent=2, sort_keys=True))
    else:
        typer.echo(f"provider: {rep['provider']}")
        typer.echo(f"model: {rep['model']}")
        typer.echo(f"base_url: {rep['base_url']}")
        typer.echo(f"api_key_env: {rep['api_key_env']}")
        typer.echo(f"api_key_present: {rep['api_key_present']}")
        typer.echo(f"ok: {rep['ok']}")
        if rep["summary"]:
            typer.echo(f"summary: {rep['summary']}")
        if rep["risks"]:
            typer.echo("risks:")
            for risk in rep["risks"]:
                typer.echo(f"- {risk}")
    if not rep.get("ok", False):
        raise typer.Exit(1)


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
    strict: Annotated[bool, typer.Option("--strict", help="Treat warnings as failures for exit status.")] = False,
) -> None:
    """Validate `.contextCLI/` and print actionable diagnostics."""
    r = _repo_opt(repo)
    ensure_repo_initialized(r)
    _config_for_repo(r, no_dotenv=no_dotenv)
    rep = doctor_report(r)
    rep["strict"] = strict
    rep["strict_ok"] = doctor_exit_ok(rep, strict=strict)
    if json_output:
        typer.echo(json.dumps(rep, indent=2, sort_keys=True))
    else:
        for ln in rep["lines"]:
            typer.echo(ln)
    if not rep.get("strict_ok", False):
        raise typer.Exit(1)


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


@app.command("migrate")
def cmd_migrate(
    repo: Annotated[Optional[Path], typer.Option("--repo", exists=True, file_okay=False, dir_okay=True)] = None,
) -> None:
    """Normalize repo-local storage metadata to the current supported schema."""
    r = _repo_opt(repo)
    out = migrate_repo(r)
    typer.echo(json.dumps(out, indent=2, sort_keys=True))


@app.command("export-state")
def cmd_export_state(
    out: Annotated[Path, typer.Option("--out", dir_okay=False, writable=True)],
    include_checkpoints: Annotated[bool, typer.Option("--checkpoints/--no-checkpoints")] = True,
    redact: Annotated[bool, typer.Option("--redact/--no-redact")] = True,
    no_dotenv: Annotated[bool, typer.Option("--no-dotenv")] = False,
    repo: Annotated[Optional[Path], typer.Option("--repo", exists=True, file_okay=False, dir_okay=True)] = None,
) -> None:
    """Export distilled context into a portable JSON bundle."""
    r = _repo_opt(repo)
    ensure_repo_initialized(r)
    cfg = _config_for_repo(r, no_dotenv=no_dotenv)
    bundle = export_state_bundle(r, cfg, include_checkpoints=include_checkpoints, redact=redact)
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
    try:
        data = json.loads(bundle_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"Import bundle is not valid JSON: {e}") from e
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
        typer.echo(f"using_direct_git_hooks_dir: {hs['using_direct_git_hooks_dir']}")
    else:
        typer.echo(f"direct_git_hook_exists: {hs['direct_git_hook_exists']}")


@hooks_app.command("wire")
def cmd_hooks_wire(
    repo_hooks: Annotated[bool, typer.Option("--repo-hooks/--git-hooks", help="Use repo hooks/ (recommended) or direct .git/hooks.")] = True,
    repo: Annotated[Optional[Path], typer.Option("--repo", exists=True, file_okay=False, dir_okay=True)] = None,
) -> None:
    """Configure Git to run hooks from `hooks/` or `.git/hooks`."""
    r = _repo_opt(repo)
    ensure_repo_initialized(r)
    out = configure_hooks_path(r, use_repo_hooks_dir=repo_hooks)
    typer.echo(json.dumps(out, indent=2, sort_keys=True))
