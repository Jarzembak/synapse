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
SECRET_PREFIXES = ("github.credentials.",)


def is_secret_key(key: str) -> bool:
    """Return whether a setting must be encrypted and hidden by bulk reads."""
    return key in SECRET_KEYS or key.startswith(SECRET_PREFIXES)


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
    if is_secret_key(key) and raw.startswith("enc:"):
        try:
            raw = _fernet().decrypt(raw[4:].encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError(
                f"encrypted setting {key!r} cannot be decrypted; check SETTINGS_ENCRYPTION_KEY"
            ) from exc
    return json.loads(raw)


def _dumps(key: str, value) -> str:
    raw = json.dumps(value)
    if is_secret_key(key) and value is not None:
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


def set_settings_if_no_repository_jobs(values: dict[str, object]) -> None:
    """Atomically freeze repository-affecting settings during active runs."""
    from sqlmodel import select, text

    from .db import get_session
    from .models import Job, Project

    with get_session() as session:
        session.exec(text("BEGIN IMMEDIATE"))
        active = session.exec(
            select(Job.id)
            .join(Project, Project.id == Job.project_id)
            .where(
                Project.source_type == "github",
                Job.status.in_(("queued", "running")),
            )
        ).first()
        if active:
            session.rollback()
            raise RuntimeError(
                "wait for active repository processing to finish before changing "
                "repository model, prompt, or analysis settings"
            )
        for key, value in values.items():
            row = session.get(Setting, key) or Setting(key=key)
            row.value = _dumps(key, value)
            session.add(row)
        session.commit()


def set_cloud_settings_if_no_pending_purge(values: dict[str, object]) -> None:
    """Keep a privacy purge pinned to the remote where copies were uploaded."""
    from sqlmodel import select, text

    from .db import get_session
    from .models import RepositorySource

    with get_session() as session:
        session.exec(text("BEGIN IMMEDIATE"))
        pending = session.exec(select(RepositorySource.id).where(
            RepositorySource.cloud_purge_pending == True  # noqa: E712
        )).first()
        current_row = session.get(Setting, "cloud.provider")
        current_provider = (
            _loads(current_row.key, current_row.value)
            if current_row and current_row.value else None)
        if pending and current_provider:
            session.rollback()
            raise RuntimeError(
                "cloud target settings are locked until the pending repository "
                "privacy purge succeeds"
            )
        for key, value in values.items():
            row = session.get(Setting, key) or Setting(key=key)
            row.value = _dumps(key, value)
            session.add(row)
        session.commit()


def all_settings(*, include_secrets: bool = False) -> dict:
    """Return settings without accidentally disclosing credentials.

    ``include_secrets`` exists for trusted backend maintenance code only.  API
    handlers should use their dedicated masked views instead.
    """
    from .db import get_session

    with get_session() as session:
        rows = session.exec(select(Setting)).all()
        return {
            r.key: _loads(r.key, r.value)
            for r in rows
            if r.value and (include_secrets or not is_secret_key(r.key))
        }


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
