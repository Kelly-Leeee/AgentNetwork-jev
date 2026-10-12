from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .prompt import SYSTEM_PROMPT

@dataclass(frozen=True)
class OpenAISettings:
    api_key: str = ""
    model: str = "gpt-5-mini"
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 60.0


@dataclass(frozen=True)
class AgentSettings:
    workspace: Path
    max_tool_rounds: int = 20
    max_output_tokens: int = 4096
    stream: bool = True
    allow_write: bool = True
    allow_exec: bool = True
    allow_web: bool = True
    max_file_chars: int = 50_000
    max_write_chars: int = 20_000
    max_command_seconds: float = 60.0
    max_command_output_chars: int = 50_000
    max_web_results: int = 8
    max_web_chars: int = 30_000
    web_timeout_seconds: float = 20.0
    system_prompt: str = SYSTEM_PROMPT


@dataclass(frozen=True)
class JevSettings:
    api_key: str = ""
    model: str = "jev-latest"


@dataclass(frozen=True)
class Settings:
    openai: OpenAISettings
    jev: JevSettings
    agent: AgentSettings


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"settings.{name} must be a JSON object")
    return value


def load_settings(path: str | Path = "settings.json") -> Settings:
    settings_path = Path(path).expanduser().resolve()
    try:
        root = _mapping(json.loads(settings_path.read_text(encoding="utf-8")), "root")
    except FileNotFoundError as exc:
        raise ValueError(f"Settings file not found: {settings_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {settings_path}: {exc}") from exc

    openai_raw = _mapping(root.get("openai", {}), "openai")
    jev_raw = _mapping(root.get("jev", {}), "jev")
    agent_raw = _mapping(root.get("agent", {}), "agent")
    workspace = (settings_path.parent / str(agent_raw.get("workspace", "."))).resolve()
    if not workspace.is_dir():
        raise ValueError(f"Agent workspace is not a directory: {workspace}")

    openai_settings = OpenAISettings(
        api_key=os.getenv("OPENAI_API_KEY", str(openai_raw.get("api_key", ""))).strip(),
        model=os.getenv("OPENAI_MODEL", str(openai_raw.get("model", "gpt-5-mini"))).strip(),
        base_url=os.getenv("OPENAI_BASE_URL", str(openai_raw.get("base_url", "https://api.openai.com/v1"))).rstrip("/"),
        timeout_seconds=float(openai_raw.get("timeout_seconds", 60.0)),
    )
    jev_settings = JevSettings(
        api_key=str(jev_raw.get("api_key", "")).strip(),
        model=str(jev_raw.get("model", "jev-latest")).strip(),
    )
    agent_settings = AgentSettings(
        workspace=workspace,
        max_tool_rounds=int(agent_raw.get("max_tool_rounds", 20)),
        max_output_tokens=int(agent_raw.get("max_output_tokens", 4096)),
        stream=bool(agent_raw.get("stream", True)),
        allow_write=bool(agent_raw.get("allow_write", True)),
        allow_exec=bool(agent_raw.get("allow_exec", True)),
        allow_web=bool(agent_raw.get("allow_web", True)),
        max_file_chars=int(agent_raw.get("max_file_chars", 50_000)),
        max_write_chars=int(agent_raw.get("max_write_chars", 20_000)),
        max_command_seconds=float(agent_raw.get("max_command_seconds", 60.0)),
        max_command_output_chars=int(agent_raw.get("max_command_output_chars", 50_000)),
        max_web_results=int(agent_raw.get("max_web_results", 8)),
        max_web_chars=int(agent_raw.get("max_web_chars", 30_000)),
        web_timeout_seconds=float(agent_raw.get("web_timeout_seconds", 20.0)),
        system_prompt=str(agent_raw.get("system_prompt") or SYSTEM_PROMPT).strip(),
    )
    if not openai_settings.model:
        raise ValueError("openai.model cannot be empty")
    if agent_settings.max_tool_rounds < 0:
        raise ValueError("agent.max_tool_rounds must be >= 0")
    if agent_settings.max_command_seconds <= 0:
        raise ValueError("agent.max_command_seconds must be > 0")
    return Settings(openai=openai_settings, jev=jev_settings, agent=agent_settings)
