"""The agent graph:   START -> agent -> (tool calls?) -> tools -> agent ... -> END

Permission prompts use LangGraph's interrupt(): the tools node pauses, the CLI asks the user,
and resumes with Command(resume=...). All approvals are collected *before* any tool runs, so
replaying the node on resume never executes a tool twice."""
from __future__ import annotations

import os
import platform
from datetime import date
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from . import tools as T


def _union(a: list[str], b: list[str]) -> list[str]:
    return list(dict.fromkeys((a or []) + (b or [])))


class AgentState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    todos: list[dict]
    read_files: Annotated[list[str], _union]   # paths read this session (read-before-edit rule)


SYSTEM_PROMPT = """You are an interactive coding agent running in the user's terminal, helping with software engineering tasks.

# Working style
- Be concise. Lead with the answer or action; no preamble or recaps.
- Explore before changing code: use glob/grep/read_file to understand conventions, then edit.
- Prefer edit_file for changes to existing files; read a file before editing it.
- Follow the project's existing style, libraries and patterns. Don't add comments unless asked.
- For multi-step tasks, plan with todo_write and keep it current.
- Use the task tool for broad searches across many files, to keep your own context small.
- After changing code, verify it: run the tests, linter or the program with bash when possible.
- Never commit, push, or delete files unless the user asks.
- Reference code as path:line.

# Environment
Working directory: {cwd}
Platform: {platform}
Date: {today}
{project_memory}"""


def load_project_memory(cwd: str) -> str:
    """Like CLAUDE.md: project instructions loaded into the system prompt."""
    for name in ("AGENTS.md", "CLAUDE.md", "LGCODE.md"):
        p = Path(cwd) / name
        if p.exists():
            return f"\n# Project instructions ({name})\n{p.read_text()[:20_000]}"
    return ""


def make_model(model: str | None = None) -> ChatAnthropic:
    return ChatAnthropic(
        model=model or os.environ.get("LGCODE_MODEL", "claude-sonnet-5-5"),
        max_tokens=16_000,
        timeout=300,
        max_retries=3,
    )


def _execute(name: str, args: dict, cwd: str, llm) -> str:
    match name:
        case "read_file":
            return T._read_file(cwd=cwd, **args)
        case "glob":
            return T._glob_files(cwd=cwd, **args)
        case "grep":
            return T._grep(cwd=cwd, **args)
        case "write_file":
            return T._write_file(cwd=cwd, **args)
        case "edit_file":
            return T._edit_file(cwd=cwd, **args)
        case "bash":
            return T._bash(cwd=cwd, **args)
        case "task":
            return run_subagent(args["prompt"], cwd, llm)
        case _:
            return f"Error: unknown tool {name}"


def build_graph(llm=None, cwd: str | None = None, tools: list | None = None,
                checkpointer=None, interactive: bool = True):
    """interactive=False skips permission prompts (used by the read-only sub-agent)."""
    cwd = cwd or os.getcwd()
    llm = llm or make_model()
    tools = tools or T.ALL_TOOLS
    tool_names = {t.name for t in tools}
    model = llm.bind_tools(tools)
    system = SystemMessage(SYSTEM_PROMPT.format(
        cwd=cwd, platform=platform.platform(), today=date.today().isoformat(),
        project_memory=load_project_memory(cwd)))

    def agent(state: AgentState, config) -> dict:
        msgs = [system, *state["messages"]]
        if state.get("todos"):
            lines = "\n".join(f"[{t['status']}] {t['content']}" for t in state["todos"])
            msgs.append(SystemMessage(f"Current todo list:\n{lines}"))
        return {"messages": [model.invoke(msgs, config)]}

    def tool_node(state: AgentState, config) -> dict:
        last: AIMessage = state["messages"][-1]
        conf = config.get("configurable", {})
        mode = conf.get("permission_mode", "ask")        # ask | auto | plan
        always = set(conf.get("always_allow", []))
        read = set(state.get("read_files", []))

        # 1) Decide every call before running anything (safe interrupt replay).
        decisions: dict[str, str | None] = {}
        for call in last.tool_calls:
            name, args = call["name"], call["args"]
            if name not in tool_names:
                decisions[call["id"]] = f"Error: tool {name} is not available."
                continue
            if name in ("edit_file", "write_file"):
                p = str(T.resolve(args.get("file_path", ""), cwd))
                if Path(p).exists() and p not in read:
                    decisions[call["id"]] = "Error: read_file this file before modifying it."
                    continue
            if name in T.NEEDS_PERMISSION and interactive:
                if mode == "plan":
                    decisions[call["id"]] = ("Denied: plan mode is on (read-only). Present your plan to the "
                                             "user instead of making changes.")
                    continue
                if mode != "auto" and name not in always:
                    answer = interrupt({"tool": name, "args": args})
                    if answer not in ("yes", "always"):
                        feedback = answer if answer not in ("no", None, "") else ""
                        decisions[call["id"]] = ("The user denied this tool call." +
                                                 (f" Their feedback: {feedback}" if feedback else
                                                  " Stop and ask them how to proceed."))
                        continue
            decisions[call["id"]] = None  # approved

        # 2) Execute approved calls.
        out: list[ToolMessage] = []
        todos = state.get("todos")
        newly_read: list[str] = []
        for call in last.tool_calls:
            name, args, cid = call["name"], call["args"], call["id"]
            if decisions[cid] is not None:
                out.append(ToolMessage(decisions[cid], tool_call_id=cid, name=name, status="error"))
                continue
            if name == "todo_write":
                todos = [dict(t) for t in args["todos"]]
                result = "Todo list updated."
            else:
                try:
                    result = _execute(name, args, cwd, llm)
                except TypeError as e:
                    result = f"Error: bad arguments for {name}: {e}"
                except Exception as e:  # noqa: BLE001
                    result = f"Error: {type(e).__name__}: {e}"
            if name in ("read_file", "write_file", "edit_file") and not result.startswith("Error"):
                newly_read.append(str(T.resolve(args["file_path"], cwd)))
            out.append(ToolMessage(result, tool_call_id=cid, name=name))

        update: dict[str, Any] = {"messages": out, "read_files": newly_read}
        if todos is not None:
            update["todos"] = todos
        return update

    def route(state: AgentState) -> str:
        last = state["messages"][-1]
        return "tools" if isinstance(last, AIMessage) and last.tool_calls else END

    g = StateGraph(AgentState)
    g.add_node("agent", agent)
    g.add_node("tools", tool_node)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route, {"tools": "tools", END: END})
    g.add_edge("tools", "agent")
    return g.compile(checkpointer=checkpointer if checkpointer is not None else InMemorySaver())


def run_subagent(prompt: str, cwd: str, llm) -> str:
    sub = build_graph(llm=llm, cwd=cwd, tools=T.READ_ONLY_TOOLS, interactive=False)
    result = sub.invoke(
        {"messages": [("user", prompt + "\n\nYou are a read-only research sub-agent. "
                       "Finish with a concise report of what you found, citing path:line.")]},
        {"configurable": {"thread_id": "subagent"}, "recursion_limit": 60},
    )
    final = result["messages"][-1]
    return final.text if hasattr(final, "text") else str(final.content)
