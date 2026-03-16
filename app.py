from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from routers import graphs, profile, analyze, games
from utils import RedisClient
import os

@asynccontextmanager
async def lifespan(app: FastAPI):
    host = os.getenv("REDIS_HOST", "localhost")
    RedisClient.init(host=host, port=6379)

    yield

    RedisClient.get_client().close()

app = FastAPI(
    lifespan=lifespan,
    title="Chess Stats API",
    description="API for chess stats applications",
    version="1.0.0",
    root_path="/api"
)

app.openapi_version = "3.1.0"

app.include_router(graphs.router)
app.include_router(profile.router)
app.include_router(analyze.router)
app.include_router(games.router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True, # ["https://chess-stat.ru", "http://localhost"],
    allow_methods=["*"],
    allow_headers=["*"], 
)

@app.get("/ping")
def ping():
    return {"message": "pong!"}

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
