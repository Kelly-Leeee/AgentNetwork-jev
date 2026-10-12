from __future__ import annotations

import json
import ipaddress
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse


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
                 max_command_output_chars: int = 50_000, allow_web: bool = True,
                 max_web_results: int = 8, max_web_chars: int = 30_000,
                 web_timeout_seconds: float = 20.0) -> None:
        self.workspace = workspace.resolve()
        self.allow_write = allow_write
        self.max_file_chars = max_file_chars
        self.max_write_chars = max_write_chars
        self.allow_exec = allow_exec
        self.max_command_seconds = max_command_seconds
        self.max_command_output_chars = max_command_output_chars
        self.allow_web = allow_web
        self.max_web_results = max_web_results
        self.max_web_chars = max_web_chars
        self.web_timeout_seconds = web_timeout_seconds
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
        if self.allow_web:
            registry.register(Tool("web_search", "Search the public web. Free and no API key required.",
                _schema({"query": {"type": "string", "description": "Search query"},
                         "max_results": {"type": "integer", "description": "Number of results"}},
                        ["query", "max_results"]), self.web_search))
            registry.register(Tool("web_fetch", "Fetch readable text from a public HTTP(S) URL.",
                _schema({"url": {"type": "string", "description": "Public HTTP(S) URL"}}, ["url"]),
                self.web_fetch))
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

    def web_search(self, args: dict[str, Any]) -> list[dict[str, str]]:
        if not self.allow_web:
            raise PermissionError("web_search is disabled")
        query = str(args["query"]).strip()
        if not query:
            raise ValueError("query cannot be empty")
        limit = min(max(int(args["max_results"]), 1), self.max_web_results)
        from ddgs import DDGS

        results = DDGS(timeout=self.web_timeout_seconds).text(query, max_results=limit)
        return [{"title": str(item.get("title") or ""),
                 "url": str(item.get("href") or item.get("url") or ""),
                 "snippet": str(item.get("body") or item.get("snippet") or "")}
                for item in results][:limit]

    def web_fetch(self, args: dict[str, Any]) -> dict[str, Any]:
        if not self.allow_web:
            raise PermissionError("web_fetch is disabled")
        import httpx
        import trafilatura

        url = str(args["url"]).strip()
        headers = {"User-Agent": "AgentRouter/0.1 (+local agent web fetch)"}
        with httpx.Client(timeout=self.web_timeout_seconds, headers=headers, follow_redirects=False) as client:
            for _ in range(6):
                self._validate_public_url(url)
                with client.stream("GET", url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError("redirect response has no Location header")
                        url = urljoin(url, location)
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").lower()
                    if not any(kind in content_type for kind in ("text/", "json", "xml", "html")):
                        raise ValueError(f"unsupported content type: {content_type or 'unknown'}")
                    chunks, size = [], 0
                    byte_limit = self.max_web_chars * 4
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > byte_limit:
                            raise ValueError(f"response exceeds byte limit {byte_limit}")
                        chunks.append(chunk)
                    raw = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
                    extracted = trafilatura.extract(raw, output_format="markdown", include_links=True,
                                                    include_tables=True) or raw
                    return {"url": str(response.url), "content_type": content_type,
                            "content": extracted[:self.max_web_chars],
                            "truncated": len(extracted) > self.max_web_chars,
                            "chars": len(extracted)}
            raise ValueError("too many redirects")

    @staticmethod
    def _validate_public_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("url must use http or https and include a hostname")
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(
                parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM)}
        except socket.gaierror as exc:
            raise ValueError(f"cannot resolve hostname: {parsed.hostname}") from exc
        for address in addresses:
            if not ipaddress.ip_address(address).is_global:
                raise PermissionError(f"refusing non-public address: {address}")

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
