"""RefTrack application entry point.

Run:  uvicorn reftrack.main:app --reload
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from reftrack import __version__, auth
from reftrack.database import backup_db, init_db
from reftrack.api import router as api_router
from reftrack.ui import router as ui_router, templates

logger = logging.getLogger("reftrack")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    made = backup_db()
    if made:
        logger.info("Daily database backup written to %s", made)
    yield


app = FastAPI(
    title="RefTrack",
    description="EPA Section 608 refrigerant compliance tracker",
    version=__version__,
    lifespan=lifespan,
)


@app.middleware("http")
async def require_auth(request: Request, call_next):
    if auth.enabled() and not auth.path_is_exempt(request.url.path):
        if not auth.is_valid(request.cookies.get(auth.COOKIE_NAME)):
            if request.url.path.startswith("/api"):
                return JSONResponse({"detail": "Not authenticated"}, status_code=401)
            return RedirectResponse("/login", status_code=303)
    return await call_next(request)


@app.get("/login", include_in_schema=False)
def login_page(request: Request):
    if not auth.enabled():
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {})


@app.post("/login", include_in_schema=False)
def login_submit(request: Request, password: str = Form(...)):
    token = auth.make_token(password)
    if token is None:
        return templates.TemplateResponse(
            request, "login.html", {"error": "Incorrect password."},
            status_code=401,
        )
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(auth.COOKIE_NAME, token, httponly=True, samesite="lax")
    return resp


@app.get("/logout", include_in_schema=False)
def logout():
    resp = RedirectResponse("/login" if auth.enabled() else "/", status_code=303)
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


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
