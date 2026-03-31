# app/models.py
from __future__ import annotations

from sqlalchemy import Column, Integer, Float, String, DateTime, Text
from sqlalchemy.sql import func

from app.db import Base


class SensorReading(Base):
    __tablename__ = "sensor_readings"

    id = Column(Integer, primary_key=True, index=True)
    temp_c = Column(Float, nullable=True)
    hum_pct = Column(Float, nullable=True)
    weight_kg = Column(Float, nullable=True)

    source = Column(String(64), nullable=False, default="rpi")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AiEvent(Base):
    __tablename__ = "ai_events"

    id = Column(Integer, primary_key=True, index=True)
    kind = Column(String(32), nullable=False)  # "audio" | "video" | "system"
    top_class = Column(String(64), nullable=False, default="unknown")
    top_conf = Column(Float, nullable=False, default=0.0)

    alert = Column(Integer, nullable=False, default=0)     # 0/1
    log_only = Column(Integer, nullable=False, default=0)  # 0/1

    payload_json = Column(Text, nullable=True)  # store full scores/detections/status snapshot
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SystemEvent(Base):
    __tablename__ = "system_events"

    id = Column(Integer, primary_key=True, index=True)
    level = Column(String(16), nullable=False, default="info")  # info/warn/error
    message = Column(Text, nullable=False)
    meta_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
