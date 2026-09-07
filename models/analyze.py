from typing import List, Optional, Literal
from pydantic import BaseModel, Field
from config import MAX_ANALYSIS_RESULTS, MAX_PGN_LENGTH

class EvalInfo(BaseModel):
    type: Literal['cp', 'mate'] = Field(description="Centipawn or mate evaluation")
    value: int = Field(..., ge=-10000, le=10000, description="Evaluation value")

class MoveResult(BaseModel):
    info: EvalInfo = Field(description="Stockfish evaluation already calculated for the position")
    best_move: Optional[str] = Field(
        None,
        pattern=r"^[a-h][1-8][a-h][1-8][qrbnQRBN]?$",
        description="Best move already selected by Stockfish, in UCI notation",
    )

class AnalyzePayload(BaseModel):
    results: List[MoveResult] = Field(
        ...,
        min_length=1,
        max_length=MAX_ANALYSIS_RESULTS,
        description="One precomputed Stockfish result for each move in the game PGN",
    )

class AnalyzeAndPgnPayload(BaseModel):
    pgn: str = Field(..., min_length=1, max_length=MAX_PGN_LENGTH, description="Game in PGN format")
    results: List[MoveResult] = Field(
        ...,
        min_length=1,
        max_length=MAX_ANALYSIS_RESULTS,
        description="One precomputed Stockfish result for each move in the PGN",
    )