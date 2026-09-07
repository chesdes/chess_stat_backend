from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn
import logging
from routers import graphs, profile, analyze, games
from config import CORS_ORIGINS

logger = logging.getLogger(__name__)
from utils import (
    RedisClient,
    UpstreamHTTPError,
    UpstreamUnavailableError,
    close_http_client,
    init_http_client,
)
import os

@asynccontextmanager
async def lifespan(app: FastAPI):
    host = os.getenv("REDIS_HOST", "localhost")
    RedisClient.init(host=host, port=6379)
    await RedisClient.get_client().ping()
    await init_http_client()

    yield

    await close_http_client()
    await RedisClient.get_client().aclose()

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
    return JSONResponse(status_code=status_code, content={"detail": detail})

@app.exception_handler(UpstreamUnavailableError)
async def upstream_unavailable_error_handler(request: Request, exc: UpstreamUnavailableError):
    return JSONResponse(status_code=503, content={"detail": "External service is unavailable"})

app.openapi_version = "3.1.0"

app.include_router(graphs.router)
app.include_router(profile.router)
app.include_router(analyze.router)
app.include_router(games.router)

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
