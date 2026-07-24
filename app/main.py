from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.deps import require_auth
from app.api.routes_auth import router as auth_router
from app.api.routes_tasks import router as tasks_router
from app.api.routes_stream import router as stream_router
from app.api.routes_location import router as location_router
from app.config import get_settings
from app.db import db
from app.security import verify_session_token
from app.workers.runner import Worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("panbridge")

BASE = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE / "web" / "templates"))
worker = Worker(db)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.data_path.mkdir(parents=True, exist_ok=True)
    await db.connect()
    worker.start()
    app.state.worker = worker
    log.info("PanBridge ready on %s:%s data=%s", settings.host, settings.port, settings.data_path)
    yield
    await worker.stop()
    await db.close()


app = FastAPI(title="PanBridge", version="0.3.8", lifespan=lifespan)
app.state.worker = worker
app.include_router(auth_router)
app.include_router(tasks_router)
app.include_router(stream_router)
app.include_router(location_router)

static_dir = BASE / "web" / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


def _logged_in(request: Request) -> bool:
    return verify_session_token(request.cookies.get("panbridge_session"))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _logged_in(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("settings.html", {"request": request})


@app.get("/tasks/{job_id}", response_class=HTMLResponse)
async def task_page(request: Request, job_id: int):
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("task.html", {"request": request, "job_id": job_id})


@app.get("/api/health")
@app.get("/health")
async def health():
    from app.config import get_settings as _gs
    s = _gs()
    return {"ok": True, "version": s.app_version}
