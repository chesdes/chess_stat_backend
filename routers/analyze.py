from fastapi import APIRouter, HTTPException
from parsers import get_parser
from utils import Analyzer, RedisClient
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
        analyze = Analyzer()
        games = await parser.get_last_games(username=username, limit=index)
        game = games[-1]
        cache = await redis.get(f"analyze:{site}:{game.url.split('/')[-1]}")
        if cache:
            try:
                res = json.loads(cache)
                return {"game": game, "analyze": res} 
            except:
                return {"game": game, "analyze": cache} 
        await redis.set(f"analyze:{site}:{game.url.split('/')[-1]}", "in progress", ex=259200)
        game_analyze = await asyncio.to_thread(analyze.analyze_pgn, game.pgn)
        result = await asyncio.to_thread(analyze.calculate_info, game_analyze, game.pgn)
        await redis.set(f"analyze:{site}:{game.url.split('/')[-1]}", json.dumps(result), ex=259200)
        return {"game": game, "analyze": result}
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))