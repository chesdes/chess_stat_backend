from fastapi import APIRouter, HTTPException
from parsers import get_parser
from utils import Analyzer, RedisClient
from dotenv import load_dotenv
from models import AnalyzePayload
import os, requests as r
import json
import asyncio

router = APIRouter(
    prefix="/analyze",
    tags=["analyze"]
)

# @router.post("/analyze/last/{site}/{username}/{index}")
# async def post_last_game_analyze(
#     site: str,
#     username: str,
#     index: int,
#     payload: AnalyzePayload
# ):
#     load_dotenv()
#     TOKEN, CHAT_ID = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT_ID")

#     try:
#         parser = get_parser(site=site)
#         analyzer = Analyzer()

#         games = await parser.get_last_games(username=username, limit=index)
#         game = games[-1]
#         game_analyze = payload.analyze
#         result = await asyncio.to_thread(
#             analyzer.calculate_info,
#             game_analyze,
#             game.pgn
#         )

#         # tg log
#         r.post(
#             f"https://api.telegram.org/bot{TOKEN}/sendMessage",
#             json={
#                 "chat_id": CHAT_ID,
#                 "text": f"GREAT ANALYZE GAME {game.url}\nBY {username}"
#             }
#         )

#         return {
#             "game": game,
#             "analyze": result
#         }

#     except Exception as e:
#         r.post(
#             f"https://api.telegram.org/bot{TOKEN}/sendMessage",
#             json={
#                 "chat_id": CHAT_ID,
#                 "text": f"ERROR ANALYZE GAME {username}\nDETAIL:\n{str(e)}"
#             }
#         )
#         raise HTTPException(status_code=400, detail=str(e))


@router.get("/last/{site}/{username}/{index}")
async def get_last_game_analyze(site: str, username: str, index: int):
    # tg logs - remove future
    load_dotenv()
    TOKEN, CHAT_ID = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT_ID")

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
        # tg logs - remove future
        r.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", 
            json={"chat_id": CHAT_ID, "text": f"GREAT ANALYZE GAME {game.url}\nBY {username}"})
        
        return {"game": game, "analyze": result}
    except Exception as e:
        # tg logs - remove future
        r.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": f"ERROR ANALYZE GAME {game.url}\nBY {username}\nDETAIL:\n{str(e)}"})
        
        raise HTTPException(status_code=404, detail=str(e))