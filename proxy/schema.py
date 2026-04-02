# proxy/schema.py
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class EventIn(BaseModel):
    device_id: str = Field(default="rpi", max_length=64)
    kind: str = Field(..., max_length=16)
    event: str = Field(..., max_length=64)
    conf: float = 0.0
    payload: Optional[dict[str, Any]] = None


class EventOut(BaseModel):
    id: int
    device_id: str
    kind: str
    event: str
    conf: float
    payload: Optional[dict[str, Any]] = None
    created_at: Optional[str] = None
