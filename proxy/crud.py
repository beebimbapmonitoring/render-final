# app/crud.py
from __future__ import annotations

import json
from typing import Optional, Dict, Any

from sqlalchemy.orm import Session

from app import models
from app.schemas import SensorCreate, AiEventCreate, SystemEventCreate


def insert_sensor_reading(db: Session, payload: SensorCreate) -> models.SensorReading:
    row = models.SensorReading(
        temp_c=payload.temp_c,
        hum_pct=payload.hum_pct,
        weight_kg=payload.weight_kg,
        source=payload.source,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def insert_ai_event(db: Session, payload: AiEventCreate) -> models.AiEvent:
    row = models.AiEvent(
        kind=payload.kind,
        top_class=payload.top_class,
        top_conf=float(payload.top_conf or 0.0),
        alert=1 if payload.alert else 0,
        log_only=1 if payload.log_only else 0,
        payload_json=json.dumps(payload.payload) if payload.payload is not None else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def insert_system_event(db: Session, payload: SystemEventCreate) -> models.SystemEvent:
    row = models.SystemEvent(
        level=payload.level,
        message=payload.message,
        meta_json=json.dumps(payload.meta) if payload.meta is not None else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
