from fastapi import APIRouter, HTTPException
from features import get_performance_graph

router = APIRouter(
    prefix="/graphs",
    tags=["graphs"]
)

@router.get("/performance/{site}/{username}/{days}")
async def get_perf_graph_points(site: str, username: str, days: int, control: str | None = None):
    try:
        return await get_performance_graph(site=site, username=username, days=days, control=control)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))