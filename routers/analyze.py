from fastapi import APIRouter, HTTPException
from parsers import get_parser
from utils import Analyzer, RedisClient
from models import AnalyzePayload, AnalyzeAndPgnPayload
import json
import asyncio

router = APIRouter(
    prefix="/analyze",
    tags=["analyze"]
)

@router.get("/last/{site}/{username}/{index}")
async def get_last_game_analyze(site: str, username: str, index: int):
    try:
        parser = get_parser(site=site)
        redis = RedisClient.get_client()
        games = await parser.get_last_games(username=username, limit=index)
        game = games[-1]
        cache_key = f"analyze:{site}:{game.url.split('/')[-1]}"
        cache = await redis.get(cache_key)
        if cache:
            try:
                res = json.loads(cache)
                if isinstance(res, (dict, list)):
                    return {"game": game, "analyze": res}
            except json.JSONDecodeError:
                pass
        return {"game": game, "analyze": None}
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))

@router.post("/last/{site}/{username}/{index}")
async def save_last_game_analyze(site: str, username: str, index: int, payload: AnalyzePayload):
    try:
        parser = get_parser(site=site)
        redis = RedisClient.get_client()
        analyze = Analyzer()
        games = await parser.get_last_games(username=username, limit=index)
        game = games[-1]
        cache_key = f"analyze:{site}:{game.url.split('/')[-1]}"
        game_analyze = [res.model_dump() for res in payload.results]
        result = await asyncio.to_thread(analyze.calculate_info, game_analyze, game.pgn)
        await redis.set(cache_key, json.dumps(result), ex=259200)
        return {"game": game, "analyze": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/pgn")
async def get_pgn_analyze_classifications(payload: AnalyzeAndPgnPayload):
    try:
        analyze = Analyzer()
        game_analyze = [res.model_dump() for res in payload.results]
        result = await asyncio.to_thread(analyze.calculate_info, game_analyze, payload.pgn)
        return {"analyze": result}
    except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
