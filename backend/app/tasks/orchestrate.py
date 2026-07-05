"""Pipeline step graph + the run-all orchestrator.

Single source of truth for step ordering and dependencies, used by:
- the projects router (pipeline board: prerequisite gating/dimming)
- the run_all task (dependency-driven dispatch, concurrent where possible)

Two dependency maps on purpose:
- HARD_DEPS gates what a step *needs* to run at all (UI dimming + manual runs).
  e.g. the deep dives only need a transcript — corrected is optional.
- RUN_DEPS orders a full run for best *quality*: the deep dives wait for the
  correction pass so they read the corrected transcript.
"""
from __future__ import annotations

import logging
import time

from sqlmodel import select

from ..db import get_session
from ..models import Artifact, Job, Project
from .celery_app import celery
from .common import set_job

log = logging.getLogger("synapse.pipeline")

# step name → human label; order defines the pipeline board
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

# what a step truly requires before it can run (UI gating, manual runs)
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

# ordering for a full run — quality-preferring (deep dives read the corrected
# transcript, so they wait for the correction pass)
RUN_DEPS: dict[str, set[str]] = {
    **HARD_DEPS,
    "summarize": {"correct"},
    "deepdive_claude": {"correct"},
    "deepdive_gemini": {"correct"},
}

# the artifact each step produces (None → checked another way)
STEP_OUTPUT: dict[str, str | None] = {
    "ingest": None,
    "download": "source_video",
    "transcribe": "transcript",
    "correct": "corrected",
    "summarize": "summary",
    "deepdive_claude": "deepdive_claude",
    "deepdive_gemini": "deepdive_gemini",
    "merge": "deepdive_merged",
    "quickref": None,  # emits many quickref_* docs; done = last job succeeded
    "podcast_script": "podcast_script",
    "tts": "podcast_audio",
    "trim": "trimmed_audio",
    "mindmap": "mindmap",
}


def applicable_steps(project: Project) -> list[str]:
    """download only applies to URL sources."""
    return [s for s, _ in STEPS if s != "download" or project.source_type == "url"]


def step_done(session, project: Project, step: str,
              artifact_types: set[str] | None = None) -> bool:
    """Has this step produced its output for this project?"""
    if artifact_types is None:
        artifact_types = {
            a.type for a in session.exec(
                select(Artifact).where(Artifact.project_id == project.id)
            ).all()
        }
    if step == "ingest":
        from .ingest import source_audio

        try:
            source_audio(project.slug)
            return True
        except FileNotFoundError:
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
    """Labels of unmet HARD prerequisites for a step (drives UI dimming)."""
    return [STEP_LABELS[d] for d in sorted(HARD_DEPS[step])
            if not step_done(session, project, d, artifact_types)]


def transitive_dependents(step: str, deps: dict[str, set[str]]) -> set[str]:
    """Every step that (directly or indirectly) depends on `step`."""
    out: set[str] = set()
    changed = True
    while changed:
        changed = False
        for s, ds in deps.items():
            if s not in out and (step in ds or ds & out):
                out.add(s)
                changed = True
    return out


def dep_satisfied(step: str, dep: str, done: set[str], pending: set[str],
                  running: set[str], failed: set[str]) -> bool:
    """Is `dep` satisfied enough to launch `step` during a run-all?

    - finished this run, or not part of this run at all → satisfied
    - still pending/running → wait
    - failed: blocks only if it's a HARD requirement; soft (quality-only)
      deps fall back gracefully (e.g. deep dives use the raw transcript
      when the correction pass failed).
    """
    if dep in done:
        return True
    if dep in pending or dep in running:
        return False
    if dep in failed:
        return dep not in HARD_DEPS[step]
    return True  # already done before this run started


@celery.task(name="run_all")
def run_all(job_id: int, project_id: int):
    """Run every remaining step, launching each as soon as its dependencies
    finish — concurrent where the graph allows, sequential where it doesn't.

    Failure policy: a failed step skips only its HARD transitive dependents;
    independent branches (and soft-dependent steps, which have fallbacks)
    keep running.
    """
    jobs: dict[str, int] = {}
    try:
        with get_session() as session:
            set_job(session, job_id, status="running", progress="planning")
            project = session.get(Project, project_id)
            if not project:
                set_job(session, job_id, status="error", error="project not found")
                return
            todo = [s for s in applicable_steps(project)
                    if not step_done(session, project, s)]
            if not todo:
                set_job(session, job_id, status="done", progress="nothing to run")
                return
            # adopt steps a user managed to start between our enqueue and now,
            # instead of dispatching them a second time
            adopted: set[str] = set()
            for step in todo:
                existing = session.exec(
                    select(Job).where(Job.project_id == project_id, Job.task == step,
                                      Job.status.in_(("queued", "running")))
                ).first()
                if existing:
                    jobs[step] = existing.id
                    adopted.add(step)
                else:
                    # one queued Job per step, upfront, so the board shows the
                    # whole plan (and run_step rejects duplicate manual runs)
                    j = Job(project_id=project_id, task=step, status="queued")
                    session.add(j)
                    session.commit()
                    session.refresh(j)
                    jobs[step] = j.id

        log.info("run_all project=%s: %d step(s): %s",
                 project_id, len(todo), ", ".join(todo))
        pending = set(todo) - adopted
        running: set[str] = set(adopted)
        done: set[str] = set()
        failed: set[str] = set()
        deadline = time.monotonic() + 6 * 3600

        while (pending or running) and time.monotonic() < deadline:
            for step in [s for s in list(pending)
                         if all(dep_satisfied(s, d, done, pending, running, failed)
                                for d in RUN_DEPS[s])]:
                pending.discard(step)
                try:
                    celery.send_task(step, args=[jobs[step], project_id])
                    running.add(step)
                    log.info("run_all project=%s: launched %s", project_id, step)
                except Exception as e:
                    failed.add(step)
                    with get_session() as session:
                        set_job(session, jobs[step], status="error",
                                error=f"could not dispatch: {e}")

            time.sleep(2)

            with get_session() as session:
                for step in list(running):
                    job = session.get(Job, jobs[step])
                    if job is None or job.status == "done":
                        running.discard(step)
                        done.add(step)
                    elif job.status == "error":
                        running.discard(step)
                        failed.add(step)
                        # skip only steps that HARD-require the failed one
                        skipped = transitive_dependents(step, HARD_DEPS) & pending
                        for dep in skipped:
                            pending.discard(dep)
                            failed.add(dep)
                            set_job(session, jobs[dep], status="error",
                                    error=f"skipped: prerequisite step "
                                          f"'{STEP_LABELS[step]}' failed")
                        log.warning("run_all project=%s: %s failed; skipped %s",
                                    project_id, step, sorted(skipped))
                set_job(session, job_id,
                        progress=f"{len(done)} done"
                                 + (f", running: {', '.join(sorted(running))}" if running else "")
                                 + (f", {len(pending)} waiting" if pending else "")
                                 + (f", {len(failed)} failed/skipped" if failed else ""))

        with get_session() as session:
            if pending or running:
                _abort_leftovers(session, jobs, pending | running, "run-all timed out")
                set_job(session, job_id, status="error",
                        error=f"timed out with {sorted(pending | running)} unfinished")
            elif failed:
                set_job(session, job_id, status="error",
                        error=f"finished with failures: {', '.join(sorted(failed))}",
                        progress=f"{len(done)} done, {len(failed)} failed/skipped")
            else:
                set_job(session, job_id, status="done",
                        progress=f"all {len(done)} step(s) complete")
    except Exception as e:
        # never leave the plan's queued Job rows stranded — they 409-block
        # every future run of those steps
        log.exception("run_all project=%s crashed", project_id)
        with get_session() as session:
            for step, jid in jobs.items():
                job = session.get(Job, jid)
                if job and job.status == "queued":
                    set_job(session, jid, status="error",
                            error=f"run-all aborted: {e}")
            set_job(session, job_id, status="error", error=str(e)[:2000])
        raise


def _abort_leftovers(session, jobs: dict[str, int], steps: set[str], reason: str):
    for step in steps:
        job = session.get(Job, jobs[step])
        if job and job.status in ("queued", "running"):
            set_job(session, jobs[step], status="error", error=reason)
