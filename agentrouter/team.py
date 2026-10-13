from __future__ import annotations

import json
import queue
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from openai import OpenAI
from typesafe_sdk import Choice, TypeSafeClient

from .agent import Agent
from .config import Settings
from .tools import WorkspaceTools


TRANSCRIPT = (
    "I've contacted support three times and nobody has solved my problem. "
    "Every person asks me to explain everything again. I'm really frustrated "
    "and I want to speak to someone who can actually help."
)

RESCHEDULE_REQUEST = (
    "Please move my onboarding call from Tuesday at 10:00 AM to Thursday at "
    "2:00 PM. I'm in Melbourne time. Please confirm once it is changed."
)

MAX_AGENTS = 5
MAX_TASKS = 10
DEMO_TASK_IDS = {"support-recovery", "meeting-reschedule"}


def agent_spec(index: int) -> tuple[str, str]:
    number = index + 1
    return f"agent-{number}", f"Agent {number}"


class EventHub:
    def __init__(self) -> None:
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        channel: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._lock:
            self._subscribers.add(channel)
        return channel

    def unsubscribe(self, channel: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(channel)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for channel in subscribers:
            channel.put(event)


@dataclass
class TeamAgent:
    id: str
    name: str
    runtime: Agent
    role: str = ""
    status: str = "idle"
    current_todo_id: str | None = None
    last_output: str = ""
    runs: list[dict[str, Any]] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "status": self.status,
            "current_todo_id": self.current_todo_id,
            "last_output": self.last_output,
            "message_count": len(self.runtime.state.messages),
            "runs": deepcopy(self.runs),
        }


class TeamOrchestrator:
    def __init__(self, settings: Settings, project_root: Path) -> None:
        self.settings = settings
        self.project_root = project_root.resolve()
        self.chats_dir = self.project_root / "chats"
        self.tasks_dir = self.project_root / "tasks"
        self.legacy_task_file = self.tasks_dir / "parallel-demo-001" / "task.json"
        self.events = EventHub()
        self._state_lock = threading.RLock()
        self._run_lock = threading.Lock()
        self._agents: dict[str, TeamAgent] = {}
        self._event_log: list[dict[str, Any]] = []
        self._messages: list[dict[str, Any]] = []
        self._paused = False
        self._restoring = True
        self._build_initial_state()
        self._restore_task_state()
        self._restoring = False
        self._persist_task_state()

    def _build_initial_state(self) -> None:
        self.task: dict[str, Any] = {
            "id": "parallel-demo-001",
            "goal": "Complete two independent customer-support workstreams in parallel.",
            "transcript": TRANSCRIPT,
            "status": "ready",
            "phase": "agent",
            "execution_mode": "step",
            "jev_call_count": 0,
            "max_jev_calls": 6,
            "routing_count": 0,
            "handoff_count": 0,
            "final_output": "",
            "last_decision": None,
            "route_history": [],
            "pending_human_question": None,
            "pending_human_questions": [],
            "human_interactions": [],
            "workstreams": [
                {
                    "id": "support-recovery",
                    "title": "Frustrated customer response",
                    "goal": "Produce a send-ready response to a frustrated customer call.",
                    "input": TRANSCRIPT,
                    "owner": "agent-1",
                    "status": "ready",
                    "final_output": "",
                },
                {
                    "id": "meeting-reschedule",
                    "title": "Meeting reschedule confirmation",
                    "goal": "Produce a clear confirmation for a customer requesting a meeting change.",
                    "input": RESCHEDULE_REQUEST,
                    "owner": "agent-2",
                    "status": "ready",
                    "final_output": "",
                },
            ],
            "todos": [
                {"id": "support-analyze", "workstream_id": "support-recovery", "step": 1,
                 "title": "Analyze the call and draft a response", "status": "assigned",
                 "owner": "agent-1", "result": "", "agent_output": None},
                {"id": "support-review", "workstream_id": "support-recovery", "step": 2,
                 "title": "Review empathy and missing next steps", "status": "pending",
                 "owner": "agent-1", "result": "", "agent_output": None},
                {"id": "support-finalize", "workstream_id": "support-recovery", "step": 3,
                 "title": "Finalize the customer response", "status": "pending",
                 "owner": "agent-1", "result": "", "agent_output": None},
                {"id": "reschedule-analyze", "workstream_id": "meeting-reschedule", "step": 1,
                 "title": "Extract the requested schedule change", "status": "assigned",
                 "owner": "agent-2", "result": "", "agent_output": None},
                {"id": "reschedule-draft", "workstream_id": "meeting-reschedule", "step": 2,
                 "title": "Draft the confirmation message", "status": "pending",
                 "owner": "agent-2", "result": "", "agent_output": None},
                {"id": "reschedule-finalize", "workstream_id": "meeting-reschedule", "step": 3,
                 "title": "Check details and finalize", "status": "pending",
                 "owner": "agent-2", "result": "", "agent_output": None},
            ],
        }
        self._agents.clear()
        for index in range(2):
            agent_id, name = agent_spec(index)
            self._agents[agent_id] = self._make_agent(agent_id, name)
        self._event_log.clear()
        self._messages.clear()
        self._emit("task_ready", "system", {"message": "Demo task is ready"})

    def _make_agent(self, agent_id: str, name: str, role: str = "") -> TeamAgent:
        client = OpenAI(api_key=self.settings.openai.api_key,
                        base_url=self.settings.openai.base_url,
                        timeout=self.settings.openai.timeout_seconds)
        agent_tools = WorkspaceTools(
            self.settings.agent.workspace,
            allow_write=self.settings.agent.allow_write,
            max_file_chars=self.settings.agent.max_file_chars,
            max_write_chars=self.settings.agent.max_write_chars,
            allow_exec=self.settings.agent.allow_exec,
            max_command_seconds=self.settings.agent.max_command_seconds,
            max_command_output_chars=self.settings.agent.max_command_output_chars,
            allow_web=self.settings.agent.allow_web,
            max_web_results=self.settings.agent.max_web_results,
            max_web_chars=self.settings.agent.max_web_chars,
            web_timeout_seconds=self.settings.agent.web_timeout_seconds,
        ).registry()
        prompt = (
            f"{self.settings.agent.system_prompt}\n\n"
            "You are working inside a team orchestrator. Your primary responsibility is to "
            "complete the task assigned by the orchestrator. Focus on that assignment rather "
            "than taking over the whole workflow. When finished, return a concise, useful result "
            "and stop calling tools. You are a peer worker with full general-purpose capabilities."
        )
        if role:
            prompt += (
                "\n\nYour user-configured role for this agent is:\n"
                f"{role}\n"
                "Use this role as a behavioral focus when completing assignments, while retaining "
                "your full general-purpose capabilities."
            )
        runtime = Agent(
            client=client,
            model=self.settings.openai.model,
            system_prompt=prompt,
            tools=agent_tools,
            max_tool_rounds=self.settings.agent.max_tool_rounds,
            max_output_tokens=self.settings.agent.max_output_tokens,
            streaming=False,
            agent_id=agent_id,
            # Team agents are stateless workers. Durable context belongs to the task;
            # their per-run process and output remain available as audit records.
            chats_dir=None,
            on_event=lambda kind, payload, aid=agent_id: self._agent_event(aid, kind, payload),
        )
        return TeamAgent(agent_id, name, runtime, role=role)

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "task": deepcopy(self.task),
                "agents": [agent.public() for agent in self._agents.values()],
                "max_agents": MAX_AGENTS,
                "events": deepcopy(self._event_log[-80:]),
                "paused": self._paused,
                "running": self._run_lock.locked(),
            }

    def add_agent(self) -> dict[str, Any]:
        with self._state_lock:
            if len(self._agents) >= MAX_AGENTS:
                raise ValueError(f"The canvas supports at most {MAX_AGENTS} agents")
            spec = agent_spec(len(self._agents))
            agent = self._make_agent(*spec)
            self._agents[agent.id] = agent
            self._emit("agent_added", agent.id, {"name": agent.name})
            return agent.public()

    def update_task(self, *, goal: str,
                    workstreams: list[dict[str, Any]]) -> dict[str, Any]:
        if self._run_lock.locked():
            raise RuntimeError("Cannot customize tasks while a step is running")
        with self._state_lock:
            if self.task["jev_call_count"] or any(agent.runs for agent in self._agents.values()):
                raise RuntimeError("Reset the demo before customizing tasks after execution has started")
            goal = goal.strip()
            if not goal or len(goal) > 500:
                raise ValueError("task goal must be between 1 and 500 characters")
            if not 2 <= len(workstreams) <= MAX_TASKS:
                raise ValueError(f"the network supports between 2 and {MAX_TASKS} tasks")
            supplied_ids = {item.get("id") for item in workstreams}
            if not DEMO_TASK_IDS.issubset(supplied_ids):
                raise ValueError("the two demo tasks must be retained")
            current = {item["id"]: item for item in self.task["workstreams"]}
            updated_workstreams: list[dict[str, Any]] = []
            updated_todos: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            agent_ids = list(self._agents)
            for index, update in enumerate(workstreams):
                workstream_id = update.get("id", "")
                if not workstream_id or workstream_id.startswith("new-"):
                    workstream_id = f"custom-{uuid4().hex[:8]}"
                if workstream_id in seen_ids:
                    raise ValueError("each task must be provided exactly once")
                seen_ids.add(workstream_id)
                title = update.get("title", "").strip()
                workstream_goal = update.get("goal", "").strip()
                workstream_input = update.get("input", "").strip()
                if not title or len(title) > 120:
                    raise ValueError("task title must be between 1 and 120 characters")
                if not workstream_goal or len(workstream_goal) > 500:
                    raise ValueError("task goal must be between 1 and 500 characters")
                if not workstream_input or len(workstream_input) > 8000:
                    raise ValueError("task input must be between 1 and 8000 characters")
                todo_titles = [str(item).strip() for item in update.get("todos", [])]
                if not todo_titles or any(not item or len(item) > 200 for item in todo_titles):
                    raise ValueError("each task needs at least one Todo of up to 200 characters")
                owner = current.get(workstream_id, {}).get("owner") or agent_ids[index % len(agent_ids)]
                updated_workstreams.append({
                    "id": workstream_id, "title": title, "goal": workstream_goal,
                    "input": workstream_input, "owner": owner, "status": "ready",
                    "final_output": "",
                })
                for step, todo_title in enumerate(todo_titles, start=1):
                    updated_todos.append({
                        "id": f"{workstream_id}-{step}", "workstream_id": workstream_id,
                        "step": step, "title": todo_title,
                        "status": "assigned" if step == 1 else "pending",
                        "owner": owner, "result": "", "agent_output": None,
                    })
            self.task["goal"] = goal
            self.task["workstreams"] = updated_workstreams
            self.task["todos"] = updated_todos
            self.task["transcript"] = next(
                item["input"] for item in updated_workstreams if item["id"] == "support-recovery"
            )
            self._emit("task_customized", "user", {
                "goal": goal,
                "workstreams": deepcopy(updated_workstreams),
                "todos": deepcopy(updated_todos),
            })
            return self.snapshot()

    def update_agent(self, agent_id: str, *, name: str,
                     role: str | None = None) -> dict[str, Any]:
        with self._state_lock:
            agent = self._require_agent(agent_id)
            name = name.strip()
            if not name:
                raise ValueError("name cannot be empty")
            if len(name) > 40:
                raise ValueError("name is too long")
            if role is not None:
                role = role.strip()
                if "\n" in role or "\r" in role:
                    raise ValueError("role must be a single line")
                if len(role) > 240:
                    raise ValueError("role is too long")
                agent.role = role
            agent.name = name
            # ChatStore restores this agent's isolated conversation automatically.
            agent.runtime = self._make_agent(agent_id, name, agent.role).runtime
            self._emit("agent_updated", agent_id, {"name": name, "role": agent.role})
            return agent.public()

    def set_paused(self, paused: bool) -> None:
        with self._state_lock:
            self._paused = paused
            self._emit("task_paused" if paused else "task_resumed", "system", {})

    def submit_human_answer(self, answer: str) -> dict[str, Any]:
        with self._state_lock:
            pending = self.task.get("pending_human_question")
            answer = answer.strip()
            if not pending:
                raise RuntimeError("There is no pending question for the user")
            if not answer:
                raise ValueError("The answer cannot be empty")
            if len(answer) > 4000:
                raise ValueError("The answer is too long")
            interaction = {**pending, "answer": answer,
                           "answered_at": datetime.now(timezone.utc).isoformat()}
            self.task["human_interactions"].append(interaction)
            workstream_id = interaction.get("workstream_id")
            queued = self.task["pending_human_questions"]
            self.task["pending_human_question"] = queued.pop(0) if queued else None
            # A user answer explicitly authorizes one additional JEV re-evaluation.
            self.task["max_jev_calls"] += 1
            self.task["status"] = "ready"
            self.task["phase"] = "agent"
            if workstream_id:
                self._workstream(workstream_id)["status"] = "ready"
            self._emit("human_answered", "ask-user", {
                "question": interaction["question"], "answer": answer,
                "workstream_id": workstream_id,
            })
        if workstream_id:
            self._route_completed_workstream(workstream_id)
        with self._state_lock:
            auto_resume = (
                self.task["execution_mode"] == "auto"
                and not self._run_lock.locked()
                and any(item["status"] == "assigned" for item in self.task["todos"])
            )
        if auto_resume:
            return self.run_next()
        return self.snapshot()

    def reset(self) -> dict[str, Any]:
        if self._run_lock.locked():
            raise RuntimeError("Cannot reset while a step is running")
        with self._state_lock:
            for agent in self._agents.values():
                agent.runtime.reset()
            self._paused = False
            self._build_initial_state()
            return self.snapshot()

    def run_next(self) -> dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("A step is already running")
        try:
            with self._state_lock:
                if self._paused:
                    raise RuntimeError("The task is paused")
                if self.task["status"] == "completed":
                    return self.snapshot()
                self.task["status"] = "running"
                phase = self.task["phase"]
            if phase == "agent":
                self._run_agent_wave(auto_chain=self.task["execution_mode"] == "auto")
            elif phase == "router":
                self._run_router_step()
            elif phase == "human":
                raise RuntimeError("Answer the pending user question before continuing")
            else:
                raise RuntimeError(f"Unknown task phase: {phase}")
            return self.snapshot()
        except Exception as exc:
            with self._state_lock:
                if self.task.get("pending_human_question"):
                    self.task["status"] = "needs_human"
                else:
                    self.task["status"] = "failed"
                    self._emit("step_failed", "system", {"error": f"{type(exc).__name__}: {exc}"})
            raise
        finally:
            self._run_lock.release()

    def set_execution_mode(self, mode: str) -> dict[str, Any]:
        if mode not in {"step", "auto"}:
            raise ValueError("execution mode must be 'step' or 'auto'")
        with self._state_lock:
            if self._run_lock.locked():
                raise RuntimeError("Cannot change execution mode while work is running")
            self.task["execution_mode"] = mode
            self._emit("execution_mode_changed", "system", {"mode": mode})
            return self.snapshot()

    def _run_agent_wave(self, *, auto_chain: bool = False) -> None:
        """Run ready todos concurrently, dynamically filling free agents in auto mode."""
        with self._state_lock:
            todos = self._next_assigned_todos_by_agent()
        if not todos:
            self._assign_next_workstream_steps()
            with self._state_lock:
                todos = self._next_assigned_todos_by_agent()
        if not todos:
            with self._state_lock:
                self._complete_parallel_demo_if_done()
                if self.task["status"] != "completed":
                    self.task["status"] = "ready"
                    self.task["phase"] = "agent"
            return

        errors: list[Exception] = []
        submitted: set[str] = set()
        futures: dict[Future[None], str] = {}

        def submit_ready(pool: ThreadPoolExecutor) -> None:
            with self._state_lock:
                ready = self._next_assigned_todos_by_agent()
                for ready_todo in ready:
                    if ready_todo["id"] in submitted:
                        continue
                    submitted.add(ready_todo["id"])
                    futures[pool.submit(self._run_one_todo_then_route, ready_todo)] = ready_todo["id"]

        with ThreadPoolExecutor(
            max_workers=len(self._agents), thread_name_prefix="agent-workstream"
        ) as pool:
            submit_ready(pool)
            while futures:
                completed, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
                for future in completed:
                    futures.pop(future)
                    try:
                        future.result()
                    except Exception as exc:
                        errors.append(exc)
                if auto_chain and not errors:
                    with self._state_lock:
                        may_continue = (
                            not self._paused
                            and self.task["status"] not in {"completed", "failed"}
                        )
                    if may_continue:
                        submit_ready(pool)
        if errors:
            raise errors[0]
        with self._state_lock:
            self._complete_parallel_demo_if_done()
            if self.task["status"] != "completed":
                self.task["status"] = "ready"
                self.task["phase"] = "agent"

    def _run_one_todo_then_route(self, todo: dict[str, Any]) -> None:
        self._run_one_todo(todo)
        self._route_completed_workstream(todo["workstream_id"])

    def _next_assigned_todos_by_agent(self) -> list[dict[str, Any]]:
        """Return at most one queued todo per agent for the next parallel wave."""
        selected: dict[str, dict[str, Any]] = {}
        for todo in sorted(self.task["todos"], key=lambda item: (item["step"], item["id"])):
            owner = todo.get("owner")
            if todo["status"] == "assigned" and owner and owner not in selected:
                selected[owner] = todo
        return list(selected.values())

    def _run_one_todo(self, todo: dict[str, Any]) -> None:
        with self._state_lock:
            agent = self._require_agent(todo["owner"])
            todo["status"] = "running"
            workstream = self._workstream(todo["workstream_id"])
            workstream["status"] = "running"
            agent.status = "running"
            agent.current_todo_id = todo["id"]
            assignment = self._assignment_prompt(agent, todo)
            run_record = {
                "id": uuid4().hex,
                "todo_id": todo["id"],
                "todo_title": todo["title"],
                "input": assignment,
                "process": [],
                "output": "",
                "status": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "completed_at": None,
                "model_calls": 0,
                "tool_calls": 0,
            }
            agent.runs.append(run_record)
            self._emit("agent_started", agent.id, {
                "run_id": run_record["id"], "todo_id": todo["id"],
                "workstream_id": todo["workstream_id"],
                "title": todo["title"], "input": assignment,
            })

        try:
            # Never carry an agent's previous assignment conversation into this run.
            # The assignment prompt below is rebuilt from the authoritative workstream state.
            agent.runtime.reset()
            result = agent.runtime.prompt(assignment)
        except Exception:
            with self._state_lock:
                agent.status = "idle"
                agent.current_todo_id = None
                run_record["status"] = "failed"
                run_record["completed_at"] = datetime.now(timezone.utc).isoformat()
                todo["status"] = "failed"
                workstream["status"] = "failed"
            raise

        with self._state_lock:
            agent.status = "idle"
            agent.current_todo_id = None
            agent_output = self._normalize_agent_output(result.output)
            canonical_output = json.dumps(agent_output, ensure_ascii=False, indent=2)
            agent.last_output = canonical_output
            run_record["output"] = canonical_output
            run_record["status"] = "completed"
            run_record["completed_at"] = datetime.now(timezone.utc).isoformat()
            run_record["model_calls"] = result.model_call_count
            run_record["tool_calls"] = result.tool_call_count
            todo["status"] = "completed"
            todo["result"] = canonical_output
            todo["agent_output"] = agent_output
            # Keep the provider-native response items with the readable assistant message.
            # This preserves tool/model details without making them the task's state model.
            self._emit("agent_completed", agent.id, {
                "run_id": run_record["id"], "todo_id": todo["id"],
                "workstream_id": todo["workstream_id"], "output": canonical_output,
                "model_calls": result.model_call_count, "tool_calls": result.tool_call_count,
            })
            response_items = getattr(result, "response_items", None)
            if isinstance(response_items, list) and self._messages:
                self._messages[-1]["openai_items"] = response_items
                self._persist_task_state()
            workstream["status"] = "ready"

    def _route_completed_workstream(self, workstream_id: str) -> None:
        """Route a completed workstream immediately, without waiting for peer agents."""
        with self._state_lock:
            workstream = self._workstream(workstream_id)
            todos = sorted(
                (item for item in self.task["todos"] if item["workstream_id"] == workstream_id),
                key=lambda item: item["step"],
            )
            remaining = [item for item in todos if item["status"] == "pending"]
            if self.task["jev_call_count"] >= self.task["max_jev_calls"]:
                self._apply_workstream_fallback(workstream, remaining)
                return
            latest = next((item for item in reversed(todos) if item["status"] == "completed"), None)
            source_agent = self._agents.get(latest["owner"]) if latest else None
            if source_agent is not None:
                source_agent.status = "routing"
            state = {
                "overall_goal": self.task["goal"],
                "workstream": {
                    "id": workstream["id"],
                    "title": workstream["title"],
                    "goal": workstream["goal"],
                    "status": workstream["status"],
                },
                "completed_step": ({
                    "id": latest["id"], "title": latest["title"], "step": latest["step"],
                    "owner": latest["owner"], "agent_output": self._todo_agent_output(latest),
                } if latest else None),
                "remaining_steps": [
                    {"id": item["id"], "title": item["title"], "step": item["step"]}
                    for item in remaining
                ],
                "latest_human_interaction": next((
                    deepcopy(item) for item in reversed(self.task["human_interactions"])
                    if item.get("workstream_id") == workstream_id
                ), None),
                "agents": [self._router_agent_state(agent) for agent in self._agents.values()],
            }
            criteria = self._workstream_route_criteria(source_agent)
            self.task["routing_count"] += 1
            workstream["status"] = "routing"
            self._emit("router_started", "jev", {
                "workstream_id": workstream_id, "state": state, "criteria": criteria,
            })

        route_record = {
            "id": uuid4().hex,
            "workstream_id": workstream_id,
            "input": state,
            "question": {
                "type": "choice",
                "instructions": (
                    "Choose the next action for this workstream only. Consider the latest output, "
                    "the remaining planned steps, and current agent availability. Finish early only "
                    "when the artifact is already send-ready and the remaining steps add no value."
                ),
                "criteria": criteria,
            },
            "output": None,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
        }
        try:
            with TypeSafeClient(api_key=self.settings.jev.api_key,
                                model=self.settings.jev.model) as client:
                response = client.system_one(
                    state=state,
                    questions={"next_action": Choice(
                        instructions=route_record["question"]["instructions"],
                        criteria=criteria,
                    )},
                )
            answer = response.choices["next_action"]
            raw = response.raw_http_response.json()
            decision = {
                "choice": answer.choice,
                "confidence": answer.confidence,
                "probabilities": answer.probabilities,
                "model": raw.get("model"),
                "usage": raw.get("usage", {}),
                "workstream_id": workstream_id,
            }
            with self._state_lock:
                self.task["jev_call_count"] += 1
                self.task["last_decision"] = decision
                route_record["output"] = decision
                route_record["completed_at"] = datetime.now(timezone.utc).isoformat()
                self.task["route_history"].append(route_record)
                self._emit("router_decided", "jev", decision)
                self._apply_workstream_decision(workstream, remaining, answer.choice)
        finally:
            with self._state_lock:
                self.task["routing_count"] = max(0, self.task["routing_count"] - 1)
                if source_agent is not None and source_agent.status == "routing":
                    source_agent.status = (
                        "assigned" if any(
                            item["status"] == "assigned" and item.get("owner") == source_agent.id
                            for item in self.task["todos"]
                        ) else "idle"
                    )

    def _workstream_route_criteria(self, source_agent: TeamAgent | None = None) -> dict[str, str]:
        criteria = {
            f"assign_{agent.id.replace('-', '_')}": (
                f"Assign the next step to available peer {agent.name}; current status: {agent.status}."
            )
            for agent in self._agents.values()
            if agent.status == "idle" or agent is source_agent
        }
        criteria["finish"] = (
            "The workstream output is complete and no remaining planned step is needed"
        )
        criteria["needs_human"] = (
            "The workstream cannot safely continue without information only the user can provide"
        )
        return criteria

    def _apply_workstream_decision(self, workstream: dict[str, Any],
                                   remaining: list[dict[str, Any]], choice: str) -> None:
        if choice == "needs_human":
            latest = next(
                item for item in reversed(self.task["todos"])
                if item["workstream_id"] == workstream["id"] and item["status"] == "completed"
            )
            unresolved = self._todo_agent_output(latest).get("unresolved", [])
            details = "\n".join(f"- {item}" for item in unresolved)
            question = (
                f"The workstream needs your input on:\n{details}" if details
                else f"What information should the team use to continue {workstream['title']}?"
            )
            request = {
                "id": uuid4().hex,
                "workstream_id": workstream["id"],
                "question": question,
                "asked_at": datetime.now(timezone.utc).isoformat(),
            }
            if self.task["pending_human_question"] is None:
                self.task["pending_human_question"] = request
            else:
                self.task["pending_human_questions"].append(request)
            workstream["status"] = "waiting_human"
            self._emit("human_question", "ask-user", {
                "workstream_id": workstream["id"], "question": question,
            })
            return
        if choice == "finish" or not remaining:
            latest = next(
                item for item in reversed(self.task["todos"])
                if item["workstream_id"] == workstream["id"] and item["status"] == "completed"
            )
            workstream["final_output"] = self._artifact_text(
                self._todo_agent_output(latest)["artifact"]
            )
            workstream["status"] = "completed"
            for item in remaining:
                item["status"] = "skipped"
            self._emit("workstream_completed", "jev", {
                "workstream_id": workstream["id"],
                "final_output": workstream["final_output"],
            })
            return
        agent_id = choice.removeprefix("assign_").replace("_", "-")
        agent = self._agents.get(agent_id)
        if agent is None or agent.status not in {"idle", "routing"}:
            self._apply_workstream_fallback(workstream, remaining)
            return
        next_todo = remaining[0]
        next_todo["owner"] = agent_id
        next_todo["status"] = "assigned"
        agent.status = "assigned"
        workstream["owner"] = agent_id
        workstream["status"] = "ready"
        self.task["handoff_count"] += 1
        self._emit("todo_assigned", agent_id, {
            "todo_id": next_todo["id"], "title": next_todo["title"],
            "workstream_id": workstream["id"],
        })

    def _apply_workstream_fallback(self, workstream: dict[str, Any],
                                   remaining: list[dict[str, Any]]) -> None:
        if not remaining:
            self._apply_workstream_decision(workstream, remaining, "finish")
            return
        preferred = self._agents.get(workstream["owner"])
        available = preferred if preferred and preferred.status == "idle" else next(
            (agent for agent in self._agents.values() if agent.status == "idle"), None
        )
        if available is None:
            workstream["status"] = "waiting"
            return
        next_todo = remaining[0]
        next_todo["owner"] = available.id
        next_todo["status"] = "assigned"
        available.status = "assigned"
        workstream["owner"] = available.id
        workstream["status"] = "ready"
        self._emit("fallback_route", available.id, {
            "todo_id": next_todo["id"], "workstream_id": workstream["id"],
        })

    def _assign_next_workstream_steps(self) -> None:
        with self._state_lock:
            for workstream in self.task["workstreams"]:
                todos = sorted(
                    (item for item in self.task["todos"]
                     if item["workstream_id"] == workstream["id"]),
                    key=lambda item: item["step"],
                )
                if any(item["status"] in {"assigned", "running", "failed"} for item in todos):
                    continue
                pending = next((item for item in todos if item["status"] == "pending"), None)
                if pending is not None:
                    pending["status"] = "assigned"
                    self._emit("todo_assigned", pending["owner"], {
                        "todo_id": pending["id"], "title": pending["title"],
                        "workstream_id": workstream["id"],
                    })

    def _complete_parallel_demo_if_done(self) -> None:
        for workstream in self.task["workstreams"]:
            todos = [item for item in self.task["todos"]
                     if item["workstream_id"] == workstream["id"]]
            if todos and all(item["status"] == "completed" for item in todos):
                latest = max(todos, key=lambda item: item["step"])
                output = self._todo_agent_output(latest)
                workstream["final_output"] = self._artifact_text(output["artifact"])
                workstream["status"] = "completed"
        if all(item["status"] == "completed" for item in self.task["workstreams"]):
            self.task["final_output"] = json.dumps({
                item["id"]: item["final_output"] for item in self.task["workstreams"]
            }, ensure_ascii=False, indent=2)
            self.task["status"] = "completed"
            self.task["phase"] = "done"
            self._emit("task_completed", "system", {"final_output": self.task["final_output"]})

    def _run_router_step(self) -> None:
        with self._state_lock:
            if not self.settings.jev.api_key:
                raise ValueError("JEV API key is empty in settings.json")
            if self.task["jev_call_count"] >= self.task["max_jev_calls"]:
                self._apply_fallback_route()
                return
            state = self._compact_router_state()
            criteria = {
                f"assign_{agent.id.replace('-', '_')}": f"Assign the next work to idle peer {agent.name}"
                for agent in self._agents.values()
                if agent.status == "idle"
            }
            criteria.update({
                "finish": "The latest result is already send-ready; no more agent work is required",
                "needs_human": "The next step requires a human decision",
            })
            self._emit("router_started", "jev", {"state": state, "criteria": criteria})

        route_record = {
            "id": uuid4().hex,
            "input": state,
            "question": {
                "type": "choice",
                "instructions": (
                    "Choose the best next team action. Use the latest completed work, remaining "
                    "todo, latest user answer when present, and each agent's live status. "
                    "Choose needs_human only when the missing information must come from the user. "
                    "Prefer independent review before finishing when the current result still has "
                    "important omissions."
                ),
                "criteria": criteria,
            },
            "output": None,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
        }

        with TypeSafeClient(api_key=self.settings.jev.api_key, model=self.settings.jev.model) as client:
            response = client.system_one(
                state=state,
                questions={"next_action": Choice(
                    instructions=route_record["question"]["instructions"],
                    criteria=criteria,
                )},
            )
        answer = response.choices["next_action"]
        raw = response.raw_http_response.json()
        decision = {
            "choice": answer.choice,
            "confidence": answer.confidence,
            "probabilities": answer.probabilities,
            "model": raw.get("model"),
            "usage": raw.get("usage", {}),
        }

        with self._state_lock:
            self.task["jev_call_count"] += 1
            self.task["last_decision"] = decision
            route_record["output"] = decision
            route_record["completed_at"] = datetime.now(timezone.utc).isoformat()
            self.task["route_history"].append(route_record)
            self._emit("router_decided", "jev", decision)
            self._apply_decision(answer.choice)

    def _apply_decision(self, choice: str) -> None:
        remaining = [todo for todo in self.task["todos"] if todo["status"] == "pending"]
        if choice == "needs_human":
            latest = next((todo for todo in reversed(self.task["todos"]) if todo["result"]), None)
            output = self._todo_agent_output(latest) if latest else {"unresolved": []}
            unresolved = output.get("unresolved", [])
            if unresolved:
                details = "\n".join(f"- {item}" for item in unresolved)
                question = f"The team needs your input on the following unresolved items:\n{details}"
            else:
                question = "What additional information should the team use before continuing?"
            self.task["pending_human_question"] = {
                "id": uuid4().hex,
                "question": question,
                "asked_at": datetime.now(timezone.utc).isoformat(),
            }
            self.task["status"] = "needs_human"
            self.task["phase"] = "human"
            self._emit("human_question", "ask-user", {"question": question})
            return
        if choice == "finish":
            latest = next((todo for todo in reversed(self.task["todos"]) if todo["result"]), None)
            output = self._todo_agent_output(latest) if latest else {"artifact": "", "unresolved": []}
            self.task["final_output"] = self._artifact_text(output["artifact"])
            for todo in remaining:
                todo["status"] = "skipped"
            self.task["status"] = "completed"
            self.task["phase"] = "done"
            self._emit("task_completed", "jev", {"final_output": self.task["final_output"]})
            return
        agent_id = choice.removeprefix("assign_").replace("_", "-")
        if agent_id not in self._agents:
            self._apply_fallback_route()
            return
        if not remaining:
            self.task["status"] = "completed"
            self.task["phase"] = "done"
            return
        todo = remaining[0]
        todo["owner"] = agent_id
        todo["status"] = "assigned"
        self.task["handoff_count"] += 1
        self.task["phase"] = "agent"
        self._emit("todo_assigned", agent_id, {"todo_id": todo["id"], "title": todo["title"]})

    def _apply_fallback_route(self) -> None:
        remaining = [todo for todo in self.task["todos"] if todo["status"] == "pending"]
        if not remaining:
            self.task["status"] = "completed"
            self.task["phase"] = "done"
            return
        assignment_counts = {
            agent_id: sum(1 for todo in self.task["todos"] if todo.get("owner") == agent_id)
            for agent_id in self._agents
        }
        available = min(
            (agent for agent in self._agents.values() if agent.status == "idle"),
            key=lambda agent: assignment_counts[agent.id],
            default=None,
        )
        if available is None:
            raise RuntimeError("No idle agent is available")
        todo = remaining[0]
        todo["owner"], todo["status"] = available.id, "assigned"
        self.task["phase"] = "agent"
        self._emit("fallback_route", available.id, {"todo_id": todo["id"]})

    def _assignment_prompt(self, agent: TeamAgent, todo: dict[str, Any]) -> str:
        workstream = self._workstream(todo["workstream_id"])
        completed = [{"title": item["title"], "agent_output": self._todo_agent_output(item)}
                     for item in self.task["todos"]
                     if item["workstream_id"] == workstream["id"]
                     and item["status"] == "completed"]
        human_context = [{"question": item["question"], "answer": item["answer"]}
                         for item in self.task["human_interactions"]
                         if item.get("workstream_id") == workstream["id"]]
        return (
            f"Overall task goal:\n{self.task['goal']}\n\n"
            f"Workstream:\n{workstream['title']}\n\n"
            f"Workstream goal:\n{workstream['goal']}\n\n"
            f"Workstream input:\n{workstream['input']}\n\n"
            f"Your assigned todo:\n{todo['title']}\n\n"
            f"Previous completed work:\n{json.dumps(completed, ensure_ascii=False)}\n\n"
            f"Relevant human context:\n{json.dumps(human_context, ensure_ascii=False)}\n\n"
            "Return exactly one valid JSON object with only these two top-level keys:\n"
            "{\"artifact\": <the completed work>, \"unresolved\": [<remaining issue>, ...]}\n"
            "The artifact may be a string, object, or array and must contain the actual completed "
            "work. The unresolved value must be an array of short English strings. Use an empty "
            "array only when you found no remaining issues. Do not recommend or name the next agent. "
            "All content must be written in English. Do not include Markdown fences or any text "
            "outside the JSON object."
        )

    def _workstream(self, workstream_id: str) -> dict[str, Any]:
        workstream = next((item for item in self.task["workstreams"]
                           if item["id"] == workstream_id), None)
        if workstream is None:
            raise ValueError(f"Unknown workstream: {workstream_id}")
        return workstream

    def _compact_router_state(self) -> dict[str, Any]:
        completed = [item for item in self.task["todos"] if item["status"] == "completed"]
        latest = completed[-1] if completed else None
        state = {
            "goal": self.task["goal"],
            "completed_todo": ({
                "title": latest["title"],
                "agent_id": latest["owner"],
            } if latest else None),
            "agent_output": self._todo_agent_output(latest) if latest else None,
            "latest_human_interaction": (
                deepcopy(self.task["human_interactions"][-1])
                if self.task["human_interactions"] else None
            ),
            "remaining_todos": [item["title"] for item in self.task["todos"]
                                if item["status"] == "pending"],
            "available_agents": [self._router_agent_state(agent)
                                  for agent in self._agents.values() if agent.status == "idle"],
        }
        return state

    @staticmethod
    def _router_agent_state(agent: TeamAgent) -> dict[str, Any]:
        state: dict[str, Any] = {
            "id": agent.id,
            "status": agent.status,
            "current_todo_id": agent.current_todo_id,
        }
        if agent.role:
            state["role"] = agent.role
        return state

    @staticmethod
    def _normalize_agent_output(raw_output: str) -> dict[str, Any]:
        candidate = raw_output.strip()
        if candidate.startswith("```"):
            lines = candidate.splitlines()
            candidate = "\n".join(lines[1:-1]).strip() if len(lines) >= 3 else candidate
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            return {
                "artifact": raw_output,
                "unresolved": ["The response did not follow the required structured output format."],
            }
        if not isinstance(parsed, dict) or "artifact" not in parsed:
            return {
                "artifact": parsed,
                "unresolved": ["The response did not include the required artifact field."],
            }
        unresolved = parsed.get("unresolved", [])
        if not isinstance(unresolved, list):
            unresolved = [str(unresolved)]
        return {
            "artifact": parsed["artifact"],
            "unresolved": [str(item) for item in unresolved],
        }

    def _todo_agent_output(self, todo: dict[str, Any]) -> dict[str, Any]:
        stored = todo.get("agent_output")
        if isinstance(stored, dict):
            return deepcopy(stored)
        return self._normalize_agent_output(str(todo.get("result", "")))

    @staticmethod
    def _artifact_text(artifact: Any) -> str:
        if isinstance(artifact, str):
            return artifact
        return json.dumps(artifact, ensure_ascii=False, indent=2)

    def _agent_event(self, agent_id: str, kind: str, payload: dict[str, Any]) -> None:
        if kind in {"model_request", "tool_start", "tool_end", "stream_end"}:
            safe = dict(payload)
            if "output" in safe:
                safe["output"] = str(safe["output"])[:1000]
            with self._state_lock:
                agent = self._agents.get(agent_id)
                active_run = agent.runs[-1] if agent and agent.runs and agent.runs[-1]["status"] == "running" else None
                if active_run is not None:
                    safe["run_id"] = active_run["id"]
                    active_run["process"].append({
                        "type": kind,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "payload": safe,
                    })
            self._emit(kind, agent_id, safe)

    def _emit(self, event_type: str, actor_id: str, payload: dict[str, Any]) -> None:
        event = {
            "id": uuid4().hex,
            "type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with self._state_lock:
            self._event_log.append(event)
            if len(self._event_log) > 200:
                del self._event_log[:-200]
            self._messages.append(self._event_message(event))
            if not self._restoring:
                self._persist_task_state()
        self.events.publish(event)

    @staticmethod
    def _event_message(event: dict[str, Any]) -> dict[str, Any]:
        """Represent orchestration activity as one chronological, OpenAI-style message."""
        event_type = event["type"]
        payload = event["payload"]
        if event_type in {"task_customized", "human_answered", "agent_started"}:
            role = "user"
        elif event_type in {"model_request", "tool_start", "tool_end", "stream_end"}:
            role = "tool"
        elif event["actor_id"] == "system":
            role = "system"
        else:
            role = "assistant"
        preferred_key = {
            "task_ready": "message",
            "agent_started": "input",
            "agent_completed": "output",
            "human_question": "question",
            "human_answered": "answer",
            "task_completed": "final_output",
            "workstream_completed": "final_output",
        }.get(event_type)
        content = payload.get(preferred_key) if preferred_key else None
        if not isinstance(content, str):
            content = json.dumps(payload, ensure_ascii=False)
        message = {
            "role": role,
            "content": content,
            "timestamp": event["timestamp"],
            "actor_id": event["actor_id"],
            "type": event_type,
            "event_id": event["id"],
            "metadata": {"payload": payload},
        }
        if event_type == "agent_completed" and isinstance(payload.get("openai_items"), list):
            message["openai_items"] = payload["openai_items"]
        return message

    def _persist_task_state(self) -> None:
        """Persist one self-contained JSON document per user-visible Task."""
        if self._restoring:
            return
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        network_state = deepcopy(self.task)
        for key in ("workstreams", "todos", "transcript", "human_interactions",
                    "route_history", "pending_human_question", "pending_human_questions"):
            network_state.pop(key, None)
        for workstream_index, workstream in enumerate(self.task["workstreams"]):
            workstream_id = workstream["id"]
            todos = [deepcopy(item) for item in self.task["todos"]
                     if item["workstream_id"] == workstream_id]
            messages = self._messages_for_workstream(workstream_id)
            involved_agents = {
                item.get("owner") for item in todos if item.get("owner")
            } | {
                item.get("actor_id") for item in messages
                if str(item.get("actor_id", "")).startswith("agent-")
            }
            task_record = deepcopy(workstream)
            task_record.update({
                "network_id": self.task["id"],
                "network_goal": self.task["goal"],
                "network_index": workstream_index,
                "todos": todos,
                "human_interactions": [deepcopy(item) for item in self.task["human_interactions"]
                                       if item.get("workstream_id") == workstream_id],
                "route_history": [deepcopy(item) for item in self.task["route_history"]
                                  if item.get("workstream_id") == workstream_id],
                "pending_human_question": deepcopy(self.task["pending_human_question"])
                if (self.task.get("pending_human_question") or {}).get("workstream_id") == workstream_id
                else None,
                "pending_human_questions": [deepcopy(item)
                                            for item in self.task["pending_human_questions"]
                                            if item.get("workstream_id") == workstream_id],
            })
            payload = {
                "version": 2,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "task": task_record,
                "network_state": network_state,
                "agent_configs": [{
                    "id": agent.id, "name": agent.name, "role": agent.role,
                } for agent in self._agents.values() if agent.id in involved_agents],
                "messages": messages,
                "paused": self._paused,
            }
            path = self.tasks_dir / f"{workstream_id}.json"
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        if self.legacy_task_file.is_file():
            self.legacy_task_file.unlink()
            try:
                self.legacy_task_file.parent.rmdir()
            except OSError:
                pass

    def _messages_for_workstream(self, workstream_id: str) -> list[dict[str, Any]]:
        todo_ids = {item["id"] for item in self.task["todos"]
                    if item["workstream_id"] == workstream_id}
        run_ids: set[str] = set()
        selected: list[dict[str, Any]] = []
        for message in self._messages:
            payload = message.get("metadata", {}).get("payload", {})
            if payload.get("workstream_id") == workstream_id or payload.get("todo_id") in todo_ids:
                if isinstance(payload.get("run_id"), str):
                    run_ids.add(payload["run_id"])
        for message in self._messages:
            payload = message.get("metadata", {}).get("payload", {})
            include = (
                payload.get("workstream_id") == workstream_id
                or payload.get("todo_id") in todo_ids
                or payload.get("run_id") in run_ids
                or message.get("type") in {"task_ready", "task_completed", "task_paused",
                                           "task_resumed", "execution_mode_changed"}
            )
            if message.get("type") == "task_customized":
                workstream = next(item for item in self.task["workstreams"]
                                  if item["id"] == workstream_id)
                tailored = deepcopy(message)
                tailored_payload = {
                    "network_goal": self.task["goal"],
                    "workstream": deepcopy(workstream),
                    "todos": [deepcopy(item) for item in self.task["todos"]
                              if item["workstream_id"] == workstream_id],
                    "workstream_id": workstream_id,
                }
                tailored["content"] = json.dumps(tailored_payload, ensure_ascii=False)
                tailored["metadata"] = {"payload": tailored_payload}
                selected.append(tailored)
            elif include:
                selected.append(deepcopy(message))
        return selected

    def _restore_task_state(self) -> None:
        """Restore per-Task files, with one-time support for the legacy parent snapshot."""
        paths = sorted(self.tasks_dir.glob("*.json")) if self.tasks_dir.is_dir() else []
        if not paths and self.legacy_task_file.is_file():
            try:
                saved = json.loads(self.legacy_task_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return
            task = saved.get("task")
            if not isinstance(task, dict) or not isinstance(task.get("workstreams"), list):
                return
            self.task = task
            self._messages = saved.get("messages") if isinstance(saved.get("messages"), list) else []
            agents = saved.get("agent_configs", saved.get("agents", []))
            paused = bool(saved.get("paused", False))
        elif paths:
            documents = []
            for path in paths:
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(value.get("task"), dict):
                    documents.append(value)
            if not documents:
                return
            documents.sort(key=lambda item: item["task"].get("network_index", 0))
            network = deepcopy(documents[0].get("network_state", {}))
            network.setdefault("id", documents[0]["task"].get("network_id", "parallel-demo-001"))
            network.setdefault("goal", documents[0]["task"].get("network_goal", ""))
            network["workstreams"], network["todos"] = [], []
            network["human_interactions"], network["route_history"] = [], []
            pending, queued = None, []
            agent_map: dict[str, dict[str, Any]] = {}
            message_map: dict[tuple[str, str], dict[str, Any]] = {}
            for document in documents:
                record = deepcopy(document["task"])
                todos = record.pop("todos", [])
                network["human_interactions"].extend(record.pop("human_interactions", []))
                network["route_history"].extend(record.pop("route_history", []))
                candidate = record.pop("pending_human_question", None)
                if candidate and pending is None:
                    pending = candidate
                queued.extend(record.pop("pending_human_questions", []))
                record.pop("network_id", None)
                record.pop("network_goal", None)
                record.pop("network_index", None)
                network["workstreams"].append(record)
                network["todos"].extend(todos)
                for item in document.get("agent_configs", []):
                    if isinstance(item, dict) and item.get("id"):
                        agent_map[item["id"]] = item
                for message in document.get("messages", []):
                    if isinstance(message, dict):
                        payload = message.get("metadata", {}).get("payload", {})
                        key = (str(message.get("event_id", "")), str(payload.get("workstream_id", "")))
                        message_map[key] = message
            network["pending_human_question"] = pending
            network["pending_human_questions"] = queued
            network["transcript"] = next((item.get("input", "") for item in network["workstreams"]
                                          if item.get("id") == "support-recovery"), "")
            self.task = network
            self._messages = sorted(message_map.values(), key=lambda item: item.get("timestamp", ""))
            agents = list(agent_map.values())
            paused = any(bool(item.get("paused")) for item in documents)
        else:
            return
        self.task["routing_count"] = 0
        if self.task.get("status") == "running":
            self.task["status"] = "ready"
        for todo in self.task.get("todos", []):
            if todo.get("status") == "running":
                todo["status"] = "assigned"
        for workstream in self.task.get("workstreams", []):
            if workstream.get("status") in {"running", "routing"}:
                workstream["status"] = "ready"
        self._agents.clear()
        for item in agents[:MAX_AGENTS]:
            agent_id = str(item.get("id", ""))
            if not agent_id:
                continue
            agent = self._make_agent(
                agent_id,
                str(item.get("name") or agent_id),
                str(item.get("role") or ""),
            )
            agent.runs = self._runs_from_messages(agent_id)
            agent.last_output = agent.runs[-1]["output"] if agent.runs else ""
            agent.status = "assigned" if any(
                todo.get("status") == "assigned" and todo.get("owner") == agent_id
                for todo in self.task.get("todos", [])
            ) else "idle"
            self._agents[agent_id] = agent
        # Rebuild the UI projection from the single durable message timeline.
        self._event_log = [{
            "id": item.get("event_id", uuid4().hex),
            "type": item.get("type", "message"),
            "actor_id": item.get("actor_id", item.get("role", "system")),
            "payload": item.get("metadata", {}).get("payload", {"content": item.get("content", "")}),
            "timestamp": item.get("timestamp", datetime.now(timezone.utc).isoformat()),
        } for item in self._messages[-200:] if isinstance(item, dict)]
        self._paused = paused

    def _runs_from_messages(self, agent_id: str) -> list[dict[str, Any]]:
        """Project persisted messages into the run shape used by the Canvas inspector."""
        runs: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for message in self._messages:
            if not isinstance(message, dict) or message.get("actor_id") != agent_id:
                continue
            payload = message.get("metadata", {}).get("payload", {})
            run_id = payload.get("run_id") if isinstance(payload, dict) else None
            if not isinstance(run_id, str):
                continue
            event_type = message.get("type")
            if event_type == "agent_started":
                runs[run_id] = {
                    "id": run_id,
                    "todo_id": payload.get("todo_id", ""),
                    "todo_title": payload.get("title", ""),
                    "input": payload.get("input", message.get("content", "")),
                    "process": [],
                    "output": "",
                    "status": "running",
                    "started_at": message.get("timestamp"),
                    "completed_at": None,
                    "model_calls": 0,
                    "tool_calls": 0,
                }
                order.append(run_id)
            elif event_type in {"model_request", "tool_start", "tool_end", "stream_end"}:
                if run_id in runs:
                    runs[run_id]["process"].append({
                        "type": event_type,
                        "timestamp": message.get("timestamp"),
                        "payload": payload,
                    })
            elif event_type == "agent_completed" and run_id in runs:
                run = runs[run_id]
                run["output"] = payload.get("output", message.get("content", ""))
                run["status"] = "completed"
                run["completed_at"] = message.get("timestamp")
                run["model_calls"] = payload.get("model_calls", 0)
                run["tool_calls"] = payload.get("tool_calls", 0)
        return [runs[run_id] for run_id in order]

    def _require_agent(self, agent_id: str) -> TeamAgent:
        try:
            return self._agents[agent_id]
        except KeyError as exc:
            raise ValueError(f"Unknown agent: {agent_id}") from exc
