from features import get_performance_graph


class PerformanceService:
    async def get_graph(self, site: str, username: str, days: int, control: str | None = None):
        return await get_performance_graph(
            site=site,
            username=username,
            days=days,
            control=control,
        )
