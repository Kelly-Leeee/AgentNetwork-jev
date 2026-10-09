from __future__ import annotations

import json
import sys
import threading
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agentrouter.loop import AgentLoop
from agentrouter.tools import WorkspaceTools


def response(response_id: str, output: list[object], text: str = "") -> SimpleNamespace:
    return SimpleNamespace(id=response_id, output=output, output_text=text)


class FakeResponses:
    def __init__(self, values: list[object]) -> None:
        self.values, self.calls = values, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.values.pop(0)


class AgentLoopTests(unittest.TestCase):
    def test_executes_tool_and_returns_answer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "note.txt").write_text("hello agent", encoding="utf-8")
            call = SimpleNamespace(type="function_call", name="read_file",
                                   arguments='{"path":"note.txt"}', call_id="call_1")
            fake = FakeResponses([response("r1", [call]), response("r2", [], "done")])
            registry = WorkspaceTools(root, allow_write=False, max_file_chars=1000,
                                      max_write_chars=1000).registry()
            result = AgentLoop(client=SimpleNamespace(responses=fake), model="test",
                               instructions="test", tools=registry).run("read it")
            self.assertEqual(result.output, "done")
            self.assertEqual(result.tool_call_count, 1)
            tool_output = fake.calls[1]["input"][0]
            self.assertEqual(tool_output["type"], "function_call_output")
            self.assertTrue(json.loads(tool_output["output"])["ok"])
            self.assertEqual(fake.calls[1]["previous_response_id"], "r1")

    def test_workspace_cannot_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = WorkspaceTools(Path(directory), allow_write=False,
                                      max_file_chars=100, max_write_chars=100).registry()
            result = json.loads(registry.execute("read_file", '{"path":"../secret"}'))
            self.assertFalse(result["ok"])
            self.assertIn("escapes workspace", result["error"])

    def test_limit_closes_pending_tool_call_before_finalizing(self) -> None:
        call = SimpleNamespace(type="function_call", name="read_file",
                               arguments='{"path":"note.txt"}', call_id="call_1")
        fake = FakeResponses([response("r1", [call]), response("r2", [], "limited")])
        with tempfile.TemporaryDirectory() as directory:
            registry = WorkspaceTools(Path(directory), allow_write=False,
                                      max_file_chars=100, max_write_chars=100).registry()
            result = AgentLoop(client=SimpleNamespace(responses=fake), model="test",
                               instructions="test", tools=registry, max_tool_rounds=0).run("read it")
        self.assertTrue(result.max_rounds_hit)
        self.assertEqual(fake.calls[1]["input"][0]["type"], "function_call_output")
        self.assertNotIn("tools", fake.calls[1])

    def test_write_is_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            readonly = WorkspaceTools(root, allow_write=False, max_file_chars=100, max_write_chars=100).registry()
            writable = WorkspaceTools(root, allow_write=True, max_file_chars=100, max_write_chars=100).registry()
            self.assertNotIn("write_file", {item["name"] for item in readonly.schemas})
            self.assertIn("write_file", {item["name"] for item in writable.schemas})

    def test_run_command_executes_script_and_captures_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "hello.py").write_text("print('hello from script')\n", encoding="utf-8")
            registry = WorkspaceTools(root, allow_write=True, max_file_chars=100,
                                      max_write_chars=100, allow_exec=True,
                                      max_command_seconds=5, max_command_output_chars=1000).registry()
            result = json.loads(registry.execute("run_command", {
                "argv": [sys.executable, "hello.py"], "cwd": ".", "timeout_seconds": 2
            }))
            self.assertTrue(result["ok"])
            self.assertEqual(result["result"]["exit_code"], 0)
            self.assertEqual(result["result"]["stdout"], "hello from script\n")

    def test_run_command_cwd_cannot_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = WorkspaceTools(Path(directory), allow_write=True, max_file_chars=100,
                                      max_write_chars=100, allow_exec=True).registry()
            result = json.loads(registry.execute("run_command", {
                "argv": [sys.executable, "-V"], "cwd": "..", "timeout_seconds": 2
            }))
            self.assertFalse(result["ok"])
            self.assertIn("escapes workspace", result["error"])

    def test_stop_terminates_active_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            call = SimpleNamespace(type="function_call", name="run_command",
                                   arguments=json.dumps({"argv": [sys.executable, "-c", "import time; time.sleep(30)"],
                                                         "cwd": ".", "timeout_seconds": 30}),
                                   call_id="call_1")
            fake = FakeResponses([response("r1", [call])])
            registry = WorkspaceTools(root, allow_write=True, max_file_chars=100,
                                      max_write_chars=100, allow_exec=True,
                                      max_command_seconds=30).registry()
            loop = AgentLoop(client=SimpleNamespace(responses=fake), model="test",
                             instructions="test", tools=registry)
            holder = []
            worker = threading.Thread(target=lambda: holder.append(loop.run("sleep")))
            worker.start()
            time.sleep(0.15)
            loop.stop()
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertTrue(holder[0].stopped)


if __name__ == "__main__":
    unittest.main()
