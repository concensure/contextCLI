You are operating inside a contextCLI workspace.

Key instructions:
- Read the injected PERSISTED WORKING STATE and POINTER INDEX before starting work.
- Do not manually edit `.contextCLI/`, `pointers.md`, `current_context.json`, or `working_state.json`.
- After a significant change, ask the user or harness to run:

  contextCLI update-context --instruction "One short sentence describing what changed"

- For long sessions or handoff points, use:

  contextCLI checkpoint --note "Short handoff note"

- A common pattern is token-saving model handoff: one model plans, another model resumes from the checkpoint and pointers.
- Use `pointers.md` to locate relevant files quickly, then read the referenced files before editing.

Task complete messages may end with: "Task complete - context automatically persisted."
