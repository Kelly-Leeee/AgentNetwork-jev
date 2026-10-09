from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Any, Callable

from .tools import ToolRegistry


@dataclass(frozen=True)
class ToolEvent:
    name: str
    arguments: str
    output: str


@dataclass(frozen=True)
class AgentResult:
    output: str
    response_id: str
    model_call_count: int
    tool_call_count: int
    max_rounds_hit: bool = False
    stopped: bool = False
    events: list[ToolEvent] = field(default_factory=list)


class AgentLoop:
    def __init__(self, *, client: Any, model: str, instructions: str, tools: ToolRegistry,
                 max_tool_rounds: int = 20, max_output_tokens: int = 4096,
                 on_event: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self.client = client
        self.model = model
        self.instructions = instructions
        self.tools = tools
        self.max_tool_rounds = max_tool_rounds
        self.max_output_tokens = max_output_tokens
        self.on_event = on_event or (lambda _kind, _payload: None)
        self._stop_requested = threading.Event()
        self._run_lock = threading.Lock()

    def stop(self) -> None:
        """Request that the active run stop and terminate active local commands."""
        self._stop_requested.set()
        self.tools.stop()
        self.on_event("stopped", {})

    def run(self, user_input: str, *, previous_response_id: str | None = None) -> AgentResult:
        if not user_input.strip():
            raise ValueError("user_input cannot be empty")
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("This AgentLoop is already running")
        self._stop_requested.clear()
        self.tools.reset()
        try:
            return self._run(user_input, previous_response_id)
        finally:
            self._run_lock.release()

    def _run(self, user_input: str, previous_response_id: str | None) -> AgentResult:
        if self._stop_requested.is_set():
            return self._stopped_result()
        response = self._create(input=user_input, previous_response_id=previous_response_id)
        model_calls, tool_calls, rounds = 1, 0, 0
        events: list[ToolEvent] = []
        while True:
            if self._stop_requested.is_set():
                return self._stopped_result(response.id, model_calls, tool_calls, events)
            calls = [item for item in response.output if getattr(item, "type", "") == "function_call"]
            if not calls:
                return AgentResult(response.output_text or "", response.id, model_calls, tool_calls, events=events)
            if rounds >= self.max_tool_rounds:
                denied_outputs = [
                    {"type": "function_call_output", "call_id": call.call_id,
                     "output": '{"ok":false,"error":"Tool execution limit reached"}'}
                    for call in calls
                ]
                final = self._create(
                    input=[*denied_outputs, {"role": "user", "content":
                           "Tool limit reached. Do not call tools. Answer from available evidence and state any gap."}],
                    previous_response_id=response.id, allow_tools=False)
                return AgentResult(final.output_text or "", final.id, model_calls + 1, tool_calls,
                                   max_rounds_hit=True, events=events)
            outputs = []
            for call in calls:
                if self._stop_requested.is_set():
                    return self._stopped_result(response.id, model_calls, tool_calls, events)
                self.on_event("tool_start", {"name": call.name, "arguments": call.arguments})
                output = self.tools.execute(call.name, call.arguments)
                events.append(ToolEvent(call.name, call.arguments, output))
                tool_calls += 1
                self.on_event("tool_end", {"name": call.name, "output": output})
                outputs.append({"type": "function_call_output", "call_id": call.call_id, "output": output})
                if self._stop_requested.is_set():
                    return self._stopped_result(response.id, model_calls, tool_calls, events)
            response = self._create(input=outputs, previous_response_id=response.id)
            model_calls += 1
            rounds += 1

    def _create(self, *, input: Any, previous_response_id: str | None, allow_tools: bool = True) -> Any:
        kwargs: dict[str, Any] = {"model": self.model, "instructions": self.instructions,
                                  "input": input, "max_output_tokens": self.max_output_tokens}
        if previous_response_id:
            kwargs["previous_response_id"] = previous_response_id
        if allow_tools and self.tools.schemas:
            kwargs.update(tools=self.tools.schemas, tool_choice="auto")
        self.on_event("model_request", {"previous_response_id": previous_response_id})
        return self.client.responses.create(**kwargs)

    @staticmethod
    def _stopped_result(response_id: str = "", model_calls: int = 0, tool_calls: int = 0,
                        events: list[ToolEvent] | None = None) -> AgentResult:
        return AgentResult(output="Agent loop stopped.", response_id=response_id,
                           model_call_count=model_calls, tool_call_count=tool_calls,
                           stopped=True, events=events or [])
