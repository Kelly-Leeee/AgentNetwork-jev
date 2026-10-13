from __future__ import annotations

import asyncio
import io
import json
import queue
import shutil
import subprocess
import tempfile
import threading
import webbrowser
import zipfile
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree

from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import load_settings
from .team import TeamOrchestrator


class AgentUpdate(BaseModel):
    name: str
    role: str | None = None


class PauseUpdate(BaseModel):
    paused: bool


class ExecutionModeUpdate(BaseModel):
    mode: str


class HumanAnswer(BaseModel):
    answer: str


class WorkstreamUpdate(BaseModel):
    id: str
    title: str
    goal: str
    input: str
    todos: list[str]


class TaskUpdate(BaseModel):
    goal: str
    workstreams: list[WorkstreamUpdate]


MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_EXTRACTED_CHARS = 100_000


def _extract_uploaded_text(filename: str, content: bytes) -> str:
    """Extract plain text from a task attachment without retaining the file."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".txt":
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("latin-1")
    elif suffix == ".docx":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                document_info = archive.getinfo("word/document.xml")
                if document_info.file_size > MAX_UPLOAD_BYTES:
                    raise ValueError("The .docx document content is too large")
                document = ElementTree.fromstring(archive.read(document_info))
        except (KeyError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
            raise ValueError("The .docx file is invalid or unreadable") from exc
        namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        paragraphs = []
        for paragraph in document.iter(f"{namespace}p"):
            value = "".join(node.text or "" for node in paragraph.iter(f"{namespace}t"))
            if value:
                paragraphs.append(value)
        text = "\n".join(paragraphs)
    elif suffix == ".doc":
        converter = shutil.which("antiword") or shutil.which("textutil")
        if not converter:
            raise ValueError("Legacy .doc parsing requires antiword or macOS textutil; convert the file to .docx and try again")
        with tempfile.TemporaryDirectory(prefix="agentrouter-doc-") as directory:
            source = Path(directory) / "upload.doc"
            source.write_bytes(content)
            if Path(converter).name == "antiword":
                command = [converter, str(source)]
            else:
                command = [converter, "-convert", "txt", "-stdout", str(source)]
            try:
                result = subprocess.run(command, capture_output=True, check=False, timeout=30)
            except subprocess.TimeoutExpired as exc:
                raise ValueError("The .doc file took too long to parse") from exc
        if result.returncode != 0:
            raise ValueError("The .doc file is invalid or unreadable")
        text = result.stdout.decode("utf-8", errors="replace")
    else:
        raise ValueError("Only .txt, .doc, and .docx files are supported")
    text = text.replace("\x00", "").strip()
    if not text:
        raise ValueError("No readable text was found in the file")
    if len(text) > MAX_EXTRACTED_CHARS:
        raise ValueError(f"Extracted text exceeds {MAX_EXTRACTED_CHARS:,} characters")
    return text


def create_app(settings_path: str | Path = "settings.json") -> FastAPI:
    settings_file = Path(settings_path).expanduser().resolve()
    orchestrator = TeamOrchestrator(load_settings(settings_file), settings_file.parent)
    web_dir = Path(__file__).resolve().parent / "web" / "dist"
    if not web_dir.is_dir():
        raise ValueError("Canvas frontend is not built. Run: cd frontend && npm install && npm run build")
    app = FastAPI(title="Agent Team Canvas", version="0.1.0")
    app.state.orchestrator = orchestrator

    @app.get("/api/state")
    def state():
        return orchestrator.snapshot()

    @app.post("/api/agents")
    def add_agent():
        try:
            return orchestrator.add_agent()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.patch("/api/task")
    def update_task(update: TaskUpdate):
        try:
            return orchestrator.update_task(
                goal=update.goal,
                workstreams=[item.model_dump() for item in update.workstreams],
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/task-input-file")
    def parse_task_input_file(
        content: bytes = Body(..., media_type="application/octet-stream"),
        x_filename: str = Header(...),
    ):
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="File must be 10 MB or smaller")
        try:
            filename = unquote(x_filename)
            return {"filename": Path(filename).name, "text": _extract_uploaded_text(filename, content)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.patch("/api/agents/{agent_id}")
    def update_agent(agent_id: str, update: AgentUpdate):
        try:
            return orchestrator.update_agent(agent_id, name=update.name, role=update.role)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/run-next")
    async def run_next():
        try:
            return await asyncio.to_thread(orchestrator.run_next)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/pause")
    def pause(update: PauseUpdate):
        orchestrator.set_paused(update.paused)
        return orchestrator.snapshot()

    @app.post("/api/execution-mode")
    def execution_mode(update: ExecutionModeUpdate):
        try:
            return orchestrator.set_execution_mode(update.mode)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/human-answer")
    def human_answer(update: HumanAnswer):
        try:
            return orchestrator.submit_human_answer(update.answer)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/reset")
    def reset():
        try:
            return orchestrator.reset()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/events")
    async def events():
        channel = orchestrator.events.subscribe()

        async def stream():
            try:
                yield f"data: {json.dumps({'type': 'connected'})}\n\n"
                while True:
                    try:
                        event = await asyncio.to_thread(channel.get, True, 15)
                        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    except queue.Empty:
                        yield ": keep-alive\n\n"
            finally:
                orchestrator.events.unsubscribe(channel)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    app.mount("/assets", StaticFiles(directory=web_dir / "assets"), name="assets")

    @app.get("/")
    def index():
        # The HTML points at content-hashed JS/CSS bundles. Never cache it, otherwise
        # an already-open Canvas can keep rendering controls from an older build.
        return FileResponse(
            web_dir / "index.html",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    return app


def run_server(settings_path: str, host: str, port: int, *, open_browser: bool = True) -> None:
    import uvicorn

    if open_browser:
        browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        url = f"http://{browser_host}:{port}"
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(settings_path), host=host, port=port)
