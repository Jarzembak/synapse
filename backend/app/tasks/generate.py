"""LLM generation steps: correct, summarize, deep dives, merge, podcast script,
mind map, tagging."""
from __future__ import annotations

import json

from sqlmodel import select

from ..db import get_session
from .. import library, llm, tagging
from ..config import advanced
from ..models import Artifact
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
    chunks = llm.chunk_text(transcript, max_chars=int(advanced("pipeline")["chunk_chars"]))
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
        transcript = best_transcript(session, project_id)
    provider, model = llm.resolve_model("summarize")
    progress(job_id, "summarizing")
    body = llm.complete("summarize", get_prompt("summary"), transcript[:80000])
    _write(project_id, "summary", "Summary", body.strip(), provider=provider, model=model)


def _deepdive(job_id: int, project_id: int, function: str, type: str, label: str):
    with get_session() as session:
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
    provider, model = llm.resolve_model("podcast_script")

    progress(job_id, "outlining episode")
    outline_system = get_prompt("podcast_outline")
    target = int(advanced("pipeline")["podcast_segments"])
    outline_system += (f"\nAim for about {target} segments."
                       if target > 0 else "\n8-14 segments.")
    outline = llm.complete_json("podcast_script", outline_system, deepdive)
    segments = outline.get("segments", [])
    if not segments:
        raise RuntimeError("outline had no segments")

    lines: list[str] = []
    prev_tail = "(start of show — open with a welcome and set up the topic)"
    for i, seg in enumerate(segments):
        progress(job_id, f"writing segment {i + 1}/{len(segments)}: {seg.get('heading', '')}")
        text = llm.complete(
            "podcast_script", get_prompt("podcast_segment"),
            f"Episode: {outline.get('title', '')}\n"
            f"Segment {i + 1}/{len(segments)}: {seg.get('heading', '')}\n"
            f"Points to cover:\n- " + "\n- ".join(seg.get("points", [])) +
            f"\n\nEnd of previous segment:\n{prev_tail}\n\n"
            f"Source deep dive (reference for accuracy):\n{deepdive[:60000]}" +
            ("\n\nThis is the FINAL segment — wrap up the show." if i == len(segments) - 1 else ""),
        ).strip()
        lines.append(text)
        prev_tail = "\n".join(text.splitlines()[-4:])

    body = f"# {outline.get('title', 'Podcast episode')}\n\n" + "\n\n".join(lines)
    _write(project_id, "podcast_script", "Podcast script", body,
           provider=provider, model=model,
           extra_meta={"segments": len(segments)})


@celery.task(name="mindmap")
@pipeline_task
def mindmap(job_id: int, project_id: int):
    with get_session() as session:
        deepdive = artifact_body(session, project_id, "deepdive_merged")
    provider, model = llm.resolve_model("mindmap")
    progress(job_id, "building topic graph")
    graph = llm.complete_json("mindmap", get_prompt("mindmap"), deepdive)
    if not graph.get("nodes"):
        raise RuntimeError("mind map had no nodes")

    # attach quick-ref links to nodes that have a matching doc
    with get_session() as session:
        from ..models import QuickRef

        refs = {r.slug: r for r in session.exec(select(QuickRef)).all()}
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
    with get_session() as session:
        art = session.get(Artifact, artifact_id)
        if not art:
            return
        try:
            _, body = library.read_doc(art.path)
            tagging.tag_artifact(session, art, body)
        except Exception:
            # tagging is best-effort; never fail the pipeline over it
            pass
