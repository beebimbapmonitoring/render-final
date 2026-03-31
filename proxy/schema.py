# app/schemas.py
from __future__ import annotations

from pydantic import BaseModel
from typing import Optional, Any, Dict


class SensorCreate(BaseModel):
    temp_c: Optional[float] = None
    hum_pct: Optional[float] = None
    weight_kg: Optional[float] = None
    source: str = "rpi"


class AiEventCreate(BaseModel):
    kind: str  # audio/video/system
    top_class: str
    top_conf: float = 0.0
    alert: bool = False
    log_only: bool = False
    payload: Optional[Dict[str, Any]] = None


class SystemEventCreate(BaseModel):
    level: str = "info"
    message: str
    meta: Optional[Dict[str, Any]] = None
