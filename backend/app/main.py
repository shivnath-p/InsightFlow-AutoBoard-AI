import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from . import config, models  # noqa: F401  (models import registers the tables)
from .database import Base, SessionLocal, engine
from .ingestion import MAX_UPLOAD_BYTES
from .routers import audit_log, auth, dashboards, datasets, runs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("insightflow")

DOC_PATHS = ("/docs", "/redoc", "/openapi.json")
UPLOAD_PATH = "/datasets/upload"
MULTIPART_OVERHEAD = 1024 * 1024  # boundaries and headers around the file bytes

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-site",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def _headers_for(path: str) -> dict[str, str]:
    headers = dict(SECURITY_HEADERS)
    if not path.startswith(DOC_PATHS):  # Swagger UI needs scripts, the JSON API never does
        headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    if config.ENABLE_HSTS:
        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return headers


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # create_all is fine for the starter; move to Alembic migrations before production.
    Base.metadata.create_all(bind=engine)
    yield


docs = {} if config.ENABLE_DOCS else {"docs_url": None, "redoc_url": None, "openapi_url": None}
app = FastAPI(title="InsightFlow AutoBoard AI", version="2.1.0", lifespan=lifespan, **docs)


class BodyLimitMiddleware:
    """Reject oversized request bodies before they are buffered in memory.

    Checks Content-Length up front and counts bytes for chunked requests, so a client cannot
    skip the limit by omitting the header. Uploads get the file limit, everything else a small one.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = (MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD if scope["path"] == UPLOAD_PATH
                 else config.MAX_JSON_BODY_BYTES)
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > limit
            except ValueError:
                too_big = True
            if too_big:
                return await self._reject(scope, receive, send)
        seen = 0
        started = False   # the downstream app has begun its own response
        rejected = False  # we already answered 413

        async def counted_receive():
            nonlocal seen, rejected
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    # FastAPI turns exceptions raised while reading the body into a 400, so
                    # the 413 is sent here, before the exception unwinds the request.
                    if not started and not rejected:
                        rejected = True
                        await self._reject(scope, receive, send)
                    raise _BodyTooLarge()
            return message

        async def tracking_send(message):
            nonlocal started
            if rejected:
                return  # a 413 is already on the wire; drop whatever the app tries to add
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, counted_receive, tracking_send)
        except _BodyTooLarge:
            if not rejected:
                raise

    @staticmethod
    async def _reject(scope, receive, send):
        response = JSONResponse({"detail": "Request body too large"}, status_code=413,
                                headers=_headers_for(scope["path"]))
        await response(scope, receive, send)


class _BodyTooLarge(Exception):
    pass


app.add_middleware(BodyLimitMiddleware)
# added last = outermost, so even the 413 response carries CORS headers
app.add_middleware(
    CORSMiddleware, allow_origins=config.CORS_ORIGINS,  # explicit origins, never "*"
    allow_credentials=False,  # tokens travel in the Authorization header, not cookies
    allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Authorization", "Content-Type"])


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for name, value in _headers_for(request.url.path).items():
        response.headers.setdefault(name, value)
    return response


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error")  # details stay in logs, never in the response
    # ServerErrorMiddleware sits outside the middleware above, so add the headers here too
    return JSONResponse({"detail": "Internal server error"}, status_code=500,
                        headers=_headers_for(request.url.path))


for r in (auth.router, datasets.router, runs.router, dashboards.router, audit_log.router):
    app.include_router(r)


@app.get("/health")
def health():
    with SessionLocal() as db:
        db.execute(text("SELECT 1"))
    return {"status": "healthy"}
