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
- [Formulas](#formulas)
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
- [Redis](https://redis.io/) (via `redis-py`'s async client) — caches computed game analyses and shared snapshots (3-day TTL), caches gzip-compressed Chess.com archives (see [Caching](#caching-of-upstream-data)), and backs the admin login rate-limiter/ban list

**Chess logic**
- [`python-chess`](https://python-chess.readthedocs.io/) for PGN parsing, board/move handling, Chess960/custom-FEN support, and the static exchange evaluation used for brilliancy detection
- [Stockfish](https://stockfishchess.org/) engine (bundled into the Docker image at build time, pinned by version/SHA256, multi-arch amd64/arm64) driven through the [`stockfish`](https://pypi.org/project/stockfish/) Python wrapper — used only for server-computed evaluations where applicable; client-submitted evaluations are the primary analysis path (see [Concepts](#concepts))
- A bundled [ECO opening database](https://en.wikipedia.org/wiki/Encyclopaedia_of_Chess_Openings) (categories A–E, fetched at Docker build time) for opening detection

**HTTP & integration**
- [`aiohttp`](https://docs.aiohttp.org/) as the async HTTP client for calling the Chess.com Published Data API, with retry/backoff on transient errors (408/425/429/5xx and timeouts) and typed upstream-error exceptions
- [PyJWT](https://pyjwt.readthedocs.io/) for signing/verifying admin bearer tokens (HS256)

**Infrastructure**
- Docker multi-stage build (a dedicated `stockfish` build stage, then the Python app stage), runs as a non-root user
- CORS via Starlette's `CORSMiddleware`, origins configurable through `CORS_ORIGINS`
- A custom `StatsMiddleware` records every request (path, endpoint, status, duration, source classification, hashed IP, cache-hit/move-count metadata, truncated error body for failed requests) for the admin dashboard, with bot/scanner detection and a background retention loop that prunes old rows
- Structured around FastAPI `APIRouter`s per domain (`profile`, `games`, `graphs`, `analyze`, `admin`), a thin `services/` layer, `features/` (performance rating and graph computation), `utils/` (HTTP client, Redis client, archive cache, analyzer, openings) and `parsers/` implementing a `BaseParser` interface per chess site (currently `chesscom`)

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

1. The client (browser) runs Stockfish locally over a game's moves and produces one evaluation (`{type, value}`) plus a `best_move` per ply. For moves that look like possible brilliancies (a sacrifice), the client may also send `accept_info` — the evaluation after the opponent captures the piece the move leaves hanging.
2. The client sends those precomputed results to `POST /analyze/pgn` or `POST /analyze/last/{site}/{username}/{index}`.
3. The server turns raw evaluations into per-move **classifications** (best / excellent / good / inaccuracy / mistake / blunder / great / brilliant / miss / theory, plus chess-stat.ru's own `chess_stat` classifier), an **accuracy** score, an **efficiency** score (see below), and **opening** detection — and caches the result in Redis for game-analysis endpoints.

### Move classification systems

Two parallel classifiers are returned per move under `move_classify`:

- `chesscom` — mirrors Chess.com's classification vocabulary (`best`, `excellent`, `good`, `inaccuracy`, `mistake`, `blunder`, `great`, `brilliant`, `miss`, `theoretical`), elo-adjusted thresholds, sacrifice/brilliancy detection.
- `chess_stat` — **chess-stat.ru's own classification system** (`advance`, `steady`, `retreat`, `miss`, `theory`). Where Chess.com's nine-tier scale can make it harder to judge how much a move actually mattered, `chess_stat` deliberately collapses move quality into three clear buckets: good moves (`advance`), moves that were slightly suboptimal but not damaging — a better move existed (`steady`), and genuinely bad moves (`retreat`). `miss` is separate from these — it flags a missed opportunity: the opponent just made a mistake that handed a significant advantage (or a forced mate), and the player failed to capitalize on it. `theory` covers known opening moves. The goal is to make it easier for a player to see, at a glance, exactly where their thinking broke down in a game, rather than getting lost in fine-grained distinctions between near-equivalent move qualities.

Both classifiers share two overrides for moves that are not the engine's best move: a move that **misses** an opportunity is labelled `miss`, and a move that **allows a forced mate** (the opponent has a mate after it that did not exist before) is labelled `retreat` (`chess_stat`) / `blunder` (`chesscom`). The exact conditions are in [Classification thresholds](#6-classification-thresholds).

### Opening detection

The first move sequence of a game is matched against an ECO opening database (categories A–E) to detect the opening name and mark "theory" moves that match known opening lines. Games with a non-standard starting position (Chess960, custom FEN) skip opening detection.

### Efficiency: who is playing better *right now*

Engine evaluation alone doesn't tell you who currently has the initiative — it only tells you who stands better. Two players can reach the exact same position through very different paths: one confidently and quickly, the other barely holding on and burning their clock to survive. `efficiency` is built to surface that difference.

For each move, efficiency combines two things:

- **Move quality** — how close the move was to the best move (`diff_expected`, i.e. the same expected-score delta used for classification), so an inaccurate move is immediately penalized regardless of how fast it was played.
- **Time spent** — the move's time cost relative to the base time control (`move_time / time_before`, capped so extreme outliers don't dominate), so a "correct" move that took forever to find is still scored as a weaker moment for that player.

These are combined into a single per-move score in `[0, 100]`, then smoothed into `eff_white` / `eff_black`: a rolling average over each side's last **3** moves (`EFF_WINDOW_PER_SIDE`), so the number reflects *recent form in the position*, not a single lucky or unlucky move. `efficiency` requires a clock in the PGN (`[%clk ...]` annotations); without clock data it — and `eff_white`/`eff_black` — is `null`.

**Reading it as an eval bar:** plotting `eff_white` against `eff_black` over the course of a game gives a second bar alongside the engine evaluation bar. The engine bar says who's ahead; the efficiency bar says who's currently under pressure. A telling pattern: White is worse by engine evaluation, but Black — despite standing better — is burning large amounts of time each move (the position is hard, Black doesn't yet feel the win), while White responds quickly and accurately. Here White's efficiency is higher than Black's even though White's evaluation is lower — a signal that Black is more likely to err as the position continues, and White has a realistic practical path back into (or to) a winning position.

### Caching of upstream data

Chess.com monthly archives and archive lists are cached in Redis as gzip-compressed JSON, so repeated profile, games, graph and analysis requests do not hit the Chess.com API again:

- **Past months** are immutable on Chess.com, so they use a sliding TTL (default 3 days) that is refreshed on every read.
- **The current month** gets a short fixed TTL (default 120 s) with no refresh.
- **The archive list** has a fixed TTL (default 10 min).

---

## Formulas

### 1. Evaluation -> expected score $w$

Each ply has a Stockfish evaluation:
- either centipawns $c$ (`{"type": "cp"}`)
- or mate in $m$ moves (`{"type": "mate"}`).

Pawn score is $s = \frac{c}{100}$. The displayed value is $s$ rounded to 2 decimals, or `# m` for mate.

Win-probability mapping (White-relative):

$
w(s) = \frac{1}{1 + 10^{-s / 4}}, \qquad
w_{\text{mate}}(m) = \begin{cases} 1.0, & m > 0 \ \text{or}\ m = 0 \ \text{and Black is checkmated} \\ 0.0, & \text{otherwise} \end{cases}
$

In plain terms:
- $s = 0 \Rightarrow w = 0.5$ (equal)
- $s = +1 \Rightarrow w \approx 0.64$
- $s = +2 \Rightarrow w \approx 0.76$
- White can deliver mate in $m$ moves (or has just mated) — $1$
- White is getting mated in $m$ moves (or has just been mated) — $0$.

### 2. Move swing $\Delta w$ (`diff_expected`)

The cost of a move is the change in win probability $w(s)$ relative to the previous ply:

$$
\Delta w_i = |\, w_i - w_{i-1} \,|, \qquad \Delta w_0 = 0
$$

$\Delta w$ is the single input for both classification systems. The smaller $\Delta w$, the better the move. The underlying assumption is that a move cannot improve your position in the eyes of an engine calculating 20+ plies ahead: the engine's best move preserves the evaluation.

### 3. Accuracy

**Per-move accuracy.** Centipawn evaluations (White-relative) are mapped to win probability, then each move is scored by how much win percentage the mover gave away:

$$
w(c) = 50 + 50\left(\frac{2}{1 + e^{-0.004\, c}} - 1\right)
$$

The drop is measured from the mover's point of view (an improvement costs nothing):

$$
D_i = \begin{cases}
w_{before} - w_{after}, & \text{White to move} \\
w_{after} - w_{before}, & \text{Black to move}
\end{cases}
$$

$$
acc_i = \begin{cases}
100, & D_i \le 0\ \text{(improvement or equal)} \\
100, & \text{mate is delivered} \\
\max(0,\ 103.1668 \cdot e^{-0.10\, D_i} - 3.1669), & D_i > 0
\end{cases}
$$

In plain terms:
- $D = 2 \Rightarrow acc_i \approx 81.3$
- $D = 5 \Rightarrow acc_i \approx 59.4$
- $D = 10 \Rightarrow acc_i \approx 34.8$
- $D = 20 \Rightarrow acc_i \approx 10.8$

Mate evaluations enter this formula as very large centipawn values, so the win percentage saturates near 0/100 and "mate vs. mate" is not treated as a mistake. The first move of a game from a custom starting position has no previous evaluation and scores 100.

**Side accuracy.** The `accuracy` value attached to each move is the plain arithmetic mean of the per-move accuracies of the side that just moved, over all of that side's moves so far. Opening-theory moves are excluded from the mean, and a side with no counted moves yet has accuracy 100. There is no length-based penalty or weighting.

The core idea is based on this [article](https://lichess.org/forum/team-zNAEJBNN/RxgX6G2C) about the Lichess accuracy metric.

### 4. Efficiency

This is an original metric intended to evaluate a position from a more *human* perspective. Combining engine evaluation with time spent can show how comfortable a player really is in a position: the engine may consider White's position winning, but if White spends a very long time finding the right moves, is the position actually comfortable for the player? Efficiency is designed to answer that question and show who has been playing "better" over the last few moves, while also indicating the likely direction of the game.

PGN clock annotations are required; without them the metric cannot be calculated. Correspondence (`daily`) games are excluded entirely.

$$
t_{move} = \max\bigl(0,\ t_{before} - t_{after} + inc\bigr)
$$

$t_{before}$ is the mover's time before the move (the previous clock value for that color, or the base time from the `TimeControl` header for that side's first move; if the header has no usable base time, the first move of each side has no efficiency), $t_{after}$ is the time after the move, and $inc$ is the increment in seconds.

$$
f = \min\!\left(\frac{t_{move}}{t_{before}},\ 2.0\right), \qquad
q = 1 - \min(\max(\Delta w_i, 0), 1), \qquad
eff_i = 100 \cdot \frac{q}{1 + 2\, f}
$$

Examples:
- a perfect move played quickly ($\Delta w = 0$, $f \approx 0$) $\Rightarrow eff_i = 100$
- a perfect move that uses 10% of the available time ($f = 0.1$) $\Rightarrow eff_i = 100/1.2 \approx 83.3$
- a half-point blunder played quickly ($\Delta w = 0.5$, $f = 0.02$) $\Rightarrow eff_i = 50/1.04 \approx 48$

The final analysis attaches a rolling efficiency average over each side's last 3 moves.

$$
Eff_{White} = \mathrm{mean}(\text{last 3 } eff_i \text{ of White}), \qquad
Eff_{Black} = \mathrm{mean}(\text{last 3 } eff_i \text{ of Black})
$$

### 5. Performance rating

Standard FIDE-style definition: the rating $R_p$ at which the expected score against the opponents actually faced equals the score actually achieved. With opponent ratings $R_1 \dots R_n$ and total score $S = W \cdot 1 + D \cdot 0.5 + L \cdot 0$:

$$ 
E(R_p) = \sum_{j=1}^{n} \frac{1}{1 + 10^{(R_j - R_p)/400}}, \qquad
E(R_p) = S \ \Rightarrow\ R_p
$$

Solved by binary search on $[\,0.8 \cdot \min R_j,\ 1.2 \cdot \max R_j\,]$ to $0.001$ tolerance, then rounded to an integer. No games $\Rightarrow 0$. In plain terms: a win pulls the rating above the opponents' average, while a loss pulls it below.

### 6. Classification thresholds

**`chess_stat`** (own system). Base class by $\Delta w$:

| Class | Condition |
|---|---|
| `advance` | played `best_move`, or $\Delta w \le 0.04$ |
| `steady` | $\Delta w \le 0.12$ |
| `retreat` | larger $\Delta w$ |
| `miss` | the move fails to capitalize on the opponent's mistake (see [Miss](#miss-missed-opportunity)) |
| `theory` | in opening book line (wins over everything) |

**`chesscom`** (Chess.com vocabulary, Elo-adjusted; $bonus = \frac{4000 - Elo}{4000} \cdot 0.05$, so weaker players get slightly wider "good" windows):

| Class | Condition |
|---|---|
| `best` | played `best_move` or $\Delta w = 0$ |
| `excellent` | $\Delta w \le 0.02 + bonus$ |
| `good` | $\Delta w \le 0.05 + bonus$ |
| `inaccuracy` | $\Delta w \le 0.10 + bonus$ |
| `mistake` | $\Delta w \le 0.20 + bonus$ |
| `blunder` | larger $\Delta w$ |
| `great` | `best` + evaluation swing compared with the player's previous move $> (100 + Elo/40)$ centipawns in the player's favor |
| `brilliant` | a sacrifice that keeps the position (see [Brilliant](#brilliant-moves)); does not have to be the engine's top move |
| `miss` | non-best move that fails to capitalize on the opponent's mistake (see [Miss](#miss-missed-opportunity)) |
| `theoretical` | in opening book line |

#### Miss (missed opportunity)

A move that is not the engine's best is a `miss` in either of two cases:

- **Missed advantage.** Let $b$ be the mover's expected score before the opponent's last move, $o$ after it, and $a$ after the mover's own move (all from the mover's point of view). It is a miss when the opponent's move raised the mover's score by at least $0.10$ to at least $0.60$ ($o - b \ge 0.10$, $o \ge 0.60$), but the mover's reply brought it back to roughly where it started: $b - a < 0.10$ and $a \le b + 0.03$. Not available when the starting position is custom (no baseline).
- **Missed mate.** The mover had a forced mate on the previous ply, the reply no longer keeps a mate, yet the mover still stands at $\ge 0.60$ expected score.

#### Brilliant moves

A move is `brilliant` when it is not a book move and all of the following hold (centipawn values are from the mover's point of view; the expected-score scale here is the Elo-style $1/(1+10^{-cp/400})$):

- the position offers at least 3 legal moves (5 when in check);
- the move gives away at most $0.12$ expected score compared with the evaluation before it, and does not lose or delay an existing forced mate;
- the position after it is not worse than $-100$ cp for the mover;
- if the mover was already winning (more than $+500$ cp without a forced mate), only a "mate trap" sacrifice counts — `accept_info` shows that capturing the offered piece lets the mover mate — and the move must risk at least 200 cp of material;
- otherwise the move qualifies if it **creates a new hanging piece** worth at least 200 cp net of what it captured (static exchange evaluation) — except that a quiet move (no capture, no check) made while the evaluation is in the $+400…+500$ cp zone does not count as a sacrifice this way — or, in any case, if it **ignores a threat**: `accept_info` shows that accepting the offered material gains the opponent at least 600 cp (or a mate), the move risks at least 200 cp, and a non-sacrificial alternative existed.

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
| `offset` | int | `0` | `0 ≤ offset ≤ MAX_PAGE_OFFSET` (`MAX_GAMES − 10`) | Games to skip |

**Response** — [`GamesPage`](#gamespage)

`total_pages` is only known (and populated) once the last page has been reached; otherwise it's `null` and `has_more` is `true`, since the backend never scans a player's entire archive just to count games. Pagination stops at `MAX_GAMES`: a page that reaches that depth is reported as the last one.

**Errors:** `404` if the player doesn't exist.

---

### Performance graphs

Performance rating is computed with the standard iterative (binary-search) formula against opponents actually faced — not a simple Elo delta.

#### `GET /graphs/performance/{site}/{username}/{days}`

Rating/performance points over a period, bucketed daily (for `days < 30`) or in roughly `days / 30`-day groupings otherwise. Buckets are aligned to 03:00 UTC. A bucket without games repeats the previous point's `value` and `rating` with `games: 0`.

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
  "profiles": {
    "white": { "...": "PlayerCard or null" },
    "black": { "...": "PlayerCard or null" }
  },
  "analyze": [ "...move classification objects, or null if not yet analyzed" ],
  "share_id": "a1B2c3D4e or null"
}
```

`profiles` contains compact player cards embedded in the analysis response, so clients do not need separate profile requests; they are loaded best-effort and a side whose card could not be loaded is `null`. When cached analysis is available, `share_id` can be used to retrieve the same snapshot through [`GET /analyze/shared/{share_id}`](#get-analyzesharedshare_id) — if the cached analysis has no live snapshot yet, one is created on this call.

**Errors:** `404` if the player or that game index doesn't exist.

---

#### `GET /analyze/shared/{share_id}`

Retrieve a previously generated analysis snapshot by its share ID. Snapshots are stored in Redis for the same 3-day TTL as regular game-analysis cache entries. Snapshots created before player cards were embedded return `null` profiles.

**Path parameters**

| Name | Type | Constraints | Description |
|---|---|---|---|
| `share_id` | string | 8–12 characters (`A-Z`, `a-z`, `0-9`, `_`, `-`) | Share ID returned by an analysis endpoint |

**Response**

```json
{
  "game": { "...": "Game object" },
  "profiles": {
    "white": { "...": "PlayerCard or null" },
    "black": { "...": "PlayerCard or null" }
  },
  "analyze": [ "...move classification objects" ],
  "share_id": "a1B2c3D4e"
}
```

**Errors:** `404` if the share ID is not found or expired; `422` if the share ID format is invalid.

---

#### `POST /analyze/last/{site}/{username}/{index}`

Submit client-computed Stockfish results for a specific recent game; the server calculates classifications, caches them (3-day TTL), and returns them. Every call mints a new `share_id`; links from earlier calls keep working until they expire.

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
- `accept_info`, optional, has the same shape as `info`: the evaluation (White's point of view) after the opponent captures the piece this move leaves hanging. Send it only for candidates for a brilliant move; it is used for mate-trap and ignored-threat detection.

**Response** — same shape as the `GET` variant, with `analyze` always populated and a newly generated `share_id`.

**Errors**

| Status | Meaning |
|---|---|
| `404` | Player or game index not found |
| `422` | `results` length doesn't match the number of moves in the PGN, or PGN/ratings are malformed |
| `429` | Analysis capacity temporarily exhausted (see [capacity limits](#rate--capacity-limits)) |
| `500` | Unexpected failure while saving the analysis |

---

#### `POST /analyze/pgn`

Classify an arbitrary PGN (not tied to a specific site/player) using client-supplied Stockfish results. Useful for analyzing games not fetched through this API, e.g. imported or manually pasted PGNs. The result is not stored in the regular game-analysis cache, but a 3-day Redis snapshot is created so it can be opened through a share link.

**Request body** — [`AnalyzeAndPgnPayload`](#analyzeandpgnpayload)

```json
{
  "pgn": "1. e4 e5 2. Nf3 Nc6 ...",
  "results": [
    { "info": { "type": "cp", "value": 20 }, "best_move": "g1f3" }
  ]
}
```

- `pgn`: 1 to `MAX_PGN_LENGTH` characters (default cap 200,000). A PGN that arrives JSON-escaped (literal `\n` and `\"` sequences, optionally wrapped in quotes) is unescaped automatically.
- `results`: 1 to `MAX_ANALYSIS_RESULTS` entries (default cap 5,000), same shape as above (including optional `accept_info`), one per ply.
- Supports Chess960 / custom starting positions via `[SetUp "1"]` + `[FEN "..."]` headers; opening detection is skipped for non-standard starting positions. For Chess960, castling is expressed as king-takes-rook in UCI.
- Unknown or missing ratings (`[WhiteElo "?"]`, `"-"`, empty) are accepted: they count as 0 in the classification Elo adjustment, and as 1500 in the returned `Game`.
- If the PGN includes `[%clk ...]` clock annotations (and ideally a `TimeControl` header), the response also includes per-move `efficiency` (see [Efficiency](#efficiency-who-is-playing-better-right-now)). Correspondence time controls never produce it.
- The returned `Game` has `site: "pgn"` and `url: null`; `end_time` comes from `UTCDate`/`UTCTime` (or `Date`/`Time`), falling back to the current time.

**Response**

```json
{
  "game": { "...": "Game object built from the PGN" },
  "profiles": { "white": null, "black": null },
  "analyze": [ "...move classification objects" ],
  "share_id": "a1B2c3D4e"
}
```

**Errors**

| Status | Meaning |
|---|---|
| `422` | `results` length doesn't match PGN move count, or PGN/ratings are malformed |
| `429` | Analysis capacity temporarily exhausted |
| `500` | Unexpected failure while analyzing the PGN |

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
- `accuracy` is a running average (win%-drop formula, see [Accuracy](#3-accuracy)) up to and including that move, for the side that just moved; opening-theory moves are not counted.
- `efficiency` is this move's individual efficiency score (`0`–`100`, see [Efficiency](#efficiency-who-is-playing-better-right-now)), combining move quality and time spent; `null` when the PGN has no clock data for this move, and always `null` for correspondence games.
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
| `429` | IP temporarily banned after repeated failed attempts (same generic message as `401`) |
| `503` | Admin panel not configured (`ADMIN_PASSWORD` unset) |

Failed logins are rate-limited per IP: 5 failures bans that IP for 30 minutes (both configurable). The failure message is identical whether the password was wrong or the IP is banned, to avoid leaking ban state. The client IP is taken from the rightmost `X-Forwarded-For` entry (the one appended by the trusted nginx proxy). If Redis is down the limiter fails open, so an outage cannot lock the admin out. Login attempts are recorded in the request log, so brute-force attempts are visible there.

#### `GET /admin/stats`

Aggregated statistics for a period: request counts, unique visitors, error rate, time series, per-endpoint breakdown, analysis cache-hit stats, and top sites/usernames.

**Auth:** `Authorization: Bearer <token>` from `/admin/login`.

**Query parameters**

| Name | Type | Description |
|---|---|---|
| `period` | string \| null | One of `today`, `24h`, `7d`, `30d`, `90d`, `1y` |
| `days` | int \| null | Deprecated; use `period` instead |
| `top_endpoints` | string \| null | Comma-separated endpoint route paths to scope the top-sites/usernames breakdown |
| `visitor` | string \| null | Filter the `recent` list to a single visitor's `ip_hash` |

**Response** — [`AdminStatsResponse`](#adminstatsresponse)

Notes on the numbers:

- **Errors** count only `5xx` responses. `4xx` (unknown player, unmatched route, bad payload, rate limit) means the backend worked as intended; the request log still shows every status.
- **Time series granularity** depends on the period: `today` and `24h` return hourly points (`date` looks like `2026-01-01T12:00`, UTC); `7d` and `30d` return daily points; `90d` returns weekly points and `1y` monthly (30-day) points. `unique_visitors` in merged points is summed visitor-days, consistent with the daily semantics (visitor hashes rotate every day).
- **Excluded from statistics:** `/ping`, `/health`, `/ready`, and the dashboard's own polling endpoints `/admin/stats` and `/admin/recent`.
- **`source`** is `bot` for crawler/script user agents, requests without a user agent, and scanner probe paths (`/.env`, `/graphql`, `/aws.json`, …; paths under the API's own prefixes such as `/profile/` are never flagged, so a player named `env` is safe); `frontend` when the request carries the `X-Frontend` header together with an allowed `Origin`/`Referer` (or a same-origin `Sec-Fetch-Site`); `other` otherwise.
- **`site` / `username`** are parsed from the request path for `/profile`, `/games/page|last`, `/analyze/last` and `/graphs/performance` routes (lowercased).
- **`error_detail`** holds the response's `detail` text for `status >= 400`, truncated to 1000 characters.

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
  "site": "chesscom | pgn",
  "white": { "...": "Player" },
  "black": { "...": "Player" },
  "end_time": "2026-01-01T12:00:00Z",
  "pgn": "1. e4 e5 ...",
  "time_control": "rapid | blitz | bullet | daily | null",
  "url": "https://... | null"
}
```

For games built from a pasted PGN (`POST /analyze/pgn`), `site` is `"pgn"`, `url` is `null` and `time_control` is the raw `TimeControl` header (for example `"600"`).

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

#### `PlayerCard`
```json
{
  "site": "chesscom",
  "username": "string",
  "avatar": "https://... | null",
  "country": "US | null",
  "title": "GM | null"
}
```

#### Analysis response
```json
{
  "game": { "...": "Game" },
  "profiles": {
    "white": { "...": "PlayerCard or null" },
    "black": { "...": "PlayerCard or null" }
  },
  "analyze": [ "...move classification objects" ],
  "share_id": "a1B2c3D4e or null"
}
```

#### `AnalyzePayload` / `AnalyzeAndPgnPayload`
See the [POST /analyze/last](#post-analyzelastsiteusernameindex) and [POST /analyze/pgn](#post-analyzepgn) sections above. Each `results` entry is a `MoveResult`:
```json
{
  "info": { "type": "cp | mate", "value": 0 },
  "best_move": "e2e4 | null",
  "accept_info": { "type": "cp | mate", "value": 0 }
}
```
`best_move` and `accept_info` are optional.

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

`summary.other` counts every request that is not `frontend` (including bots); `daily[].bots` is the bot share of `requests`. `analysis.local` / `analysis.cache` split analysis requests into freshly computed and served from cache, with their move totals.

`recent` in `/admin/stats` is always empty (`limit=0`); use `/admin/recent` for the paged request log.

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

Failures of the upstream chess site are mapped centrally: an upstream `404` becomes `404` ("External resource not found"), an upstream `429` becomes `429` ("External service rate limit exceeded"), any other upstream HTTP error becomes `502`, and a timeout or connection failure becomes `503`.

**Common status codes across endpoints**

| Status | Meaning |
|---|---|
| `404` | Player, game, shared snapshot or route not found — also used for unsupported `site` values |
| `422` | Request validation failed (bad payload shape, mismatched analysis result count, malformed PGN, invalid share ID) |
| `429` | Analysis capacity exhausted, the upstream chess site is rate-limiting us, or (admin login only) too many failed attempts |
| `500` | Unexpected failure while saving or computing an analysis |
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

**Caching** (see [Caching of upstream data](#caching-of-upstream-data)):

| Variable | Default | Purpose |
|---|---|---|
| `ARCHIVE_CACHE_TTL_SECONDS` | 259,200 (3 days) | Sliding TTL of past-month archives |
| `ARCHIVE_CURRENT_MONTH_TTL_SECONDS` | 120 | Fixed TTL of the current month's archive |
| `ARCHIVE_CACHE_NEGATIVE_TTL_SECONDS` | 3,600 | How long an upstream `404` is remembered |
| `ARCHIVE_LIST_CACHE_TTL_SECONDS` | 600 | TTL of a player's archive list |

**Statistics & admin:**

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | — | Statistics database; when empty, request statistics are disabled |
| `STATS_IP_SALT` | — | Salt for visitor hashes; at least 32 characters are required when `DATABASE_URL` is set, otherwise the app refuses to start |
| `STATS_RETENTION_DAYS` | 400 | Events older than this are deleted (checked every 6 hours) |
| `ADMIN_PASSWORD` | — | Admin password; the admin panel is disabled when unset |
| `ADMIN_SECRET` | derived from the password | HS256 signing key for admin tokens |
| `ADMIN_TOKEN_TTL_SECONDS` | 86,400 | Admin token lifetime |
| `ADMIN_MAX_ATTEMPTS` | 5 | Failed logins before an IP is banned |
| `ADMIN_BAN_SECONDS` | 1,800 | Ban duration |
| `CORS_ORIGINS` | `chess-stat.ru`, `www.chess-stat.ru`, `localhost:3000` | Comma-separated allowed origins; also used to recognise requests from the frontend |
| `REDIS_HOST` | `localhost` | Redis host |

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
- [Формулы](#формулы)
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
- [Redis](https://redis.io/) (через асинхронный клиент `redis-py`) — кэширует вычисленные анализы партий и share-снимки (TTL 3 дня), кэширует сжатые gzip архивы Chess.com (см. [Кэширование](#кэширование-данных-внешнего-сайта)) и обеспечивает rate-limit/бан-лист для логина в админку

**Шахматная логика**
- [`python-chess`](https://python-chess.readthedocs.io/) для парсинга PGN, работы с доской/ходами, поддержки Chess960/кастомных FEN и статического размена (SEE), используемого при поиске блестящих ходов
- Движок [Stockfish](https://stockfishchess.org/) (встраивается в Docker-образ при сборке, версия и SHA256 зафиксированы, поддержка amd64/arm64), управляется через Python-обёртку [`stockfish`](https://pypi.org/project/stockfish/) — используется только там, где применим серверный расчёт оценок; основной путь анализа — оценки, присланные клиентом (см. [Концепции](#концепции))
- Встроенная [база данных дебютов ECO](https://en.wikipedia.org/wiki/Encyclopaedia_of_Chess_Openings) (категории A–E, скачивается при сборке Docker-образа) для определения дебюта

**HTTP и интеграции**
- [`aiohttp`](https://docs.aiohttp.org/) как асинхронный HTTP-клиент для обращений к Chess.com Published Data API, с ретраями/backoff при временных ошибках (408/425/429/5xx и таймауты) и типизированными исключениями upstream-ошибок
- [PyJWT](https://pyjwt.readthedocs.io/) для подписи/проверки bearer-токенов админки (HS256)

**Инфраструктура**
- Многоэтапная сборка Docker (отдельный этап сборки `stockfish`, затем этап Python-приложения), запуск от непривилегированного пользователя
- CORS через `CORSMiddleware` из Starlette, разрешённые источники настраиваются через `CORS_ORIGINS`
- Кастомный `StatsMiddleware` записывает каждый запрос (путь, эндпоинт, статус, длительность, классификация источника, хэш IP, метаданные попадания в кэш/количества ходов, усечённое тело ответа для неудачных запросов) для админ-дашборда, с детекцией ботов/сканеров и фоновым циклом ретеншна, который чистит старые записи
- Структура построена вокруг `APIRouter` FastAPI по доменам (`profile`, `games`, `graphs`, `analyze`, `admin`), тонкого слоя `services/`, `features/` (расчёт перформанс-рейтинга и графиков), `utils/` (HTTP-клиент, клиент Redis, кэш архивов, анализатор, дебюты) и `parsers/`, реализующих интерфейс `BaseParser` для каждого шахматного сайта (сейчас — `chesscom`)

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

1. Клиент (браузер) локально запускает Stockfish по ходам партии и получает одну оценку (`{type, value}`) плюс `best_move` для каждого полухода. Для ходов, похожих на возможные блестящие (жертва), клиент может дополнительно прислать `accept_info` — оценку после того, как соперник забирает фигуру, оставленную ходом под боем.
2. Клиент отправляет эти готовые результаты в `POST /analyze/pgn` или `POST /analyze/last/{site}/{username}/{index}`.
3. Сервер превращает сырые оценки в **классификации** ходов (best / excellent / good / inaccuracy / mistake / blunder / great / brilliant / miss / theory, плюс собственная система классификации chess-stat.ru — `chess_stat`), рассчитывает **accuracy** (точность), показатель **efficiency** (эффективность, см. ниже) и определяет **дебют** — и кэширует результат в Redis для эндпоинтов анализа партий.

### Системы классификации ходов

Для каждого хода возвращаются два параллельных классификатора в `move_classify`:

- `chesscom` — повторяет словарь классификации Chess.com (`best`, `excellent`, `good`, `inaccuracy`, `mistake`, `blunder`, `great`, `brilliant`, `miss`, `theoretical`), пороги скорректированы по рейтингу (elo), есть детекция жертв/блестящих ходов.
- `chess_stat` — **собственная система классификации chess-stat.ru** (`advance`, `steady`, `retreat`, `miss`, `theory`). Там, где девятиуровневая шкала Chess.com может усложнить оценку того, насколько ход действительно повлиял на партию, `chess_stat` сознательно сводит качество хода к трём понятным категориям: хорошие ходы (`advance`), ходы, слегка неоптимальные, но не вредящие позиции — существовал лучший ход (`steady`), и по-настоящему плохие ходы (`retreat`). `miss` стоит отдельно от этих категорий — это упущенная возможность: соперник только что допустил ошибку, давшую значительное преимущество (или форсированный мат), а игрок им не воспользовался. `theory` — для известных дебютных ходов. Цель — дать игроку возможность сразу увидеть, в какой именно момент партии его ход мысли дал сбой, вместо того чтобы теряться в тонких различиях между почти равноценными по качеству ходами.

У обоих классификаторов есть два общих переопределения для ходов, не совпадающих с лучшим ходом движка: ход, **упустивший** возможность, получает `miss`, а ход, **допускающий форсированный мат** (после него у соперника появляется мат, которого раньше не было), получает `retreat` (`chess_stat`) / `blunder` (`chesscom`). Точные условия — в разделе [Пороги классификации](#6-пороги-классификации).

### Определение дебюта

Начальная последовательность ходов партии сопоставляется с базой дебютов ECO (категории A–E), чтобы определить название дебюта и пометить ходы «theory», совпадающие с известными дебютными линиями. Для партий с нестандартной начальной позицией (Chess960, кастомный FEN) определение дебюта пропускается.

### Efficiency: кто на самом деле сильнее играет *прямо сейчас*

Одной оценки движка недостаточно, чтобы понять, у кого сейчас инициатива — она показывает только, у кого позиция лучше. Два игрока могут прийти к одной и той же позиции совершенно разными путями: один — уверенно и быстро, другой — еле держится и тратит на это всё время на часах. `efficiency` (эффективность) призвана показать именно эту разницу.

Для каждого хода эффективность объединяет два фактора:

- **Качество хода** — насколько ход был близок к лучшему (`diff_expected`, та же дельта ожидаемого счёта, что используется для классификации), поэтому неточный ход сразу штрафуется независимо от того, насколько быстро он был сделан.
- **Затраченное время** — доля времени, потраченного на ход, относительно базового контроля времени (`move_time / time_before`, с ограничением сверху, чтобы экстремальные выбросы не доминировали), поэтому «правильный», но мучительно долго найденный ход всё равно оценивается как более слабый момент для этого игрока.

Эти факторы объединяются в единый показатель по каждому ходу в диапазоне `[0, 100]`, а затем сглаживаются в `eff_white` / `eff_black`: скользящее среднее по последним **3** ходам каждой стороны (`EFF_WINDOW_PER_SIDE`), чтобы число отражало *текущую форму игрока в позиции*, а не один случайный удачный или неудачный ход. `efficiency` требует наличия часов в PGN (аннотации `[%clk ...]`); без данных о часах он — и `eff_white`/`eff_black` — равны `null`.

**Как читать это в виде eval-бара:** если построить `eff_white` относительно `eff_black` по ходу партии, получится второй бар рядом с баром оценки движка. Бар движка говорит, кто сейчас впереди по позиции; бар эффективности — кто сейчас под давлением. Показательная картина: белые проигрывают по оценке движка, но чёрные — хоть и стоят лучше — тратят очень много времени на каждый ход (позиция сложная, чёрные ещё не чувствуют выигрыша), в то время как белые отвечают быстро и точно. Здесь эффективность белых выше эффективности чёрных, даже если оценка белых ниже — это сигнал, что чёрные с большей вероятностью ошибутся по ходу дальнейшей партии, и у белых есть реальный практический шанс вернуться в выигрышную (или как минимум равную) позицию.

### Кэширование данных внешнего сайта

Месячные архивы Chess.com и списки архивов кэшируются в Redis в виде сжатого gzip JSON, чтобы повторные запросы профиля, партий, графиков и анализа не обращались к API Chess.com снова:

- **Прошедшие месяцы** на Chess.com неизменяемы, поэтому для них используется скользящий TTL (по умолчанию 3 дня), который продлевается при каждом чтении.
- **Текущий месяц** получает короткий фиксированный TTL (по умолчанию 120 с) без продления.
- **Список архивов** имеет фиксированный TTL (по умолчанию 10 минут).

---

## Формулы

### 1. Оценка -> ожидаемый счёт $w$

У каждого полухода есть оценка Stockfish: 
- либо сантипешки $c$ (`{"type": "cp"}`) 
- либо мат в $m$ ходов (`{"type": "mate"}`). 

Счёт в пешках: $s = \frac{c}{100}$. Отображается значение $s$, округлённое до 2 знаков, либо `# m` для мата.

Переход к вероятности выигрыша (относительно белых):

$$
w(s) = \frac{1}{1 + 10^{-s / 4}}, \qquad
w_{\text{mate}}(m) = \begin{cases} 1.0, & m > 0 \ \text{или}\ m = 0 \ \text{и чёрным поставлен мат} \\ 0.0, & \text{иначе} \end{cases}
$$

Проще говоря: 
- $s = 0 \Rightarrow w = 0{,}5$ (равно)
- $s = +1 \Rightarrow w \approx 0{,}64$
- $s = +2 \Rightarrow w \approx 0{,}76$. 
- Белые могут **поставить** мат в $m$ ходов (или мат только что поставлен) — $1$
- Белые могут **получить** мат в $m$ ходов (или им только что поставлен мат) — $0$.

### 2. Колебание оценки $\Delta w$ (`diff_expected`)

Цена хода — разница вероятности выигрыша $w(s)$ относительно прошлого полухода:

$$
\Delta w_i = |\, w_i - w_{i-1} \,|, \qquad \Delta w_0 = 0
$$

$\Delta w$ — единый показатель для обеих систем классификации. Чем меньше $\Delta w$ - тем лучше ход. гипотеза основывается на том, что своим ходом ты не можешь улучшить свое положение в глазах движка, который просчитывает позицию на 20+ ходов вперед. Если ты делаешь лучший ход по версии движка, то сохраняешь оценку.

### 3. Точность (accuracy)

**Точность хода.** Сантипешечные оценки (относительно белых) переводятся в вероятность победы, дальше каждый ход оценивается по тому, сколько win-процентов ходивший отдал:

$$
w(c) = 50 + 50\left(\frac{2}{1 + e^{-0{,}004\, c}} - 1\right)
$$

Просадка считается с точки зрения ходившего (улучшение=0):

$$
D_i = \begin{cases}
w_{before} - w_{after}, & \text{ход белых} \\
w_{after} - w_{before}, & \text{ход чёрных}
\end{cases}
$$

$$
acc_i = \begin{cases}
100, & D_i \le 0\ \text{(улучшение или равенство)} \\
100, & \text{ставится мат} \\
\max(0,\ 103{,}1668 \cdot e^{-0{,}10\, D_i} - 3{,}1669), & D_i > 0
\end{cases}
$$

Проще говоря: 
- $D = 2 \Rightarrow acc_i \approx 81{,}3$ 
- $D = 5 \Rightarrow acc_i \approx 59{,}4$ 
- $D = 10 \Rightarrow acc_i \approx 34{,}8$ 
- $D = 20 \Rightarrow acc_i \approx 10{,}8$

Оценки с матом попадают в формулу как очень большие значения в сантипешках, поэтому win-процент насыщается у 0/100, и «мат против мата» ошибкой не считается. Первый ход партии из кастомной начальной позиции не имеет предыдущей оценки и получает 100.

**Точность стороны.** Значение `accuracy`, прикрепляемое к каждому ходу, — обычное среднее арифметическое точностей ходов той стороны, которая только что сходила, по всем её ходам до этого момента. Дебютные («theory») ходы в среднее не входят, а у стороны, у которой ещё нет учтённых ходов, точность равна 100. Штрафов или весов за длину партии нет.

Фундамент идеи основан на этой [статье](https://lichess.org/forum/team-zNAEJBNN/RxgX6G2C) которая посвящена метрике точности на Lichess

### 4. Эффективность (efficiency)

Собственная иновационная идея метрики которая позволяет оценивать позицию более *человеческим* взглядом. Собирая оценку движка и потраченное время в одну формулу, мы можем увидеть насколько комфортно себя чувствует игрок в этой позиции: движок может оценивать позицию как выигрышную для белых, но если игрок белых тратит очень много времени на поиск верных ходов, то можно ли сказать что позиция комфортна для него и по *человечески* выигрышна? Думаю что скорее нет, чем да. Для решения этой проблемы и была создана метрика эффективности, которая показывает кто играет "лучше" последние несколько ходов, по совместительству предсказывая в каком направлении, вероятно, развернется партия в будущем.

Требуются часы в PGN, иначе расчитать метрику возможности не будет. Партии по переписке (`daily`) исключены полностью.

$$
t_{move} = \max\bigl(0,\ t_{before} - t_{after} + inc\bigr)
$$

$t_{before}$ — время ходившего до хода (предыдущие часы того же цвета, либо базовое время из заголовка `TimeControl` для первого хода каждой стороны; если в заголовке нет пригодного базового времени, у первого хода каждой стороны эффективности нет), $t_{after}$ — часы после, $inc$ — инкремент в секундах.

$$
f = \min\!\left(\frac{t_{move}}{t_{before}},\ 2{,}0\right), \qquad
q = 1 - \min(\max(\Delta w_i, 0), 1), \qquad
eff_i = 100 \cdot \frac{q}{1 + 2\, f}
$$

Примеры: 
- идеальный быстрый ход ($\Delta w = 0$, $f \approx 0$) $\Rightarrow eff_i=100$
- идеальный, но съевший 10% времени ($f = 0{,}1$) $\Rightarrow eff_i=100/1{,}2 \approx 83{,}3$
- зевок в пол-очка, сыгранный быстро ($\Delta w = 0{,}5$, $f = 0{,}02$) $\Rightarrow eff_i=50/1{,}04 \approx 48$

К ходам в итоговом анализе прикрепляется скользящее среднее значение эффективности по последним 3 ходам стороны.

### 5. Перформанс-рейтинг

Стандартное определение в стиле ФИДЕ: рейтинг $R_p$, при котором ожидаемый счёт против реально встреченных соперников равен фактически набранному. При рейтингах соперников $R_1 \dots R_n$ и сумме $S = W \cdot 1 + D \cdot 0{,}5 + L \cdot 0$:

$$
E(R_p) = \sum_{j=1}^{n} \frac{1}{1 + 10^{(R_j - R_p)/400}}, \qquad
E(R_p) = S \ \Rightarrow\ R_p
$$

Решается бинпоиском на $[\,0{,}8 \cdot \min R_j,\ 1{,}2 \cdot \max R_j\,]$ с точностью $0{,}001$, затем округляется до целого. 

Проще говоря: победа тянет рейтинг выше среднего рейтинга соперников, поражение — ниже.

### 6. Пороги классификации

**`chess_stat`** (своя система). Базовый класс по $\Delta w$:

| Класс | Условие |
|---|---|
| `advance` | сыгран лучший ход по версии движка или $\Delta w \le 0{,}04$ |
| `steady` | $\Delta w \le 0{,}12$ |
| `retreat` | больший $\Delta w$ |
| `miss` | ход не воспользовался ошибкой соперника (см. [Miss](#miss-упущенная-возможность)) |
| `theory` | ход из дебютной книги (приоритетнее всего) |

**`chesscom`** (словарь Chess.com, Elo-поправка; $bonus = \frac{4000 - Elo}{4000} \cdot 0{,}05$ — слабым игрокам "хорошие" окна чуть шире):

| Класс | Условие |
|---|---|
| `best` | сыгран `best_move` или $\Delta w = 0$ |
| `excellent` | $\Delta w \le 0{,}02 + bonus$ |
| `good` | $\Delta w \le 0{,}05 + bonus$ |
| `inaccuracy` | $\Delta w \le 0{,}10 + bonus$ |
| `mistake` | $\Delta w \le 0{,}20 + bonus$ |
| `blunder` | больший $\Delta w$ |
| `great` | `best` + скачок оценки по сравнению с твоим прошлым ходом $> (100 + Elo/40)$ сантипешек в твою пользу |
| `brilliant` | жертва, сохраняющая позицию (см. [Блестящие ходы](#блестящие-ходы)); не обязан быть первой линией движка |
| `miss` | не лучший ход, не воспользовавшийся ошибкой соперника (см. [Miss](#miss-упущенная-возможность)) |
| `theoretical` | ход из дебютной книги |

#### Miss (упущенная возможность)

Ход, не являющийся лучшим ходом движка, получает `miss` в одном из двух случаев:

- **Упущенное преимущество.** Пусть $b$ — ожидаемый счёт ходящего до последнего хода соперника, $o$ — после него, $a$ — после собственного хода ходящего (всё с точки зрения ходящего). Это miss, если ход соперника поднял счёт ходящего не менее чем на $0{,}10$ и до значения не ниже $0{,}60$ ($o - b \ge 0{,}10$, $o \ge 0{,}60$), но ответ ходящего вернул его примерно туда, откуда всё началось: $b - a < 0{,}10$ и $a \le b + 0{,}03$. Для кастомной начальной позиции не определяется (нет базовой оценки).
- **Упущенный мат.** На предыдущем полуходе у ходящего был форсированный мат, ответом мат не сохранён, при этом ходящий всё ещё стоит на $\ge 0{,}60$ ожидаемого счёта.

#### Блестящие ходы

Ход считается `brilliant`, если он не дебютный и выполняется всё перечисленное (значения в сантипешках — с точки зрения ходящего; шкала ожидаемого счёта здесь — эло-подобная $1/(1+10^{-cp/400})$):

- в позиции не менее 3 легальных ходов (5 — если стоит шах);
- ход отдаёт не более $0{,}12$ ожидаемого счёта по сравнению с оценкой до него и не теряет и не отдаляет уже имеющийся форсированный мат;
- позиция после хода для ходящего не хуже $-100$ сантипешек;
- если ходящий уже выигрывал (больше $+500$ сантипешек без форсированного мата), засчитывается только жертва-«ловушка с матом» — `accept_info` показывает, что взятие предложенной фигуры позволяет ходящему поставить мат, — и ход должен рисковать не менее чем 200 сантипешками материала;
- в остальных случаях ход подходит, если он **создаёт новую висящую фигуру** стоимостью не менее 200 сантипешек за вычетом взятого материала (статический размен) — за исключением тихого хода (без взятия и шаха) при оценке в зоне $+400…+500$ сантипешек, который так жертвой не считается, — либо в любом случае, если он **игнорирует угрозу**: `accept_info` показывает, что принятие предложенного материала даёт сопернику не менее 600 сантипешек (или мат), ход рискует не менее чем 200 сантипешками, и существовал нежертвенный альтернативный ход.

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
| `offset` | int | `0` | `0 ≤ offset ≤ MAX_PAGE_OFFSET` (`MAX_GAMES − 10`) | Сколько партий пропустить |

**Ответ** — [`GamesPage`](#gamespage-1)

`total_pages` известен (и заполнен) только после достижения последней страницы; до этого — `null`, а `has_more` — `true`, поскольку бэкенд никогда не сканирует весь архив игрока только ради подсчёта партий. Пагинация ограничена `MAX_GAMES`: страница, дошедшая до этой глубины, считается последней.

**Ошибки:** `404`, если игрок не найден.

---

### Графики перформанса

Перформанс-рейтинг рассчитывается стандартной итеративной формулой (бинарный поиск) против реальных соперников — а не простой дельтой Эло.

#### `GET /graphs/performance/{site}/{username}/{days}`

Точки рейтинга/перформанса за период, сгруппированные по дням (при `days < 30`) или примерно по `days / 30` дней в группе в остальных случаях. Границы групп выровнены по 03:00 UTC. Если в группе нет партий, повторяются `value` и `rating` предыдущей точки с `games: 0`.

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
  "profiles": {
    "white": { "...": "PlayerCard или null" },
    "black": { "...": "PlayerCard или null" }
  },
  "analyze": [ "...объекты классификации ходов, либо null, если анализ ещё не выполнен" ],
  "share_id": "a1B2c3D4e или null"
}
```

`profiles` содержит компактные карточки игроков прямо в ответе анализа, поэтому клиенту не нужны отдельные запросы профиля; они загружаются по принципу best-effort, и сторона, карточку которой загрузить не удалось, возвращается как `null`. Если анализ уже закэширован, `share_id` можно использовать для получения того же snapshot через [`GET /analyze/shared/{share_id}`](#get-analyzesharedshare_id) — если для закэшированного анализа ещё нет живого snapshot, он создаётся при этом вызове.

**Ошибки:** `404`, если игрок или партия с таким индексом не найдены.

---

#### `GET /analyze/shared/{share_id}`

Получить ранее созданный snapshot анализа по share ID. Snapshot хранится в Redis с тем же TTL в 3 дня, что и обычный кэш анализа партий. Snapshot, созданные до появления встроенных карточек игроков, возвращают `null` в `profiles`.

**Параметры пути**

| Имя | Тип | Ограничения | Описание |
|---|---|---|---|
| `share_id` | string | 8–12 символов (`A-Z`, `a-z`, `0-9`, `_`, `-`) | Share ID, возвращённый эндпоинтом анализа |

**Ответ**

```json
{
  "game": { "...": "объект Game" },
  "profiles": {
    "white": { "...": "PlayerCard или null" },
    "black": { "...": "PlayerCard или null" }
  },
  "analyze": [ "...объекты классификации ходов" ],
  "share_id": "a1B2c3D4e"
}
```

**Ошибки:** `404`, если share ID не найден или истёк; `422`, если формат share ID некорректен.

---

#### `POST /analyze/last/{site}/{username}/{index}`

Отправить результаты Stockfish, вычисленные на клиенте, для конкретной недавней партии; сервер рассчитывает классификации, кэширует их (TTL 3 дня) и возвращает. Каждый вызов создаёт новый `share_id`; ссылки от предыдущих вызовов продолжают работать до истечения срока.

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
- `accept_info`, опционален, той же формы, что и `info`: оценка (с точки зрения белых) после того, как соперник забирает фигуру, оставленную этим ходом под боем. Отправляйте только для кандидатов в блестящие ходы; используется для поиска ловушек с матом и проигнорированных угроз.

**Ответ** — такая же форма, как у варианта `GET`, но `analyze` всегда заполнен и возвращается новый `share_id`.

**Ошибки**

| Статус | Значение |
|---|---|
| `404` | Игрок или партия с таким индексом не найдены |
| `422` | Длина `results` не совпадает с количеством ходов в PGN, либо PGN/рейтинги некорректны |
| `429` | Ёмкость для анализа временно исчерпана (см. [лимиты ёмкости](#лимиты-скорости-и-ёмкости)) |
| `500` | Непредвиденный сбой при сохранении анализа |

---

#### `POST /analyze/pgn`

Классифицировать произвольный PGN (не привязанный к конкретному сайту/игроку), используя результаты Stockfish, присланные клиентом. Полезно для анализа партий, полученных не через этот API — например, импортированных или вставленных вручную PGN. Результат не попадает в обычный кэш анализа партий, но для него создаётся Redis-snapshot на 3 дня, доступный через share link.

**Тело запроса** — [`AnalyzeAndPgnPayload`](#analyzepayload--analyzeandpgnpayload)

```json
{
  "pgn": "1. e4 e5 2. Nf3 Nc6 ...",
  "results": [
    { "info": { "type": "cp", "value": 20 }, "best_move": "g1f3" }
  ]
}
```

- `pgn`: от 1 до `MAX_PGN_LENGTH` символов (по умолчанию максимум 200 000). PGN, пришедший в JSON-экранированном виде (буквальные последовательности `\n` и `\"`, возможно обёрнутый в кавычки), автоматически разэкранируется.
- `results`: от 1 до `MAX_ANALYSIS_RESULTS` элементов (по умолчанию максимум 5000), та же форма, что и выше (включая опциональный `accept_info`), по одному на полуход.
- Поддерживает Chess960 / кастомные начальные позиции через заголовки `[SetUp "1"]` + `[FEN "..."]`; определение дебюта пропускается для нестандартных начальных позиций. В Chess960 рокировка в UCI записывается как «король берёт ладью».
- Неизвестные или отсутствующие рейтинги (`[WhiteElo "?"]`, `"-"`, пустая строка) допускаются: в поправке Elo при классификации они считаются как 0, а в возвращаемом `Game` — как 1500.
- Если PGN содержит аннотации часов `[%clk ...]` (и в идеале заголовок `TimeControl`), в ответе также присутствует показатель `efficiency` для каждого хода (см. [Efficiency](#efficiency-кто-на-самом-деле-сильнее-играет-прямо-сейчас)). Для контролей по переписке он не считается никогда.
- Возвращаемый `Game` имеет `site: "pgn"` и `url: null`; `end_time` берётся из `UTCDate`/`UTCTime` (или `Date`/`Time`), иначе — текущее время.

**Ответ**

```json
{
  "game": { "...": "объект Game, построенный из PGN" },
  "profiles": { "white": null, "black": null },
  "analyze": [ "...объекты классификации ходов" ],
  "share_id": "a1B2c3D4e"
}
```

**Ошибки**

| Статус | Значение |
|---|---|
| `422` | Длина `results` не совпадает с количеством ходов в PGN, либо PGN/рейтинги некорректны |
| `429` | Ёмкость для анализа временно исчерпана |
| `500` | Непредвиденный сбой при анализе PGN |

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
- `accuracy` — накопительное среднее (формула просадки win-процентов, см. [Точность](#3-точность-accuracy)) до этого хода включительно, для стороны, только что сходившей; дебютные ходы не учитываются.
- `efficiency` — показатель эффективности отдельного хода (`0`–`100`, см. [Efficiency](#efficiency-кто-на-самом-деле-сильнее-играет-прямо-сейчас)), объединяющий качество хода и затраченное на него время; `null`, если для этого хода в PGN нет данных о часах, и всегда `null` для партий по переписке.
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
| `429` | IP временно забанен после множества неудачных попыток (то же общее сообщение, что и при `401`) |
| `503` | Админ-панель не настроена (`ADMIN_PASSWORD` не задан) |

Неудачные попытки входа ограничиваются по IP: 5 неудач банят IP на 30 минут (оба значения настраиваются). Сообщение об ошибке одинаково и при неверном пароле, и при активном бане — чтобы не раскрывать статус бана. IP клиента берётся из крайней правой записи `X-Forwarded-For` (её добавляет доверенный nginx). Если Redis недоступен, ограничитель пропускает запросы (fail open), чтобы сбой не заблокировал администратора. Попытки входа записываются в лог запросов, поэтому перебор пароля там виден.

#### `GET /admin/stats`

Агрегированная статистика за период: количество запросов, уникальные посетители, доля ошибок, временной ряд, разбивка по эндпоинтам, статистика попаданий в кэш анализа и топ сайтов/пользователей.

**Авторизация:** `Authorization: Bearer <token>`, полученный из `/admin/login`.

**Параметры запроса**

| Имя | Тип | Описание |
|---|---|---|
| `period` | string \| null | Один из `today`, `24h`, `7d`, `30d`, `90d`, `1y` |
| `days` | int \| null | Устарел; используйте `period` |
| `top_endpoints` | string \| null | Пути эндпоинтов через запятую, ограничивающие разбивку топ сайтов/пользователей |
| `visitor` | string \| null | Фильтр списка `recent` по `ip_hash` конкретного посетителя |

**Ответ** — [`AdminStatsResponse`](#adminstatsresponse-1)

Пояснения к цифрам:

- **Ошибками** считаются только ответы `5xx`. `4xx` (неизвестный игрок, несуществующий маршрут, неверный payload, rate limit) означает, что бэкенд отработал как задумано; в логе запросов по-прежнему виден каждый статус.
- **Гранулярность временного ряда** зависит от периода: `today` и `24h` возвращают почасовые точки (`date` вида `2026-01-01T12:00`, UTC); `7d` и `30d` — дневные; `90d` — недельные, `1y` — месячные (по 30 дней). `unique_visitors` в объединённых точках — сумма посетителей-дней, в соответствии с дневной семантикой (хэши посетителей меняются каждый день).
- **Исключены из статистики:** `/ping`, `/health`, `/ready`, а также собственные запросы дашборда `/admin/stats` и `/admin/recent`.
- **`source`** равен `bot` для user-agent краулеров/скриптов, запросов без user-agent и путей-проб сканеров (`/.env`, `/graphql`, `/aws.json`, …; пути под собственными префиксами API, например `/profile/`, никогда не помечаются, поэтому игрок с ником `env` не страдает); `frontend` — когда запрос содержит заголовок `X-Frontend` вместе с разрешённым `Origin`/`Referer` (либо same-origin `Sec-Fetch-Site`); иначе `other`.
- **`site` / `username`** разбираются из пути запроса для маршрутов `/profile`, `/games/page|last`, `/analyze/last` и `/graphs/performance` (в нижнем регистре).
- **`error_detail`** содержит текст `detail` ответа при `status >= 400`, усечённый до 1000 символов.

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
  "site": "chesscom | pgn",
  "white": { "...": "Player" },
  "black": { "...": "Player" },
  "end_time": "2026-01-01T12:00:00Z",
  "pgn": "1. e4 e5 ...",
  "time_control": "rapid | blitz | bullet | daily | null",
  "url": "https://... | null"
}
```

Для партий, построенных из вставленного PGN (`POST /analyze/pgn`), `site` равен `"pgn"`, `url` — `null`, а `time_control` — сырой заголовок `TimeControl` (например, `"600"`).

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

#### `PlayerCard`
```json
{
  "site": "chesscom",
  "username": "string",
  "avatar": "https://... | null",
  "country": "US | null",
  "title": "GM | null"
}
```

#### Ответ анализа
```json
{
  "game": { "...": "Game" },
  "profiles": {
    "white": { "...": "PlayerCard или null" },
    "black": { "...": "PlayerCard или null" }
  },
  "analyze": [ "...объекты классификации ходов" ],
  "share_id": "a1B2c3D4e или null"
}
```

#### `AnalyzePayload` / `AnalyzeAndPgnPayload`
См. разделы [POST /analyze/last](#post-analyzelastsiteusernameindex-1) и [POST /analyze/pgn](#post-analyzepgn-1) выше. Каждый элемент `results` — это `MoveResult`:
```json
{
  "info": { "type": "cp | mate", "value": 0 },
  "best_move": "e2e4 | null",
  "accept_info": { "type": "cp | mate", "value": 0 }
}
```
`best_move` и `accept_info` опциональны.

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

`summary.other` — все запросы, кроме `frontend` (включая ботов); `daily[].bots` — доля ботов внутри `requests`. `analysis.local` / `analysis.cache` делят запросы анализа на посчитанные заново и отданные из кэша, с суммами ходов.

Поле `recent` в `/admin/stats` всегда пустое (`limit=0`); постраничный лог запросов — в `/admin/recent`.

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

Сбои внешнего шахматного сайта обрабатываются централизованно: `404` от внешнего сайта превращается в `404` («External resource not found»), `429` — в `429` («External service rate limit exceeded»), любая другая HTTP-ошибка внешнего сайта — в `502`, а таймаут или обрыв соединения — в `503`.

**Общие коды статусов для всех эндпоинтов**

| Статус | Значение |
|---|---|
| `404` | Игрок, партия, share-snapshot или маршрут не найдены — также используется для неподдерживаемых значений `site` |
| `422` | Валидация запроса не прошла (некорректная форма payload, несовпадение количества результатов анализа, некорректный PGN, неверный share ID) |
| `429` | Ёмкость анализа исчерпана, внешний шахматный сайт ограничивает нас по частоте, либо (только для логина в админку) слишком много неудачных попыток |
| `500` | Непредвиденный сбой при сохранении или расчёте анализа |
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

**Кэширование** (см. [Кэширование данных внешнего сайта](#кэширование-данных-внешнего-сайта)):

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `ARCHIVE_CACHE_TTL_SECONDS` | 259 200 (3 дня) | Скользящий TTL архивов прошедших месяцев |
| `ARCHIVE_CURRENT_MONTH_TTL_SECONDS` | 120 | Фиксированный TTL архива текущего месяца |
| `ARCHIVE_CACHE_NEGATIVE_TTL_SECONDS` | 3 600 | Сколько помнится `404` от внешнего сайта |
| `ARCHIVE_LIST_CACHE_TTL_SECONDS` | 600 | TTL списка архивов игрока |

**Статистика и админка:**

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `DATABASE_URL` | — | База статистики; если пусто, сбор статистики запросов отключён |
| `STATS_IP_SALT` | — | Соль для хэшей посетителей; при заданном `DATABASE_URL` нужно минимум 32 символа, иначе приложение не запустится |
| `STATS_RETENTION_DAYS` | 400 | События старше этого срока удаляются (проверка каждые 6 часов) |
| `ADMIN_PASSWORD` | — | Пароль администратора; без него админ-панель отключена |
| `ADMIN_SECRET` | выводится из пароля | Ключ подписи (HS256) токенов админки |
| `ADMIN_TOKEN_TTL_SECONDS` | 86 400 | Время жизни токена админки |
| `ADMIN_MAX_ATTEMPTS` | 5 | Число неудачных входов до бана IP |
| `ADMIN_BAN_SECONDS` | 1 800 | Длительность бана |
| `CORS_ORIGINS` | `chess-stat.ru`, `www.chess-stat.ru`, `localhost:3000` | Разрешённые источники через запятую; также используется для распознавания запросов фронтенда |
| `REDIS_HOST` | `localhost` | Хост Redis |

Больше информации появится по мере развития проекта.