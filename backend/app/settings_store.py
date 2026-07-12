"""JSON-valued key/value settings backed by the Setting table.

Keys in use:
    model.<function>      {"provider": ..., "model": ...} per-function override
    glossary              list[str] domain terms injected into correction prompts
    tts.voices            {"host_a": ..., "host_b": ...}
"""
from __future__ import annotations

import json
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from sqlmodel import select

from .models import Setting

SECRET_KEYS = {"cloud.config"}


def _fernet() -> Fernet:
    from .config import settings

    if settings.settings_encryption_key:
        key = base64.urlsafe_b64encode(
            hashlib.sha256(settings.settings_encryption_key.encode("utf-8")).digest())
        return Fernet(key)
    path = settings.db_path.parent / ".settings.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        generated = Fernet.generate_key()
        try:
            with path.open("xb") as handle:
                handle.write(generated)
            path.chmod(0o600)
        except FileExistsError:
            pass
    return Fernet(path.read_bytes().strip())


def _loads(key: str, raw: str):
    if key in SECRET_KEYS and raw.startswith("enc:"):
        try:
            raw = _fernet().decrypt(raw[4:].encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError(
                f"encrypted setting {key!r} cannot be decrypted; check SETTINGS_ENCRYPTION_KEY"
            ) from exc
    return json.loads(raw)


def _dumps(key: str, value) -> str:
    raw = json.dumps(value)
    if key in SECRET_KEYS and value is not None:
        return "enc:" + _fernet().encrypt(raw.encode("utf-8")).decode("ascii")
    return raw


def get_setting(key: str, default=None):
    from .db import get_session

    with get_session() as session:
        row = session.get(Setting, key)
        if row is None or row.value == "":
            return default
        return _loads(key, row.value)


def set_setting(key: str, value) -> None:
    from .db import get_session

    with get_session() as session:
        row = session.get(Setting, key) or Setting(key=key)
        row.value = _dumps(key, value)
        session.add(row)
        session.commit()


def all_settings() -> dict:
    from .db import get_session

    with get_session() as session:
        rows = session.exec(select(Setting)).all()
        return {r.key: _loads(r.key, r.value) for r in rows if r.value}


def delete_settings_prefix(prefix: str) -> None:
    """Remove every setting whose key starts with prefix (e.g. cached project
    tag markers when the tag vocabulary changes)."""
    from sqlmodel import text

    from .db import get_session

    with get_session() as session:
        session.exec(
            text("DELETE FROM setting WHERE key LIKE :p").bindparams(p=prefix + "%")
        )
        session.commit()
