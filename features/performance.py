from parsers import get_parser
from datetime import datetime
from models import Game

async def get_performance_of_last_games(site: str, username: str, games: int, control: str | None = None) -> int:
    username = username.lower()
    parser = get_parser(site)
    games_list = await parser.get_last_games(username=username, limit=games, control=control)
    return get_performance(games=games_list, username=username)

async def get_performance_of_last_months(site: str, username: str, months: int, control: str | None = None) -> int:
    username = username.lower()
    parser = get_parser(site)
    cur_month = datetime.today().month
    cur_year = datetime.today().year

    for i in range(months):
        games_list += await parser.get_month_games(username,cur_year,cur_month,control)
        if cur_month > 1:
            cur_month -= 1
        else:
            cur_month = 12
            cur_year -= 1
    return get_performance(games=games_list, username=username)

async def get_performance(games: list[Game], username: str):
    username = username.lower()
    ops = []
    score = 0
    for i in games:
        if i.white.username.lower() == username:
            ops.append(i.black.rating)
            match i.white.result:
                case "win": score += 1
                case "loss": score += 0.5
        elif i.black.username.lower() == username:
            ops.append(i.white.rating)
            match i.black.result:
                case "win": score += 1
                case "loss": score += 0.5
    return _performance_rating(ops, score)

def _expected_score(opponent_ratings: list[float], own_rating: float) -> float:
    """How many points we expect to score in a tourney with these opponents"""
    return sum(
        1 / (1 + 10**((opponent_rating - own_rating) / 400))
        for opponent_rating in opponent_ratings
    )


def _performance_rating(opponent_ratings: list[float], score: float) -> int:
    """Calculate mathematically perfect performance rating with binary search"""
    if len(opponent_ratings) > 0:
        lo, hi = min(opponent_ratings)*0.8, max(opponent_ratings)*1.2
    else:
        return 0

    while hi - lo > 0.001:
        mid = (lo + hi) / 2
        exp_score = _expected_score(opponent_ratings, mid)
        if exp_score < score:
            lo = mid
        else:
            hi = mid

    return round(mid)