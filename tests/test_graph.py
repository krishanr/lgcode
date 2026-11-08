"""Offline tests: a scripted fake model drives the real graph, tools and interrupts."""
from pathlib import Path

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.types import Command

from lgcode.graph import build_graph


class ScriptedModel(BaseChatModel):
    script: list = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kw):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        return ChatResult(generations=[ChatGeneration(message=self.script.pop(0))])


def call(name, args, i):
    return {"name": name, "args": args, "id": f"c{i}", "type": "tool_call"}


def cfg(mode="ask", tid="t"):
    return {"configurable": {"thread_id": tid, "permission_mode": mode, "always_allow": []}}


def tool_results(state):
    return [m.content for m in state.values["messages"] if isinstance(m, ToolMessage)]


def test_read_then_edit_with_approval(tmp_path: Path):
    f = tmp_path / "app.py"
    f.write_text("print('hello')\n")
    llm = ScriptedModel(script=[
        AIMessage("", tool_calls=[call("read_file", {"file_path": "app.py"}, 1)]),
        AIMessage("", tool_calls=[call("edit_file", {"file_path": "app.py", "old_string": "hello",
                                                     "new_string": "world"}, 2)]),
        AIMessage("Done."),
    ])
    g = build_graph(llm=llm, cwd=str(tmp_path))
    g.invoke({"messages": [("user", "change it")]}, cfg())
    st = g.get_state(cfg())
    assert st.interrupts and st.interrupts[0].value["tool"] == "edit_file"
    assert f.read_text() == "print('hello')\n"          # nothing ran before approval
    g.invoke(Command(resume="yes"), cfg())
    assert f.read_text() == "print('world')\n"
    assert g.get_state(cfg()).values["messages"][-1].content == "Done."


def test_edit_without_read_is_refused(tmp_path: Path):
    (tmp_path / "a.txt").write_text("x")
    llm = ScriptedModel(script=[
        AIMessage("", tool_calls=[call("edit_file", {"file_path": "a.txt", "old_string": "x", "new_string": "y"}, 1)]),
        AIMessage("ok"),
    ])
    g = build_graph(llm=llm, cwd=str(tmp_path))
    g.invoke({"messages": [("user", "go")]}, cfg("auto"))
    assert "read_file this file" in tool_results(g.get_state(cfg("auto")))[0]
    assert (tmp_path / "a.txt").read_text() == "x"


def test_deny_with_feedback_and_plan_mode(tmp_path: Path):
    llm = ScriptedModel(script=[
        AIMessage("", tool_calls=[call("bash", {"command": "rm -rf build"}, 1)]),
        AIMessage("", tool_calls=[call("bash", {"command": "echo hi"}, 2)]),
        AIMessage("fine"),
    ])
    g = build_graph(llm=llm, cwd=str(tmp_path))
    g.invoke({"messages": [("user", "clean")]}, cfg())
    g.invoke(Command(resume="use make clean instead"), cfg())
    assert "make clean instead" in tool_results(g.get_state(cfg()))[0]
    # second call arrives while interrupted; switch to plan mode -> denied without prompting
    g.invoke(Command(resume="no"), cfg())
    llm.script = [AIMessage("", tool_calls=[call("bash", {"command": "ls"}, 3)]), AIMessage("plan")]
    g.invoke({"messages": [("user", "again")]}, cfg("plan"))
    assert "plan mode" in tool_results(g.get_state(cfg("plan")))[-1]


def test_parallel_calls_two_interrupts_no_double_exec(tmp_path: Path):
    llm = ScriptedModel(script=[
        AIMessage("", tool_calls=[call("bash", {"command": "echo 1 >> log"}, 1),
                                  call("bash", {"command": "echo 2 >> log"}, 2)]),
        AIMessage("done"),
    ])
    g = build_graph(llm=llm, cwd=str(tmp_path))
    g.invoke({"messages": [("user", "x")]}, cfg())
    g.invoke(Command(resume="yes"), cfg())
    assert not (tmp_path / "log").exists()   # second approval still pending
    g.invoke(Command(resume="yes"), cfg())
    assert (tmp_path / "log").read_text() == "1\n2\n"


def test_todos_glob_grep_and_subagent(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text("def target():\n    pass\n")
    llm = ScriptedModel(script=[
        AIMessage("", tool_calls=[
            call("todo_write", {"todos": [{"content": "find target", "status": "in_progress"}]}, 1),
            call("glob", {"pattern": "**/*.py"}, 2),
            call("grep", {"pattern": "def target", "output_mode": "content"}, 3),
            call("task", {"description": "find", "prompt": "where is target?"}, 4),
        ]),
        # sub-agent's own turns:
        AIMessage("", tool_calls=[call("grep", {"pattern": "target"}, 5)]),
        AIMessage("target is in src/m.py:1"),
        # back to the main agent:
        AIMessage("Found it."),
    ])
    g = build_graph(llm=llm, cwd=str(tmp_path))
    g.invoke({"messages": [("user", "find target")]}, cfg())
    st = g.get_state(cfg())
    res = tool_results(st)
    assert st.values["todos"][0]["status"] == "in_progress"
    assert "m.py" in res[1] and "1:def target" in res[2] and "src/m.py:1" in res[3]
