"""Dependency-aware, restart-safe orchestration for the media pipeline."""
from __future__ import annotations

import json
import logging
import time

from sqlalchemy.exc import IntegrityError
from sqlmodel import select, text

from ..db import get_session
from ..models import Artifact, Job, Project
from ..settings_store import get_setting
from .celery_app import celery
from .common import TERMINAL_JOB_STATES, set_job, transition_job

log = logging.getLogger("synapse.pipeline")

STEPS: list[tuple[str, str]] = [
    ("ingest", "Ingest media"),
    ("download", "Download & keep media"),
    ("transcribe", "Transcript"),
    ("correct", "Correction pass"),
    ("summarize", "Summary"),
    ("deepdive_claude", "Deep dive (Claude)"),
    ("deepdive_gemini", "Deep dive (Gemini)"),
    ("merge", "Merge deep dives"),
    ("quickref", "Quick-references"),
    ("podcast_script", "Podcast script"),
    ("tts", "Podcast audio"),
    ("trim", "Trim audio"),
    ("mindmap", "Mind map"),
]
STEP_NAMES = {s for s, _ in STEPS}
STEP_LABELS = dict(STEPS)

HARD_DEPS: dict[str, set[str]] = {
    "ingest": set(),
    "download": set(),
    "transcribe": {"ingest"},
    "correct": {"transcribe"},
    "summarize": {"transcribe"},
    "deepdive_claude": {"transcribe"},
    "deepdive_gemini": {"transcribe"},
    "merge": {"deepdive_claude", "deepdive_gemini"},
    "quickref": {"merge"},
    "podcast_script": {"merge"},
    "tts": {"podcast_script"},
    "trim": {"ingest", "transcribe"},
    "mindmap": {"merge"},
}

RUN_DEPS: dict[str, set[str]] = {
    **HARD_DEPS,
    "summarize": {"correct"},
    "deepdive_claude": {"correct"},
    "deepdive_gemini": {"correct"},
}

STEP_OUTPUT: dict[str, str | None] = {
    "ingest": None,
    "download": "source_video",
    "transcribe": "transcript",
    "correct": "corrected",
    "summarize": "summary",
    "deepdive_claude": "deepdive_claude",
    "deepdive_gemini": "deepdive_gemini",
    "merge": "deepdive_merged",
    "quickref": None,
    "podcast_script": "podcast_script",
    "tts": "podcast_audio",
    "trim": "trimmed_audio",
    "mindmap": "mindmap",
}

BUILTIN_PROFILES: dict[str, dict] = {
    "full": {
        "label": "Full production",
        "description": "Every applicable artifact, including media, podcast audio and trim.",
        "steps": [s for s, _ in STEPS],
    },
    "research": {
        "label": "Research library",
        "description": "Transcript, analysis, quick-references and mind map; no generated audio.",
        "steps": [
            "ingest", "transcribe", "correct", "summarize",
            "deepdive_claude", "deepdive_gemini", "merge", "quickref", "mindmap",
        ],
    },
    "quick": {
        "label": "Quick notes",
        "description": "Ingest, transcript correction and summary only.",
        "steps": ["ingest", "transcribe", "correct", "summarize"],
    },
    "audio": {
        "label": "Audio edition",
        "description": "Analysis plus podcast and cleaned source audio.",
        "steps": [
            "ingest", "transcribe", "correct", "deepdive_claude",
            "deepdive_gemini", "merge", "podcast_script", "tts", "trim",
        ],
    },
}


def pipeline_profiles() -> dict[str, dict]:
    custom = get_setting("pipeline.profiles") or {}
    clean: dict[str, dict] = {}
    for key, profile in custom.items():
        steps = [s for s in profile.get("steps", []) if s in STEP_NAMES]
        if key and steps:
            clean[key] = {
                "label": profile.get("label") or key,
                "description": profile.get("description") or "Custom pipeline profile",
                "steps": steps,
                "custom": True,
            }
    return {**BUILTIN_PROFILES, **clean}


def applicable_steps(project: Project) -> list[str]:
    return [s for s, _ in STEPS if s != "download" or project.source_type == "url"]


def _artifact_for_step(session, project_id: int, step: str) -> Artifact | None:
    output = STEP_OUTPUT[step]
    if not output:
        return None
    return session.exec(
        select(Artifact).where(Artifact.project_id == project_id, Artifact.type == output)
    ).first()


def step_stale(session, project: Project, step: str) -> bool:
    """Whether the output was produced from older inputs/configuration."""
    try:
        from ..provenance import is_step_stale

        return is_step_stale(session, project, step)
    except Exception:
        log.warning("could not evaluate staleness for project=%s step=%s",
                    project.id, step, exc_info=True)
        return False


def step_done(session, project: Project, step: str,
              artifact_types: set[str] | None = None) -> bool:
    if artifact_types is None:
        artifact_types = {
            a.type for a in session.exec(
                select(Artifact).where(Artifact.project_id == project.id)
            ).all()
        }
    if step == "ingest":
        from .ingest import source_audio

        try:
            path = source_audio(project.slug)
            return path.is_file() and path.stat().st_size > 0
        except (FileNotFoundError, OSError):
            return False
    if step == "quickref":
        job = session.exec(
            select(Job).where(Job.project_id == project.id, Job.task == "quickref",
                              Job.status == "done")
        ).first()
        return job is not None
    return STEP_OUTPUT[step] in artifact_types


def missing_deps(session, project: Project, step: str,
                 artifact_types: set[str] | None = None) -> list[str]:
    return [STEP_LABELS[d] for d in sorted(HARD_DEPS[step])
            if not step_done(session, project, d, artifact_types)]


def transitive_dependents(step: str, deps: dict[str, set[str]]) -> set[str]:
    out: set[str] = set()
    changed = True
    while changed:
        changed = False
        for candidate, requirements in deps.items():
            if candidate not in out and (step in requirements or requirements & out):
                out.add(candidate)
                changed = True
    return out


def dependency_closure(steps: set[str], deps: dict[str, set[str]] = RUN_DEPS) -> set[str]:
    out = set(steps)
    changed = True
    while changed:
        changed = False
        for step in list(out):
            before = len(out)
            out.update(deps[step])
            changed = changed or len(out) != before
    return out


def dep_satisfied(step: str, dep: str, done: set[str], pending: set[str],
                  running: set[str], failed: set[str]) -> bool:
    if dep in done:
        return True
    if dep in pending or dep in running:
        return False
    if dep in failed:
        # A profile run deliberately ordered this dependency. Continuing would
        # let consumers read an older artifact left behind by the failed
        # attempt (for example a stale corrected transcript). Direct manual
        # runs retain their raw-input fallbacks because they bypass this graph.
        return False
    return True


def _job_options(job: Job) -> dict:
    try:
        value = json.loads(job.options or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _selected_steps(project: Project, options: dict) -> set[str]:
    applicable = set(applicable_steps(project))
    if options.get("steps") is not None:
        selected = {s for s in options["steps"] if s in STEP_NAMES}
    else:
        profile = pipeline_profiles().get(options.get("profile") or "full", BUILTIN_PROFILES["full"])
        selected = set(profile["steps"])
    return dependency_closure(selected) & applicable


def maybe_start_next_run_all() -> None:
    """Atomically claim and dispatch the oldest queued whole-project run."""
    with get_session() as session:
        if session.exec(
            select(Job).where(Job.task == "run_all", Job.status == "running")
        ).first():
            return
        nxt = session.exec(
            select(Job).where(Job.task == "run_all", Job.status == "queued")
            .order_by(Job.created)
        ).first()
        if not nxt:
            return
        try:
            result = session.exec(text(
                "UPDATE job SET status='running', progress='starting', "
                "started=CURRENT_TIMESTAMP, heartbeat=CURRENT_TIMESTAMP, "
                "updated=CURRENT_TIMESTAMP WHERE id=:id AND status='queued' "
                "AND NOT EXISTS (SELECT 1 FROM job WHERE task='run_all' "
                "AND status='running' AND id<>:id)"
            ).bindparams(id=nxt.id))
            session.commit()
        except IntegrityError:
            session.rollback()
            return
        if getattr(result, "rowcount", 0) != 1:
            return
        try:
            async_result = celery.send_task("run_all", args=[nxt.id, nxt.project_id])
            set_job(session, nxt.id, celery_id=async_result.id)
            log.info("run_all: started project=%s (job=%s)", nxt.project_id, nxt.id)
        except Exception as exc:
            log.exception("run_all dispatch failed for job=%s", nxt.id)
            set_job(session, nxt.id, status="error", error=f"could not dispatch: {exc}")


def _active_step(session, project_id: int, step: str) -> Job | None:
    return session.exec(
        select(Job).where(Job.project_id == project_id, Job.task == step,
                          Job.status.in_(("queued", "running")))
        .order_by(Job.created.desc())
    ).first()


def _create_step_job(parent_id: int, project_id: int, step: str) -> Job | None:
    with get_session() as session:
        existing = _active_step(session, project_id, step)
        if existing:
            # A queued row without a broker id is a prior split-brain/plan ghost.
            if existing.status == "queued" and not existing.celery_id:
                set_job(session, existing.id, status="error",
                        error="orphaned before broker dispatch")
            else:
                return existing
        job = Job(project_id=project_id, task=step, parent_job_id=parent_id)
        session.add(job)
        try:
            session.commit()
            session.refresh(job)
            return job
        except IntegrityError:
            session.rollback()
            return _active_step(session, project_id, step)


def _dispatch_step(parent_id: int, project_id: int, step: str) -> tuple[Job | None, Exception | None]:
    job = _create_step_job(parent_id, project_id, step)
    if not job:
        return None, RuntimeError("could not claim an active step job")
    if job.celery_id or job.status == "running":
        return job, None
    try:
        result = celery.send_task(step, args=[job.id, project_id])
        with get_session() as session:
            set_job(session, job.id, celery_id=result.id)
            return session.get(Job, job.id), None
    except Exception as exc:
        with get_session() as session:
            set_job(session, job.id, status="error", error=f"could not dispatch: {exc}")
        return job, exc


def _skip_steps(parent_id: int, project_id: int, steps: set[str], reason: str) -> None:
    for step in steps:
        with get_session() as session:
            existing = _active_step(session, project_id, step)
            if existing:
                set_job(session, existing.id, status="error", error=reason)
                continue
            job = Job(project_id=project_id, task=step, parent_job_id=parent_id,
                      status="error", error=reason)
            session.add(job)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()


def cancel_children(parent_job_id: int, reason: str = "parent run canceled") -> int:
    """Revoke and terminally fence every nonterminal child of a run."""
    with get_session() as session:
        children = session.exec(
            select(Job).where(Job.parent_job_id == parent_job_id,
                              Job.status.in_(("queued", "running")))
        ).all()
        for child in children:
            transition_job(session, child.id, {"queued", "running"}, "canceled", error=reason)
            if child.celery_id:
                try:
                    celery.control.revoke(child.celery_id, terminate=True)
                except Exception:
                    log.warning("could not revoke child job=%s", child.id, exc_info=True)
        return len(children)


@celery.task(name="run_all")
def run_all(job_id: int, project_id: int):
    """Run the selected profile, dispatching steps as dependencies complete."""
    jobs: dict[str, int] = {}
    try:
        with get_session() as session:
            parent = session.get(Job, job_id)
            if not parent or parent.status != "running":
                return
            project = session.get(Project, project_id)
            if not project or project.deleting:
                set_job(session, job_id, status="error", error="project not found or deleting")
                return
            options = _job_options(parent)
            selected = _selected_steps(project, options)
            force_steps = {s for s in options.get("force_steps", []) if s in selected}
            todo = [s for s in applicable_steps(project) if s in selected and (
                s in force_steps or not step_done(session, project, s) or step_stale(session, project, s)
            )]
            if not todo:
                set_job(session, job_id, status="done", progress="nothing to run")
                return

            adopted: set[str] = set()
            for step in todo:
                existing = _active_step(session, project_id, step)
                if existing and (existing.status == "running" or existing.celery_id):
                    jobs[step] = existing.id
                    adopted.add(step)

        log.info("run_all project=%s profile=%s: %s", project_id,
                 options.get("profile", "full"), ", ".join(todo))
        pending = set(todo) - adopted
        running = set(adopted)
        done: set[str] = set()
        failed: set[str] = set()
        deadline = time.monotonic() + float(options.get("timeout_seconds") or 6 * 3600)

        while (pending or running) and time.monotonic() < deadline:
            with get_session() as session:
                parent = session.get(Job, job_id)
                if not parent or parent.status != "running":
                    cancel_children(job_id)
                    return
                set_job(session, job_id, heartbeat=parent.updated)

            ready = [s for s in list(pending)
                     if all(dep_satisfied(s, dep, done, pending, running, failed)
                            for dep in RUN_DEPS[s])]
            for step in ready:
                pending.discard(step)
                child, error = _dispatch_step(job_id, project_id, step)
                if child:
                    jobs[step] = child.id
                if error or not child:
                    failed.add(step)
                    skipped = transitive_dependents(step, RUN_DEPS) & pending
                    pending.difference_update(skipped)
                    failed.update(skipped)
                    _skip_steps(job_id, project_id, skipped,
                                f"skipped: prerequisite '{STEP_LABELS[step]}' failed")
                else:
                    running.add(step)

            time.sleep(1)
            with get_session() as session:
                for step in list(running):
                    child = session.get(Job, jobs[step])
                    if child is None:
                        running.discard(step)
                        failed.add(step)
                        continue
                    if child.status == "done":
                        running.discard(step)
                        done.add(step)
                    elif child.status in ("error", "canceled"):
                        running.discard(step)
                        failed.add(step)
                        skipped = transitive_dependents(step, RUN_DEPS) & pending
                        pending.difference_update(skipped)
                        failed.update(skipped)
                        _skip_steps(job_id, project_id, skipped,
                                    f"skipped: prerequisite '{STEP_LABELS[step]}' failed")
                set_job(
                    session, job_id,
                    progress=f"{len(done)} done"
                    + (f", running: {', '.join(sorted(running))}" if running else "")
                    + (f", {len(pending)} waiting" if pending else "")
                    + (f", {len(failed)} failed/skipped" if failed else ""),
                )

        with get_session() as session:
            if pending or running:
                cancel_children(job_id, "run timed out")
                set_job(session, job_id, status="error",
                        error=f"timed out with {sorted(pending | running)} unfinished")
            elif failed:
                set_job(session, job_id, status="error",
                        error=f"finished with failures: {', '.join(sorted(failed))}",
                        progress=f"{len(done)} done, {len(failed)} failed/skipped")
            else:
                set_job(session, job_id, status="done",
                        progress=f"all {len(done)} step(s) complete")
    except Exception as exc:
        log.exception("run_all project=%s crashed", project_id)
        cancel_children(job_id, f"run aborted: {exc}")
        with get_session() as session:
            set_job(session, job_id, status="error", error=str(exc)[:2000])
        raise
    finally:
        maybe_start_next_run_all()


def _abort_leftovers(session, jobs: dict[str, int], steps: set[str], reason: str):
    """Compatibility helper retained for tests/older callers."""
    for step in steps:
        job_id = jobs.get(step)
        if not job_id:
            continue
        child = session.get(Job, job_id)
        if child and child.status in ("queued", "running"):
            transition_job(session, child.id, {"queued", "running"}, "canceled", error=reason)
