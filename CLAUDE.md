# lgcode

A Claude Code-style coding agent built as a LangGraph `StateGraph`. See README.md for the feature map; this file only holds what the code doesn't tell you.

## Gotchas

- **Interrupt replay.** LangGraph re-runs a node from the top when it resumes after `interrupt()`. In `tool_node`, collect every approval first and execute only afterwards; anything that runs before an `interrupt()` call can run twice. A test covers this, so keep it passing.
- **Adding a tool touches four places:** the `@tool` schema stub in `tools.py` (it only raises `NotImplementedError`), an execution branch in `graph._execute`, `ALL_TOOLS`, and `NEEDS_PERMISSION` if it mutates anything. Missing the `_execute` branch fails at runtime with "unknown tool", not at import.
- **Read-before-edit** is enforced in `tool_node` via `read_files` state, not in `tools.py`. Don't move or bypass it when refactoring the tools.
- **The sub-agent** (`run_subagent`) builds a separate non-interactive graph with `READ_ONLY_TOOLS`. It must never get write or bash tools.
- **Bash is a fresh shell per call**, so `cd` doesn't persist. This is intentional.

## Testing

- Tests are offline: a scripted fake model drives the real graph, tools and interrupts. They need no API key and must stay that way. Run `python -m pytest tests -q`.
- Use the `claude` conda env; `pytest` is not in `pyproject.toml` dependencies.

## Conventions

- Default model is `claude-sonnet-5-5` (override with `-m` or `$LGCODE_MODEL`). Don't hard-code a model elsewhere.
- `.env` holds API keys: never read it into output, commit it, or remove it from `.gitignore`.
- Never commit or push unless asked.
