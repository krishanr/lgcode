"""Terminal REPL: streams the model's text, shows tool activity, handles permission interrupts."""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from .graph import build_graph, make_model

DIM, BOLD, CYAN, GREEN, YELLOW, RED, RESET = "\033[2m", "\033[1m", "\033[36m", "\033[32m", "\033[33m", "\033[31m", "\033[0m"

HELP = """Commands:
  /help              show this help
  /mode ask|auto|plan  ask = confirm edits & shell (default); auto = no prompts; plan = read-only
  /todos             show the task list
  /compact [focus]   summarize the conversation and continue in a fresh context
  /clear             start a new conversation
  /cost              token usage this session
  /exit              quit
Tip: end a line with \\ to continue typing on the next line. Ctrl+C stops the current turn."""


def _preview(args: dict) -> str:
    for key in ("command", "file_path", "pattern", "description"):
        if key in args:
            return str(args[key]).splitlines()[0][:100]
    return json.dumps(args)[:100]


class Session:
    def __init__(self, model: str | None, mode: str, cwd: str):
        self.llm = make_model(model)
        self.graph = build_graph(llm=self.llm, cwd=cwd)
        self.mode = mode
        self.always_allow: list[str] = []
        self.thread = str(uuid.uuid4())
        self.carryover: str | None = None   # summary injected after /compact
        self.tokens_in = self.tokens_out = 0

    @property
    def config(self) -> dict:
        return {"configurable": {"thread_id": self.thread, "permission_mode": self.mode,
                                 "always_allow": self.always_allow},
                "recursion_limit": 200}

    # -- running a turn -------------------------------------------------------
    def _heal(self) -> None:
        """If a previous turn was cut off mid-tool-call, answer the dangling calls."""
        state = self.graph.get_state(self.config)
        if not state.values.get("messages"):
            return
        last = state.values["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            self.graph.update_state(self.config, {"messages": [
                ToolMessage("Interrupted by user.", tool_call_id=c["id"], name=c["name"], status="error")
                for c in last.tool_calls]}, as_node="tools")

    def _stream(self, payload, quiet: bool = False) -> None:
        printing_text = False
        for mode, data in self.graph.stream(payload, self.config, stream_mode=["messages", "updates"]):
            if mode == "messages":
                chunk, meta = data
                # Only the top-level agent's tokens (sub-agents run under a nested namespace).
                if quiet or meta.get("langgraph_node") != "agent" or "|" in meta.get("langgraph_checkpoint_ns", ""):
                    continue
                if isinstance(chunk, AIMessage) and chunk.text:
                    if not printing_text:
                        print()
                        printing_text = True
                    print(chunk.text, end="", flush=True)
            elif mode == "updates":
                for node, update in data.items():
                    if node == "agent":
                        msg = update["messages"][-1]
                        if usage := getattr(msg, "usage_metadata", None):
                            self.tokens_in += usage.get("input_tokens", 0)
                            self.tokens_out += usage.get("output_tokens", 0)
                        if printing_text:
                            print()
                            printing_text = False
                        if not quiet:
                            for c in msg.tool_calls:
                                print(f"{CYAN}● {c['name']}{RESET}{DIM}({_preview(c['args'])}){RESET}")
                    elif node == "tools" and not quiet:
                        for m in update["messages"]:
                            text = str(m.content)
                            color = RED if m.status == "error" or text.startswith("Error") else DIM
                            first = text.splitlines()[0][:110] if text else ""
                            extra = len(text.splitlines()) - 1
                            print(f"  {color}⎿ {first}{f' (+{extra} lines)' if extra > 0 else ''}{RESET}")
                        if update.get("todos"):
                            self.print_todos(update["todos"])
        if printing_text:
            print()

    def run_turn(self, text: str, quiet: bool = False) -> str:
        self._heal()
        if self.carryover:
            text = f"Summary of our earlier conversation:\n{self.carryover}\n\n---\n\n{text}"
            self.carryover = None
        payload: object = {"messages": [HumanMessage(text)]}
        while True:
            self._stream(payload, quiet)
            state = self.graph.get_state(self.config)
            if not state.interrupts:
                break
            payload = Command(resume=self.ask_permission(state.interrupts[0].value))
        final = self.graph.get_state(self.config).values["messages"][-1]
        return final.text if isinstance(final, AIMessage) else ""

    def ask_permission(self, req: dict) -> str:
        tool, args = req["tool"], req["args"]
        print(f"\n{YELLOW}{BOLD}Permission needed: {tool}{RESET}")
        if tool == "bash":
            print(f"  {BOLD}$ {args.get('command')}{RESET}")
        elif tool == "edit_file":
            print(f"  {args.get('file_path')}")
            for line in str(args.get("old_string", "")).splitlines()[:15]:
                print(f"  {RED}- {line}{RESET}")
            for line in str(args.get("new_string", "")).splitlines()[:15]:
                print(f"  {GREEN}+ {line}{RESET}")
        elif tool == "write_file":
            content = str(args.get("content", ""))
            print(f"  {args.get('file_path')} ({len(content.splitlines())} lines)")
            for line in content.splitlines()[:10]:
                print(f"  {GREEN}+ {line}{RESET}")
        while True:
            try:
                ans = input(f"{YELLOW}Allow? [y]es / [n]o / [a]lways for {tool} / or type feedback: {RESET}").strip()
            except EOFError:
                return "no"
            if ans.lower() in ("y", "yes", ""):
                return "yes"
            if ans.lower() in ("n", "no"):
                return "no"
            if ans.lower() in ("a", "always"):
                self.always_allow.append(tool)
                return "always"
            return ans  # free-text feedback = deny with instructions

    # -- commands ---------------------------------------------------------------
    def print_todos(self, todos: list[dict] | None = None) -> None:
        todos = todos if todos is not None else self.graph.get_state(self.config).values.get("todos") or []
        if not todos:
            print(f"{DIM}(no todos){RESET}")
        for t in todos:
            mark = {"completed": f"{GREEN}☒", "in_progress": f"{YELLOW}◐", "pending": "☐"}[t["status"]]
            print(f"  {mark} {t['content']}{RESET}")

    def compact(self, focus: str = "") -> None:
        msgs = self.graph.get_state(self.config).values.get("messages", [])
        if not msgs:
            print(f"{DIM}Nothing to compact.{RESET}")
            return
        transcript = "\n\n".join(f"[{m.type}] {str(m.content)[:4000]}" for m in msgs)
        prompt = ("Summarize this coding session so work can continue in a fresh context. Include: the user's "
                  "goals and instructions, files read and changed, decisions made, errors hit and how they were "
                  "fixed, and what remains to do." + (f" Focus on: {focus}" if focus else "") +
                  f"\n\n<transcript>\n{transcript[-150_000:]}\n</transcript>")
        summary = self.llm.invoke(prompt).text
        todos = self.graph.get_state(self.config).values.get("todos") or []
        if todos:
            summary += "\n\nTodo list at the time of compaction:\n" + "\n".join(
                f"[{t['status']}] {t['content']}" for t in todos)
        self.thread = str(uuid.uuid4())
        self.carryover = summary
        print(f"{DIM}Compacted {len(msgs)} messages into a summary.{RESET}")

    def handle_command(self, line: str) -> bool:
        cmd, _, arg = line.partition(" ")
        if cmd in ("/exit", "/quit"):
            return False
        if cmd == "/help":
            print(HELP)
        elif cmd == "/clear":
            self.thread, self.carryover = str(uuid.uuid4()), None
            print(f"{DIM}Started a new conversation.{RESET}")
        elif cmd == "/mode":
            if arg in ("ask", "auto", "plan"):
                self.mode = arg
            print(f"Permission mode: {BOLD}{self.mode}{RESET}")
        elif cmd == "/todos":
            self.print_todos()
        elif cmd == "/compact":
            self.compact(arg)
        elif cmd == "/cost":
            print(f"Tokens — input: {self.tokens_in:,}  output: {self.tokens_out:,}")
        else:
            print(f"Unknown command {cmd}. Try /help.")
        return True


def read_input() -> str:
    lines = []
    prompt = f"\n{BOLD}> {RESET}"
    while True:
        line = input(prompt)
        if line.endswith("\\"):
            lines.append(line[:-1])
            prompt = "  "
            continue
        lines.append(line)
        return "\n".join(lines).strip()


def main() -> None:
    ap = argparse.ArgumentParser(prog="lgcode", description="A Claude Code-style coding agent built on LangGraph.")
    ap.add_argument("prompt", nargs="*", help="initial prompt")
    ap.add_argument("-p", "--print", action="store_true", help="run the prompt once, print the answer, exit")
    ap.add_argument("-m", "--model", help="Anthropic model id (default: $LGCODE_MODEL or claude-sonnet-5-5)")
    ap.add_argument("--mode", choices=["ask", "auto", "plan"], default="ask", help="permission mode")
    ap.add_argument("-C", "--cwd", default=os.getcwd(), help="project directory")
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Set ANTHROPIC_API_KEY first.")

    cwd = os.path.abspath(args.cwd)
    first = " ".join(args.prompt).strip()

    if args.print:
        if not first:
            first = sys.stdin.read()
        s = Session(args.model, "auto" if args.mode == "ask" else args.mode, cwd)
        print(s.run_turn(first, quiet=True))
        return

    s = Session(args.model, args.mode, cwd)
    try:
        import readline  # noqa: F401  (line editing + history)
    except ImportError:
        pass
    print(f"{BOLD}lgcode{RESET} {DIM}· {cwd} · mode: {s.mode} · /help for commands{RESET}")
    while True:
        try:
            text = first or read_input()
            first = ""
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text.startswith("/"):
            if not s.handle_command(text):
                break
            continue
        try:
            s.run_turn(text)
        except KeyboardInterrupt:
            print(f"\n{RED}Interrupted.{RESET}")
        except Exception as e:  # noqa: BLE001
            print(f"\n{RED}Error: {type(e).__name__}: {e}{RESET}")


if __name__ == "__main__":
    main()
