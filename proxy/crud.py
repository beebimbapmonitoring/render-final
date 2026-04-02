# proxy/crud.py
from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import desc
from sqlalchemy.orm import Session

from .models import EventLog
from .schema import EventIn


def create_event(db: Session, payload: EventIn) -> EventLog:
    row = EventLog(
        device_id=payload.device_id.strip() or "rpi",
        kind=payload.kind.strip().lower(),
        event=payload.event.strip().lower(),
        conf=float(payload.conf or 0.0),
        payload_json=json.dumps(payload.payload) if payload.payload is not None else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_events(db: Session, limit: int = 50, kind: Optional[str] = None, event: Optional[str] = None) -> list[EventLog]:
    q = db.query(EventLog)

    if kind:
        q = q.filter(EventLog.kind == kind.strip().lower())
    if event:
        q = q.filter(EventLog.event == event.strip().lower())

    return q.order_by(desc(EventLog.created_at), desc(EventLog.id)).limit(limit).all()
