import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from backend.database import init_db, get_engine
from backend.champions import refresh_version
from backend.api.routes import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

app = FastAPI(
    title="LoL Pro Player Stats",
    description="LCK pro-play stats explorer for Teddy, Ruler, Kiin and Zeus",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.middleware("http")
async def no_cache_frontend(request, call_next):
    """Prevent browsers from caching the frontend.

    Both this app and the sibling `predictionmodel` app default to
    127.0.0.1:8000. Without this, a browser can keep running a *cached*
    frontend from the other project against this backend — e.g. calling the
    sibling's `/api/search`, which 404s here. No-store forces a fresh fetch.
    """
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/")
    async def serve_index():
        return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.on_event("startup")
async def on_startup():
    init_db(get_engine())
    refresh_version()
    logging.getLogger(__name__).info("Database initialised.")
