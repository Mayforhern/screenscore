import logging
import os
from pathlib import Path

from fastapi.responses import HTMLResponse
from starlette.requests import Request
from starlette.routing import Route

from google.adk.cli.fast_api import get_fast_api_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_DIR = Path(__file__).parent

adk_app = get_fast_api_app(
    agents_dir=str(_DIR),
    web=True,
    a2a=False,
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "8000")),
    trace_to_cloud=False,
    auto_create_session=False,
)

_INDEX_PATH = _DIR / "screenscore" / "templates" / "index.html"
_INDEX_PATH = _DIR / "screenscore" / "templates" / "index.html"
try:
    _index_html = _INDEX_PATH.read_text()
except FileNotFoundError:
    logger.warning("index.html not found at %s — serving minimal fallback", _INDEX_PATH)
    _index_html = (
        "<!DOCTYPE html><html><head><title>ScreenScore</title></head>"
        "<body><h1>ScreenScore</h1>"
        "<p><a href='/dev-ui/'>Launch Agent →</a></p></body></html>"
    )


async def _landing(request: Request):
    return HTMLResponse(content=_index_html)


# Remove the ADK redirect that sends / → /dev-ui/ so our page wins
adk_app.router.routes = [
    r for r in adk_app.router.routes
    if not (isinstance(r, Route) and r.path == "/")
]

# Add our landing page at / and /home
adk_app.add_api_route("/", _landing, methods=["GET"], include_in_schema=False)
adk_app.add_api_route("/home", _landing, methods=["GET"], include_in_schema=False)

from starlette.staticfiles import StaticFiles

_STATIC_DIR = _DIR / "screenscore" / "static"
if _STATIC_DIR.exists():
    adk_app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

async def _favicon(request: Request):
    fav_path = _STATIC_DIR / "favicon.png"
    if fav_path.exists():
        from starlette.responses import FileResponse
        return FileResponse(fav_path, media_type="image/png")
    return HTMLResponse(content="", status_code=404)

adk_app.add_api_route("/favicon.ico", _favicon, methods=["GET"], include_in_schema=False)
adk_app.add_api_route("/favicon.png", _favicon, methods=["GET"], include_in_schema=False)

logger.info("ScreenScore app initialized")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(adk_app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))