from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "name": self.name, "description": self.description,
                "parameters": self.parameters, "strict": True}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._stop_handlers: list[Callable[[], None]] = []
        self._reset_handlers: list[Callable[[], None]] = []

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def register_stop_handler(self, handler: Callable[[], None]) -> None:
        self._stop_handlers.append(handler)

    def register_reset_handler(self, handler: Callable[[], None]) -> None:
        self._reset_handlers.append(handler)

    def stop(self) -> None:
        for handler in self._stop_handlers:
            handler()

    def reset(self) -> None:
        for handler in self._reset_handlers:
            handler()

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    def execute(self, name: str, arguments: str | dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return json.dumps({"ok": False, "error": f"Unknown tool: {name}"}, ensure_ascii=False)
        try:
            parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
            if not isinstance(parsed, dict):
                raise ValueError("arguments must decode to a JSON object")
            return json.dumps({"ok": True, "result": tool.handler(parsed)}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


class WorkspaceTools:
    def __init__(self, workspace: Path, *, allow_write: bool, max_file_chars: int, max_write_chars: int,
                 allow_exec: bool = True, max_command_seconds: float = 60.0,
                 max_command_output_chars: int = 50_000) -> None:
        self.workspace = workspace.resolve()
        self.allow_write = allow_write
        self.max_file_chars = max_file_chars
        self.max_write_chars = max_write_chars
        self.allow_exec = allow_exec
        self.max_command_seconds = max_command_seconds
        self.max_command_output_chars = max_command_output_chars
        self._stop_requested = threading.Event()
        self._process_lock = threading.Lock()
        self._active_processes: set[subprocess.Popen[str]] = set()

    def registry(self) -> ToolRegistry:
        self._stop_requested.clear()
        registry = ToolRegistry()
        registry.register_stop_handler(self.stop)
        registry.register_reset_handler(self.reset)
        registry.register(Tool("list_files", "List files under a workspace directory.", _schema({
            "path": {"type": "string", "description": "Workspace-relative directory, or '.'"},
            "recursive": {"type": "boolean", "description": "Whether to recurse"}}, ["path", "recursive"]), self.list_files))
        registry.register(Tool("read_file", "Read a UTF-8 text file from the workspace.", _schema({
            "path": {"type": "string", "description": "Workspace-relative file path"}}, ["path"]), self.read_file))
        registry.register(Tool("search_files", "Regex-search UTF-8 files in the workspace.", _schema({
            "query": {"type": "string", "description": "Python regular expression"},
            "path": {"type": "string", "description": "Workspace-relative file or directory"}}, ["query", "path"]), self.search_files))
        if self.allow_write:
            registry.register(Tool("write_file", "Write a UTF-8 file inside the workspace, creating parents.", _schema({
                "path": {"type": "string", "description": "Workspace-relative file path"},
                "content": {"type": "string", "description": "Complete new file content"}}, ["path", "content"]), self.write_file))
        if self.allow_exec:
            registry.register(Tool("run_command",
                "Run a command or script in the workspace. Pass an argv array, not shell source code. No pipes or redirection.",
                _schema({
                    "argv": {"type": "array", "items": {"type": "string"},
                             "description": "Executable and arguments, e.g. ['python3', 'script.py']"},
                    "cwd": {"type": "string", "description": "Workspace-relative working directory, or '.'"},
                    "timeout_seconds": {"type": "number", "description": "Positive timeout capped by settings"}
                }, ["argv", "cwd", "timeout_seconds"]), self.run_command))
        return registry

    def _resolve(self, relative: str) -> Path:
        candidate = (self.workspace / relative).resolve()
        if candidate != self.workspace and self.workspace not in candidate.parents:
            raise PermissionError(f"Path escapes workspace: {relative}")
        return candidate

    def list_files(self, args: dict[str, Any]) -> list[str]:
        target = self._resolve(str(args["path"]))
        if not target.is_dir():
            raise NotADirectoryError(args["path"])
        iterator = target.rglob("*") if args["recursive"] else target.iterdir()
        return [str(path.relative_to(self.workspace)) for path in sorted(iterator)
                if ".git" not in path.parts][:500]

    def read_file(self, args: dict[str, Any]) -> dict[str, Any]:
        text = self._resolve(str(args["path"])).read_text(encoding="utf-8")
        return {"content": text[:self.max_file_chars], "truncated": len(text) > self.max_file_chars, "chars": len(text)}

    def search_files(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        pattern = re.compile(str(args["query"]))
        target = self._resolve(str(args["path"]))
        paths = [target] if target.is_file() else target.rglob("*")
        matches: list[dict[str, Any]] = []
        for path in paths:
            if not path.is_file() or ".git" in path.parts:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            for line_number, line in enumerate(lines, 1):
                if pattern.search(line):
                    matches.append({"path": str(path.relative_to(self.workspace)), "line": line_number, "text": line[:500]})
                    if len(matches) >= 200:
                        return matches
        return matches

    def write_file(self, args: dict[str, Any]) -> dict[str, Any]:
        if not self.allow_write:
            raise PermissionError("write_file is disabled")
        content = str(args["content"])
        if len(content) > self.max_write_chars:
            raise ValueError(f"content exceeds max_write_chars={self.max_write_chars}")
        path = self._resolve(str(args["path"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {"path": str(path.relative_to(self.workspace)), "chars_written": len(content)}

    def run_command(self, args: dict[str, Any]) -> dict[str, Any]:
        if not self.allow_exec:
            raise PermissionError("run_command is disabled")
        argv = args["argv"]
        if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
            raise ValueError("argv must be a non-empty array of non-empty strings")
        cwd = self._resolve(str(args["cwd"]))
        if not cwd.is_dir():
            raise NotADirectoryError(args["cwd"])
        requested_timeout = float(args["timeout_seconds"])
        if requested_timeout <= 0:
            raise ValueError("timeout_seconds must be > 0")
        timeout = min(requested_timeout, self.max_command_seconds)
        if self._stop_requested.is_set():
            return {"argv": argv, "stopped": True, "stdout": "", "stderr": ""}
        process = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, stdin=subprocess.DEVNULL, shell=False)
        with self._process_lock:
            self._active_processes.add(process)
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if self._stop_requested.wait(0.05):
                    self._terminate(process)
                    stdout, stderr = process.communicate()
                    return {"argv": argv, "cwd": str(cwd.relative_to(self.workspace)) or ".",
                            "stopped": True, "stdout": self._truncate(stdout)[0],
                            "stderr": self._truncate(stderr)[0]}
                if time.monotonic() >= deadline:
                    self._terminate(process)
                    stdout, stderr = process.communicate()
                    return {"argv": argv, "cwd": str(cwd.relative_to(self.workspace)) or ".",
                            "timed_out": True, "timeout_seconds": timeout,
                            "stdout": self._truncate(stdout)[0], "stderr": self._truncate(stderr)[0]}
            stdout_raw, stderr_raw = process.communicate()
            stdout, stdout_truncated = self._truncate(stdout_raw)
            stderr, stderr_truncated = self._truncate(stderr_raw)
            return {"argv": argv, "cwd": str(cwd.relative_to(self.workspace)) or ".",
                    "exit_code": process.returncode, "stdout": stdout, "stderr": stderr,
                    "stdout_truncated": stdout_truncated, "stderr_truncated": stderr_truncated}
        except KeyboardInterrupt:
            self._terminate(process)
            raise
        finally:
            with self._process_lock:
                self._active_processes.discard(process)

    def stop(self) -> None:
        self._stop_requested.set()
        with self._process_lock:
            processes = list(self._active_processes)
        for process in processes:
            self._terminate(process)

    def reset(self) -> None:
        self._stop_requested.clear()

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()

    def _truncate(self, value: str) -> tuple[str, bool]:
        return value[:self.max_command_output_chars], len(value) > self.max_command_output_chars
