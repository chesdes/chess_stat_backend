from datetime import datetime, timedelta, timezone
from .performance import get_performance
from parsers import get_parser

async def get_performance_graph(site: str, username: str, days: int, control: str | None = None):
    if days < 30:
        step_days = 1
    else:
        step_days = round(days/30)
    parser = get_parser(site)
    username = username.lower()

    now = datetime.now(tz=timezone.utc)
    end_date = now.replace(hour=3, minute=0, second=0, microsecond=0)
    if now >= end_date:
        end_date += timedelta(days=1)

    start_date = end_date - timedelta(days=days)
    start_date = start_date.replace(hour=3, minute=0, second=0, microsecond=0)

    intervals = []
    current = start_date
    while current < end_date:
        next_step = current + timedelta(days=step_days)
        intervals.append((current, next_step))
        current = next_step

    all_games = await parser.get_games_in_period(
        username=username,
        start=start_date.timestamp(),
        end=end_date.timestamp(),
        control=control
    )

    if not all_games:
        return []

    maxp = 0
    minp = 10000
    points = []
    for start, end in intervals:
        games_in_step = [
            g for g in all_games
            if start <= g.end_time < end
        ]
        if not games_in_step:
            if points:
                points.append({
                    "timestamp": int(start.timestamp()),
                    "value": points[-1]["value"],
                    "rating": points[-1]["rating"],
                    "games": 0,
                })
            continue
        
        perf_value = await get_performance(games=games_in_step, username=username)
        last_game = games_in_step[0]
        points.append({
            "timestamp": int(start.timestamp()),
            "value": perf_value,
            "rating": last_game.white.rating if last_game.white.username.lower() == username else last_game.black.rating,
            "games": len(games_in_step)
        })
        maxp = perf_value if perf_value > maxp else maxp
        minp = perf_value if perf_value < minp else minp

    full_per_perf = await get_performance(games=all_games, username=username)

    return {"max": maxp, "min": minp, "full": full_per_perf, "points": points}
