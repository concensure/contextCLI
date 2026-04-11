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
  events.jsonl
  working_state.json
  pointers.md
  current_context.json
  artifacts/
  checkpoints/
```

It also creates example hook files in `hooks/`.

To let Git run contextCLI after each commit:

```bash
git config core.hooksPath hooks
```

Check whether Git is actually wired to use the generated hook:

```bash
contextCLI hooks status
```

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

Run health checks:

```bash
contextCLI doctor
```

Show effective configuration:

```bash
contextCLI config
```

Install or remove hook templates:

```bash
contextCLI hooks install
contextCLI hooks status
contextCLI hooks uninstall
```

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

For local Ollama models, set `api_provider = "ollama"` and set `summarizer_model` to the local model name. Ollama usually does not need an API key. Configure the Ollama endpoint in `api_base_url` or with `OLLAMA_BASE_URL`.

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
