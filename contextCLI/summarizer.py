from __future__ import annotations

import json
import os
from typing import Any, Optional

import httpx


def _system_prompt() -> str:
    return "\n".join(
        [
            "You are a cheap reflection model for a coding agent.",
            "Your job: update a repo's distilled working state and a Claude-style pointer index.",
            "",
            "Return ONLY valid JSON. No markdown. No prose.",
            "Constraints:",
            "- pointers_md must be <= max_pointer_lines lines.",
            "- each pointer line must be <= 150 chars if possible.",
            "- pointers format:",
            "  - [label](reference) -- description; file:line [gotcha]",
            "",
            "Output JSON schema:",
            "{",
            '  "working_state": { "turns": int, "last_compaction_turn": int, "open_items": [str], "last_updated_at": str },',
            '  "current_context": { "summary": str, "goals": [str], "risks": [str] },',
            '  "pointers_md": "# Pointers\\n- ...\\n",',
            '  "topics": [ { "name": "topic_slug", "content": "markdown" } ]',
            "}",
        ]
    )


def _user_prompt(
    recent_events: list[dict[str, Any]],
    working_state: dict[str, Any],
    pointers_md: str,
    max_pointer_lines: int,
    reason: str,
) -> str:
    payload = {
        "reason": reason,
        "max_pointer_lines": max_pointer_lines,
        "recent_events": recent_events,
        "working_state": working_state,
        "existing_pointers_md": pointers_md,
    }
    return json.dumps(payload, indent=2, sort_keys=True)

def _no_op(
    *,
    working_state: dict[str, Any],
    pointers_md: str,
    summary: str,
    risks: list[str],
) -> dict[str, Any]:
    return {
        "_reflected": False,
        "working_state": working_state,
        "current_context": {"summary": summary, "goals": [], "risks": risks},
        "pointers_md": pointers_md or "# Pointers\n",
        "topics": [],
    }


def _post_json(url: str, headers: dict[str, str], body: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    with httpx.Client(timeout=timeout_s) as client:
        r = client.post(url, json=body, headers=headers)
        r.raise_for_status()
        return r.json()


def cheap_reflect(
    *,
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    recent_events: list[dict[str, Any]],
    working_state: dict[str, Any],
    pointers_md: str,
    max_pointer_lines: int,
    reason: str,
    timeout_s: float = 20.0,
) -> dict[str, Any]:
    """
    Minimal "cheap model" call.

    Default is `openai_compatible`:
      POST {base_url}/chat/completions with Authorization: Bearer {api_key}

    If no API key is present, returns a conservative no-op update.
    """
    if provider in ("ollama", "ollama_local"):
        api_key = api_key or ""
    elif not api_key:
        return _no_op(
            working_state=working_state,
            pointers_md=pointers_md,
            summary="No API key configured; contextCLI recorded events but did not reflect.",
            risks=["Missing API key; set config.toml api_key_env and export that env var."],
        )

    content: Optional[str] = None
    if provider in ("ollama", "ollama_local"):
        # Ollama uses its local chat API. Configure api_base_url or OLLAMA_BASE_URL.
        try:
            ollama_base_url = base_url or os.environ.get("OLLAMA_BASE_URL", "")
            if not ollama_base_url:
                return _no_op(
                    working_state=working_state,
                    pointers_md=pointers_md,
                    summary="Ollama endpoint is not configured.",
                    risks=["Set api_base_url in config.toml or OLLAMA_BASE_URL in the environment."],
                )
            url = ollama_base_url.rstrip("/") + "/api/chat"
            body = {
                "model": model,
                "stream": False,
                "format": "json",
                "messages": [
                    {"role": "system", "content": _system_prompt()},
                    {
                        "role": "user",
                        "content": _user_prompt(
                            recent_events=recent_events,
                            working_state=working_state,
                            pointers_md=pointers_md,
                            max_pointer_lines=max_pointer_lines,
                            reason=reason,
                        ),
                    },
                ],
            }
            data = _post_json(url, headers={}, body=body, timeout_s=timeout_s)
            try:
                content = data["message"]["content"]
            except Exception:
                content = None
        except Exception as e:
            return _no_op(
                working_state=working_state,
                pointers_md=pointers_md,
                summary="Model call failed.",
                risks=[f"ollama error: {type(e).__name__}: {e}"],
            )

    elif provider in ("openai_compatible", "together", "openrouter", "cerebras"):
        # Provider defaults (OpenAI-compatible Chat Completions).
        # If user hasn't customized base_url, override per-provider.
        if provider != "openai_compatible" and base_url.rstrip("/") == "https://api.openai.com/v1":
            base_url = ""
        if provider == "together" and not base_url:
            base_url = "https://api.together.xyz/v1"
        elif provider == "openrouter" and not base_url:
            base_url = "https://openrouter.ai/api/v1"
        elif provider == "cerebras" and not base_url:
            base_url = "https://api.cerebras.ai/v1"
        try:
            url = base_url.rstrip("/") + "/chat/completions"
            body = {
                "model": model,
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": _system_prompt()},
                    {
                        "role": "user",
                        "content": _user_prompt(
                            recent_events=recent_events,
                            working_state=working_state,
                            pointers_md=pointers_md,
                            max_pointer_lines=max_pointer_lines,
                            reason=reason,
                        ),
                    },
                ],
            }
            headers = {"authorization": f"Bearer {api_key}"}
            # OpenRouter supports optional identity headers; harmless if omitted.
            if provider == "openrouter":
                headers.setdefault("x-title", "contextCLI")
            data = _post_json(url, headers, body, timeout_s)
            content = data["choices"][0]["message"]["content"]
        except Exception as e:
            return _no_op(
                working_state=working_state,
                pointers_md=pointers_md,
                summary="Model call failed.",
                risks=[f"{provider} error: {type(e).__name__}: {e}"],
            )

    elif provider == "anthropic":
        # Anthropic Messages API. Set `api_base_url` to "https://api.anthropic.com/v1".
        url = base_url.rstrip("/") + "/messages"
        body = {
            "model": model,
            "max_tokens": 1200,
            "temperature": 0.2,
            "system": _system_prompt(),
            "messages": [
                {
                    "role": "user",
                    "content": _user_prompt(
                        recent_events=recent_events,
                        working_state=working_state,
                        pointers_md=pointers_md,
                        max_pointer_lines=max_pointer_lines,
                        reason=reason,
                    ),
                }
            ],
        }
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        try:
            data = _post_json(url, headers, body, timeout_s)
            blocks = data.get("content", [])
            if blocks and isinstance(blocks, list):
                content = blocks[0].get("text")
        except Exception as e:
            return _no_op(
                working_state=working_state,
                pointers_md=pointers_md,
                summary="Model call failed.",
                risks=[f"anthropic error: {type(e).__name__}: {e}"],
            )

    elif provider == "gemini":
        # Gemini generateContent. Set base_url to "https://generativelanguage.googleapis.com/v1beta/models".
        # model becomes "gemini-2.0-flash" etc.
        url = base_url.rstrip("/") + f"/{model}:generateContent"
        body = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": _system_prompt()},
                        {
                            "text": _user_prompt(
                                recent_events=recent_events,
                                working_state=working_state,
                                pointers_md=pointers_md,
                                max_pointer_lines=max_pointer_lines,
                                reason=reason,
                            )
                        },
                    ],
                }
            ],
            "generationConfig": {"temperature": 0.2},
        }
        # Gemini uses ?key=
        url = url + f"?key={api_key}"
        try:
            data = _post_json(url, headers={}, body=body, timeout_s=timeout_s)
            content = data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            return _no_op(
                working_state=working_state,
                pointers_md=pointers_md,
                summary="Model call failed.",
                risks=[f"gemini error: {type(e).__name__}: {e}"],
            )

    else:
        return _no_op(
            working_state=working_state,
            pointers_md=pointers_md,
            summary=f"Unsupported api_provider: {provider}",
            risks=["Set api_provider=openai_compatible|together|openrouter|cerebras|anthropic|gemini|ollama or extend summarizer.py."],
        )

    if not content:
        return _no_op(
            working_state=working_state,
            pointers_md=pointers_md,
            summary="No content returned from model.",
            risks=[],
        )

    # Parse the JSON the model returned.
    try:
        out = json.loads(content)
        if isinstance(out, dict):
            out["_reflected"] = True
            return out
    except json.JSONDecodeError:
        pass

    return _no_op(
        working_state=working_state,
        pointers_md=pointers_md,
        summary="Model returned invalid JSON.",
        risks=[],
    )
