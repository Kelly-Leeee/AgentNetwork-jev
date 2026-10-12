from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from openai import OpenAI

from .agent import Agent
from .config import load_settings
from .tools import WorkspaceTools


def _event_printer(kind: str, payload: dict[str, Any], *, verbose: bool) -> None:
    if kind == "text_delta":
        print(payload["delta"], end="", flush=True)
    elif kind == "stream_end":
        print(flush=True)
    elif verbose and kind == "tool_start":
        print(f"[tool] {payload['name']} {payload['arguments']}", file=sys.stderr)
    elif verbose and kind == "tool_end":
        print(f"[tool-result] {str(payload['output']).replace(chr(10), ' ')[:300]}", file=sys.stderr)


def build_agent(settings_path: str, verbose: bool) -> Agent:
    settings = load_settings(settings_path)
    settings_file = Path(settings_path).expanduser().resolve()
    if not settings.openai.api_key:
        raise ValueError("OpenAI API key is empty. Fill settings.json or export OPENAI_API_KEY.")
    client = OpenAI(api_key=settings.openai.api_key, base_url=settings.openai.base_url,
                    timeout=settings.openai.timeout_seconds)
    tools = WorkspaceTools(settings.agent.workspace, allow_write=settings.agent.allow_write,
                           max_file_chars=settings.agent.max_file_chars,
                           max_write_chars=settings.agent.max_write_chars,
                           allow_exec=settings.agent.allow_exec,
                           max_command_seconds=settings.agent.max_command_seconds,
                           max_command_output_chars=settings.agent.max_command_output_chars,
                           allow_web=settings.agent.allow_web,
                           max_web_results=settings.agent.max_web_results,
                           max_web_chars=settings.agent.max_web_chars,
                           web_timeout_seconds=settings.agent.web_timeout_seconds).registry()
    return Agent(client=client, model=settings.openai.model, system_prompt=settings.agent.system_prompt,
                 tools=tools, max_tool_rounds=settings.agent.max_tool_rounds,
                 max_output_tokens=settings.agent.max_output_tokens,
                 streaming=settings.agent.stream,
                 agent_id="default",
                 chats_dir=settings_file.parent / "chats",
                 on_event=(lambda kind, payload: _event_printer(kind, payload, verbose=verbose))
                 if settings.agent.stream or verbose else None)


def build_loop(settings_path: str, verbose: bool):
    """Backward-compatible alias; new callers should use build_agent()."""
    return build_agent(settings_path, verbose)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a real OpenAI tool-calling agent loop.")
    parser.add_argument("prompt", nargs="*", help="Task; omit for interactive mode")
    parser.add_argument("--settings", default="settings.json")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--canvas", action="store_true", help="Launch the local Agent Team Canvas")
    parser.add_argument("--host", default="127.0.0.1", help="Canvas bind address")
    parser.add_argument("--port", type=int, default=8765, help="Canvas port")
    args = parser.parse_args(argv)
    try:
        if args.canvas:
            from .server import run_server

            run_server(args.settings, args.host, args.port)
            return 0
        agent = build_agent(args.settings, args.verbose)
        if args.prompt:
            try:
                result = agent.prompt(" ".join(args.prompt))
                if not agent.streaming:
                    print(result.output)
            except KeyboardInterrupt:
                agent.stop()
                print("\nAgent loop stopped.", file=sys.stderr)
                return 130
            return 0
        print("AgentRouter interactive mode. Type /exit to quit.")
        while True:
            try:
                prompt = input("you> ").strip()
            except EOFError:
                print()
                return 0
            if prompt in {"/exit", "/quit"}:
                return 0
            if not prompt:
                continue
            try:
                if agent.streaming:
                    print("agent> ", end="", flush=True)
                result = agent.prompt(prompt)
            except KeyboardInterrupt:
                agent.stop()
                print("\nAgent loop stopped.")
                continue
            if not agent.streaming:
                print(f"agent> {result.output}")
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
