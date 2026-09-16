# chess-stat.ru backend

Backend API for [chess-stat.ru](https://chess-stat.ru) — chess statistics and game analysis built on FastAPI.

**🇬🇧 [English](#english)** · **🇷🇺 [Русский](#русский)**

---

<a id="english"></a>

# 🇬🇧 English

Backend API for [chess-stat.ru](https://chess-stat.ru) — chess statistics and game analysis built on FastAPI. It proxies and aggregates data from Chess.com, calculates performance ratings and rating graphs, and turns client-side Stockfish evaluations into move classifications (accuracy, blunders, brilliancies, opening theory, etc).

Interactive OpenAPI docs: **[chess-stat.ru/api/docs](https://chess-stat.ru/api/docs)**

All routes below are mounted under the `/api` root path in production (`root_path="/api"`), so e.g. `/profile/{site}/{username}` is reachable at `https://chess-stat.ru/api/profile/{site}/{username}`.

## Table of contents

- [Tech stack](#tech-stack)
- [Concepts](#concepts)
- [Endpoints](#endpoints)
  - [Profile](#profile)
  - [Games](#games)
  - [Performance graphs](#performance-graphs)
  - [Analysis](#analysis)
  - [Admin](#admin)
  - [Health](#health)
- [Data models](#data-models)
- [Error format](#error-format)
- [Rate & capacity limits](#rate--capacity-limits)

---

## Tech stack

**Language & runtime**
- Python 3.12 (`python:3.12-slim-bookworm` base image)

**Web framework**
- [FastAPI](https://fastapi.tiangolo.com/) 0.121 on top of [Starlette](https://www.starlette.io/) 0.49, served by [Uvicorn](https://www.uvicorn.org/) 0.38 (with `uvloop` and `httptools` for the async event loop / HTTP parsing)
- [Pydantic](https://docs.pydantic.dev/) v2 for request/response models and validation
- OpenAPI 3.1 schema, docs auto-generated at `/api/docs`

**Data layer**
- **PostgreSQL** (via `asyncpg`) in production for request/traffic statistics, **SQLite** (via `aiosqlite`) as the lightweight/test backend — same codebase, driver picked from `DATABASE_URL`
- [SQLAlchemy](https://www.sqlalchemy.org/) 2.0 (async ORM, `DeclarativeBase` models)
- [Alembic](https://alembic.sqlalchemy.org/) for schema migrations, run automatically on container start (`entrypoint.sh`)
- [Redis](https://redis.io/) (via `redis-py`'s async client) — caches computed game analyses (3-day TTL) and backs the admin login rate-limiter/ban list

**Chess logic**
- [`python-chess`](https://python-chess.readthedocs.io/) for PGN parsing, board/move handling, Chess960/custom-FEN support
- [Stockfish](https://stockfishchess.org/) engine (bundled into the Docker image at build time, pinned by version/SHA256, multi-arch amd64/arm64) driven through the [`stockfish`](https://pypi.org/project/stockfish/) Python wrapper — used only for server-computed evaluations where applicable; client-submitted evaluations are the primary analysis path (see [Concepts](#concepts))
- A bundled [ECO opening database](https://en.wikipedia.org/wiki/Encyclopaedia_of_Chess_Openings) (categories A–E, fetched at Docker build time) for opening detection

**HTTP & integration**
- [`aiohttp`](https://docs.aiohttp.org/) as the async HTTP client for calling the Chess.com Published Data API, with retry/backoff on transient errors (408/425/429/5xx) and typed upstream-error exceptions
- [PyJWT](https://pyjwt.readthedocs.io/) for signing/verifying admin bearer tokens (HS256)

**Infrastructure**
- Docker multi-stage build (a dedicated `stockfish` build stage, then the Python app stage), runs as a non-root user
- CORS via Starlette's `CORSMiddleware`, origins configurable through `CORS_ORIGINS`
- A custom `StatsMiddleware` records every request (path, endpoint, status, duration, source classification, hashed IP, cache-hit/move-count metadata) for the admin dashboard, with bot/scanner detection and a background retention loop that prunes old rows
- Structured around FastAPI `APIRouter`s per domain (`profile`, `games`, `graphs`, `analyze`, `admin`), a thin `services/` layer, and `parsers/` implementing a `BaseParser` interface per chess site (currently `chesscom`)

**Testing**
- Python's built-in `unittest` (with `unittest.mock`) — no pytest dependency; async tests driven via `asyncio.run(...)`

---

## Concepts

### Supported sites

Every player-scoped endpoint takes a `site` path parameter. Currently the only supported value is:

- `chesscom` — [Chess.com Published Data API](https://www.chess.com/news/view/published-data-api)

Requesting an unsupported site returns a `404`.

### Analysis model: client computes, server classifies

Running Stockfish is expensive, so this API does **not** run a chess engine itself for every request. Instead:

1. The client (browser) runs Stockfish locally over a game's moves and produces one evaluation (`{type, value}`) plus a `best_move` per ply.
2. The client sends those precomputed results to `POST /analyze/pgn` or `POST /analyze/last/{site}/{username}/{index}`.
3. The server turns raw evaluations into per-move **classifications** (best / excellent / good / inaccuracy / mistake / blunder / great / brilliant / miss / theory, plus chess-stat.ru's own `chess_stat` classifier), an **accuracy** score, an **efficiency** score (see below), and **opening** detection — and caches the result in Redis for game-analysis endpoints.

### Move classification systems

Two parallel classifiers are returned per move under `move_classify`:

- `chesscom` — mirrors Chess.com's classification vocabulary (`best`, `excellent`, `good`, `inaccuracy`, `mistake`, `blunder`, `great`, `brilliant`, `miss`, `theoretical`), elo-adjusted thresholds, sacrifice/brilliancy detection.
- `chess_stat` — **chess-stat.ru's own classification system** (`advance`, `steady`, `retreat`, `miss`, `theory`). Where Chess.com's nine-tier scale can make it harder to judge how much a move actually mattered, `chess_stat` deliberately collapses move quality into three clear buckets: good moves (`advance`), moves that were slightly suboptimal but not damaging — a better move existed (`steady`), and genuinely bad moves (`retreat`). `miss` is separate from these — it flags a missed opportunity: the opponent just made a mistake that handed a significant advantage, and the player failed to capitalize on it. `theory` covers known opening moves. The goal is to make it easier for a player to see, at a glance, exactly where their thinking broke down in a game, rather than getting lost in fine-grained distinctions between near-equivalent move qualities.

### Opening detection

The first move sequence of a game is matched against an ECO opening database (categories A–E) to detect the opening name and mark "theory" moves that match known opening lines.

### Efficiency: who is playing better *right now*

Engine evaluation alone doesn't tell you who currently has the initiative — it only tells you who stands better. Two players can reach the exact same position through very different paths: one confidently and quickly, the other barely holding on and burning their clock to survive. `efficiency` is built to surface that difference.

For each move, efficiency combines two things:

- **Move quality** — how close the move was to the best move (`diff_expected`, i.e. the same expected-score delta used for classification), so an inaccurate move is immediately penalized regardless of how fast it was played.
- **Time spent** — the move's time cost relative to the base time control (`move_time / time_before`, capped so extreme outliers don't dominate), so a "correct" move that took forever to find is still scored as a weaker moment for that player.

These are combined into a single per-move score in `[0, 100]`, then smoothed into `eff_white` / `eff_black`: a rolling average over each side's last **3** moves (`EFF_WINDOW_PER_SIDE`), so the number reflects *recent form in the position*, not a single lucky or unlucky move. `efficiency` requires a clock in the PGN (`[%clk ...]` annotations); without clock data it — and `eff_white`/`eff_black` — is `null`.

**Reading it as an eval bar:** plotting `eff_white` against `eff_black` over the course of a game gives a second bar alongside the engine evaluation bar. The engine bar says who's ahead; the efficiency bar says who's currently under pressure. A telling pattern: White is worse by engine evaluation, but Black — despite standing better — is burning large amounts of time each move (the position is hard, Black doesn't yet feel the win), while White responds quickly and accurately. Here White's efficiency is higher than Black's even though White's evaluation is lower — a signal that Black is more likely to err as the position continues, and White has a realistic practical path back into (or to) a winning position.

---

## Endpoints

### Profile

#### `GET /profile/{site}/{username}`

Returns a public player profile: avatar, title, country, and win/loss/draw statistics broken down by time control (rapid/blitz/bullet/daily).

**Path parameters**

| Name | Type | Description |
|---|---|---|
| `site` | string | Chess site, e.g. `chesscom` |
| `username` | string | Player username (case-insensitive) |

**Response** — [`PlayerProfile`](#playerprofile)

**Errors:** `404` if the player doesn't exist or the site is unsupported.

---

### Games

#### `GET /games/last/{site}/{username}/{amount}`

Returns the most recent games for a player, newest first.

**Path parameters**

| Name | Type | Constraints | Description |
|---|---|---|---|
| `site` | string | — | Chess site |
| `username` | string | — | Player username |
| `amount` | int | `1 ≤ amount ≤ MAX_GAMES` (default cap 300) | Number of games to return |

**Response** — array of [`Game`](#game)

**Errors:** `404` if the player doesn't exist.

---

#### `GET /games/page/{site}/{username}`

Paginated game history, for infinite-scroll style UIs without downloading a player's full history.

**Path parameters:** `site`, `username`.

**Query parameters**

| Name | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `limit` | int | `10` | `1 ≤ limit ≤ MAX_PAGE_SIZE` (default cap 50) | Games per page |
| `offset` | int | `0` | `0 ≤ offset ≤ MAX_PAGE_OFFSET` | Games to skip |

**Response** — [`GamesPage`](#gamespage)

`total_pages` is only known (and populated) once the last page has been reached; otherwise it's `null` and `has_more` is `true`, since the backend never scans a player's entire archive just to count games.

**Errors:** `404` if the player doesn't exist.

---

### Performance graphs

Performance rating is computed with the standard iterative (binary-search) formula against opponents actually faced — not a simple Elo delta.

#### `GET /graphs/performance/{site}/{username}/{days}`

Rating/performance points over a period, bucketed daily (for `days < 30`) or in roughly `days / 30` groupings otherwise.

**Path parameters**

| Name | Type | Constraints | Description |
|---|---|---|---|
| `site` | string | — | Chess site |
| `username` | string | — | Player username |
| `days` | int | `1 ≤ days ≤ 3650` | Number of days to include, counting back from now |

**Query parameters**

| Name | Type | Description |
|---|---|---|
| `control` | string \| null | Optional time control filter (`rapid`, `blitz`, `bullet`, `daily`) |

**Response** — [`PerformanceGraph`](#performancegraph) or `[]` if the player has no games in the period.

**Errors:** `404` if the player doesn't exist.

---

#### `GET /graphs/performance/{site}/{username}/{days}/all`

Same as above but returns all four time controls in a single call, to avoid four separate round trips. Each control resolves independently — if fetching one control's graph fails, it comes back as `[]` rather than failing the whole request.

**Response**

```json
{
  "rapid": { "...": "PerformanceGraph or []" },
  "blitz": { "...": "PerformanceGraph or []" },
  "bullet": { "...": "PerformanceGraph or []" },
  "daily": { "...": "PerformanceGraph or []" }
}
```

---

### Analysis

#### `GET /analyze/last/{site}/{username}/{index}`

Fetch a previously cached analysis for a recent game, without submitting new Stockfish results. Use this first to avoid re-running Stockfish client-side if the game was already analyzed by someone else.

**Path parameters**

| Name | Type | Constraints | Description |
|---|---|---|---|
| `site` | string | — | Chess site |
| `username` | string | — | Player username |
| `index` | int | `1 ≤ index ≤ MAX_GAMES` | 1-based position in the player's recent games (1 = most recent) |

**Response**

```json
{
  "game": { "...": "Game object" },
  "analyze": [ "...move classification objects, or null if not yet analyzed" ]
}
```

**Errors:** `404` if the player or that game index doesn't exist.

---

#### `POST /analyze/last/{site}/{username}/{index}`

Submit client-computed Stockfish results for a specific recent game; the server calculates classifications, caches them (3-day TTL), and returns them.

**Path parameters:** same as the `GET` variant above.

**Request body** — [`AnalyzePayload`](#analyzepayload)

```json
{
  "results": [
    { "info": { "type": "cp", "value": 34 }, "best_move": "e2e4" },
    { "info": { "type": "mate", "value": -2 }, "best_move": "d1h5" }
  ]
}
```

- `results` must contain exactly one entry per ply (half-move) in the game's PGN.
- `info.type` is `"cp"` (centipawns) or `"mate"` (moves to mate); `info.value` is clamped to `[-10000, 10000]`.
- `best_move`, if present, must be UCI notation (`^[a-h][1-8][a-h][1-8][qrbnQRBN]?$`).

**Response** — same shape as the `GET` variant, with `analyze` always populated.

**Errors**

| Status | Meaning |
|---|---|
| `404` | Player or game index not found |
| `422` | `results` length doesn't match the number of moves in the PGN, or PGN/ratings are malformed |
| `429` | Analysis capacity temporarily exhausted (see [capacity limits](#rate--capacity-limits)) |

---

#### `POST /analyze/pgn`

Classify an arbitrary PGN (not tied to a specific site/player) using client-supplied Stockfish results. Useful for analyzing games not fetched through this API, e.g. imported or manually pasted PGNs. Results here are **not cached**.

**Request body** — [`AnalyzeAndPgnPayload`](#analyzeandpgnpayload)

```json
{
  "pgn": "1. e4 e5 2. Nf3 Nc6 ...",
  "results": [
    { "info": { "type": "cp", "value": 20 }, "best_move": "g1f3" }
  ]
}
```

- `pgn`: 1 to `MAX_PGN_LENGTH` characters (default cap 200,000).
- `results`: 1 to `MAX_ANALYSIS_RESULTS` entries (default cap 5,000), same shape as above, one per ply.
- Supports Chess960 / custom starting positions via `[SetUp "1"]` + `[FEN "..."]` headers; opening detection is skipped for non-standard starting positions.
- If the PGN includes `[%clk ...]` clock annotations (and ideally a `TimeControl` header), the response also includes per-move `efficiency` (see [Efficiency](#efficiency-who-is-playing-better-right-now)).

**Response**

```json
{ "analyze": [ "...move classification objects" ] }
```

**Errors**

| Status | Meaning |
|---|---|
| `422` | `results` length doesn't match PGN move count, or PGN/ratings are malformed |
| `429` | Analysis capacity temporarily exhausted |

---

#### Move classification object shape

Each entry returned by the analysis endpoints:

```json
{
  "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
  "move": "e7e5",
  "best_move": "e7e5",
  "evaluation": 0.3,
  "expected_score": 0.54,
  "accuracy": 98.7,
  "diff_expected": 0.0,
  "efficiency": 96.4,
  "move_time": 4.0,
  "time_before": 180.0,
  "eff_white": 96.4,
  "eff_black": null,
  "move_classify": {
    "chesscom": "best",
    "chess_stat": "advance"
  }
}
```

- `evaluation` is either a float in pawns (centipawn score / 100) or a string like `"# 3"` / `"# -0"` for forced mate.
- `accuracy` is a running average (Chess.com-style win% based formula) up to and including that move, for the side that just moved.
- `efficiency` is this move's individual efficiency score (`0`–`100`, see [Efficiency](#efficiency-who-is-playing-better-right-now)) combining move quality and time spent; `null` when the PGN has no clock data for this move.
- `move_time` / `time_before` are the seconds spent on this move and the clock time available beforehand (from PGN `[%clk ...]` tags, or the time control's base time for the very first move of each side); both `null` without clock data.
- `eff_white` / `eff_black` are the rolling average of the last 3 efficiency values for White/Black respectively — read together as an "efficiency eval bar" showing who is currently playing better, independent of the raw engine evaluation. Only the field for the side that just moved updates on a given entry; the other carries the previous value forward.

---

### Admin

Internal endpoints for the site's traffic dashboard. Not part of the public API surface — require a bearer token and are only meaningful when `DATABASE_URL` and `ADMIN_PASSWORD` are configured on the server.

#### `POST /admin/login`

Exchange the admin password for a short-lived bearer token.

**Request body**

```json
{ "password": "..." }
```

**Response** — [`AdminLoginResponse`](#adminloginresponse)

**Errors**

| Status | Meaning |
|---|---|
| `401` | Wrong password (generic message, doesn't reveal ban state) |
| `429` | IP temporarily banned after repeated failed attempts |
| `503` | Admin panel not configured (`ADMIN_PASSWORD` unset) |

Failed logins are rate-limited per IP: 5 failures bans that IP for 30 minutes (both configurable). The failure message is identical whether the password was wrong or the IP is banned, to avoid leaking ban state.

#### `GET /admin/stats`

Aggregated statistics for a period: request counts, unique visitors, error rate, daily time series, per-endpoint breakdown, analysis cache-hit stats, and top sites/usernames.

**Auth:** `Authorization: Bearer <token>` from `/admin/login`.

**Query parameters**

| Name | Type | Description |
|---|---|---|
| `period` | string \| null | One of `today`, `24h`, `7d`, `30d`, `90d`, `1y` |
| `days` | int \| null | Deprecated; use `period` instead |
| `top_endpoints` | string \| null | Comma-separated endpoint route paths to scope the top-sites/usernames breakdown |
| `visitor` | string \| null | Filter to a single visitor's `ip_hash` |

**Response** — [`AdminStatsResponse`](#adminstatsresponse)

#### `GET /admin/recent`

Paged raw request log, newest first.

**Auth:** same as above.

**Query parameters**

| Name | Type | Default | Description |
|---|---|---|---|
| `period` / `days` | — | — | Same as `/admin/stats` |
| `visitor` | string \| null | — | Filter by visitor `ip_hash` |
| `limit` | int | `100` | `1–200` rows per page |
| `offset` | int | `0` | Rows to skip |

**Response** — array of [`RecentRequest`](#recentrequest)

---

### Health

Simple liveness/readiness probes, excluded from traffic statistics.

| Endpoint | Description |
|---|---|
| `GET /ping` | Always returns `{"message": "pong!"}` |
| `GET /health` | Always returns `{"status": "ok"}` |
| `GET /ready` | Returns `{"status": "ready"}`, or `503` if Redis is unreachable |

---

## Data models

#### `Player`
```json
{ "username": "string", "rating": 0, "result": "win | loss | draw" }
```

#### `Game`
```json
{
  "site": "chesscom",
  "white": { "...": "Player" },
  "black": { "...": "Player" },
  "end_time": "2026-01-01T12:00:00Z",
  "pgn": "1. e4 e5 ...",
  "time_control": "rapid | blitz | bullet | daily | null",
  "url": "https://... | null"
}
```

#### `GamesPage`
```json
{
  "games": [ "...Game" ],
  "offset": 0,
  "limit": 10,
  "total_pages": 3,
  "has_more": false
}
```

#### `PlayerProfile`
```json
{
  "site": "chesscom",
  "username": "string",
  "avatar": "https://... | null",
  "country": "US | null",
  "title": "GM | null",
  "stats": { "...": "Stats" }
}
```

#### `Stats`
```json
{
  "total_games": 0,
  "wins": 0,
  "losses": 0,
  "draws": 0,
  "rapid": { "...": "TimeControl" },
  "blitz": { "...": "TimeControl" },
  "bullet": { "...": "TimeControl" },
  "daily": { "...": "TimeControl" }
}
```

#### `TimeControl`
```json
{ "rating": 0, "games": 0, "wins": 0, "losses": 0, "draws": 0 }
```

#### `PerformanceGraph`
```json
{
  "max": 0,
  "min": 0,
  "full": 0,
  "points": [
    { "timestamp": 0, "value": 0, "rating": 0, "games": 0 }
  ]
}
```

#### `AnalyzePayload` / `AnalyzeAndPgnPayload`
See the [POST /analyze/last](#post-analyzelastsiteusernameindex) and [POST /analyze/pgn](#post-analyzepgn) sections above.

#### `AdminLoginResponse`
```json
{ "token": "string", "expires_in": 86400 }
```

#### `AdminStatsResponse`
```json
{
  "summary": {
    "requests": 0, "unique_visitors": 0, "errors": 0,
    "avg_duration_ms": 0, "frontend": 0, "other": 0, "period": "30d"
  },
  "daily": [
    { "date": "2026-01-01", "requests": 0, "analyses": 0, "unique_visitors": 0, "errors": 0, "bots": 0 }
  ],
  "endpoints": [
    { "endpoint": "/profile/{site}/{username}", "requests": 0, "errors": 0, "avg_duration_ms": 0, "unique_visitors": 0, "frontend": 0, "other": 0 }
  ],
  "recent": [ "...RecentRequest" ],
  "analysis": {
    "total": 0, "cache_hit_rate": 0.0, "avg_moves": 0.0,
    "local": 0, "cache": 0, "local_moves": 0, "cache_moves": 0, "avg_local_moves": 0.0
  },
  "top": { "sites": [ { "name": "chesscom", "requests": 0, "unique_visitors": 0 } ], "usernames": [] },
  "period": "30d"
}
```

#### `RecentRequest`
```json
{
  "ts": "2026-01-01T12:00:00Z",
  "method": "GET",
  "path": "/profile/chesscom/magnus",
  "endpoint": "/profile/{site}/{username}",
  "status": 200,
  "duration_ms": 42,
  "source": "frontend | bot | other",
  "site": "chesscom | null",
  "username": "magnus | null",
  "cache_hit": true,
  "moves_count": 40,
  "visitor_name": "brave-otter-07",
  "ip_hash": "sha256 hex | null",
  "error_detail": "string | null"
}
```

`ip_hash` is salted with the current UTC date, so the same visitor gets a different hash every day — it identifies a visitor for grouping *within* a day only, never across days. `visitor_name` is a deterministic, human-readable nickname derived from that hash for the same purpose.

---

## Error format

All errors follow FastAPI's default shape:

```json
{ "detail": "Human-readable message" }
```

or, for request validation errors (`422`), FastAPI's standard field-level error list.

**Common status codes across endpoints**

| Status | Meaning |
|---|---|
| `404` | Player, game, or route not found — also used for unsupported `site` values |
| `422` | Request validation failed (bad payload shape, mismatched analysis result count, malformed PGN) |
| `429` | Analysis capacity exhausted, or (admin login only) too many failed attempts |
| `502` | Upstream chess site request failed |
| `503` | Upstream chess site unavailable, or admin panel not configured |

---

## Rate & capacity limits

These are server-side defaults (all configurable via environment variables) — not something a client needs to self-throttle for unless it starts seeing `429`s:

| Limit | Default | Purpose |
|---|---|---|
| `MAX_GAMES` | 300 | Max games returned per `/games/last` request and max analyzable game index |
| `MAX_PAGE_SIZE` | 50 | Max `limit` for `/games/page` |
| `MAX_PGN_LENGTH` | 200,000 chars | Max PGN size accepted by `/analyze/pgn` |
| `MAX_ANALYSIS_RESULTS` | 5,000 | Max moves accepted per analysis request |
| `ANALYSIS_CONCURRENCY` | 10 | Concurrent server-side analysis calculations before `429`s start |

More information in the future, as the project develops.

---

<a id="русский"></a>

# 🇷🇺 Русский

Backend API для [chess-stat.ru](https://chess-stat.ru) — статистика и анализ шахматных партий на FastAPI. Сервис проксирует и агрегирует данные с Chess.com, рассчитывает перформанс-рейтинг и графики рейтинга, а также превращает присланные клиентом оценки Stockfish в классификацию ходов (точность, зевки, блестящие ходы, теория дебютов и т.д).

Интерактивная документация OpenAPI: **[chess-stat.ru/api/docs](https://chess-stat.ru/api/docs)**

Все маршруты ниже в продакшене монтируются под корневым путём `/api` (`root_path="/api"`), то есть, например, `/profile/{site}/{username}` доступен по адресу `https://chess-stat.ru/api/profile/{site}/{username}`.

## Содержание

- [Стек технологий](#стек-технологий)
- [Концепции](#концепции)
- [Эндпоинты](#эндпоинты)
  - [Профиль](#профиль)
  - [Партии](#партии)
  - [Графики перформанса](#графики-перформанса)
  - [Анализ](#анализ)
  - [Админка](#админка)
  - [Health-проверки](#health-проверки)
- [Модели данных](#модели-данных)
- [Формат ошибок](#формат-ошибок)
- [Лимиты скорости и ёмкости](#лимиты-скорости-и-ёмкости)

---

## Стек технологий

**Язык и рантайм**
- Python 3.12 (базовый образ `python:3.12-slim-bookworm`)

**Веб-фреймворк**
- [FastAPI](https://fastapi.tiangolo.com/) 0.121 поверх [Starlette](https://www.starlette.io/) 0.49, работает под [Uvicorn](https://www.uvicorn.org/) 0.38 (с `uvloop` и `httptools` для асинхронного event loop / парсинга HTTP)
- [Pydantic](https://docs.pydantic.dev/) v2 для моделей запросов/ответов и валидации
- Схема OpenAPI 3.1, документация автогенерируется по пути `/api/docs`

**Слой данных**
- **PostgreSQL** (через `asyncpg`) в продакшене для статистики запросов/трафика, **SQLite** (через `aiosqlite`) как облегчённый бэкенд для тестов — одна и та же кодовая база, драйвер выбирается по `DATABASE_URL`
- [SQLAlchemy](https://www.sqlalchemy.org/) 2.0 (асинхронный ORM, модели на `DeclarativeBase`)
- [Alembic](https://alembic.sqlalchemy.org/) для миграций схемы, запускается автоматически при старте контейнера (`entrypoint.sh`)
- [Redis](https://redis.io/) (через асинхронный клиент `redis-py`) — кэширует вычисленные анализы партий (TTL 3 дня) и обеспечивает rate-limit/бан-лист для логина в админку

**Шахматная логика**
- [`python-chess`](https://python-chess.readthedocs.io/) для парсинга PGN, работы с доской/ходами, поддержки Chess960/кастомных FEN
- Движок [Stockfish](https://stockfishchess.org/) (встраивается в Docker-образ при сборке, версия и SHA256 зафиксированы, поддержка amd64/arm64), управляется через Python-обёртку [`stockfish`](https://pypi.org/project/stockfish/) — используется только там, где применим серверный расчёт оценок; основной путь анализа — оценки, присланные клиентом (см. [Концепции](#концепции))
- Встроенная [база данных дебютов ECO](https://en.wikipedia.org/wiki/Encyclopaedia_of_Chess_Openings) (категории A–E, скачивается при сборке Docker-образа) для определения дебюта

**HTTP и интеграции**
- [`aiohttp`](https://docs.aiohttp.org/) как асинхронный HTTP-клиент для обращений к Chess.com Published Data API, с ретраями/backoff при временных ошибках (408/425/429/5xx) и типизированными исключениями upstream-ошибок
- [PyJWT](https://pyjwt.readthedocs.io/) для подписи/проверки bearer-токенов админки (HS256)

**Инфраструктура**
- Многоэтапная сборка Docker (отдельный этап сборки `stockfish`, затем этап Python-приложения), запуск от непривилегированного пользователя
- CORS через `CORSMiddleware` из Starlette, разрешённые источники настраиваются через `CORS_ORIGINS`
- Кастомный `StatsMiddleware` записывает каждый запрос (путь, эндпоинт, статус, длительность, классификация источника, хэш IP, метаданные попадания в кэш/количества ходов) для админ-дашборда, с детекцией ботов/сканеров и фоновым циклом ретеншна, который чистит старые записи
- Структура построена вокруг `APIRouter` FastAPI по доменам (`profile`, `games`, `graphs`, `analyze`, `admin`), тонкого слоя `services/` и `parsers/`, реализующих интерфейс `BaseParser` для каждого шахматного сайта (сейчас — `chesscom`)

**Тестирование**
- Встроенный `unittest` Python (с `unittest.mock`) — без зависимости от pytest; асинхронные тесты запускаются через `asyncio.run(...)`

---

## Концепции

### Поддерживаемые сайты

Каждый эндпоинт, привязанный к игроку, принимает параметр пути `site`. Сейчас поддерживается только одно значение:

- `chesscom` — [Chess.com Published Data API](https://www.chess.com/news/view/published-data-api)

Запрос с неподдерживаемым сайтом возвращает `404`.

### Модель анализа: клиент считает, сервер классифицирует

Запуск Stockfish — дорогая операция, поэтому этот API **не** запускает шахматный движок сам на каждый запрос. Вместо этого:

1. Клиент (браузер) локально запускает Stockfish по ходам партии и получает одну оценку (`{type, value}`) плюс `best_move` для каждого полухода.
2. Клиент отправляет эти готовые результаты в `POST /analyze/pgn` или `POST /analyze/last/{site}/{username}/{index}`.
3. Сервер превращает сырые оценки в **классификации** ходов (best / excellent / good / inaccuracy / mistake / blunder / great / brilliant / miss / theory, плюс собственная система классификации chess-stat.ru — `chess_stat`), рассчитывает **accuracy** (точность), показатель **efficiency** (эффективность, см. ниже) и определяет **дебют** — и кэширует результат в Redis для эндпоинтов анализа партий.

### Системы классификации ходов

Для каждого хода возвращаются два параллельных классификатора в `move_classify`:

- `chesscom` — повторяет словарь классификации Chess.com (`best`, `excellent`, `good`, `inaccuracy`, `mistake`, `blunder`, `great`, `brilliant`, `miss`, `theoretical`), пороги скорректированы по рейтингу (elo), есть детекция жертв/блестящих ходов.
- `chess_stat` — **собственная система классификации chess-stat.ru** (`advance`, `steady`, `retreat`, `miss`, `theory`). Там, где девятиуровневая шкала Chess.com может усложнить оценку того, насколько ход действительно повлиял на партию, `chess_stat` сознательно сводит качество хода к трём понятным категориям: хорошие ходы (`advance`), ходы, слегка неоптимальные, но не вредящие позиции — существовал лучший ход (`steady`), и по-настоящему плохие ходы (`retreat`). `miss` стоит отдельно от этих категорий — это упущенная возможность: соперник только что допустил ошибку, давшую значительное преимущество, а игрок им не воспользовался. `theory` — для известных дебютных ходов. Цель — дать игроку возможность сразу увидеть, в какой именно момент партии его ход мысли дал сбой, вместо того чтобы теряться в тонких различиях между почти равноценными по качеству ходами.

### Определение дебюта

Начальная последовательность ходов партии сопоставляется с базой дебютов ECO (категории A–E), чтобы определить название дебюта и пометить ходы «theory», совпадающие с известными дебютными линиями.

### Efficiency: кто на самом деле сильнее играет *прямо сейчас*

Одной оценки движка недостаточно, чтобы понять, у кого сейчас инициатива — она показывает только, у кого позиция лучше. Два игрока могут прийти к одной и той же позиции совершенно разными путями: один — уверенно и быстро, другой — еле держится и тратит на это всё время на часах. `efficiency` (эффективность) призвана показать именно эту разницу.

Для каждого хода эффективность объединяет два фактора:

- **Качество хода** — насколько ход был близок к лучшему (`diff_expected`, та же дельта ожидаемого счёта, что используется для классификации), поэтому неточный ход сразу штрафуется независимо от того, насколько быстро он был сделан.
- **Затраченное время** — доля времени, потраченного на ход, относительно базового контроля времени (`move_time / time_before`, с ограничением сверху, чтобы экстремальные выбросы не доминировали), поэтому «правильный», но мучительно долго найденный ход всё равно оценивается как более слабый момент для этого игрока.

Эти факторы объединяются в единый показатель по каждому ходу в диапазоне `[0, 100]`, а затем сглаживаются в `eff_white` / `eff_black`: скользящее среднее по последним **3** ходам каждой стороны (`EFF_WINDOW_PER_SIDE`), чтобы число отражало *текущую форму игрока в позиции*, а не один случайный удачный или неудачный ход. `efficiency` требует наличия часов в PGN (аннотации `[%clk ...]`); без данных о часах он — и `eff_white`/`eff_black` — равны `null`.

**Как читать это в виде eval-бара:** если построить `eff_white` относительно `eff_black` по ходу партии, получится второй бар рядом с баром оценки движка. Бар движка говорит, кто сейчас впереди по позиции; бар эффективности — кто сейчас под давлением. Показательная картина: белые проигрывают по оценке движка, но чёрные — хоть и стоят лучше — тратят очень много времени на каждый ход (позиция сложная, чёрные ещё не чувствуют выигрыша), в то время как белые отвечают быстро и точно. Здесь эффективность белых выше эффективности чёрных, даже если оценка белых ниже — это сигнал, что чёрные с большей вероятностью ошибутся по ходу дальнейшей партии, и у белых есть реальный практический шанс вернуться в выигрышную (или как минимум равную) позицию.

---

## Эндпоинты

### Профиль

#### `GET /profile/{site}/{username}`

Возвращает публичный профиль игрока: аватар, титул, страну и статистику побед/поражений/ничьих в разбивке по контролю времени (rapid/blitz/bullet/daily).

**Параметры пути**

| Имя | Тип | Описание |
|---|---|---|
| `site` | string | Шахматный сайт, например `chesscom` |
| `username` | string | Имя пользователя (без учёта регистра) |

**Ответ** — [`PlayerProfile`](#playerprofile-1)

**Ошибки:** `404`, если игрок не найден или сайт не поддерживается.

---

### Партии

#### `GET /games/last/{site}/{username}/{amount}`

Возвращает последние партии игрока, от новых к старым.

**Параметры пути**

| Имя | Тип | Ограничения | Описание |
|---|---|---|---|
| `site` | string | — | Шахматный сайт |
| `username` | string | — | Имя пользователя |
| `amount` | int | `1 ≤ amount ≤ MAX_GAMES` (по умолчанию максимум 300) | Количество партий |

**Ответ** — массив [`Game`](#game-1)

**Ошибки:** `404`, если игрок не найден.

---

#### `GET /games/page/{site}/{username}`

Постраничная история партий — для интерфейсов с бесконечной прокруткой, без загрузки всей истории игрока.

**Параметры пути:** `site`, `username`.

**Параметры запроса**

| Имя | Тип | По умолчанию | Ограничения | Описание |
|---|---|---|---|---|
| `limit` | int | `10` | `1 ≤ limit ≤ MAX_PAGE_SIZE` (по умолчанию максимум 50) | Партий на странице |
| `offset` | int | `0` | `0 ≤ offset ≤ MAX_PAGE_OFFSET` | Сколько партий пропустить |

**Ответ** — [`GamesPage`](#gamespage-1)

`total_pages` известен (и заполнен) только после достижения последней страницы; до этого — `null`, а `has_more` — `true`, поскольку бэкенд никогда не сканирует весь архив игрока только ради подсчёта партий.

**Ошибки:** `404`, если игрок не найден.

---

### Графики перформанса

Перформанс-рейтинг рассчитывается стандартной итеративной формулой (бинарный поиск) против реальных соперников — а не простой дельтой Эло.

#### `GET /graphs/performance/{site}/{username}/{days}`

Точки рейтинга/перформанса за период, сгруппированные по дням (при `days < 30`) или примерно по `days / 30` группам в остальных случаях.

**Параметры пути**

| Имя | Тип | Ограничения | Описание |
|---|---|---|---|
| `site` | string | — | Шахматный сайт |
| `username` | string | — | Имя пользователя |
| `days` | int | `1 ≤ days ≤ 3650` | Количество дней, отсчитывая назад от текущего момента |

**Параметры запроса**

| Имя | Тип | Описание |
|---|---|---|
| `control` | string \| null | Опциональный фильтр по контролю времени (`rapid`, `blitz`, `bullet`, `daily`) |

**Ответ** — [`PerformanceGraph`](#performancegraph-1) или `[]`, если у игрока нет партий за период.

**Ошибки:** `404`, если игрок не найден.

---

#### `GET /graphs/performance/{site}/{username}/{days}/all`

То же самое, но сразу для всех четырёх контролей времени за один вызов, чтобы избежать четырёх отдельных запросов. Каждый контроль обрабатывается независимо — если получить график для одного из них не удалось, вместо провала всего запроса вернётся `[]` для этого контроля.

**Ответ**

```json
{
  "rapid": { "...": "PerformanceGraph или []" },
  "blitz": { "...": "PerformanceGraph или []" },
  "bullet": { "...": "PerformanceGraph или []" },
  "daily": { "...": "PerformanceGraph или []" }
}
```

---

### Анализ

#### `GET /analyze/last/{site}/{username}/{index}`

Получить ранее закэшированный анализ недавней партии, без отправки новых результатов Stockfish. Используйте этот эндпоинт первым, чтобы не запускать Stockfish на клиенте повторно, если партия уже была проанализирована кем-то другим.

**Параметры пути**

| Имя | Тип | Ограничения | Описание |
|---|---|---|---|
| `site` | string | — | Шахматный сайт |
| `username` | string | — | Имя пользователя |
| `index` | int | `1 ≤ index ≤ MAX_GAMES` | Позиция в списке недавних партий игрока (1-based, 1 = самая свежая) |

**Ответ**

```json
{
  "game": { "...": "объект Game" },
  "analyze": [ "...объекты классификации ходов, либо null, если анализ ещё не выполнен" ]
}
```

**Ошибки:** `404`, если игрок или партия с таким индексом не найдены.

---

#### `POST /analyze/last/{site}/{username}/{index}`

Отправить результаты Stockfish, вычисленные на клиенте, для конкретной недавней партии; сервер рассчитывает классификации, кэширует их (TTL 3 дня) и возвращает.

**Параметры пути:** те же, что и в варианте `GET` выше.

**Тело запроса** — [`AnalyzePayload`](#analyzepayload--analyzeandpgnpayload)

```json
{
  "results": [
    { "info": { "type": "cp", "value": 34 }, "best_move": "e2e4" },
    { "info": { "type": "mate", "value": -2 }, "best_move": "d1h5" }
  ]
}
```

- `results` должен содержать ровно по одному элементу на каждый полуход в PGN партии.
- `info.type` — `"cp"` (сантипешки) или `"mate"` (число ходов до мата); `info.value` ограничен диапазоном `[-10000, 10000]`.
- `best_move`, если указан, должен быть в нотации UCI (`^[a-h][1-8][a-h][1-8][qrbnQRBN]?$`).

**Ответ** — такая же форма, как у варианта `GET`, но `analyze` всегда заполнен.

**Ошибки**

| Статус | Значение |
|---|---|
| `404` | Игрок или партия с таким индексом не найдены |
| `422` | Длина `results` не совпадает с количеством ходов в PGN, либо PGN/рейтинги некорректны |
| `429` | Ёмкость для анализа временно исчерпана (см. [лимиты ёмкости](#лимиты-скорости-и-ёмкости)) |

---

#### `POST /analyze/pgn`

Классифицировать произвольный PGN (не привязанный к конкретному сайту/игроку), используя результаты Stockfish, присланные клиентом. Полезно для анализа партий, полученных не через этот API — например, импортированных или вставленных вручную PGN. Результаты здесь **не кэшируются**.

**Тело запроса** — [`AnalyzeAndPgnPayload`](#analyzepayload--analyzeandpgnpayload)

```json
{
  "pgn": "1. e4 e5 2. Nf3 Nc6 ...",
  "results": [
    { "info": { "type": "cp", "value": 20 }, "best_move": "g1f3" }
  ]
}
```

- `pgn`: от 1 до `MAX_PGN_LENGTH` символов (по умолчанию максимум 200 000).
- `results`: от 1 до `MAX_ANALYSIS_RESULTS` элементов (по умолчанию максимум 5000), та же форма, что и выше, по одному на полуход.
- Поддерживает Chess960 / кастомные начальные позиции через заголовки `[SetUp "1"]` + `[FEN "..."]`; определение дебюта пропускается для нестандартных начальных позиций.
- Если PGN содержит аннотации часов `[%clk ...]` (и в идеале заголовок `TimeControl`), в ответе также присутствует показатель `efficiency` для каждого хода (см. [Efficiency](#efficiency-кто-на-самом-деле-сильнее-играет-прямо-сейчас)).

**Ответ**

```json
{ "analyze": [ "...объекты классификации ходов" ] }
```

**Ошибки**

| Статус | Значение |
|---|---|
| `422` | Длина `results` не совпадает с количеством ходов в PGN, либо PGN/рейтинги некорректны |
| `429` | Ёмкость для анализа временно исчерпана |

---

#### Форма объекта классификации хода

Каждый элемент, возвращаемый эндпоинтами анализа:

```json
{
  "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
  "move": "e7e5",
  "best_move": "e7e5",
  "evaluation": 0.3,
  "expected_score": 0.54,
  "accuracy": 98.7,
  "diff_expected": 0.0,
  "efficiency": 96.4,
  "move_time": 4.0,
  "time_before": 180.0,
  "eff_white": 96.4,
  "eff_black": null,
  "move_classify": {
    "chesscom": "best",
    "chess_stat": "advance"
  }
}
```

- `evaluation` — либо число в пешках (сантипешечная оценка / 100), либо строка вида `"# 3"` / `"# -0"` для форсированного мата.
- `accuracy` — накопительное среднее (формула на основе win% в стиле Chess.com) до этого хода включительно, для стороны, только что сходившей.
- `efficiency` — показатель эффективности отдельного хода (`0`–`100`, см. [Efficiency](#efficiency-кто-на-самом-деле-сильнее-играет-прямо-сейчас)), объединяющий качество хода и затраченное на него время; `null`, если для этого хода в PGN нет данных о часах.
- `move_time` / `time_before` — сколько секунд потрачено на ход и сколько времени было на часах до него (из тегов `[%clk ...]` PGN, либо базовое время контроля для самого первого хода каждой стороны); оба `null` без данных о часах.
- `eff_white` / `eff_black` — скользящее среднее последних 3 значений эффективности для белых/чёрных соответственно — читаются вместе как «eval-бар эффективности», показывающий, кто сейчас играет сильнее, независимо от сырой оценки движка. В каждой записи обновляется только поле стороны, которая только что сходила; второе несёт предыдущее значение дальше.

---

### Админка

Внутренние эндпоинты для дашборда трафика сайта. Не часть публичного API — требуют bearer-токен и имеют смысл только при настроенных на сервере `DATABASE_URL` и `ADMIN_PASSWORD`.

#### `POST /admin/login`

Обменять пароль администратора на короткоживущий bearer-токен.

**Тело запроса**

```json
{ "password": "..." }
```

**Ответ** — [`AdminLoginResponse`](#adminloginresponse-1)

**Ошибки**

| Статус | Значение |
|---|---|
| `401` | Неверный пароль (общее сообщение, не раскрывает статус бана) |
| `429` | IP временно забанен после множества неудачных попыток |
| `503` | Админ-панель не настроена (`ADMIN_PASSWORD` не задан) |

Неудачные попытки входа ограничиваются по IP: 5 неудач банят IP на 30 минут (оба значения настраиваются). Сообщение об ошибке одинаково и при неверном пароле, и при активном бане — чтобы не раскрывать статус бана.

#### `GET /admin/stats`

Агрегированная статистика за период: количество запросов, уникальные посетители, доля ошибок, дневной временной ряд, разбивка по эндпоинтам, статистика попаданий в кэш анализа и топ сайтов/пользователей.

**Авторизация:** `Authorization: Bearer <token>`, полученный из `/admin/login`.

**Параметры запроса**

| Имя | Тип | Описание |
|---|---|---|
| `period` | string \| null | Один из `today`, `24h`, `7d`, `30d`, `90d`, `1y` |
| `days` | int \| null | Устарел; используйте `period` |
| `top_endpoints` | string \| null | Пути эндпоинтов через запятую, ограничивающие разбивку топ сайтов/пользователей |
| `visitor` | string \| null | Фильтр по `ip_hash` конкретного посетителя |

**Ответ** — [`AdminStatsResponse`](#adminstatsresponse-1)

#### `GET /admin/recent`

Постраничный сырой лог запросов, от новых к старым.

**Авторизация:** та же, что и выше.

**Параметры запроса**

| Имя | Тип | По умолчанию | Описание |
|---|---|---|---|
| `period` / `days` | — | — | Те же, что и в `/admin/stats` |
| `visitor` | string \| null | — | Фильтр по `ip_hash` посетителя |
| `limit` | int | `100` | `1–200` строк на странице |
| `offset` | int | `0` | Сколько строк пропустить |

**Ответ** — массив [`RecentRequest`](#recentrequest-1)

---

### Health-проверки

Простые проверки живости/готовности, исключены из статистики трафика.

| Эндпоинт | Описание |
|---|---|
| `GET /ping` | Всегда возвращает `{"message": "pong!"}` |
| `GET /health` | Всегда возвращает `{"status": "ok"}` |
| `GET /ready` | Возвращает `{"status": "ready"}`, либо `503`, если Redis недоступен |

---

## Модели данных

#### `Player`
```json
{ "username": "string", "rating": 0, "result": "win | loss | draw" }
```

#### `Game`
```json
{
  "site": "chesscom",
  "white": { "...": "Player" },
  "black": { "...": "Player" },
  "end_time": "2026-01-01T12:00:00Z",
  "pgn": "1. e4 e5 ...",
  "time_control": "rapid | blitz | bullet | daily | null",
  "url": "https://... | null"
}
```

#### `GamesPage`
```json
{
  "games": [ "...Game" ],
  "offset": 0,
  "limit": 10,
  "total_pages": 3,
  "has_more": false
}
```

#### `PlayerProfile`
```json
{
  "site": "chesscom",
  "username": "string",
  "avatar": "https://... | null",
  "country": "US | null",
  "title": "GM | null",
  "stats": { "...": "Stats" }
}
```

#### `Stats`
```json
{
  "total_games": 0,
  "wins": 0,
  "losses": 0,
  "draws": 0,
  "rapid": { "...": "TimeControl" },
  "blitz": { "...": "TimeControl" },
  "bullet": { "...": "TimeControl" },
  "daily": { "...": "TimeControl" }
}
```

#### `TimeControl`
```json
{ "rating": 0, "games": 0, "wins": 0, "losses": 0, "draws": 0 }
```

#### `PerformanceGraph`
```json
{
  "max": 0,
  "min": 0,
  "full": 0,
  "points": [
    { "timestamp": 0, "value": 0, "rating": 0, "games": 0 }
  ]
}
```

#### `AnalyzePayload` / `AnalyzeAndPgnPayload`
См. разделы [POST /analyze/last](#post-analyzelastsiteusernameindex-1) и [POST /analyze/pgn](#post-analyzepgn-1) выше.

#### `AdminLoginResponse`
```json
{ "token": "string", "expires_in": 86400 }
```

#### `AdminStatsResponse`
```json
{
  "summary": {
    "requests": 0, "unique_visitors": 0, "errors": 0,
    "avg_duration_ms": 0, "frontend": 0, "other": 0, "period": "30d"
  },
  "daily": [
    { "date": "2026-01-01", "requests": 0, "analyses": 0, "unique_visitors": 0, "errors": 0, "bots": 0 }
  ],
  "endpoints": [
    { "endpoint": "/profile/{site}/{username}", "requests": 0, "errors": 0, "avg_duration_ms": 0, "unique_visitors": 0, "frontend": 0, "other": 0 }
  ],
  "recent": [ "...RecentRequest" ],
  "analysis": {
    "total": 0, "cache_hit_rate": 0.0, "avg_moves": 0.0,
    "local": 0, "cache": 0, "local_moves": 0, "cache_moves": 0, "avg_local_moves": 0.0
  },
  "top": { "sites": [ { "name": "chesscom", "requests": 0, "unique_visitors": 0 } ], "usernames": [] },
  "period": "30d"
}
```

#### `RecentRequest`
```json
{
  "ts": "2026-01-01T12:00:00Z",
  "method": "GET",
  "path": "/profile/chesscom/magnus",
  "endpoint": "/profile/{site}/{username}",
  "status": 200,
  "duration_ms": 42,
  "source": "frontend | bot | other",
  "site": "chesscom | null",
  "username": "magnus | null",
  "cache_hit": true,
  "moves_count": 40,
  "visitor_name": "brave-otter-07",
  "ip_hash": "sha256 hex | null",
  "error_detail": "string | null"
}
```

`ip_hash` подсаливается текущей датой UTC, поэтому один и тот же посетитель получает разный хэш каждый день — он идентифицирует посетителя для группировки только *в пределах одного дня*, но никогда — между днями. `visitor_name` — детерминированный человекочитаемый никнейм, выводимый из этого хэша с той же целью.

---

## Формат ошибок

Все ошибки следуют стандартной форме FastAPI:

```json
{ "detail": "Человекочитаемое сообщение" }
```

или, для ошибок валидации запроса (`422`) — стандартному постатейному списку ошибок FastAPI.

**Общие коды статусов для всех эндпоинтов**

| Статус | Значение |
|---|---|
| `404` | Игрок, партия или маршрут не найдены — также используется для неподдерживаемых значений `site` |
| `422` | Валидация запроса не прошла (некорректная форма payload, несовпадение количества результатов анализа, некорректный PGN) |
| `429` | Ёмкость анализа исчерпана, либо (только для логина в админку) слишком много неудачных попыток |
| `502` | Запрос к внешнему шахматному сайту не удался |
| `503` | Внешний шахматный сайт недоступен, либо админ-панель не настроена |

---

## Лимиты скорости и ёмкости

Это серверные значения по умолчанию (все настраиваются через переменные окружения) — клиенту не нужно самостоятельно ограничивать себя, если только он не начал получать `429`:

| Лимит | По умолчанию | Назначение |
|---|---|---|
| `MAX_GAMES` | 300 | Максимум партий на запрос `/games/last` и максимальный индекс анализируемой партии |
| `MAX_PAGE_SIZE` | 50 | Максимальный `limit` для `/games/page` |
| `MAX_PGN_LENGTH` | 200 000 символов | Максимальный размер PGN, принимаемый `/analyze/pgn` |
| `MAX_ANALYSIS_RESULTS` | 5 000 | Максимум ходов, принимаемых в одном запросе анализа |
| `ANALYSIS_CONCURRENCY` | 10 | Количество параллельных серверных расчётов анализа до появления `429` |

Больше информации появится по мере развития проекта.