from typing import List, Optional, Literal
from pydantic import BaseModel, Field

class EvalInfo(BaseModel):
    type: Literal['cp', 'mate']
    value: int = Field(..., ge=-10000, le=10000)

class MoveResult(BaseModel):
    info: EvalInfo
    best_move: Optional[str] = Field(
        None, 
        pattern=r"^[a-h][1-8][a-h][1-8][qrbnQRBN]?$"
    )

class AnalyzePayload(BaseModel):
    results: List[MoveResult]

class AnalyzeAndPgnPayload(BaseModel):
    pgn: str
    results: List[MoveResult]