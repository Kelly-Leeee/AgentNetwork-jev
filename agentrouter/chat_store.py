from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable


_AGENT_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class ChatStore:
    """Persist one rolling daily conversation snapshot per agent."""

    def __init__(self, root: str | Path, agent_id: str, *, max_files: int = 5,
                 today: Callable[[], date] = date.today) -> None:
        if not _AGENT_ID.fullmatch(agent_id):
            raise ValueError("agent_id may contain only letters, numbers, '-' and '_'")
        if max_files < 1:
            raise ValueError("max_files must be >= 1")
        self.agent_id = agent_id
        self.directory = Path(root).expanduser().resolve() / agent_id
        self.max_files = max_files
        self._today = today

    def load_latest(self) -> dict[str, Any] | None:
        for path in reversed(self._chat_files()):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict) and value.get("agent_id") == self.agent_id:
                return value
        return None

    def save(self, *, messages: list[dict[str, Any]], response_items: list[dict[str, Any]],
             last_response_id: str | None) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{self._today().isoformat()}.json"
        payload = {
            "version": 1,
            "agent_id": self.agent_id,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "last_response_id": last_response_id,
            "messages": messages,
            "response_items": response_items,
        }
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        self._prune()
        return path

    def _chat_files(self) -> list[Path]:
        if not self.directory.is_dir():
            return []
        return sorted(path for path in self.directory.glob("????-??-??.json") if path.is_file())

    def _prune(self) -> None:
        files = self._chat_files()
        for path in files[:-self.max_files]:
            path.unlink()
