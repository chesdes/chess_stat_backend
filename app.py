from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn
import logging
import asyncio
import os
from routers import graphs, profile, analyze, games, admin
from config import CORS_ORIGINS, DATABASE_URL, STATS_IP_SALT, STATS_RETENTION_DAYS
from middleware import StatsMiddleware
from db import dispose_engine
from services import retention_loop

logger = logging.getLogger(__name__)
from utils import (
    RedisClient,
    UpstreamHTTPError,
    UpstreamUnavailableError,
    close_http_client,
    init_http_client,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    if DATABASE_URL and len(STATS_IP_SALT) < 32:
        raise RuntimeError("STATS_IP_SALT must contain at least 32 characters when statistics are enabled")

    host = os.getenv("REDIS_HOST", "localhost")
    RedisClient.init(host=host, port=6379)
    await RedisClient.get_client().ping()
    await init_http_client()

    retention_task = None
    if DATABASE_URL:
        retention_task = asyncio.create_task(retention_loop(STATS_RETENTION_DAYS))
    else:
        logger.warning("DATABASE_URL is not set: request statistics collection is disabled")

    yield

    if retention_task is not None:
        retention_task.cancel()
        try:
            await retention_task
        except asyncio.CancelledError:
            pass
    await close_http_client()
    await RedisClient.get_client().aclose()
    await dispose_engine()

app = FastAPI(
    lifespan=lifespan,
    title="Chess Stats API",
    description=(
        "Public API for Chess.com player profiles, game history, performance "
        "graphs and chess game analysis. Analysis endpoints accept PGN data "
        "and evaluations already produced by Stockfish on the client, then "
        "calculate classifications and statistics on the server."
    ),
    version="1.0.0",
    root_path="/api"
)

@app.exception_handler(UpstreamHTTPError)
async def upstream_http_error_handler(request: Request, exc: UpstreamHTTPError):
    status_code = {
        404: 404,
        429: 429,
    }.get(exc.status_code, 502)
    detail = {
        404: "External resource not found",
        429: "External service rate limit exceeded",
    }.get(exc.status_code, "External service request failed")
    logger.warning(
        "Upstream HTTP %s on %s (our %s %s)",
        exc.status_code,
        exc.url or "<unknown url>",
        request.method,
        request.url.path,
    )
    return JSONResponse(status_code=status_code, content={"detail": detail})

@app.exception_handler(UpstreamUnavailableError)
async def upstream_unavailable_error_handler(request: Request, exc: UpstreamUnavailableError):
    logger.warning("Upstream unavailable on our %s %s", request.method, request.url.path)
    return JSONResponse(status_code=503, content={"detail": "External service is unavailable"})

app.openapi_version = "3.1.0"

app.include_router(graphs.router)
app.include_router(profile.router)
app.include_router(analyze.router)
app.include_router(games.router)
app.include_router(admin.router)

app.add_middleware(StatsMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"], 
)

@app.get("/ping")
def ping():
    return {"message": "pong!"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/ready")
async def ready():
    try:
        await RedisClient.get_client().ping()
    except Exception as exc:
        logger.exception("Readiness check failed")
        raise HTTPException(status_code=503, detail="Redis is unavailable") from exc
    return {"status": "ready"}

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
