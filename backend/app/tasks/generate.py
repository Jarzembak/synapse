"""LLM generation steps: correct, summarize, deep dives, merge, podcast script,
mind map, tagging."""
from __future__ import annotations

import json
import logging

from sqlmodel import select

log = logging.getLogger(__name__)

from ..db import get_session
from .. import library, llm, tagging
from ..config import advanced
from ..models import Artifact, Project
from ..settings_store import get_setting
from .celery_app import celery
from .common import (
    artifact_body, auto_tag, best_transcript, get_project, pipeline_task, progress,
)
from .prompts import get_prompt

DEPTH_HINTS = {
    "concise": "\nKeep the document focused and tight — around 1500 words.",
    "standard": "",
    "exhaustive": "\nBe exhaustive — cover every concept, tool and procedure in maximum depth.",
}


def _sanitize_mindmap_graph(raw: dict) -> dict:
    """Reduce model JSON to the inert graph schema rendered by the UI.

    Model-supplied URLs, paths, styling, HTML, and arbitrary ReactFlow data are
    deliberately discarded. IDs are normalized and relationship endpoints are
    resolved only through the accepted-node map.
    """
    nodes: list[dict] = []
    id_map: dict[str, str] = {}
    used: set[str] = set()
    raw_nodes = raw.get("nodes") if isinstance(raw, dict) else None
    for value in raw_nodes[:500] if isinstance(raw_nodes, list) else []:
        if not isinstance(value, dict):
            continue
        label = str(value.get("label") or "").strip()[:200]
        raw_id = str(value.get("id") or "").strip()
        if not label:
            continue
        base = library.make_slug(raw_id or label)[:80]
        node_id = base
        suffix = 2
        while node_id in used:
            tail = f"-{suffix}"
            node_id = base[:80 - len(tail)] + tail
            suffix += 1
        used.add(node_id)
        if raw_id and raw_id not in id_map:
            id_map[raw_id] = node_id
        id_map.setdefault(node_id, node_id)
        nodes.append({
            "id": node_id,
            "label": label,
            "kind": library.make_slug(str(value.get("kind") or "concept"))[:40],
            "summary": str(value.get("summary") or "").strip()[:1_000],
        })

    edges: list[dict] = []
    raw_edges = raw.get("edges") if isinstance(raw, dict) else None
    for value in raw_edges[:1_000] if isinstance(raw_edges, list) else []:
        if not isinstance(value, dict):
            continue
        source = id_map.get(str(value.get("source") or "").strip())
        target = id_map.get(str(value.get("target") or "").strip())
        if not source or not target:
            continue
        edges.append({
            "source": source,
            "target": target,
            "label": str(value.get("label") or "").strip()[:200],
        })
    return {"nodes": nodes, "edges": edges}


def _write(project_id: int, type: str, title_prefix: str, body: str,
           provider: str | None = None, model: str | None = None,
           extra_meta: dict | None = None) -> int:
    with get_session() as session:
        project = get_project(session, project_id)
        art = library.write_artifact(
            session,
            project_id=project_id,
            project_slug=project.slug,
            type=type,
            title=f"{title_prefix} — {project.title}",
            body=body,
            provider=provider,
            model=model,
            extra_meta=extra_meta,
        )
        auto_tag(project_id, art.id)
        return art.id


@celery.task(name="correct")
@pipeline_task
def correct(job_id: int, project_id: int):
    with get_session() as session:
        transcript = artifact_body(session, project_id, "transcript")
    glossary = get_setting("glossary", [])
    system = get_prompt("correct")
    if glossary:
        system += "\n\nGlossary of known-correct terms:\n" + ", ".join(glossary)

    provider, model = llm.resolve_model("correct")
    # overlap=0: each chunk is corrected and concatenated verbatim, so any
    # carried-over tail would be duplicated at every chunk boundary.
    chunks = llm.chunk_text(transcript, max_chars=int(advanced("pipeline")["chunk_chars"]),
                            overlap=0)
    fixed: list[str] = []
    for i, chunk in enumerate(chunks):
        progress(job_id, f"correcting chunk {i + 1}/{len(chunks)}")
        fixed.append(llm.complete("correct", system, chunk).strip())
    _write(project_id, "corrected", "Corrected transcript", "\n".join(fixed),
           provider=provider, model=model)


@celery.task(name="summarize")
@pipeline_task
def summarize(job_id: int, project_id: int):
    with get_session() as session:
        project = get_project(session, project_id)
        if project.source_type == "github":
            from .repository import generate_repository_document

            return generate_repository_document(
                job_id, project_id, artifact_type="summary",
                function="repository_overview", prompt_name="repository_overview",
                title_prefix="Repository overview")
        transcript = best_transcript(session, project_id)
    provider, model = llm.resolve_model("summarize")
    progress(job_id, "summarizing")
    body = llm.complete("summarize", get_prompt("summary"), transcript[:80000])
    _write(project_id, "summary", "Summary", body.strip(), provider=provider, model=model)


def _deepdive(job_id: int, project_id: int, function: str, type: str, label: str):
    with get_session() as session:
        project = get_project(session, project_id)
        if project.source_type == "github":
            from .repository import generate_repository_deepdive

            perspective = "a" if type == "deepdive_claude" else "b"
            return generate_repository_deepdive(
                job_id, project_id, function=function,
                artifact_type=type, perspective=perspective)
        transcript = best_transcript(session, project_id)
    provider, model = llm.resolve_model(function)
    progress(job_id, f"generating {label} deep dive ({model})")
    depth = str(advanced("pipeline")["deepdive_depth"])
    system = get_prompt("deepdive") + DEPTH_HINTS.get(depth, "")
    body = llm.complete(function, system, transcript[:400000])
    _write(project_id, type, f"Deep dive ({label})", body.strip(),
           provider=provider, model=model)


@celery.task(name="deepdive_claude")
@pipeline_task
def deepdive_claude(job_id: int, project_id: int):
    _deepdive(job_id, project_id, "deepdive_claude", "deepdive_claude", "Claude")


@celery.task(name="deepdive_gemini")
@pipeline_task
def deepdive_gemini(job_id: int, project_id: int):
    _deepdive(job_id, project_id, "deepdive_gemini", "deepdive_gemini", "Gemini")


@celery.task(name="merge")
@pipeline_task
def merge(job_id: int, project_id: int):
    with get_session() as session:
        project = get_project(session, project_id)
        if project.source_type == "github":
            from .repository import merge_repository_deepdives

            return merge_repository_deepdives(job_id, project_id)
        claude_dd = artifact_body(session, project_id, "deepdive_claude")
        gemini_dd = artifact_body(session, project_id, "deepdive_gemini")
    provider, model = llm.resolve_model("merge")
    progress(job_id, f"merging deep dives ({model})")
    body = llm.complete(
        "merge", get_prompt("merge"),
        f"## DOCUMENT 1 (Claude)\n\n{claude_dd}\n\n---\n\n## DOCUMENT 2 (Gemini)\n\n{gemini_dd}",
    )
    _write(project_id, "deepdive_merged", "Deep dive (merged)", body.strip(),
           provider=provider, model=model)


@celery.task(name="podcast_script")
@pipeline_task
def podcast_script(job_id: int, project_id: int):
    with get_session() as session:
        deepdive = artifact_body(session, project_id, "deepdive_merged")
        project = get_project(session, project_id)
    provider, model = llm.resolve_model("podcast_script")

    progress(job_id, "outlining episode")
    outline_system = get_prompt("podcast_outline")
    target = int(advanced("pipeline")["podcast_segments"])
    outline_system += (f"\nAim for about {target} segments."
                       if target > 0 else "\n8-14 segments.")
    outline = llm.complete_json(
        "podcast_script", outline_system, deepdive[:64_000], max_tokens=2_500)
    raw_segments = outline.get("segments", []) if isinstance(outline, dict) else []
    segments = [segment for segment in raw_segments if isinstance(segment, dict)]
    if not segments:
        raise RuntimeError("outline had no segments")
    absolute_max = 24
    requested_max = (min(absolute_max, max(1, target * 2)) if target > 0
                     else 10 if project.source_type == "github" else 16)
    outline_truncated = len(segments) > requested_max
    if outline_truncated:
        progress(
            job_id,
            f"limiting podcast outline to {requested_max} of {len(segments)} segments",
        )
        segments = segments[:requested_max]

    lines: list[str] = []
    prev_tail = "(start of show — open with a welcome and set up the topic)"
    for i, seg in enumerate(segments):
        heading = str(seg.get("heading") or "")[:500]
        points = ([str(point)[:1_000] for point in seg.get("points", [])[:30]
                   if isinstance(point, (str, int, float))]
                  if isinstance(seg.get("points"), list) else [])
        progress(job_id, f"writing segment {i + 1}/{len(segments)}: {heading}")
        text = llm.complete(
            "podcast_script", get_prompt("podcast_segment"),
            f"Episode: {outline.get('title', '')}\n"
            f"Segment {i + 1}/{len(segments)}: {heading}\n"
            f"Points to cover:\n- " + "\n- ".join(points) +
            f"\n\nEnd of previous segment:\n{prev_tail}\n\n"
            f"Source deep dive (reference for accuracy):\n{deepdive[:60000]}" +
            ("\n\nThis is the FINAL segment — wrap up the show." if i == len(segments) - 1 else ""),
            max_tokens=2_000,
        ).strip()
        lines.append(text)
        prev_tail = "\n".join(text.splitlines()[-4:])

    body = f"# {outline.get('title', 'Podcast episode')}\n\n" + "\n\n".join(lines)
    _write(project_id, "podcast_script", "Podcast script", body,
           provider=provider, model=model,
           extra_meta={"segments": len(segments),
                       "outline_truncated": outline_truncated or None})


@celery.task(name="mindmap")
@pipeline_task
def mindmap(job_id: int, project_id: int):
    with get_session() as session:
        deepdive = artifact_body(session, project_id, "deepdive_merged")
    provider, model = llm.resolve_model("mindmap")
    progress(job_id, "building topic graph")
    graph = llm.complete_json(
        "mindmap", get_prompt("mindmap"), deepdive[:64_000], max_tokens=2_500)
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list):
        raise RuntimeError("mind map had no nodes")
    graph = _sanitize_mindmap_graph(graph)
    if not graph["nodes"]:
        raise RuntimeError("mind map had no valid nodes")

    # attach quick-ref links to nodes that have a matching doc
    with get_session() as session:
        from ..models import QuickRef

        refs = {}
        for ref in session.exec(select(QuickRef)).all():
            artifact = session.exec(select(Artifact).where(
                Artifact.path == ref.path
            )).first()
            if (artifact
                    and not library.artifact_is_restricted(session, artifact)
                    and not library.artifact_is_repository_derived(session, artifact)):
                refs[ref.slug] = ref
        for node in graph["nodes"]:
            ref = refs.get(library.make_slug(node.get("label", "")))
            if ref:
                node["quickref"] = ref.path

    body = "```json\n" + json.dumps(graph, indent=2) + "\n```"
    with get_session() as session:
        project = get_project(session, project_id)
        art = library.write_artifact(
            session,
            project_id=project_id,
            project_slug=project.slug,
            type="mindmap",
            title=f"Mind map — {project.title}",
            body=body,
            provider=provider,
            model=model,
            extra_meta={"nodes": len(graph["nodes"]), "edges": len(graph.get("edges", []))},
        )
        auto_tag(project_id, art.id)


@celery.task(name="tag_artifact")
def tag_task(artifact_id: int):
    """Individual tagging — used for quick-ref docs only (their content is
    their own; project artifacts are tagged via tag_project)."""
    with get_session() as session:
        initial = session.get(Artifact, artifact_id)
        if not initial:
            return
        project = session.get(Project, initial.project_id) if initial.project_id else None
        project_id = initial.project_id
        local_only = bool(
            getattr(initial, "restricted", False)
            or library.artifact_is_repository_derived(session, initial))
    if project and project.source_type == "github":
        from ..repository import repository_processing_policy

        local_only = bool(local_only or repository_processing_policy(project_id))
    with get_session() as session:
        art = session.get(Artifact, artifact_id)
        if not art:
            return
        try:
            _, body = library.read_doc(art.path)
            with llm.project_scope(
                    art.project_id,
                    local_only=(True if local_only else None)):
                names = tagging.tag_text(
                    session, art.title, art.type, body,
                    local_only=local_only)
                library.apply_tags(session, art, names)
        except Exception:
            # tagging is best-effort; never fail the pipeline over it
            log.warning("tagging failed for artifact %s (%s)",
                        artifact_id, art.title, exc_info=True)


# Richest-first ranking of documents a project's canonical tag set derives from.
TAG_SOURCE_RANK = [
    "deepdive_merged", "repo_architecture", "repo_inventory",
    "corrected", "transcript", "summary",
]


@celery.task(name="tag_project")
def tag_project(project_id: int):
    """Project-level tagging: one LLM call over the richest document the
    project has, propagated to ALL its artifacts (quick-refs excluded).

    Metadata-only artifacts (source video/audio, podcast/trimmed audio, mind
    map) have near-empty bodies and used to pick up unrelated tags when tagged
    independently — they now inherit this canonical set instead. The result is
    cached (keyed on the source doc's type + updated stamp) so repeated
    pipeline steps propagate without re-running the LLM.
    """
    from ..settings_store import get_setting, set_setting

    try:
        with get_session() as policy_session:
            project = policy_session.get(Project, project_id)
        policy_local = False
        if project and project.source_type == "github":
            from ..repository import repository_processing_policy

            repository_processing_policy(project_id)
            policy_local = True
        with get_session() as session:
            arts = session.exec(
                select(Artifact).where(Artifact.project_id == project_id)
            ).all()
            by_type = {a.type: a for a in arts}
            source = next(
                (by_type[t] for t in TAG_SOURCE_RANK if t in by_type), None
            )
            if source is None:
                return  # nothing substantive to tag from yet

            local_only = bool(getattr(source, "restricted", False))
            local_only = bool(local_only or policy_local)

            marker_key = f"projtags.{project_id}"
            marker = get_setting(marker_key) or {}
            prev_slugs = marker.get("slugs")  # canonical set last propagated
            fresh = (marker.get("source_type") == source.type
                     and marker.get("updated") == source.updated.isoformat())
            if fresh:
                tags = marker.get("tags", [])
            else:
                _, body = library.read_doc(source.path)
                with llm.project_scope(
                        project_id,
                        local_only=(True if local_only else None)):
                    tags = tagging.tag_text(
                        session, source.title, source.type, body,
                        local_only=local_only,
                    )
            slugs = sorted({library.make_slug(t) for t in tags if library.make_slug(t)})
            set_setting(marker_key, {
                "source_type": source.type,
                "updated": source.updated.isoformat(),
                "tags": tags,
                "slugs": slugs,
            })

            for art in arts:
                if art.type.startswith("quickref_"):
                    continue  # cross-project docs keep their own tags
                current = library.current_tags(session, art.id)
                if current and prev_slugs is not None and current != prev_slugs:
                    continue  # user curated this artifact's tags by hand — keep them
                try:
                    library.apply_tags(session, art, tags)
                except Exception:
                    session.rollback()  # one bad artifact must not stop the rest
                    log.warning("tag propagation failed for artifact %s (%s)",
                                art.id, art.title, exc_info=True)
    except Exception:
        # tagging is best-effort; never fail the pipeline over it
        log.warning("project tagging failed for project %s", project_id, exc_info=True)
