from __future__ import annotations

import asyncio
import json
import queue
from pathlib import Path

from fastapi import FastAPI, HTTPException
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
        return FileResponse(web_dir / "index.html")

    return app


def run_server(settings_path: str, host: str, port: int) -> None:
    import uvicorn

    uvicorn.run(create_app(settings_path), host=host, port=port)
