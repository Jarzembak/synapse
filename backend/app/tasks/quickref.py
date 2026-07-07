"""Quick-reference docs: one per tool / technique / concept / technology (plus
any user-defined categories), auto-merged across videos.

Kinds: a TOOL doc is a user-friendly instruction manual, a TECHNIQUE doc is a
step-by-step recipe for a specific task, a CONCEPT doc is a crisp explainer, a
TECHNOLOGY doc is a platform/protocol primer — see the per-kind templates in
prompts.py. Custom categories (Settings → Quick-ref categories) carry their own
doc prompt and their description is appended to the extraction call below.

Matching policy (user-confirmed): the LLM sees the existing quick-ref index
(slugs + aliases) and must map each mention to an existing doc or justify a new
one; matched name variants are stored as aliases for future exact matching.
Before any merge the previous version is snapshotted to .history/.
"""
from __future__ import annotations

import json

from sqlmodel import select

from ..db import get_session
from .. import categories, library, llm
from ..models import QuickRef, QuickRefSource
from .celery_app import celery
from .common import artifact_body, auto_tag, get_project, pipeline_task, progress
from .prompts import get_prompt


def _index(session) -> list[dict]:
    return [
        {"kind": r.kind, "slug": r.slug, "title": r.title,
         "aliases": library.parse_aliases(r.aliases)}
        for r in session.exec(select(QuickRef)).all()
    ]


def extraction_system(index: list[dict], cats: list[dict]) -> str:
    """Entity-extraction system prompt: the (editable) base prompt, definitions
    of user-defined categories, the existing index, and the reply schema with
    the full kind enum — the last three are code-side so custom categories work
    without hand-editing the base prompt."""
    system = get_prompt("extract_entities")
    custom = [c for c in cats if not c["builtin"]]
    if custom:
        system += "\n\nUser-defined categories (classify into these too):\n" + "\n".join(
            f"- {c['label'].upper()} (kind \"{c['key']}\"): {c.get('description', '')}"
            for c in custom
        )
    kind_enum = "|".join(c["key"] for c in cats)
    return (
        system
        + "\n\nExisting quick-reference index — map mentions to an existing entry "
          "whenever it is the same thing under a variant name:\n"
        + json.dumps(index)
        + '\n\nReply as {"entities": [{"name": "...", "kind": "' + kind_enum
        + '", "existing_slug": "slug or null", "why_new": "only if new"}]}'
    )


def doc_prompt(kind: str, cats: dict[str, dict]) -> str:
    """Doc-writing prompt for a kind: built-ins from the prompt registry
    (override-aware), custom categories from their stored prompt."""
    cat = cats[kind]
    if cat["builtin"]:
        return get_prompt(f"quickref_{kind}")
    return cat.get("prompt") or get_prompt("quickref_concept")


@celery.task(name="quickref")
@pipeline_task
def quickref(job_id: int, project_id: int):
    with get_session() as session:
        deepdive = artifact_body(session, project_id, "deepdive_merged")
        project = get_project(session, project_id)
        index = _index(session)

    cats = categories.all_categories()
    progress(job_id, "identifying tools, techniques, concepts and technologies")
    extraction = llm.complete_json(
        "quickref",
        extraction_system(index, cats),
        deepdive[:100000],
    )

    cat_map = {c["key"]: c for c in cats}
    entities = extraction.get("entities", [])
    for i, ent in enumerate(entities):
        name = (ent.get("name") or "").strip()
        kind = ent.get("kind")
        if not name or kind not in cat_map:
            continue
        progress(job_id, f"quick-ref {i + 1}/{len(entities)}: {name}")
        _upsert_quickref(project_id, project.slug, project.title,
                         name, kind, ent.get("existing_slug"), deepdive, cat_map)


def _upsert_quickref(project_id: int, project_slug: str, project_title: str,
                     name: str, kind: str, existing_slug: str | None,
                     deepdive: str, cat_map: dict[str, dict]) -> None:
    with get_session() as session:
        ref = None
        if existing_slug:
            matches = session.exec(
                select(QuickRef).where(QuickRef.slug == existing_slug)
            ).all()
            # slugs are only unique per kind — prefer the same-kind match
            ref = next((m for m in matches if m.kind == kind),
                       matches[0] if matches else None)
        if ref is None:  # exact slug/alias fallback
            slug = library.make_slug(name)
            for candidate in session.exec(select(QuickRef).where(QuickRef.kind == kind)).all():
                if candidate.slug == slug or slug in [
                    library.make_slug(a) for a in library.parse_aliases(candidate.aliases)
                ]:
                    ref = candidate
                    break

    if ref:
        # the doc's established kind wins over this run's (re)classification —
        # writing a different quickref_<kind> type at the same path would fork
        # a second Artifact row and leave a stale FTS entry
        kind = ref.kind

    provider, model = llm.resolve_model("quickref")
    source_note = (
        f"\n\nSource material is from the video: {project_title!r}. "
        "Attribute examples with 'From: " + project_title + "'."
    )

    if ref:
        meta, existing_body = library.read_doc(ref.path)
        body = llm.complete(
            "quickref",
            get_prompt("quickref_merge") + source_note,
            f"EXISTING DOCUMENT:\n\n{existing_body}\n\n---\n\n"
            f"NEW SOURCE MATERIAL (about {name}):\n\n{deepdive[:80000]}",
        ).strip()
        rel_path = ref.path
        snapshot = library.snapshot_history(rel_path)
    else:
        body = llm.complete(
            "quickref",
            doc_prompt(kind, cat_map) + source_note,
            f"Subject: {name}\n\n{deepdive[:80000]}",
        ).strip()
        rel_path = f"{categories.kind_dir(kind)}/{library.make_slug(name)}.md"
        snapshot = None

    with get_session() as session:
        if ref:
            ref = session.get(QuickRef, ref.id)
            aliases = library.parse_aliases(ref.aliases)
            if name != ref.title and name not in aliases:
                aliases.append(name)
                ref.aliases = json.dumps(aliases)
        else:
            ref = QuickRef(kind=kind, slug=library.make_slug(name),
                           title=name, path=rel_path, aliases="[]")
        session.add(ref)
        session.commit()
        session.refresh(ref)

        contributors = session.exec(
            select(QuickRefSource).where(QuickRefSource.quickref_id == ref.id)
        ).all()
        if project_id not in [c.project_id for c in contributors]:
            session.add(QuickRefSource(quickref_id=ref.id, project_id=project_id))
            session.commit()

        body += f"\n\n---\nContributing videos: {library.wikilink(f'projects/{project_slug}/deepdive_merged')}"
        art = library.write_artifact(
            session,
            project_id=project_id,          # last contributor
            project_slug=project_slug,
            type=f"quickref_{kind}",
            title=name,
            body=body,
            rel_path=rel_path,
            provider=provider,
            model=model,
            extra_meta={
                "aliases": library.parse_aliases(ref.aliases),
                "history_snapshot": snapshot,
            },
        )
        auto_tag(project_id, art.id)
