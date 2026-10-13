from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from agentrouter.config import AgentSettings, JevSettings, OpenAISettings, Settings
from agentrouter.team import TeamOrchestrator


class TeamOrchestratorTests(unittest.TestCase):
    def settings(self, workspace: Path) -> Settings:
        return Settings(
            openai=OpenAISettings(api_key="test", model="test"),
            jev=JevSettings(api_key="test", model="jev-latest"),
            agent=AgentSettings(workspace=workspace, allow_write=False, allow_exec=False,
                                allow_web=False),
        )

    def test_demo_starts_with_two_isolated_agents_and_parallel_workstreams(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            team = TeamOrchestrator(self.settings(root), root)
            state = team.snapshot()
            self.assertEqual([agent["id"] for agent in state["agents"]], ["agent-1", "agent-2"])
            assigned = [todo for todo in state["task"]["todos"] if todo["status"] == "assigned"]
            self.assertEqual([(todo["workstream_id"], todo["owner"]) for todo in assigned], [
                ("support-recovery", "agent-1"),
                ("meeting-reschedule", "agent-2"),
            ])
            self.assertEqual(len(state["task"]["workstreams"]), 2)
            self.assertEqual(state["task"]["max_jev_calls"], 6)

    def test_agent_wave_runs_both_workstreams_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            team = TeamOrchestrator(self.settings(root), root)
            rendezvous = threading.Barrier(2, timeout=2)

            def prompt(_text: str):
                rendezvous.wait()
                return SimpleNamespace(
                    output='{"artifact":"done","unresolved":[]}',
                    model_call_count=1,
                    tool_call_count=0,
                )

            team._agents["agent-1"].runtime.prompt = prompt
            team._agents["agent-2"].runtime.prompt = prompt
            team._route_completed_workstream = lambda _workstream_id: team._assign_next_workstream_steps()
            state = team.run_next()

            first_steps = [todo for todo in state["task"]["todos"] if todo["step"] == 1]
            second_steps = [todo for todo in state["task"]["todos"] if todo["step"] == 2]
            self.assertTrue(all(todo["status"] == "completed" for todo in first_steps))
            self.assertTrue(all(todo["status"] == "assigned" for todo in second_steps))
            self.assertEqual(state["task"]["status"], "ready")

    def test_auto_mode_continues_until_both_workstreams_finish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            team = TeamOrchestrator(self.settings(root), root)
            calls = []

            def prompt(text: str):
                calls.append(text)
                return SimpleNamespace(
                    output='{"artifact":"done","unresolved":[]}',
                    model_call_count=1,
                    tool_call_count=0,
                )

            team._agents["agent-1"].runtime.prompt = prompt
            team._agents["agent-2"].runtime.prompt = prompt
            team._route_completed_workstream = lambda _workstream_id: team._assign_next_workstream_steps()
            team.set_execution_mode("auto")

            state = team.run_next()

            self.assertEqual(len(calls), 6)
            self.assertEqual(state["task"]["status"], "completed")
            self.assertTrue(all(
                workstream["status"] == "completed"
                for workstream in state["task"]["workstreams"]
            ))

    def test_execution_mode_rejects_unknown_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            team = TeamOrchestrator(self.settings(Path(directory)), Path(directory))
            with self.assertRaisesRegex(ValueError, "execution mode"):
                team.set_execution_mode("turbo")

    def test_optional_agent_role_is_only_added_to_its_router_state_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            team = TeamOrchestrator(self.settings(root), root)
            initial_agents = team._compact_router_state()["available_agents"]
            self.assertTrue(all("role" not in agent for agent in initial_agents))

            updated = team.update_agent("agent-1", name="Agent 1", role="Careful reviewer")

            self.assertEqual(updated["role"], "Careful reviewer")
            self.assertIn(
                "Your user-configured role for this agent is:\nCareful reviewer",
                team._agents["agent-1"].runtime.state.system_prompt,
            )
            router_agents = team._compact_router_state()["available_agents"]
            self.assertEqual(router_agents[0]["role"], "Careful reviewer")
            self.assertNotIn("role", router_agents[1])

            team.update_agent("agent-1", name="Agent 1", role="   ")
            self.assertNotIn("role", team._compact_router_state()["available_agents"][0])
            self.assertNotIn(
                "Your user-configured role for this agent is:",
                team._agents["agent-1"].runtime.state.system_prompt,
            )

    def test_optional_agent_role_must_be_one_short_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            team = TeamOrchestrator(self.settings(Path(directory)), Path(directory))
            with self.assertRaisesRegex(ValueError, "single line"):
                team.update_agent("agent-1", name="Agent 1",
                                  role="reviewer\nwith another instruction")
            with self.assertRaisesRegex(ValueError, "too long"):
                team.update_agent("agent-1", name="Agent 1", role="r" * 241)

    def test_completed_agent_routes_before_peer_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            team = TeamOrchestrator(self.settings(root), root)
            agent_one_routed = threading.Event()

            def first_prompt(_text: str):
                return SimpleNamespace(output='{"artifact":"one","unresolved":[]}',
                                       model_call_count=1, tool_call_count=0)

            def second_prompt(_text: str):
                self.assertTrue(agent_one_routed.wait(timeout=2))
                return SimpleNamespace(output='{"artifact":"two","unresolved":[]}',
                                       model_call_count=1, tool_call_count=0)

            def route(workstream_id: str):
                if workstream_id == "support-recovery":
                    agent_one_routed.set()
                team._assign_next_workstream_steps()

            team._agents["agent-1"].runtime.prompt = first_prompt
            team._agents["agent-2"].runtime.prompt = second_prompt
            team._route_completed_workstream = route

            team.run_next()
            self.assertTrue(agent_one_routed.is_set())

    def test_auto_mode_starts_newly_routed_agent_before_slow_peer_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            team = TeamOrchestrator(self.settings(root), root)
            team.add_agent()
            agent_three_started = threading.Event()

            def first_prompt(_text: str):
                return SimpleNamespace(output='{"artifact":"one","unresolved":[]}',
                                       model_call_count=1, tool_call_count=0)

            def slow_second_prompt(_text: str):
                self.assertTrue(agent_three_started.wait(timeout=2))
                return SimpleNamespace(output='{"artifact":"two","unresolved":[]}',
                                       model_call_count=1, tool_call_count=0)

            def third_prompt(_text: str):
                agent_three_started.set()
                return SimpleNamespace(output='{"artifact":"three","unresolved":[]}',
                                       model_call_count=1, tool_call_count=0)

            def route(workstream_id: str):
                if workstream_id == "support-recovery":
                    next_todo = team.task["todos"][1]
                    if next_todo["status"] == "pending":
                        next_todo["owner"] = "agent-3"
                        next_todo["status"] = "assigned"
                        team._agents["agent-3"].status = "assigned"

            team._agents["agent-1"].runtime.prompt = first_prompt
            team._agents["agent-2"].runtime.prompt = slow_second_prompt
            team._agents["agent-3"].runtime.prompt = third_prompt
            team._route_completed_workstream = route
            team.set_execution_mode("auto")

            team.run_next()

            self.assertTrue(agent_three_started.is_set())
            self.assertEqual(team.task["todos"][1]["status"], "completed")

    def test_routing_reserves_assigned_agent_from_competing_workstreams(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            team = TeamOrchestrator(self.settings(root), root)
            first = team.task["workstreams"][0]
            second = team.task["workstreams"][1]
            first_remaining = [team.task["todos"][1]]
            second_remaining = [team.task["todos"][4]]
            team.task["todos"][0]["status"] = "completed"
            team.task["todos"][3]["status"] = "completed"

            team._apply_workstream_decision(first, first_remaining, "assign_agent_1")
            self.assertEqual(team._agents["agent-1"].status, "assigned")

            team._apply_workstream_fallback(second, second_remaining)
            self.assertEqual(second_remaining[0]["owner"], "agent-2")
            self.assertEqual(team._agents["agent-2"].status, "assigned")

    def test_jev_options_match_currently_available_agents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            team = TeamOrchestrator(self.settings(root), root)
            team.add_agent()

            all_available = team._workstream_route_criteria()
            self.assertEqual(len(all_available), 5)

            source = team._agents["agent-1"]
            source.status = "routing"
            team._agents["agent-2"].status = "running"
            partially_available = team._workstream_route_criteria(source)
            self.assertEqual(set(partially_available), {
                "assign_agent_1", "assign_agent_3", "finish", "needs_human",
            })

    def test_canvas_accepts_at_most_five_agents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            team = TeamOrchestrator(self.settings(root), root)
            self.assertEqual(team.snapshot()["max_agents"], 5)
            self.assertEqual(team.add_agent()["id"], "agent-3")
            self.assertEqual(team.add_agent()["id"], "agent-4")
            self.assertEqual(team.add_agent()["id"], "agent-5")
            with self.assertRaisesRegex(ValueError, "at most 5"):
                team.add_agent()

    def test_demo_tasks_can_be_customized_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            team = TeamOrchestrator(self.settings(root), root)
            workstreams = [
                {"id": item["id"], "title": f"Custom {index}",
                 "goal": f"Goal {index}", "input": f"Input {index}",
                 "todos": ["Research", "Write", "Verify", "Deliver"]}
                for index, item in enumerate(team.task["workstreams"], start=1)
            ]
            workstreams.append({
                "id": "new-client-id", "title": "New task",
                "goal": "Complete a new task", "input": "New task context",
                "todos": ["Only user-defined step"],
            })
            state = team.update_task(goal="Custom network goal", workstreams=workstreams)
            self.assertEqual(state["task"]["goal"], "Custom network goal")
            self.assertEqual(state["task"]["workstreams"][1]["input"], "Input 2")
            self.assertEqual(len(state["task"]["workstreams"]), 3)
            custom = state["task"]["workstreams"][2]
            self.assertTrue(custom["id"].startswith("custom-"))
            custom_todos = [item for item in state["task"]["todos"]
                            if item["workstream_id"] == custom["id"]]
            self.assertEqual([item["title"] for item in custom_todos], ["Only user-defined step"])

            team._agents["agent-1"].runs.append({"id": "run-1"})
            with self.assertRaisesRegex(RuntimeError, "Reset the demo"):
                team.update_task(goal="Another goal", workstreams=workstreams)

    def test_task_context_is_restored_from_local_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            team = TeamOrchestrator(self.settings(root), root)
            workstreams = [{
                "id": item["id"], "title": item["title"], "goal": item["goal"],
                "input": f"Persisted input {index}", "todos": ["Persisted step"],
            } for index, item in enumerate(team.task["workstreams"], start=1)]
            team.update_task(goal="Persisted network goal", workstreams=workstreams)

            restored = TeamOrchestrator(self.settings(root), root)

            self.assertEqual(restored.task["goal"], "Persisted network goal")
            self.assertEqual(restored.task["workstreams"][0]["input"], "Persisted input 1")
            task_files = sorted((root / "tasks").glob("*.json"))
            self.assertEqual([item.name for item in task_files], [
                "meeting-reschedule.json", "support-recovery.json",
            ])
            saved = json.loads((root / "tasks" / "support-recovery.json").read_text(encoding="utf-8"))
            self.assertNotIn("events", saved)
            self.assertIn("agent_configs", saved)
            self.assertNotIn("agents", saved)
            self.assertTrue(all("runs" not in item for item in saved["agent_configs"]))
            self.assertEqual(saved["messages"][-1]["role"], "user")
            self.assertEqual(saved["messages"][-1]["type"], "task_customized")
            self.assertIn("Persisted network goal", saved["messages"][-1]["content"])

    def test_agent_runtime_is_reset_before_each_task_scoped_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            team = TeamOrchestrator(self.settings(root), root)
            agent = team._agents["agent-1"]
            reset_calls = []
            agent.runtime.reset = lambda: reset_calls.append(True)
            agent.runtime.prompt = lambda _text: SimpleNamespace(
                output='{"artifact":"done","unresolved":[]}',
                model_call_count=1,
                tool_call_count=0,
            )

            team._run_one_todo(team.task["todos"][0])

            self.assertEqual(reset_calls, [True])
            saved = json.loads(
                (root / "tasks" / "support-recovery.json").read_text(encoding="utf-8")
            )
            completed = next(item for item in saved["messages"]
                             if item["type"] == "agent_completed")
            self.assertIn("run_id", completed["metadata"]["payload"])

            restored = TeamOrchestrator(self.settings(root), root)
            self.assertEqual(len(restored._agents["agent-1"].runs), 1)
            self.assertEqual(restored._agents["agent-1"].runs[0]["status"], "completed")

    def test_name_edit_rebuilds_runtime_and_keeps_agent_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            team = TeamOrchestrator(self.settings(root), root)
            updated = team.update_agent("agent-2", name="Peer Two")
            self.assertEqual(updated["name"], "Peer Two")
            self.assertEqual(updated["id"], "agent-2")
            self.assertEqual(updated["role"], "")

    def test_router_decision_assigns_next_pending_todo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            team = TeamOrchestrator(self.settings(root), root)
            team.task["todos"][0]["status"] = "completed"
            team.task["todos"][0]["result"] = "Draft response"
            team._apply_decision("assign_agent_2")
            next_todo = team.task["todos"][1]
            self.assertEqual(next_todo["owner"], "agent-2")
            self.assertEqual(next_todo["status"], "assigned")
            self.assertEqual(team.task["phase"], "agent")

    def test_finish_uses_latest_completed_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            team = TeamOrchestrator(self.settings(root), root)
            team.task["todos"][0]["status"] = "completed"
            team.task["todos"][0]["result"] = "Send-ready response"
            team._apply_decision("finish")
            self.assertEqual(team.task["status"], "completed")
            self.assertEqual(team.task["final_output"], "Send-ready response")

    def test_agent_process_events_are_kept_in_its_run_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            team = TeamOrchestrator(self.settings(root), root)
            agent = team._agents["agent-1"]
            agent.runs.append({"id": "run-1", "status": "running", "process": []})

            team._agent_event("agent-1", "tool_start", {
                "name": "read_file", "arguments": {"path": "sample.txt"},
            })

            runs = team.snapshot()["agents"][0]["runs"]
            self.assertEqual(runs[0]["process"][0]["type"], "tool_start")
            self.assertEqual(runs[0]["process"][0]["payload"]["name"], "read_file")

    def test_route_history_is_initialized_independently_from_latest_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            team = TeamOrchestrator(self.settings(root), root)
            state = team.snapshot()
            self.assertEqual(state["task"]["route_history"], [])
            self.assertIsNone(state["task"]["last_decision"])

    def test_agent_output_contract_keeps_only_artifact_and_unresolved(self) -> None:
        output = TeamOrchestrator._normalize_agent_output(
            '{"artifact":{"reply":"Hello"},"unresolved":["Confirm timing"],"extra":"drop me"}'
        )
        self.assertEqual(output, {
            "artifact": {"reply": "Hello"},
            "unresolved": ["Confirm timing"],
        })

    def test_router_state_identifies_agent_output_and_available_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            team = TeamOrchestrator(self.settings(root), root)
            todo = team.task["todos"][0]
            todo["status"] = "completed"
            todo["agent_output"] = {
                "artifact": {"reply": "Draft"},
                "unresolved": ["Confirm escalation path"],
            }
            state = team._compact_router_state()
            self.assertEqual(state["completed_todo"]["agent_id"], "agent-1")
            self.assertEqual(state["agent_output"], todo["agent_output"])
            self.assertIn("available_agents", state)
            self.assertEqual(state["available_agents"][0]["status"], "idle")
            self.assertNotIn("latest_result", state)

    def test_needs_human_collects_answer_and_returns_to_router(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            team = TeamOrchestrator(self.settings(root), root)
            todo = team.task["todos"][0]
            todo["status"] = "completed"
            todo["result"] = '{"artifact":"Draft","unresolved":["Confirm refund authority"]}'
            todo["agent_output"] = {
                "artifact": "Draft", "unresolved": ["Confirm refund authority"],
            }

            team._apply_decision("needs_human")
            self.assertEqual(team.task["phase"], "human")
            self.assertIn("Confirm refund authority", team.task["pending_human_question"]["question"])

            previous_budget = team.task["max_jev_calls"]
            state = team.submit_human_answer("A supervisor approved the refund.")
            self.assertEqual(state["task"]["phase"], "agent")
            self.assertIsNone(state["task"]["pending_human_question"])
            self.assertEqual(state["task"]["max_jev_calls"], previous_budget + 1)
            self.assertEqual(
                team._compact_router_state()["latest_human_interaction"]["answer"],
                "A supervisor approved the refund.",
            )

    def test_workstream_needs_human_does_not_pause_peer_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            team = TeamOrchestrator(self.settings(root), root)
            workstream = team.task["workstreams"][0]
            latest = team.task["todos"][0]
            latest["status"] = "completed"
            latest["agent_output"] = {
                "artifact": "Draft", "unresolved": ["Confirm refund authority"],
            }
            remaining = [team.task["todos"][1], team.task["todos"][2]]

            team._apply_workstream_decision(workstream, remaining, "needs_human")

            self.assertEqual(workstream["status"], "waiting_human")
            self.assertEqual(
                team.task["pending_human_question"]["workstream_id"], "support-recovery"
            )
            self.assertEqual(team.task["todos"][3]["status"], "assigned")

            rerouted = []
            team._route_completed_workstream = rerouted.append
            state = team.submit_human_answer("A supervisor approved the refund.")
            self.assertEqual(rerouted, ["support-recovery"])
            self.assertIsNone(state["task"]["pending_human_question"])

    def test_human_answer_resumes_auto_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            team = TeamOrchestrator(self.settings(root), root)
            workstream = team.task["workstreams"][0]
            latest = team.task["todos"][0]
            latest["status"] = "completed"
            latest["agent_output"] = {"artifact": "Draft", "unresolved": ["Need approval"]}
            team._apply_workstream_decision(workstream, [team.task["todos"][1]], "needs_human")
            team.task["execution_mode"] = "auto"
            resumed = []

            def route(_workstream_id: str):
                team.task["todos"][1]["status"] = "assigned"

            def run_next():
                resumed.append(True)
                return team.snapshot()

            team._route_completed_workstream = route
            team.run_next = run_next
            team.submit_human_answer("Approved")

            self.assertEqual(resumed, [True])

    def test_fallback_prefers_least_used_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            team = TeamOrchestrator(self.settings(root), root)
            team.task["todos"][0]["status"] = "completed"
            team.task["todos"][3]["owner"] = None
            team.task["todos"][3]["status"] = "pending"
            team._apply_fallback_route()
            self.assertEqual(team.task["todos"][1]["owner"], "agent-2")


if __name__ == "__main__":
    unittest.main()
