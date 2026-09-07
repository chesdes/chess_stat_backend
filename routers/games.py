from fastapi import APIRouter, HTTPException, Path, Query
from models import Game, GamesPage
from config import MAX_GAMES, MAX_PAGE_OFFSET, MAX_PAGE_SIZE
from services import GamesService
from utils import UpstreamError
import logging

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/games",
    tags=["games"]
)

games_service = GamesService()
    
@router.get(
    "/last/{site}/{username}/{amount}",
    response_model=list[Game],
    summary="Get the latest games",
    response_description="Games ordered from newest to oldest",
)
async def get_last_games(
    site: str,
    username: str,
    amount: int = Path(..., ge=1, le=MAX_GAMES, description="Number of games to return"),
):
    """Return up to the requested number of recent games for a player."""
    try:
        return await games_service.get_last_games(site, username, amount)
    except UpstreamError:
        raise
    except Exception:
        logger.exception("Failed to load games")
        raise HTTPException(status_code=404, detail="Unable to load games")

@router.get(
    "/page/{site}/{username}",
    response_model=GamesPage,
    summary="Get a page of games",
    response_description="A page of games and pagination metadata",
)
async def get_games_page(
    site: str,
    username: str,
    limit: int = Query(10, ge=1, le=MAX_PAGE_SIZE, description="Number of games per page"),
    offset: int = Query(0, ge=0, le=MAX_PAGE_OFFSET, description="Number of games to skip"),
):
    """Return a page of recent games without sending the full history."""
    try:
        return await games_service.get_games_page(site, username, limit, offset)
    except UpstreamError:
        raise
    except Exception:
        logger.exception("Failed to load games page")
        raise HTTPException(status_code=404, detail="Unable to load games page")