"""RefTrack application entry point.

Run:  uvicorn reftrack.main:app --reload
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from reftrack import __version__
from reftrack.api import router as api_router
from reftrack.database import init_db
from reftrack.ui import router as ui_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="RefTrack",
    description="EPA Section 608 refrigerant compliance tracker",
    version=__version__,
    lifespan=lifespan,
)


app.include_router(api_router)
app.include_router(ui_router)
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
    name="static",
)


@app.get("/health")
def health():
    return {"status": "ok", "version": __version__}
