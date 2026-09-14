import asyncio

from features import get_performance_graph

CONTROLS = ("rapid", "blitz", "bullet", "daily")


class PerformanceService:
    async def get_graph(self, site: str, username: str, days: int, control: str | None = None):
        return await get_performance_graph(
            site=site,
            username=username,
            days=days,
            control=control,
        )

    async def get_all_graphs(self, site: str, username: str, days: int) -> dict:
        """Fetch all four time-control graphs concurrently in one call."""
        results = await asyncio.gather(
            *(self.get_graph(site, username, days, control) for control in CONTROLS),
            return_exceptions=True,
        )
        out: dict = {}
        for control, result in zip(CONTROLS, results):
            out[control] = [] if isinstance(result, Exception) else result
        return out
