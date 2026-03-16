from fastapi import APIRouter, HTTPException
from parsers import get_parser

router = APIRouter(
    prefix="/profile",
    tags=["profile"]
)

@router.get("/{site}/{username}")
async def get_profile(site: str, username: str):
    try:
        parser = get_parser(site=site)
        return await parser.get_profile(username=username)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))
    
