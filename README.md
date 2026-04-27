# contextCLI

contextCLI is a persistence layer for AI coding agents.

AI coding tools can be very capable during one session, but they often lose useful project context across days, branches, handoffs, or fresh chats. contextCLI gives a project its own small memory folder so an agent can resume work without asking you to recap everything.

It stores:

- an append-only event log
- a compact working state
- a short `pointers.md` index for important files and decisions
- checkpoints that can be resumed later

The storage lives inside the project you initialize: `.contextCLI/`. If you use contextCLI on several projects, each project gets its own separate `.contextCLI/` folder.

## What You Get

- Resume a task later without rewriting the whole history.
- Keep a small `pointers.md` file instead of pasting long transcripts into prompts.
- Let Git hooks and agent hooks update context automatically.
- Keep a local audit trail and checkpoints for handoff.
- Hand work from one model to another with less prompt bloat, for example planning in Claude Opus and coding in Claude Sonnet.

## Install

For normal use:

```bash
pip install contextCLI
```

For local development from this repository:

```bash
python -m pip install -e .
```

Check that the command works:

```bash
contextCLI --help
```

## Start Using It

Open a terminal in the project you want to give persistent context to, then run:

```bash
contextCLI init --enable-pointers
```

This creates:

```text
.contextCLI/
  config.toml
  metadata.json
  events.jsonl
  working_state.json
  pointers.md
  current_context.json
  artifacts/
  checkpoints/
```

It also creates example hook files in `hooks/`.

- `hooks/post-commit`
- `hooks/claude-end-of-turn.sh`

To let Git run contextCLI after each commit:

```bash
git config core.hooksPath hooks
```

Check whether Git is actually wired to use the generated hook:

```bash
contextCLI hooks status
```

That command also shows whether the Claude end-of-turn template exists in `hooks/`.

## Use It From Anywhere

You do not have to `cd` into the target project if you pass `--repo`.

Windows PowerShell:

```powershell
contextCLI init --repo "PATH_TO_PROJECT" --enable-pointers
contextCLI status --repo "PATH_TO_PROJECT"
contextCLI update-context --repo "PATH_TO_PROJECT" --instruction "Summarize what changed" --force
```

Replace `PATH_TO_PROJECT` with the folder path of the project you want contextCLI to manage.

## Common Commands

Show the current state:

```bash
contextCLI status
```

Show token-savings and context-size metrics:

```bash
contextCLI metrics
```

Record a metrics snapshot for trend tracking:

```bash
contextCLI metrics --record
```

Review recent metrics snapshots and the change from the previous snapshot:

```bash
contextCLI metrics-history
```

If only one snapshot exists, `metrics-history` treats it as a baseline instead of pretending there is already a trend.

Generate a markdown report for current metrics plus recent trend history:

```bash
contextCLI metrics-report --out contextCLI-metrics.md
```

Add a short note and optionally compact immediately:

```bash
contextCLI update-context --instruction "Finished the routing refactor" --force
```

Create a checkpoint:

```bash
contextCLI checkpoint --note "Before changing provider setup"
```

Resume from the latest checkpoint:

```bash
contextCLI resume latest
```

One common use case is token-saving handover between different models. For example:

1. Use a stronger planning model to explore architecture and leave a distilled checkpoint.
2. Switch to a cheaper or faster coding model.
3. Resume with `contextCLI resume latest` instead of pasting a long planning transcript.

`contextCLI metrics` gives an objective estimate of that saving by comparing:

- the distilled resume context you would inject with `contextCLI`
- the raw recent event log you would otherwise need to paste or summarize manually

The default estimate uses a simple `chars / 4` token approximation and reports both absolute and percentage savings.
It also reports whether the distilled resume context is actually smaller than the raw baseline and gives a simple recommendation.

Run health checks:

```bash
contextCLI doctor
```

`doctor` checks the storage files, provider configuration, hook wiring, and whether `.gitignore` and `.env.example` were set up safely.
It exits with a non-zero status when it finds a problem, so you can use it in scripts or CI.
When possible, it also prints the exact `contextCLI` command to fix the problem.
`contextCLI doctor --json` includes machine-readable issue codes, severities, and summary counts.
Use `contextCLI doctor --strict` if you want warnings to fail CI too.

Normalize old or partially upgraded storage files to the current schema:

```bash
contextCLI migrate
```

Repair a damaged or half-created setup without deleting your saved context:

```bash
contextCLI repair
```

If a stale `.contextCLI/.lock` file is left behind after a crash, you can ask repair to remove it:

```bash
contextCLI repair --clear-stale-lock
```

Export a portable bundle of the distilled state, pointers, current context, config, and checkpoints:

```bash
contextCLI export-state --out contextCLI-export.json
```

Import that bundle into another repo:

```bash
contextCLI import-state contextCLI-export.json --repo "PATH_TO_PROJECT"
```

By default, export redacts secret-like text patterns from the portable bundle. Import keeps the target repo's existing provider configuration and only merges checkpoints that are not already present.
If the import file is malformed, `contextCLI` stops with a readable validation error instead of partially importing it.

Show effective configuration:

```bash
contextCLI config
```

Install or remove hook templates:

```bash
contextCLI hooks install
contextCLI hooks wire --repo-hooks
contextCLI hooks status
contextCLI hooks uninstall
```

`contextCLI hooks wire --repo-hooks` tells Git to use the repository `hooks/` folder. `contextCLI hooks wire --git-hooks` tells Git to use `.git/hooks` instead.

## Provider Setup

Edit `.contextCLI/config.toml`.

Or use the command:

```bash
contextCLI configure --provider openrouter --model "MODEL_NAME" --api-key-env OPENROUTER_API_KEY
```

When you choose a supported provider, contextCLI fills in the usual default base URL and API-key variable name for that provider. You can still override them explicitly.

Important fields:

- `api_provider`: provider name, such as `openai_compatible`, `together`, `openrouter`, `cerebras`, `anthropic`, `gemini`, or `ollama`
- `summarizer_model`: model name to use for cheap reflection
- `api_key_env`: environment variable name that contains the API key
- `load_env_file`: whether contextCLI may read `.env` from the target repo
- `capture_git_patch`: whether post-commit compaction stores full patch text; default is off
- `redact_event_secrets`: whether contextCLI redacts common secret patterns before writing events

You can also change the main safety flags from the CLI:

```bash
contextCLI configure --enable-pointers --load-env-file --no-capture-git-patch --redact-event-secrets
```

For hosted providers, put the actual key in a `.env` file in the target project:

```env
OPENROUTER_API_KEY=
```

Use the env var name that matches your config. Do not put the actual key in `config.toml`.

To verify that an OpenRouter setup is actually working:

```bash
contextCLI config
contextCLI doctor
contextCLI validate-provider
contextCLI update-context --instruction "Verification run" --force
```

What to check:

- `contextCLI config` should show:
  - `api_provider: "openrouter"`
  - `api_base_url: "https://openrouter.ai/api/v1"`
  - `api_key_env: "OPENROUTER_API_KEY"`
- `contextCLI doctor` should not report `missing_api_key`
- `contextCLI validate-provider` should report `ok: true`
- `contextCLI update-context --force` should return `"reflected": true` when the provider call succeeds

If `doctor` reports `missing_api_key`, add `OPENROUTER_API_KEY=` to `.env` in the target repo or export it in the shell environment.

For local Ollama models, set `api_provider = "ollama"` and set `summarizer_model` to the local model name. Ollama usually does not need an API key. A base URL alone is not enough: you also need a reachable Ollama server and a model name that exists locally. Configure the Ollama endpoint in `api_base_url` or with `OLLAMA_BASE_URL`.

`contextCLI init` creates `.env.example` so a new user can see which variable names are supported before creating `.env`.

## Agent Integration

Claude Code:

Use `hooks/claude-end-of-turn.sh` as an optional end-of-turn hook. It calls:

```bash
contextCLI update-context --instruction "Claude turn completed"
```

Git:

Use `hooks/post-commit` as a Git post-commit hook. It calls:

```bash
contextCLI auto-compaction
```

Both hooks are fail-open: if the model provider is unavailable, your normal workflow should continue.

## Files That Should Not Be Committed

The target project should ignore:

```text
.contextCLI/
.env
```

`contextCLI init` adds those entries to `.gitignore` when possible.

## For Maintainers

This repository keeps local test scaffolding out of the packaged project. The CI workflow runs tests when a `tests/` folder is present and otherwise runs a CLI smoke test.
