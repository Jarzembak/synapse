from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import select, text

from ..db import get_session
from ..models import Artifact, Project, QuickRef, QuickRefSource
from .. import categories, library

router = APIRouter(prefix="/api/quickrefs", tags=["quickrefs"])


def _serialize_ref(session, ref: QuickRef) -> dict:
    sources = session.exec(
        select(Project)
        .join(QuickRefSource, QuickRefSource.project_id == Project.id)
        .where(QuickRefSource.quickref_id == ref.id)
    ).all()
    art = session.exec(select(Artifact).where(Artifact.path == ref.path)).first()
    return {
        **ref.model_dump(),
        "aliases": library.parse_aliases(ref.aliases),
        "sources": [{"id": p.id, "title": p.title} for p in sources],
        "tags": library.current_tags(session, art.id) if art else [],
        "updated": art.updated.isoformat() if art else None,
    }


@router.get("")
def list_quickrefs(kind: str = ""):
    with get_session() as session:
        stmt = select(QuickRef).order_by(QuickRef.kind, QuickRef.title)
        if kind:
            stmt = stmt.where(QuickRef.kind == kind)
        return [_serialize_ref(session, r) for r in session.exec(stmt).all()]


# --- categories (declared before /{ref_id} so the literal path wins) ---

RESERVED_DIRS = {"projects", ".history"}


@router.get("/categories")
def list_categories():
    with get_session() as session:
        counts = dict(session.exec(
            text("SELECT kind, COUNT(*) FROM quickref GROUP BY kind")
        ).all())
    return [{**c, "count": counts.get(c["key"], 0)}
            for c in categories.all_categories()]


class CategoryCreate(BaseModel):
    label: str
    plural: str = ""
    icon: str = ""
    description: str
    prompt: str


class CategoryUpdate(BaseModel):
    label: str | None = None
    plural: str | None = None
    icon: str | None = None
    description: str | None = None
    prompt: str | None = None


@router.post("/categories")
def create_category(req: CategoryCreate):
    label = req.label.strip()
    if not label:
        raise HTTPException(400, "label is required")
    if not req.description.strip():
        raise HTTPException(400, "description is required — entity extraction uses it "
                                 "to decide what belongs in this category")
    if not req.prompt.strip():
        raise HTTPException(400, "prompt is required — it writes this category's docs")
    key = library.make_slug(label)
    plural = req.plural.strip() or f"{label}s"
    cat_dir = library.make_slug(plural)
    existing = categories.all_categories()
    if any(c["key"] == key for c in existing):
        raise HTTPException(409, f"category {key!r} already exists")
    if cat_dir in RESERVED_DIRS or any(c["dir"] == cat_dir for c in existing):
        raise HTTPException(409, f"library folder {cat_dir!r} is already in use")
    cat = {"key": key, "label": label, "plural": plural,
           "icon": req.icon.strip() or "📄", "dir": cat_dir,
           "description": req.description.strip(), "prompt": req.prompt.strip()}
    categories.save_custom_categories(categories.custom_categories() + [cat])
    return {**cat, "builtin": False, "count": 0}


@router.put("/categories/{key}")
def update_category(key: str, req: CategoryUpdate):
    """Edit a custom category. Its key and library folder stay fixed so
    existing docs and QuickRef rows never orphan."""
    if key in categories.BUILTIN_KEYS:
        raise HTTPException(400, "built-in categories are fixed — edit their "
                                 "prompts in Settings → Advanced → Prompt editor")
    custom = categories.custom_categories()
    cat = next((c for c in custom if c["key"] == key), None)
    if not cat:
        raise HTTPException(404)
    for field in ("label", "plural", "icon", "description", "prompt"):
        value = getattr(req, field)
        if value is None:
            continue
        if not value.strip():
            raise HTTPException(400, f"{field} cannot be blank")
        cat[field] = value.strip()
    categories.save_custom_categories(custom)
    return cat


@router.delete("/categories/{key}")
def delete_category(key: str):
    if key in categories.BUILTIN_KEYS:
        raise HTTPException(400, "built-in categories cannot be deleted")
    custom = categories.custom_categories()
    if not any(c["key"] == key for c in custom):
        raise HTTPException(404)
    with get_session() as session:
        used = len(session.exec(select(QuickRef).where(QuickRef.kind == key)).all())
    if used:
        raise HTTPException(409, f"{used} quick-ref(s) still use this category — "
                                 "delete those docs first")
    categories.save_custom_categories([c for c in custom if c["key"] != key])
    return {"ok": True}


def _versions(ref: QuickRef) -> list[str]:
    hist_dir = library.lib_path(f".history/{ref.path}").parent
    prefix = library.lib_path(ref.path).name + "."
    if not hist_dir.exists():
        return []
    return sorted(
        (p.name for p in hist_dir.iterdir() if p.name.startswith(prefix)),
        reverse=True,
    )


def _version_path(ref: QuickRef, name: str):
    """Resolve only a snapshot that belongs to this exact quick-reference.

    Rejecting path separators is not sufficient: histories for every doc in a
    category share a directory, so another doc's valid basename could
    otherwise be read or reverted through this ref's endpoint.
    """
    if name not in _versions(ref):
        raise HTTPException(404)
    return library.lib_path(f".history/{ref.path}").parent / name


@router.get("/{ref_id}")
def get_quickref(ref_id: int):
    with get_session() as session:
        ref = session.get(QuickRef, ref_id)
        if not ref:
            raise HTTPException(404)
        try:
            meta, body = library.read_doc(ref.path)
        except FileNotFoundError:
            # disk is source of truth; the vault is user-editable (Obsidian),
            # so a deleted/renamed file leaves an orphaned row — mirror the
            # artifacts endpoint's 410 rather than a raw 500.
            raise HTTPException(410, "quick-ref file missing from library")
        return {
            "ref": _serialize_ref(session, ref),
            "meta": meta,
            "body": body,
            "versions": _versions(ref),
        }


@router.delete("/{ref_id}")
def delete_quickref(ref_id: int):
    """Permanently delete a quick-ref doc: DB rows, FTS entry, the file and its
    history snapshots. This is also how a used custom category becomes
    deletable."""
    with get_session() as session:
        ref = session.get(QuickRef, ref_id)
        if not ref:
            raise HTTPException(404)
        rel_path = ref.path
        versions = _versions(ref)
        for art in session.exec(select(Artifact).where(Artifact.path == rel_path)).all():
            library.delete_search_chunks(session, art.id)
            session.exec(text("DELETE FROM artifact_fts WHERE artifact_id = :id")
                         .bindparams(id=art.id))
            session.exec(text("DELETE FROM artifacttag WHERE artifact_id = :id")
                         .bindparams(id=art.id))
            session.delete(art)
        session.exec(text("DELETE FROM quickrefsource WHERE quickref_id = :id")
                     .bindparams(id=ref_id))
        session.delete(ref)
        session.commit()

    library.lib_path(rel_path).unlink(missing_ok=True)
    hist_dir = library.lib_path(f".history/{rel_path}").parent
    for name in versions:
        (hist_dir / name).unlink(missing_ok=True)
    return {"ok": True}


@router.get("/{ref_id}/versions/{name}")
def get_version(ref_id: int, name: str):
    with get_session() as session:
        ref = session.get(QuickRef, ref_id)
    if not ref:
        raise HTTPException(404)
    path = _version_path(ref, name)
    if not path.exists():
        raise HTTPException(404)
    return {"name": name, "body": path.read_text(encoding="utf-8")}


@router.post("/{ref_id}/revert/{name}")
def revert(ref_id: int, name: str):
    with get_session() as session:
        ref = session.get(QuickRef, ref_id)
        if not ref:
            raise HTTPException(404)
        src = _version_path(ref, name)
        if not src.exists():
            raise HTTPException(404)
        reverted = src.read_bytes()
        target = library.lib_path(ref.path)
        current = target.read_bytes() if target.exists() else None
        library.snapshot_history(ref.path)  # current becomes a version too
        try:
            library._atomic_write_bytes(target, reverted)

            # re-sync FTS from the reverted content
            from ..models import Artifact

            art = session.exec(select(Artifact).where(Artifact.path == ref.path)).first()
            if art:
                _, body = library.read_doc(ref.path)
                library.sync_fts(session, art, body)
                library.sync_search_chunks(session, art, body)
                session.commit()
                library._queue_semantic_index(art)
        except Exception:
            session.rollback()
            if current is None:
                target.unlink(missing_ok=True)
            else:
                library._atomic_write_bytes(target, current)
            raise
    return {"ok": True}
