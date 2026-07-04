from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlmodel import select

from ..db import get_session
from ..models import Project, QuickRef, QuickRefSource
from .. import library

router = APIRouter(prefix="/api/quickrefs", tags=["quickrefs"])


def _serialize_ref(session, ref: QuickRef) -> dict:
    sources = session.exec(
        select(Project)
        .join(QuickRefSource, QuickRefSource.project_id == Project.id)
        .where(QuickRefSource.quickref_id == ref.id)
    ).all()
    return {
        **ref.model_dump(),
        "aliases": library.parse_aliases(ref.aliases),
        "sources": [{"id": p.id, "title": p.title} for p in sources],
    }


@router.get("")
def list_quickrefs(kind: str = ""):
    with get_session() as session:
        stmt = select(QuickRef).order_by(QuickRef.kind, QuickRef.title)
        if kind:
            stmt = stmt.where(QuickRef.kind == kind)
        return [_serialize_ref(session, r) for r in session.exec(stmt).all()]


def _versions(ref: QuickRef) -> list[str]:
    hist_dir = library.lib_path(f".history/{ref.path}").parent
    prefix = library.lib_path(ref.path).name + "."
    if not hist_dir.exists():
        return []
    return sorted(
        (p.name for p in hist_dir.iterdir() if p.name.startswith(prefix)),
        reverse=True,
    )


@router.get("/{ref_id}")
def get_quickref(ref_id: int):
    with get_session() as session:
        ref = session.get(QuickRef, ref_id)
        if not ref:
            raise HTTPException(404)
        meta, body = library.read_doc(ref.path)
        return {
            "ref": _serialize_ref(session, ref),
            "meta": meta,
            "body": body,
            "versions": _versions(ref),
        }


@router.get("/{ref_id}/versions/{name}")
def get_version(ref_id: int, name: str):
    with get_session() as session:
        ref = session.get(QuickRef, ref_id)
    if not ref or "/" in name or "\\" in name or ".." in name:
        raise HTTPException(404)
    path = library.lib_path(f".history/{ref.path}").parent / name
    if not path.exists():
        raise HTTPException(404)
    return {"name": name, "body": path.read_text(encoding="utf-8")}


@router.post("/{ref_id}/revert/{name}")
def revert(ref_id: int, name: str):
    with get_session() as session:
        ref = session.get(QuickRef, ref_id)
        if not ref or "/" in name or "\\" in name or ".." in name:
            raise HTTPException(404)
        src = library.lib_path(f".history/{ref.path}").parent / name
        if not src.exists():
            raise HTTPException(404)
        library.snapshot_history(ref.path)  # current becomes a version too
        library.lib_path(ref.path).write_bytes(src.read_bytes())

        # re-sync FTS from the reverted content
        from ..models import Artifact

        art = session.exec(select(Artifact).where(Artifact.path == ref.path)).first()
        if art:
            _, body = library.read_doc(ref.path)
            library.sync_fts(session, art, body)
            session.commit()
    return {"ok": True}
