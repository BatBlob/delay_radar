"""HTTP API and the map page."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from delay_radar.board import REFRESH_SECONDS, Board
from delay_radar.tracks import display_minor_path, display_path

PAGE = Path(__file__).resolve().parent / "static" / "index.html"
DATA = Path("data")


def public_root(value: str | None = None) -> str:
    raw = os.environ.get("DELAY_RADAR_ROOT", "") if value is None else value
    return raw.strip().rstrip("/")


def render_page(root: str | None = None) -> str:
    return PAGE.read_text(encoding="utf-8").replace("__DELAY_RADAR_ROOT__", public_root(root))


def _tracks(path: Path):
    if not path.exists():
        return {"type": "FeatureCollection", "features": []}
    return FileResponse(path, media_type="application/geo+json")


def create_app(board: Board | None = None) -> FastAPI:
    app = FastAPI(title="Delay Radar")
    app.state.board = board or Board(DATA)

    @app.on_event("startup")
    def _startup() -> None:
        app.state.board.start()

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/")
    def index():
        return HTMLResponse(render_page())

    @app.get("/api/tracks")
    def tracks():
        return _tracks(display_path(app.state.board.data_dir / "cache"))

    @app.get("/api/tracks-minor")
    def tracks_minor():
        return _tracks(display_minor_path(app.state.board.data_dir / "cache"))

    @app.get("/api/positions")
    def positions():
        geo, _status = app.state.board.snapshot()
        if geo is None:
            return JSONResponse({"ready": False}, status_code=503)
        return geo

    @app.post("/api/refresh")
    def refresh_now():
        app.state.board.request_refresh()
        geo, payload = app.state.board.snapshot()
        if payload is None:
            return JSONResponse({"ready": False}, status_code=503)
        return {"positions": geo, "status": payload}

    @app.get("/api/status")
    def status():
        _geo, payload = app.state.board.snapshot()
        if payload is None:
            return JSONResponse({"ready": False}, status_code=503)
        return payload

    @app.get("/api/stream")
    async def stream():
        async def events():
            previous = None
            while True:
                geo, payload = app.state.board.snapshot()
                if payload is not None:
                    body = json.dumps({"positions": geo, "status": payload})
                    if body != previous:
                        previous = body
                        yield f"event: update\ndata: {body}\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    static_dir = PAGE.parent
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")
    return app


def serve(replay: str | None = None, host: str | None = None, port: int | None = None, data_dir: Path | None = None) -> None:
    import uvicorn

    host = host or os.environ.get("HOST", "127.0.0.1")
    port = int(port or os.environ.get("PORT", "8000"))
    data = Path(data_dir or os.environ.get("DELAY_RADAR_DATA", DATA))
    app = create_app(Board(data, replay=replay))
    print(f"map http://{host}:{port}  refresh {REFRESH_SECONDS}s  replay={replay or 'live'}  data={data}")
    uvicorn.run(app, host=host, port=port, log_level="info")
