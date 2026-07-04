"""Tail the per-service log files without leaving the browser / needing docker CLI."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException

from ..logging_setup import log_file_for

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("")
def list_services():
    log_dir = os.environ.get("LOG_DIR", "")
    if not log_dir or not Path(log_dir).is_dir():
        return {"file_logging": False, "services": []}
    services = sorted(
        p.name.removeprefix("synapse-").removesuffix(".log")
        for p in Path(log_dir).glob("synapse-*.log")
    )
    return {"file_logging": True, "services": services}


@router.get("/{service}")
def tail(service: str, lines: int = 200):
    if not service.replace("-", "").isalnum():
        raise HTTPException(400, "invalid service name")
    path = log_file_for(service)
    if path is None:
        raise HTTPException(404, "file logging is not configured (LOG_DIR unset)")
    if not path.exists():
        raise HTTPException(404, f"no log file for service {service!r}")
    lines = max(1, min(lines, 2000))
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return {"service": service, "lines": content[-lines:]}
