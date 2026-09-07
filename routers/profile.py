from fastapi import APIRouter, HTTPException
from services import ProfileService
from models import PlayerProfile
from utils import UpstreamError
import logging

logger = logging.getLogger(__name__)
profile_service = ProfileService()

router = APIRouter(
    prefix="/profile",
    tags=["profile"]
)

@router.get(
    "/{site}/{username}",
    response_model=PlayerProfile,
    summary="Get a player's profile",
    response_description="Profile, ratings and win/loss/draw statistics",
)
async def get_profile(
    site: str,
    username: str,
):
    """Return a public player profile from the selected chess site."""
    try:
        return await profile_service.get_profile(site, username)
    except UpstreamError:
        raise
    except Exception:
        logger.exception("Failed to load profile")
        raise HTTPException(status_code=404, detail="Unable to load profile")
    
