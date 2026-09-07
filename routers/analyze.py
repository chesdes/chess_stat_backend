from fastapi import APIRouter, HTTPException, Path
from models import AnalyzePayload, AnalyzeAndPgnPayload
from services import AnalysisCapacityError, AnalysisService, GameNotFoundError
from utils import UpstreamError
from config import MAX_GAMES
import logging

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/analyze",
    tags=["analyze"]
)
analysis_service = AnalysisService()

@router.get(
    "/last/{site}/{username}/{index}",
    summary="Get cached analysis for a recent game",
    response_description="The selected game and cached analysis, or null when not analyzed",
)
async def get_last_game_analyze(
    site: str,
    username: str,
    index: int = Path(..., ge=1, le=MAX_GAMES, description="1-based position in recent games"),
):
    """Get the latest selected game and its cached analysis, if available."""
    try:
        return await analysis_service.get_last_game_analysis(site, username, index)
    except GameNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except UpstreamError:
        raise
    except Exception:
        logger.exception("Failed to load game analysis")
        raise HTTPException(status_code=404, detail="Unable to load game analysis")

@router.post(
    "/last/{site}/{username}/{index}",
    summary="Analyze and cache a recent game",
    response_description="The selected game and server-calculated classifications",
)
async def save_last_game_analyze(
    site: str,
    username: str,
    payload: AnalyzePayload,
    index: int = Path(..., ge=1, le=MAX_GAMES, description="1-based position in recent games"),
):
    """Use supplied Stockfish results to calculate, cache and return classifications for a recent game."""
    try:
        return await analysis_service.save_last_game_analysis(site, username, index, payload)
    except GameNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AnalysisCapacityError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except UpstreamError:
        raise
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception:
        logger.exception("Failed to save game analysis")
        raise HTTPException(status_code=500, detail="Unable to save game analysis")

@router.post(
    "/pgn",
    summary="Classify a PGN using Stockfish results",
    response_description="Move classifications calculated from the PGN and supplied Stockfish results",
)
async def get_pgn_analyze_classifications(payload: AnalyzeAndPgnPayload):
    """Calculate move classifications from a PGN and precomputed Stockfish results."""
    try:
        return await analysis_service.analyze_pgn(payload)
    except AnalysisCapacityError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except UpstreamError:
        raise
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception:
        logger.exception("Failed to analyze PGN")
        raise HTTPException(status_code=500, detail="Unable to analyze PGN")
