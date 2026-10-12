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
    response_items: list[dict[str, Any]] = field(default_factory=list)


class AgentLoop:
    def __init__(self, *, client: Any, model: str, instructions: str, tools: ToolRegistry,
                 max_tool_rounds: int = 20, max_output_tokens: int = 4096,
                 streaming: bool = False,
                 on_event: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self.client = client
        self.model = model
        self.instructions = instructions
        self.tools = tools
        self.max_tool_rounds = max_tool_rounds
        self.max_output_tokens = max_output_tokens
        self.streaming = streaming
        self.on_event = on_event or (lambda _kind, _payload: None)
        self._stop_requested = threading.Event()
        self._run_lock = threading.Lock()

    def stop(self) -> None:
        """Request that the active run stop and terminate active local commands."""
        self._stop_requested.set()
        self.tools.stop()
        self.on_event("stopped", {})

    def abort(self) -> None:
        """Pi-compatible alias for an unconditional stop request."""
        self.stop()

    def run(self, user_input: str, *, history: list[dict[str, Any]] | None = None) -> AgentResult:
        if not user_input.strip():
            raise ValueError("user_input cannot be empty")
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("This AgentLoop is already running")
        self._stop_requested.clear()
        self.tools.reset()
        try:
            return self._run(user_input, list(history or []))
        finally:
            self._run_lock.release()

    def _run(self, user_input: str, history: list[dict[str, Any]]) -> AgentResult:
        if self._stop_requested.is_set():
            return self._stopped_result()
        context = [*history, {"role": "user", "content": user_input}]
        response = self._create(input=context)
        model_calls, tool_calls, rounds = 1, 0, 0
        events: list[ToolEvent] = []
        while True:
            if self._stop_requested.is_set():
                return self._stopped_result(response.id, model_calls, tool_calls, events, context)
            context.extend(self._response_output_items(response))
            calls = [item for item in response.output if getattr(item, "type", "") == "function_call"]
            if not calls:
                return AgentResult(response.output_text or "", response.id, model_calls, tool_calls,
                                   events=events, response_items=context)
            if rounds >= self.max_tool_rounds:
                denied_outputs = [
                    {"type": "function_call_output", "call_id": call.call_id,
                     "output": '{"ok":false,"error":"Tool execution limit reached"}'}
                    for call in calls
                ]
                final = self._create(
                    input=[*context, *denied_outputs, {"role": "user", "content":
                           "Tool limit reached. Do not call tools. Answer from available evidence and state any gap."}],
                    allow_tools=False)
                final_context = [*context, *denied_outputs, *self._response_output_items(final)]
                return AgentResult(final.output_text or "", final.id, model_calls + 1, tool_calls,
                                   max_rounds_hit=True, events=events, response_items=final_context)
            outputs = []
            for call in calls:
                if self._stop_requested.is_set():
                    return self._stopped_result(response.id, model_calls, tool_calls, events, context)
                self.on_event("tool_start", {"name": call.name, "arguments": call.arguments})
                output = self.tools.execute(call.name, call.arguments)
                events.append(ToolEvent(call.name, call.arguments, output))
                tool_calls += 1
                self.on_event("tool_end", {"name": call.name, "output": output})
                outputs.append({"type": "function_call_output", "call_id": call.call_id, "output": output})
                if self._stop_requested.is_set():
                    return self._stopped_result(response.id, model_calls, tool_calls, events, context)
            context.extend(outputs)
            response = self._create(input=context)
            model_calls += 1
            rounds += 1

    def _create(self, *, input: Any, allow_tools: bool = True) -> Any:
        kwargs: dict[str, Any] = {"model": self.model, "instructions": self.instructions,
                                  "input": input, "max_output_tokens": self.max_output_tokens}
        if allow_tools and self.tools.schemas:
            kwargs.update(tools=self.tools.schemas, tool_choice="auto")
        self.on_event("model_request", {"input_items": len(input) if isinstance(input, list) else 1})
        if not self.streaming:
            return self.client.responses.create(**kwargs)
        stream = self.client.responses.create(**kwargs, stream=True)
        return self._consume_stream(stream)

    def _consume_stream(self, stream: Any) -> Any:
        completed_response = None

        def consume(events: Any) -> None:
            nonlocal completed_response
            for event in events:
                event_type = getattr(event, "type", "")
                if event_type == "response.output_text.delta":
                    self.on_event("text_delta", {"delta": getattr(event, "delta", "")})
                elif event_type == "response.completed":
                    completed_response = event.response
                elif event_type in {"response.failed", "response.incomplete", "error"}:
                    message = getattr(event, "message", None)
                    response = getattr(event, "response", None)
                    error = getattr(response, "error", None) if response is not None else None
                    raise RuntimeError(message or str(error or event_type))

        if hasattr(stream, "__enter__"):
            with stream as events:
                consume(events)
        else:
            consume(stream)
        if completed_response is None:
            getter = getattr(stream, "get_final_response", None)
            if getter is not None:
                completed_response = getter()
        if completed_response is None:
            raise RuntimeError("Streaming response ended without response.completed")
        self.on_event("stream_end", {"response_id": getattr(completed_response, "id", "")})
        return completed_response

    @staticmethod
    def _response_output_items(response: Any) -> list[dict[str, Any]]:
        items = []
        for item in response.output:
            if hasattr(item, "model_dump"):
                items.append(item.model_dump(exclude_none=True))
            elif isinstance(item, dict):
                items.append(dict(item))
            else:
                data = vars(item).copy()
                data.pop("type", None)
                data["type"] = getattr(item, "type", "")
                items.append(data)
        return items

    @staticmethod
    def _stopped_result(response_id: str = "", model_calls: int = 0, tool_calls: int = 0,
                        events: list[ToolEvent] | None = None,
                        response_items: list[dict[str, Any]] | None = None) -> AgentResult:
        return AgentResult(output="Agent loop stopped.", response_id=response_id,
                           model_call_count=model_calls, tool_call_count=tool_calls,
                           stopped=True, events=events or [], response_items=response_items or [])
