from fastapi import APIRouter, HTTPException
from parsers import get_parser

router = APIRouter(
    prefix="/games",
    tags=["games"]
)
    
@router.get("/last/{site}/{username}/{amount}")
async def get_last_game_analyze(site: str, username: str, amount: int):
    try:
        parser = get_parser(site=site)
        games = await parser.get_last_games(username=username, limit=amount)
        return games
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))