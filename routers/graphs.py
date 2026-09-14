from fastapi import APIRouter, HTTPException, Path, Query
from services import PerformanceService
from models import PerformanceGraph
from parsers import PlayerNotFoundError
from utils import UpstreamError
import logging

logger = logging.getLogger(__name__)
performance_service = PerformanceService()

router = APIRouter(
    prefix="/graphs",
    tags=["graphs"]
)

@router.get(
    "/performance/{site}/{username}/{days}",
    response_model=PerformanceGraph | list,
    summary="Get performance graph data",
    response_description="Performance and rating points for the selected period",
)
async def get_perf_graph_points(
    site: str,
    username: str,
    days: int = Path(..., ge=1, le=3650, description="Number of days to include"),
    control: str | None = Query(None, description="Optional time control filter, for example rapid or blitz"),
):
    """Return performance-rating points grouped over the requested period."""
    try:
        return await performance_service.get_graph(site, username, days, control)
    except UpstreamError:
        raise
    except PlayerNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Player not found") from exc
    except Exception:
        logger.exception("Failed to load performance graph")
        raise HTTPException(status_code=404, detail="Unable to load performance graph")


@router.get(
    "/performance/{site}/{username}/{days}/all",
    summary="Get all time-control performance graphs in one request",
    response_description="Mapping of control name to graph data (or empty list)",
)
async def get_perf_graph_all(
    site: str,
    username: str,
    days: int = Path(..., ge=1, le=3650, description="Number of days to include"),
):
    """Return rapid/blitz/bullet/daily graphs together to avoid 4 round-trips."""
    try:
        return await performance_service.get_all_graphs(site, username, days)
    except UpstreamError:
        raise
    except PlayerNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Player not found") from exc
    except Exception:
        logger.exception("Failed to load performance graphs")
        raise HTTPException(status_code=404, detail="Unable to load performance graphs")
