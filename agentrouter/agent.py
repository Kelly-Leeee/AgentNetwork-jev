from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .chat_store import ChatStore
from .loop import AgentLoop, AgentResult
from .tools import ToolRegistry


@dataclass
class AgentState:
    """Mutable, user-visible state owned by an Agent."""

    system_prompt: str
    tools: ToolRegistry
    messages: list[dict[str, Any]] = field(default_factory=list)
    last_response_id: str | None = None
    response_items: list[dict[str, Any]] = field(default_factory=list, repr=False)
    is_running: bool = False
    stopped: bool = False
    error_message: str | None = None
    last_result: AgentResult | None = None


class Agent:
    """Stateful agent facade; AgentLoop remains the low-level turn executor."""

    def __init__(self, *, client: Any, model: str, system_prompt: str, tools: ToolRegistry,
                 max_tool_rounds: int = 20, max_output_tokens: int = 4096,
                 streaming: bool = False, on_event=None, agent_id: str = "default",
                 chats_dir: str | Path | None = None, max_chat_files: int = 5) -> None:
        self.state = AgentState(system_prompt=system_prompt, tools=tools)
        self.agent_id = agent_id
        self._chat_store = (ChatStore(chats_dir, agent_id, max_files=max_chat_files)
                            if chats_dir is not None else None)
        self._restore_state()
        self.streaming = streaming
        self._loop = AgentLoop(client=client, model=model, instructions=system_prompt,
                               tools=tools, max_tool_rounds=max_tool_rounds,
                               max_output_tokens=max_output_tokens, streaming=streaming,
                               on_event=on_event)
        self._state_lock = threading.Lock()

    def prompt(self, text: str) -> AgentResult:
        if not text.strip():
            raise ValueError("prompt cannot be empty")
        with self._state_lock:
            if self.state.is_running:
                raise RuntimeError("Agent is already processing a prompt")
            self.state.is_running = True
            self.state.stopped = False
            self.state.error_message = None
            history = list(self.state.response_items)
            self.state.messages.append({"role": "user", "content": text})
        try:
            result = self._loop.run(text, history=history)
            with self._state_lock:
                self.state.last_result = result
                self.state.stopped = result.stopped
                if not result.stopped:
                    self.state.last_response_id = result.response_id
                    self.state.response_items = list(result.response_items)
                    self.state.messages.append({"role": "assistant", "content": result.output})
                for event in result.events:
                    self.state.messages.append({"role": "tool", "name": event.name,
                                                "arguments": event.arguments, "content": event.output})
            self._save_state()
            return result
        except Exception as exc:
            with self._state_lock:
                self.state.error_message = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            with self._state_lock:
                self.state.is_running = False

    def stop(self) -> None:
        self._loop.stop()
        with self._state_lock:
            self.state.stopped = True

    def abort(self) -> None:
        """Pi-style name for forcibly stopping the active run."""
        self.stop()

    def reset(self) -> None:
        with self._state_lock:
            if self.state.is_running:
                raise RuntimeError("Agent is already processing; stop or wait before reset")
            self.state.messages.clear()
            self.state.last_response_id = None
            self.state.response_items.clear()
            self.state.stopped = False
            self.state.error_message = None
            self.state.last_result = None
        self._save_state()

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        """Block until the current synchronous run becomes idle."""
        import time

        deadline = None if timeout is None else time.monotonic() + timeout
        while self.state.is_running:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def _restore_state(self) -> None:
        if self._chat_store is None:
            return
        saved = self._chat_store.load_latest()
        if saved is None:
            return
        messages = saved.get("messages")
        response_items = saved.get("response_items")
        if isinstance(messages, list):
            self.state.messages = messages
        if isinstance(response_items, list):
            self.state.response_items = response_items
        last_response_id = saved.get("last_response_id")
        if isinstance(last_response_id, str) or last_response_id is None:
            self.state.last_response_id = last_response_id

    def _save_state(self) -> None:
        if self._chat_store is None:
            return
        with self._state_lock:
            messages = list(self.state.messages)
            response_items = list(self.state.response_items)
            last_response_id = self.state.last_response_id
        self._chat_store.save(messages=messages, response_items=response_items,
                              last_response_id=last_response_id)
