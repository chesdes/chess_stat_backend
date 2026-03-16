from pydantic import BaseModel
from typing import Any, List, Dict

class AnalyzePayload(BaseModel):
    analyze: List[Dict[str, Any]]