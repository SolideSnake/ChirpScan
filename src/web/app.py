from pathlib import Path
from typing import Any, Dict

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse

from src.runtime.manager import RuntimeManager, configure_logging

app = FastAPI(title="Tweet Monitor UI", version="0.1.0")
manager = RuntimeManager()
configure_logging("INFO")
manager.load_saved_config()
STATIC_DIR = Path(__file__).parent / "static"


@app.on_event("shutdown")
async def _shutdown_runtime() -> None:
    await manager.stop()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.svg")
async def favicon_svg() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


@app.get("/favicon.png")
async def favicon_png() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.png", media_type="image/png")


@app.get("/favicon.ico")
async def favicon_ico() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.ico", media_type="image/x-icon")


@app.get("/api/config")
async def get_config() -> Dict[str, Any]:
    return manager.get_config()


@app.post("/api/config")
async def save_config(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    try:
        return manager.save_config(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/status")
async def get_status() -> Dict[str, Any]:
    return manager.status()


@app.get("/api/logs")
async def get_logs() -> Dict[str, Any]:
    return {"lines": manager.logs()}


@app.post("/api/logs/clear")
async def clear_logs() -> Dict[str, Any]:
    manager.clear_logs()
    return {"ok": True}


@app.post("/api/start")
async def start_runtime() -> Dict[str, Any]:
    return await manager.start()


@app.post("/api/stop")
async def stop_runtime() -> Dict[str, Any]:
    return await manager.stop()


@app.post("/api/restart")
async def restart_runtime() -> Dict[str, Any]:
    return await manager.restart()


@app.post("/api/test-send")
async def test_send() -> Dict[str, Any]:
    ok = await manager.test_send()
    return {"ok": ok}
