#!/usr/bin/env sh
set -eu

# Optional Claude Code end-of-turn hook.
# Run this from a repository initialized with `contextCLI init`.
# The instruction can be passed as the first argument or via CONTEXTCLI_INSTRUCTION.

instruction="${1:-${CONTEXTCLI_INSTRUCTION:-Claude turn completed}}"

contextCLI update-context --instruction "$instruction" || true
