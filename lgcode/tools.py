"""Tools for the agent. Schemas come from @tool; execution happens in graph.tool_node,
which adds permission checks, read-before-edit tracking and output truncation."""
from __future__ import annotations

import glob as _glob
import os
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from langchain_core.tools import tool
from pydantic import BaseModel

MAX_OUTPUT = 30_000  # chars returned to the model per tool call


def _truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n\n... [{len(text) - limit} chars truncated] ...\n\n{text[-half:]}"


def resolve(path: str, cwd: str) -> Path:
    p = Path(path).expanduser()
    return (p if p.is_absolute() else Path(cwd) / p).resolve()


# ---------------------------------------------------------------- read-only tools

@tool
def read_file(file_path: str, offset: int = 1, limit: int = 2000) -> str:
    """Read a text file. Returns lines prefixed with line numbers (cat -n style).
    Use offset (1-based line) and limit to page through large files.
    You must read a file before editing or overwriting it."""
    raise NotImplementedError  # executed in graph.tool_node


def _read_file(file_path: str, cwd: str, offset: int = 1, limit: int = 2000) -> str:
    p = resolve(file_path, cwd)
    if not p.exists():
        return f"Error: {p} does not exist."
    if p.is_dir():
        return f"Error: {p} is a directory. Use glob to list it."
    try:
        lines = p.read_text(errors="replace").splitlines()
    except Exception as e:  # noqa: BLE001
        return f"Error reading {p}: {e}"
    if not lines:
        return "(empty file)"
    start = max(offset, 1) - 1
    chunk = lines[start : start + limit]
    out = "\n".join(f"{i:>6}\t{line[:2000]}" for i, line in enumerate(chunk, start + 1))
    if start + limit < len(lines):
        out += f"\n... ({len(lines) - start - limit} more lines; use offset={start + limit + 1})"
    return out


@tool
def glob(pattern: str, path: str = ".") -> str:
    """Find files by glob pattern (e.g. '**/*.py', 'src/**/*.ts'), newest first."""
    raise NotImplementedError


def _glob_files(pattern: str, cwd: str, path: str = ".") -> str:
    base = resolve(path, cwd)
    hits = [Path(h) for h in _glob.glob(str(base / pattern), recursive=True)]
    hits = [h for h in hits if h.is_file() and not any(part in {".git", "node_modules", "__pycache__", ".venv"} for part in h.parts)]
    hits.sort(key=lambda h: h.stat().st_mtime, reverse=True)
    if not hits:
        return "No files found."
    shown = "\n".join(str(h) for h in hits[:200])
    return shown + (f"\n... and {len(hits) - 200} more" if len(hits) > 200 else "")


@tool
def grep(pattern: str, path: str = ".", glob: str | None = None,
         output_mode: Literal["files_with_matches", "content", "count"] = "files_with_matches",
         case_insensitive: bool = False) -> str:
    """Search file contents with a regex (ripgrep syntax). Narrow with `glob` (e.g. '*.py').
    output_mode: files_with_matches (default), content (matching lines with numbers), or count."""
    raise NotImplementedError


def _grep(pattern: str, cwd: str, path: str = ".", glob: str | None = None,
          output_mode: str = "files_with_matches", case_insensitive: bool = False) -> str:
    base = str(resolve(path, cwd))
    if shutil.which("rg"):
        cmd = ["rg", "--no-heading", "--color=never"]
        cmd += {"files_with_matches": ["-l"], "count": ["-c"], "content": ["-n"]}[output_mode]
        if case_insensitive:
            cmd.append("-i")
        if glob:
            cmd += ["--glob", glob]
        cmd += ["--", pattern, base]
    else:
        cmd = ["grep", "-rE"] + {"files_with_matches": ["-l"], "count": ["-c"], "content": ["-n"]}[output_mode]
        if case_insensitive:
            cmd.append("-i")
        if glob:
            cmd.append(f"--include={glob}")
        cmd += ["--", pattern, base]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode == 1:
        return "No matches."
    if r.returncode not in (0, 1):
        return f"Error: {r.stderr.strip()}"
    return _truncate(r.stdout.strip())


# ---------------------------------------------------------------- mutating tools

@tool
def write_file(file_path: str, content: str) -> str:
    """Create a file or fully overwrite one. For an existing file you must read_file it first;
    prefer edit_file for partial changes."""
    raise NotImplementedError


def _write_file(file_path: str, content: str, cwd: str) -> str:
    p = resolve(file_path, cwd)
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    p.write_text(content)
    return f"{'Updated' if existed else 'Created'} {p} ({len(content.splitlines())} lines)."


@tool
def edit_file(file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """Replace an exact string in a file. old_string must match exactly (including indentation)
    and be unique unless replace_all=True. You must read_file the file first."""
    raise NotImplementedError


def _edit_file(file_path: str, old_string: str, new_string: str, cwd: str, replace_all: bool = False) -> str:
    p = resolve(file_path, cwd)
    if not p.exists():
        return f"Error: {p} does not exist."
    text = p.read_text()
    n = text.count(old_string)
    if old_string == new_string:
        return "Error: old_string and new_string are identical."
    if n == 0:
        return "Error: old_string not found. Re-read the file and match it exactly."
    if n > 1 and not replace_all:
        return f"Error: old_string occurs {n} times. Add surrounding context to make it unique, or set replace_all=true."
    p.write_text(text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1))
    return f"Edited {p} ({n if replace_all else 1} replacement{'s' if replace_all and n > 1 else ''})."


@tool
def bash(command: str, timeout: int = 120) -> str:
    """Run a shell command in the project directory and return stdout+stderr and exit code.
    Each call is a fresh shell (cd does not persist). Prefer read_file/grep/glob over cat/grep/find."""
    raise NotImplementedError


def _bash(command: str, cwd: str, timeout: int = 120) -> str:
    try:
        r = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True,
                           timeout=min(timeout, 600), executable="/bin/bash")
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s."
    out = (r.stdout + (("\n" + r.stderr) if r.stderr else "")).strip()
    return _truncate(f"{out}\n[exit code {r.returncode}]".strip())


# ---------------------------------------------------------------- planning & delegation

class Todo(BaseModel):
    content: str
    status: Literal["pending", "in_progress", "completed"]


@tool
def todo_write(todos: list[Todo]) -> str:
    """Replace the task list. Use it for multi-step work: write all steps up front, keep exactly
    one in_progress, and mark items completed as soon as they're done."""
    raise NotImplementedError


@tool
def task(description: str, prompt: str) -> str:
    """Launch a read-only sub-agent (read_file, glob, grep) for broad codebase exploration.
    It starts with no context: give it a complete, self-contained prompt. Returns its final report."""
    raise NotImplementedError


READ_ONLY_TOOLS = [read_file, glob, grep]
ALL_TOOLS = [read_file, write_file, edit_file, bash, glob, grep, todo_write, task]
NEEDS_PERMISSION = {"write_file", "edit_file", "bash"}
