from __future__ import annotations

import json
import sys
import threading
import time
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from agentrouter.agent import Agent
from agentrouter.chat_store import ChatStore
from agentrouter.loop import AgentLoop
from agentrouter.config import load_settings
from agentrouter.prompt import SYSTEM_PROMPT
from agentrouter.tools import WorkspaceTools


def response(response_id: str, output: list[object], text: str = "") -> SimpleNamespace:
    return SimpleNamespace(id=response_id, output=output, output_text=text)


class FakeResponses:
    def __init__(self, values: list[object]) -> None:
        self.values, self.calls = values, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.values.pop(0)


class FakeStream:
    def __init__(self, events: list[object]) -> None:
        self.events = events

    def __enter__(self):
        return iter(self.events)

    def __exit__(self, *_args):
        return False


class AgentLoopTests(unittest.TestCase):
    def test_settings_use_central_system_prompt_by_default(self) -> None:
        settings = load_settings(Path(__file__).resolve().parents[1] / "settings.json")
        self.assertEqual(settings.agent.system_prompt, SYSTEM_PROMPT)

    def test_agent_owns_state_and_continues_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeResponses([response("r1", [], "one"), response("r2", [], "two")])
            tools = WorkspaceTools(Path(directory), allow_write=False, max_file_chars=100,
                                   max_write_chars=100, allow_exec=False).registry()
            agent = Agent(client=SimpleNamespace(responses=fake), model="test",
                          system_prompt="system", tools=tools)
            agent.prompt("first")
            agent.prompt("second")
            self.assertEqual(agent.state.last_response_id, "r2")
            self.assertNotIn("previous_response_id", fake.calls[1])
            self.assertEqual(fake.calls[1]["input"][0]["content"], "first")
            self.assertEqual(fake.calls[1]["input"][-1]["content"], "second")
            self.assertEqual([message["role"] for message in agent.state.messages],
                             ["user", "assistant", "user", "assistant"])

    def test_agent_chat_history_is_restored_per_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = WorkspaceTools(root, allow_write=False, max_file_chars=100,
                                   max_write_chars=100, allow_exec=False).registry()
            first = Agent(client=SimpleNamespace(responses=FakeResponses([response("r1", [], "one")])),
                          model="test", system_prompt="system", tools=tools,
                          agent_id="agent-1", chats_dir=root / "chats")
            first.prompt("first")

            second_fake = FakeResponses([response("r2", [], "two")])
            restored = Agent(client=SimpleNamespace(responses=second_fake), model="test",
                             system_prompt="system", tools=tools, agent_id="agent-1",
                             chats_dir=root / "chats")
            restored.prompt("second")

            self.assertEqual(second_fake.calls[0]["input"][0]["content"], "first")
            self.assertEqual(second_fake.calls[0]["input"][-1]["content"], "second")
            self.assertTrue((root / "chats" / "agent-1" / f"{date.today().isoformat()}.json").is_file())

    def test_chat_store_keeps_only_five_latest_daily_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            current = [date(2026, 1, 1)]
            store = ChatStore(directory, "agent-1", max_files=5, today=lambda: current[0])
            for day in range(7):
                current[0] = date(2026, 1, 1) + timedelta(days=day)
                store.save(messages=[{"day": day}], response_items=[], last_response_id=None)

            files = sorted(store.directory.glob("*.json"))
            self.assertEqual(len(files), 5)
            self.assertEqual(files[0].name, "2026-01-03.json")
            self.assertEqual(files[-1].name, "2026-01-07.json")

    def test_streaming_emits_deltas_and_keeps_completed_response(self) -> None:
        final = response("r1", [], "hello")
        stream = FakeStream([
            SimpleNamespace(type="response.output_text.delta", delta="hel"),
            SimpleNamespace(type="response.output_text.delta", delta="lo"),
            SimpleNamespace(type="response.completed", response=final),
        ])
        fake = FakeResponses([stream])
        deltas = []
        with tempfile.TemporaryDirectory() as directory:
            tools = WorkspaceTools(Path(directory), allow_write=False, max_file_chars=100,
                                   max_write_chars=100, allow_exec=False).registry()
            result = AgentLoop(client=SimpleNamespace(responses=fake), model="test",
                               instructions="test", tools=tools, streaming=True,
                               on_event=lambda kind, payload: deltas.append(payload["delta"])
                               if kind == "text_delta" else None).run("hi")
        self.assertEqual(result.output, "hello")
        self.assertEqual(deltas, ["hel", "lo"])
        self.assertTrue(fake.calls[0]["stream"])

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
            tool_output = next(item for item in fake.calls[1]["input"]
                               if item.get("type") == "function_call_output")
            self.assertEqual(tool_output["type"], "function_call_output")
            self.assertTrue(json.loads(tool_output["output"])["ok"])
            self.assertNotIn("previous_response_id", fake.calls[1])

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
        self.assertTrue(any(item.get("type") == "function_call_output"
                            for item in fake.calls[1]["input"]))
        self.assertNotIn("tools", fake.calls[1])

    def test_write_is_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            readonly = WorkspaceTools(root, allow_write=False, max_file_chars=100, max_write_chars=100).registry()
            writable = WorkspaceTools(root, allow_write=True, max_file_chars=100, max_write_chars=100).registry()
            self.assertNotIn("write_file", {item["name"] for item in readonly.schemas})
            self.assertIn("write_file", {item["name"] for item in writable.schemas})
            self.assertNotIn("search_files", {item["name"] for item in writable.schemas})

    def test_web_tools_are_registered_and_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            enabled = WorkspaceTools(Path(directory), allow_write=False, max_file_chars=100,
                                     max_write_chars=100, allow_web=True).registry()
            disabled = WorkspaceTools(Path(directory), allow_write=False, max_file_chars=100,
                                      max_write_chars=100, allow_web=False).registry()
            self.assertIn("web_search", {item["name"] for item in enabled.schemas})
            self.assertIn("web_fetch", {item["name"] for item in enabled.schemas})
            self.assertNotIn("web_search", {item["name"] for item in disabled.schemas})

    def test_web_fetch_rejects_private_networks(self) -> None:
        with self.assertRaises(PermissionError):
            WorkspaceTools._validate_public_url("http://127.0.0.1/private")

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
