"""JSON-valued key/value settings backed by the Setting table.

Keys in use:
    model.<function>      {"provider": ..., "model": ...} per-function override
    glossary              list[str] domain terms injected into correction prompts
    tts.voices            {"host_a": ..., "host_b": ...}
"""
from __future__ import annotations

import json

from sqlmodel import select

from .models import Setting


def get_setting(key: str, default=None):
    from .db import get_session

    with get_session() as session:
        row = session.get(Setting, key)
        if row is None or row.value == "":
            return default
        return json.loads(row.value)


def set_setting(key: str, value) -> None:
    from .db import get_session

    with get_session() as session:
        row = session.get(Setting, key) or Setting(key=key)
        row.value = json.dumps(value)
        session.add(row)
        session.commit()


def all_settings() -> dict:
    from .db import get_session

    with get_session() as session:
        rows = session.exec(select(Setting)).all()
        return {r.key: json.loads(r.value) for r in rows if r.value}
