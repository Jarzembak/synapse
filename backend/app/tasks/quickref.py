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
import logging

from sqlmodel import select

from ..db import get_session
from .. import categories, library, llm
from ..models import Artifact, QuickRef, QuickRefSource
from .celery_app import celery
from .common import artifact_body, auto_tag, get_project, pipeline_task, progress
from .prompts import get_prompt

log = logging.getLogger("synapse.quickref")
MAX_QUICKREF_ENTITIES = 50


def _index(session) -> list[dict]:
    public: list[dict] = []
    for ref in session.exec(select(QuickRef)).all():
        artifact = session.exec(
            select(Artifact).where(Artifact.path == ref.path)
        ).first()
        # Missing or sticky-private canonical docs are local knowledge, not
        # vocabulary for a later public cloud-model extraction prompt.
        if (not artifact or library.artifact_is_restricted(session, artifact)
                or library.artifact_is_repository_derived(session, artifact)):
            continue
        public.append({
            "kind": ref.kind,
            "slug": ref.slug,
            "title": ref.title,
            "aliases": library.parse_aliases(ref.aliases),
        })
    return public


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
    # Repository guides can be large.  Extract from every derived-document
    # chunk and merge entities deterministically instead of keeping only an
    # arbitrary prefix.  No raw repository text enters this step.
    if project.source_type == "github":
        extracted: list[dict] = []
        chunks = llm.chunk_text(deepdive, max_chars=60000, overlap=0)
        for chunk_index, chunk in enumerate(chunks, 1):
            progress(job_id, f"identifying topics {chunk_index}/{len(chunks)}")
            result = llm.complete_json(
                "quickref", extraction_system(index, cats), chunk,
                max_tokens=2_500)
            batch = result.get("entities", []) if isinstance(result, dict) else []
            for entity in batch:
                if not isinstance(entity, dict):
                    continue
                enriched = dict(entity)
                # This is trusted task metadata, overwritten rather than read
                # from model JSON. A topic found after the first guide chunk
                # must be written from the context that actually mentioned it.
                enriched["_source_context"] = chunk
                extracted.append(enriched)
        seen: set[tuple[str, str]] = set()
        entities = []
        for entity in extracted:
            key = (str(entity.get("kind", "")),
                   str(entity.get("existing_slug") or
                       library.make_slug(str(entity.get("name", "")))))
            if key not in seen:
                seen.add(key)
                entities.append(entity)
    else:
        extraction = llm.complete_json(
            "quickref", extraction_system(index, cats), deepdive[:60_000],
            max_tokens=2_500)
        batch = extraction.get("entities", []) if isinstance(extraction, dict) else []
        entities = [entity for entity in batch if isinstance(entity, dict)]

    entity_limit = 24 if project.source_type == "github" else MAX_QUICKREF_ENTITIES
    if len(entities) > entity_limit:
        log.warning(
            "project %s quick-reference extraction returned %d entities; capped at %d",
            project_id, len(entities), entity_limit)
        progress(
            job_id,
            f"limiting quick references to {entity_limit} of {len(entities)} topics",
        )
        entities = entities[:entity_limit]

    cat_map = {c["key"]: c for c in cats}
    for i, ent in enumerate(entities):
        name = (ent.get("name") or "").strip()
        kind = ent.get("kind")
        if not name or kind not in cat_map:
            continue
        progress(job_id, f"quick-ref {i + 1}/{len(entities)}: {name}")
        source_context = (
            str(ent.get("_source_context") or "")
            if project.source_type == "github" else deepdive)
        _upsert_quickref(project_id, project.slug, project.title,
                         name, kind, ent.get("existing_slug"),
                         source_context, cat_map)


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
    source_kind = "video"
    source_label = project_title
    source_sha = ""
    local_only = False
    reset_restricted_ref = False
    ref_repository_derived = False
    with get_session() as session:
        project = get_project(session, project_id)
        if project.source_type == "github":
            from ..repository import (current_repository_snapshot,
                                      repository_source_for_project)

            source = repository_source_for_project(session, project_id)
            snapshot = current_repository_snapshot(session, project_id)
            sha = str(getattr(snapshot, "resolved_sha", ""))
            source_sha = sha
            owner = str(getattr(source, "owner", ""))
            repo = str(getattr(source, "repository", getattr(source, "name", "")))
            source_kind = "repository"
            source_label = f"{owner}/{repo} @ {sha[:12]}"
            local_only = bool(
                getattr(source, "local_only", False)
                or getattr(source, "is_private", getattr(source, "private", False)))
            local_only = bool(local_only or llm._repository_local_only())
        if ref is not None and not local_only:
            # A repository can be imported while public and later become
            # private.  Visibility refresh then makes the old shared artifact
            # sticky-private.  Never merge that body into a public model call;
            # start a clean public canonical document at a new path instead.
            existing_artifact = session.exec(
                select(Artifact).where(Artifact.path == ref.path)
            ).first()
            reset_restricted_ref = bool(
                existing_artifact
                and library.artifact_is_restricted(session, existing_artifact))
            ref_repository_derived = bool(
                existing_artifact
                and library.artifact_is_repository_derived(
                    session, existing_artifact))
        local_only = bool(local_only or ref_repository_derived)

    if source_kind == "repository":
        source_note = (
            f"\n\nSource material is from repository {source_label!r}. "
            "Use source-neutral language. Attribute examples as 'From: "
            f"{source_label} — path:lines' and preserve only immutable commit links "
            "already present in the material. Do not mention videos or timestamps."
        )
    else:
        source_note = (
            f"\n\nSource material is from the video: {project_title!r}. "
            "Attribute examples with 'From: " + project_title
            + " [HH:MM:SS]' using only timestamps already present in the material."
        )

    if local_only:
        # Private-derived quick references never enter the cross-project index:
        # a future public merge could otherwise send their existing body to a
        # cloud model.  They remain fully searchable project artifacts.
        body = llm.complete(
            "quickref", doc_prompt(kind, cat_map) + source_note,
            f"Subject: {name}\n\n{deepdive[:60_000]}", max_tokens=2_500,
        ).strip()
        rel_path = (
            f"projects/{project_slug}/quickrefs/{kind}/"
            f"{library.make_slug(name)}.md")
        history_snapshot = None
        previous_commit = ""
        if library.lib_path(rel_path).exists():
            meta, _old_body = library.read_doc(rel_path)
            previous_commit = str(meta.get("commit_sha") or "")
            if previous_commit and previous_commit != source_sha:
                history_snapshot = library.snapshot_history(rel_path)
        body += (f"\n\n---\nContributing source: "
                 f"{library.wikilink(f'projects/{project_slug}/deepdive_merged')}")
        with get_session() as session:
            art = library.write_artifact(
                session, project_id=project_id, project_slug=project_slug,
                type=f"quickref_{kind}", title=name, body=body,
                rel_path=rel_path, provider=provider, model=model,
                extra_meta={"source_kind": source_kind,
                            "source_label": source_label,
                            "commit_sha": source_sha,
                            "project_local": True,
                            "previous_commit": previous_commit or None,
                            "history_snapshot": history_snapshot},
            )
            auto_tag(project_id, art.id)
        return

    if ref and not reset_restricted_ref:
        meta, existing_body = library.read_doc(ref.path)
        body = llm.complete(
            "quickref",
            get_prompt("quickref_merge") + source_note,
            f"EXISTING DOCUMENT:\n\n{existing_body}\n\n---\n\n"
            f"NEW SOURCE MATERIAL (about {name}):\n\n{deepdive[:80000]}",
            max_tokens=2_500,
        ).strip()
        rel_path = ref.path
        snapshot = library.snapshot_history(rel_path)
    else:
        body = llm.complete(
            "quickref",
            doc_prompt(kind, cat_map) + source_note,
            f"Subject: {name}\n\n{deepdive[:80000]}",
            max_tokens=2_500,
        ).strip()
        rel_path = (None if reset_restricted_ref else
                    f"{categories.kind_dir(kind)}/{library.make_slug(name)}.md")
        snapshot = None

    with get_session() as session:
        # Serialize canonical-path allocation and publication.  This prevents
        # two concurrent resets from selecting the same archive path.
        from sqlmodel import text

        session.exec(text("BEGIN IMMEDIATE"))
        if ref:
            ref = session.get(QuickRef, ref.id)
            if reset_restricted_ref:
                # Keep the private file and Artifact row intact for local
                # history/search, but repoint the public cross-project index
                # to a document generated only from public material.
                base = f"{categories.kind_dir(kind)}/{ref.slug}-public"
                suffix = 1
                while True:
                    candidate = f"{base}{'' if suffix == 1 else f'-{suffix}'}.md"
                    artifact_collision = session.exec(
                        select(Artifact).where(Artifact.path == candidate)
                    ).first()
                    ref_collision = session.exec(
                        select(QuickRef).where(
                            QuickRef.path == candidate, QuickRef.id != ref.id)
                    ).first()
                    if not artifact_collision and not ref_collision:
                        rel_path = candidate
                        break
                    suffix += 1
                ref.path = rel_path
                ref.title = name
                ref.aliases = "[]"
                # The fresh public document has no lineage from the private
                # canonical body. Retaining those rows would cause a later
                # visibility refresh to re-restrict this clean replacement.
                for contributor in session.exec(
                    select(QuickRefSource).where(
                        QuickRefSource.quickref_id == ref.id)
                ).all():
                    session.delete(contributor)
            else:
                aliases = library.parse_aliases(ref.aliases)
                if name != ref.title and name not in aliases:
                    aliases.append(name)
                    ref.aliases = json.dumps(aliases)
        else:
            ref = QuickRef(kind=kind, slug=library.make_slug(name),
                           title=name, path=rel_path, aliases="[]")
        session.add(ref)
        session.flush()

        contributors = session.exec(
            select(QuickRefSource).where(QuickRefSource.quickref_id == ref.id)
        ).all()
        if project_id not in [c.project_id for c in contributors]:
            session.add(QuickRefSource(quickref_id=ref.id, project_id=project_id))
            session.flush()

        body += (f"\n\n---\nContributing {source_kind}: "
                 f"{library.wikilink(f'projects/{project_slug}/deepdive_merged')}")
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
                "source_kind": source_kind,
                "source_label": source_label,
            },
        )
        auto_tag(project_id, art.id)
