from __future__ import annotations

import argparse
import sys
from typing import Any

from openai import OpenAI

from .config import load_settings
from .loop import AgentLoop
from .tools import WorkspaceTools


def _event_printer(kind: str, payload: dict[str, Any]) -> None:
    if kind == "tool_start":
        print(f"[tool] {payload['name']} {payload['arguments']}", file=sys.stderr)
    elif kind == "tool_end":
        print(f"[tool-result] {str(payload['output']).replace(chr(10), ' ')[:300]}", file=sys.stderr)


def build_loop(settings_path: str, verbose: bool) -> AgentLoop:
    settings = load_settings(settings_path)
    if not settings.openai.api_key:
        raise ValueError("OpenAI API key is empty. Fill settings.json or export OPENAI_API_KEY.")
    client = OpenAI(api_key=settings.openai.api_key, base_url=settings.openai.base_url,
                    timeout=settings.openai.timeout_seconds)
    tools = WorkspaceTools(settings.agent.workspace, allow_write=settings.agent.allow_write,
                           max_file_chars=settings.agent.max_file_chars,
                           max_write_chars=settings.agent.max_write_chars,
                           allow_exec=settings.agent.allow_exec,
                           max_command_seconds=settings.agent.max_command_seconds,
                           max_command_output_chars=settings.agent.max_command_output_chars).registry()
    return AgentLoop(client=client, model=settings.openai.model, instructions=settings.agent.system_prompt,
                     tools=tools, max_tool_rounds=settings.agent.max_tool_rounds,
                     max_output_tokens=settings.agent.max_output_tokens,
                     on_event=_event_printer if verbose else None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a real OpenAI tool-calling agent loop.")
    parser.add_argument("prompt", nargs="*", help="Task; omit for interactive mode")
    parser.add_argument("--settings", default="settings.json")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    try:
        loop = build_loop(args.settings, args.verbose)
        if args.prompt:
            try:
                print(loop.run(" ".join(args.prompt)).output)
            except KeyboardInterrupt:
                loop.stop()
                print("\nAgent loop stopped.", file=sys.stderr)
                return 130
            return 0
        print("AgentRouter interactive mode. Type /exit to quit.")
        previous_response_id: str | None = None
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
                result = loop.run(prompt, previous_response_id=previous_response_id)
            except KeyboardInterrupt:
                loop.stop()
                print("\nAgent loop stopped.")
                continue
            previous_response_id = result.response_id
            print(f"agent> {result.output}")
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
