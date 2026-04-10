# contextCLI Agent Skill Template

Use this template in an agent instruction file such as `CLAUDE.md`, Cursor rules, or another coding-agent rules document.

## Operating Rules

You are working in a repository that may use contextCLI for persistent state.

At the start of work:

- Check whether `.contextCLI/` exists.
- If persisted state or a pointer index is injected into the prompt, treat it as orientation, not as a substitute for reading files.
- Use pointer lines to find relevant files quickly.

During work:

- Do not manually edit `.contextCLI/`, `pointers.md`, `working_state.json`, `current_context.json`, or checkpoint files.
- Keep updates concise.
- Read referenced source files before making code changes.

After meaningful progress:

```bash
contextCLI update-context --instruction "Short summary of what changed"
```

Before pausing or handing off:

```bash
contextCLI checkpoint --note "Short handoff note"
```

To resume:

```bash
contextCLI resume latest
```

Pointer format:

```text
- [label](reference) -- short description; file:line [tag]
```

Completion wording:

```text
Task complete - context automatically persisted.
```
