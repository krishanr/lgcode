# lgcode — a Claude Code-style coding agent on LangGraph

A terminal coding agent built as a LangGraph `StateGraph`: Claude reads, searches and edits your code, runs shell commands with your permission, tracks a todo list, and delegates broad searches to a read-only sub-agent.

```
START ─► agent ──(tool calls?)──► tools ─┐
           ▲          │ no              │
           │          ▼                 │
           │         END                │
           └────────────────────────────┘
```

## Install & run

```bash
pip install -e .
export ANTHROPIC_API_KEY=sk-ant-...
lgcode                              # interactive REPL in the current directory
lgcode "add type hints to utils.py" # start with a prompt
lgcode -p "summarize this repo"     # one-shot, prints the answer (no prompts; auto mode)
lgcode --mode plan -C ~/proj        # read-only mode in another directory
```

Model: `-m <model-id>` or `$LGCODE_MODEL` (default `claude-sonnet-5-5`).

## What maps to what

| Claude Code feature | Here |
|---|---|
| Agent loop | `agent` node (model with bound tools) ⇄ `tools` node, conditional edge on `tool_calls` |
| Read / Write / Edit / Bash / Glob / Grep | `lgcode/tools.py` — exact-string edits, unique-match rule, output truncation, ripgrep |
| "Read before edit" | `read_files` in graph state; edits to unread existing files are refused |
| Permission prompts | `interrupt()` in the tools node → CLI asks → `Command(resume=...)` |
| Permission modes | `/mode ask` (default), `auto` (no prompts), `plan` (read-only) |
| "Always allow", deny with feedback | answer `a`, or type instructions instead of `n` |
| TodoWrite | `todo_write` tool → `todos` in state, re-shown to the model every turn |
| Task / sub-agents | `task` tool runs a separate read-only graph and returns its report |
| CLAUDE.md | `AGENTS.md`, `CLAUDE.md` or `LGCODE.md` in the project root goes into the system prompt |
| /compact, /clear | summary carried into a fresh thread / new thread |
| Streaming | `stream_mode=["messages", "updates"]` |
| Esc to interrupt | Ctrl+C; dangling tool calls are closed out on the next turn |

## Design notes

- **Interrupt safety.** LangGraph re-runs a node from the top when it resumes after `interrupt()`. The tools node therefore collects *every* approval first and only then executes, so parallel tool calls with multiple prompts never run anything twice (covered by a test).
- **Persistence.** Uses `InMemorySaver`. For resumable sessions, swap in `SqliteSaver` / `PostgresSaver` in `build_graph(checkpointer=...)` and reuse the `thread_id`.
- **Bash** is a fresh shell per call (no persistent `cd`), same as Claude Code's default behavior with an explicit cwd.

## Extending

- **New tool:** declare it with `@tool` in `tools.py` (schema only), add an execution branch in `graph._execute`, add it to `ALL_TOOLS`, and to `NEEDS_PERMISSION` if it mutates anything.
- **MCP servers:** load tools with `langchain-mcp-adapters` (`MultiServerMCPClient(...).get_tools()`) and pass them in `tools=`; call them via `tool.invoke(args)` in `_execute`'s default branch.
- **Other models:** `build_graph(llm=...)` accepts any LangChain chat model that supports `bind_tools`.
- **LangSmith tracing:** set `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY`.

## Tests

```bash
pip install pytest && pytest -q
```
Offline: a scripted fake model drives the real graph, tools and interrupts — no API key needed.

## Differences from the real Claude Code

lgcode reproduces the core loop, tools and permission model, but not the extensibility layer, the multi-agent features, session persistence, or the polished UI.

**Tools**
- No `WebFetch`, `WebSearch`, `NotebookEdit` or `Monitor`.
- Bash starts a fresh shell on every call (`cd` doesn't persist) and has no background execution; long-running commands block until the timeout (600s max).
- `read_file` is text-only: no images, PDFs or notebooks.

**Agents**
- One sub-agent type: read-only and synchronous. Claude Code has typed agents (Explore, Plan, general-purpose), background agents, forks, worktree isolation and inter-agent messaging.

**Extensibility**
- No MCP (only a suggestion under *Extending*), skills, hooks, plugins, custom slash commands or settings files with permission rules.
- Only six slash commands: `/help`, `/clear`, `/mode`, `/todos`, `/compact`, `/cost`.

**Permissions and safety**
- Permissions are per tool name, not per command pattern or path (e.g. no `Bash(npm test:*)`), and there is no shell sandbox. "Always allow" on `bash` allows every command.
- Read-before-edit tracks files read this session only; it doesn't detect changes on disk after the read.

**Sessions and persistence**
- State lives in `InMemorySaver` and is lost on exit. No session resume, `--continue`, checkpoints or rewind.

**UX**
- Simple REPL rather than a TUI: no diff previews, no Esc interrupt (Ctrl+C instead), no IDE integration, no scheduled or remote runs.
- The system prompt is short compared with Claude Code's detailed guidance on git, PRs, tool use and safety.

**What the LangGraph version adds**
- An explicit, inspectable graph; any LangChain chat model; LangSmith tracing; LangGraph checkpointers (SQLite/Postgres); and offline tests driven by a scripted fake model.
