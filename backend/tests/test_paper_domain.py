from __future__ import annotations

import io
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, select, text

from app import library
from app.db import _migrate, get_session, init_db
from app.main import app
from app.models import (
    Artifact,
    Job,
    PaperChunk,
    PaperSeries,
    PaperSeriesPart,
    PaperSource,
    Project,
)
from app.settings_store import get_setting, set_setting


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def _minimal_pdf(label: str = "paper") -> bytes:
    # Import validates type and immutably stores the bytes; parser behavior is
    # covered by paper-engine fixtures, so the API test need not construct a
    # complete cross-reference table.
    return (f"%PDF-1.7\n% {label}\n%%EOF\n").encode()


def _upload(client: TestClient, *, local_only: bool = True) -> dict:
    title = f"Paper domain {uuid.uuid4().hex}"
    response = client.post(
        "/api/papers/upload",
        data={
            "title": title,
            "ocr_languages": json.dumps(["eng", "deu"]),
            "local_only": str(local_only).lower(),
            "analyze": "false",
        },
        files={"file": ("dense-paper.pdf", io.BytesIO(_minimal_pdf(title)), "application/pdf")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_paper_upload_is_immutable_and_always_cloud_excluded(client):
    payload = _upload(client, local_only=False)
    project_id = payload["project"]["id"]
    assert payload["project"]["source_type"] == "paper"
    assert payload["source"]["local_only"] is False
    assert payload["source"]["privacy_locked"] is False
    assert payload["source"]["ocr_languages"] == ["eng", "deu"]
    assert payload["source"]["pdf_url"].endswith(f"/{project_id}/source")

    with get_session() as session:
        source = session.exec(select(PaperSource).where(
            PaperSource.project_id == project_id
        )).one()
        artifact = session.exec(select(Artifact).where(
            Artifact.project_id == project_id,
            Artifact.type == "source_paper",
        )).one()
        assert source.relative_path.startswith(f"projects/{payload['project']['slug']}/")
        assert len(source.source_hash) == 64
        assert artifact.cloud_sync_excluded is True
        assert artifact.restricted is False

    pdf = client.get(payload["source"]["pdf_url"])
    assert pdf.status_code == 200
    assert pdf.headers["content-type"].startswith("application/pdf")
    assert pdf.content.startswith(b"%PDF-")


def test_shared_model_settings_are_locked_during_active_paper_jobs(client):
    payload = _upload(client)
    project_id = payload["project"]["id"]
    setting_key = "model.paper_map"
    previous = get_setting(setting_key)
    previous_local_model = get_setting("repository.local_model")
    with get_session() as session:
        job = Job(project_id=project_id, task="paper_analyze", status="running")
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id

    try:
        response = client.put(f"/api/settings/models/{setting_key.removeprefix('model.')}", json={
            "provider": "ollama",
            "model": "guard-test-model",
        })
        assert response.status_code == 409, response.text
        assert "repository or paper processing" in response.text
        assert get_setting(setting_key) == previous

        # Local-only papers are pinned to repository.local_model regardless of
        # their model-matrix choice, so that shared setting must also stay
        # immutable for the duration of a paper job.
        response = client.put("/api/repositories/settings", json={
            "local_model": "guard-paper-model",
        })
        assert response.status_code == 409, response.text
        assert "repository or paper processing" in response.text
        assert get_setting("repository.local_model") == previous_local_model

        # Repository scan bounds do not affect papers and remain independently
        # editable while paper analysis is active.
        repository_settings = client.get("/api/repositories/settings").json()
        response = client.put("/api/repositories/settings", json={
            "limits": {
                "max_files": repository_settings["limits"]["max_files"],
            },
        })
        assert response.status_code == 200, response.text
    finally:
        with get_session() as session:
            job = session.get(Job, job_id)
            if job:
                session.delete(job)
                session.commit()
        set_setting(setting_key, previous)
        set_setting("repository.local_model", previous_local_model)


def test_plan_is_versioned_and_critical_coverage_is_enforced(client):
    payload = _upload(client)
    project_id = payload["project"]["id"]
    with get_session() as session:
        source = session.exec(select(PaperSource).where(
            PaperSource.project_id == project_id
        )).one()
        source.status = "ready"
        source.quality_grade = "GOOD"
        source.quality_report = json.dumps({
            "analysis_blocked": False,
            "poor_pages": [],
            "blocked_reasons": [],
        })
        chunk = PaperChunk(
            source_id=source.id,
            chunk_index=0,
            evidence_id=f"paper-{source.source_hash[:12]}-p1-critical",
            page_number=1,
            body="The primary result.",
            body_hash="1" * 64,
            flags=json.dumps({"critical": True}),
        )
        session.add(source)
        session.add(chunk)
        session.commit()
        session.refresh(chunk)
        evidence_id = chunk.evidence_id

    created = client.post(f"/api/papers/{project_id}/series", json={
        "audience": "practitioner",
        "auto_plan": False,
    })
    assert created.status_code == 200, created.text
    series_id = created.json()["id"]

    too_many = client.put(f"/api/paper-series/{series_id}/plan", json={
        "expected_version": 0,
        "parts": [
            {"position": position, "title": f"Part {position}"}
            for position in range(1, 7)
        ],
    })
    assert too_many.status_code == 422
    assert "at most 5" in too_many.text

    missing = client.put(f"/api/paper-series/{series_id}/plan", json={
        "expected_version": 0,
        "parts": [{"position": 1, "title": "What the result means", "evidence": []}],
    })
    assert missing.status_code == 200, missing.text
    assert missing.json()["plan_version"] == 1
    denied = client.post(f"/api/paper-series/{series_id}/approve", json={
        "expected_version": 1,
    })
    assert denied.status_code == 422
    assert "critical coverage" in denied.text

    part_id = missing.json()["parts"][0]["id"]
    covered = client.put(f"/api/paper-series/{series_id}/plan", json={
        "expected_version": 1,
        "parts": [{
            "id": part_id,
            "position": 1,
            "title": "What the result means",
            "focus": "Interpret the central result without overstating it.",
            "evidence": [{
                "evidence_id": evidence_id,
                "role": "primary",
                "importance": "critical",
            }],
        }],
    })
    assert covered.status_code == 200, covered.text
    approved = client.post(f"/api/paper-series/{series_id}/approve", json={
        "expected_version": 2,
    })
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"

    stale_write = client.put(f"/api/paper-series/{series_id}/plan", json={
        "expected_version": 1,
        "parts": [{"id": part_id, "position": 1, "title": "Stale client"}],
    })
    assert stale_write.status_code == 409


def test_completed_part_structure_is_locked(client):
    payload = _upload(client)
    project_id = payload["project"]["id"]
    with get_session() as session:
        source = session.exec(select(PaperSource).where(
            PaperSource.project_id == project_id
        )).one()
        source.status = "ready"
        source.quality_grade = "GOOD"
        session.add(source)
        session.commit()
    created = client.post(f"/api/papers/{project_id}/series", json={
        "audience": "expert", "auto_plan": False,
    }).json()
    series_id = created["id"]
    planned = client.put(f"/api/paper-series/{series_id}/plan", json={
        "expected_version": 0,
        "parts": [{"position": 1, "title": "Methods"}],
    }).json()
    part_id = planned["parts"][0]["id"]
    with get_session() as session:
        part = session.get(PaperSeriesPart, part_id)
        part.status = "complete"
        part.script_status = "complete"
        session.add(part)
        session.commit()
    changed = client.put(f"/api/paper-series/{series_id}/plan", json={
        "expected_version": 1,
        "parts": [{"id": part_id, "position": 1, "title": "Changed methods"}],
    })
    assert changed.status_code == 409
    assert "structurally locked" in changed.text


def test_track_deletion_retains_source_and_project_deletion_removes_it(client):
    payload = _upload(client)
    project_id = payload["project"]["id"]
    with get_session() as session:
        source = session.exec(select(PaperSource).where(
            PaperSource.project_id == project_id
        )).one()
        series = session.exec(select(PaperSeries).where(
            PaperSeries.project_id == project_id
        )).one()
        pdf_path = library.lib_path(source.relative_path)
        assert pdf_path.is_file()
        series_id = series.id

    deleted_track = client.delete(f"/api/paper-series/{series_id}")
    assert deleted_track.status_code == 200, deleted_track.text
    assert deleted_track.json()["source_retained"] is True
    with get_session() as session:
        assert session.exec(select(PaperSource).where(
            PaperSource.project_id == project_id
        )).one()
        assert not session.exec(select(PaperSeries).where(
            PaperSeries.project_id == project_id
        )).first()
    assert pdf_path.is_file()

    deleted_project = client.delete(f"/api/projects/{project_id}")
    assert deleted_project.status_code == 200, deleted_project.text
    with get_session() as session:
        assert session.get(Project, project_id) is None
        assert not session.exec(select(PaperSource).where(
            PaperSource.project_id == project_id
        )).first()
    assert not pdf_path.exists()


def test_critical_topic_cannot_rely_only_on_acknowledged_poor_pages(client):
    payload = _upload(client)
    project_id = payload["project"]["id"]
    evidence_id = f"gap-only-{uuid.uuid4().hex}"
    with get_session() as session:
        source = session.exec(select(PaperSource).where(
            PaperSource.project_id == project_id
        )).one()
        source.status = "ready"
        source.quality_grade = "POOR"
        source.quality_report = json.dumps({
            "analysis_blocked": True,
            "poor_pages": [2],
            "blocked_reasons": [{"kind": "page", "page_number": 2, "grade": "POOR"}],
        })
        source.acknowledged_pages = json.dumps([{
            "page": 2, "reason": "The scan is incomplete but retained for transparency.",
        }])
        source.coverage_report = json.dumps({
            "topics": [{
                "id": "gap-critical",
                "title": "Critical result on damaged page",
                "importance": "critical",
                "evidence_ids": [evidence_id],
            }],
        })
        session.add(source)
        session.add(PaperChunk(
            source_id=source.id,
            chunk_index=0,
            evidence_id=evidence_id,
            page_number=2,
            body="A critical result extracted from the acknowledged poor page.",
            body_hash="2" * 64,
        ))
        session.commit()
    created = client.post(f"/api/papers/{project_id}/series", json={
        "audience": "practitioner", "auto_plan": False,
    })
    assert created.status_code == 200, created.text
    series_id = created.json()["id"]
    planned = client.put(f"/api/paper-series/{series_id}/plan", json={
        "expected_version": 0,
        "parts": [{
            "position": 1,
            "title": "Damaged evidence",
            "focus": "Explain the surviving evidence and its extraction caveat.",
            "evidence_ids": [evidence_id],
        }],
    })
    assert planned.status_code == 200, planned.text
    denied = client.post(f"/api/paper-series/{series_id}/approve", json={
        "expected_version": 1,
    })
    assert denied.status_code == 422
    assert "acknowledged POOR pages" in denied.text

    part_id = planned.json()["parts"][0]["id"]
    demoted = client.put(f"/api/paper-series/{series_id}/plan", json={
        "expected_version": 1,
        "parts": [{
            "id": part_id,
            "position": 1,
            "title": "Damaged evidence",
            "focus": "Explain the surviving evidence and its extraction caveat.",
            "evidence_ids": [evidence_id],
        }],
        "omissions": [{
            "topic_id": "gap-critical",
            "importance": "critical",
            "reason": "Demoted because its only support is an acknowledged POOR scan.",
        }],
    })
    assert demoted.status_code == 200, demoted.text
    approved = client.post(f"/api/paper-series/{series_id}/approve", json={
        "expected_version": 2,
    })
    assert approved.status_code == 200, approved.text


def test_schema_v3_job_scope_indexes_allow_independent_tracks():
    init_db()
    with get_session() as session:
        suffix = uuid.uuid4().hex
        from app.models import Project

        project = Project(
            slug=f"paper-index-{suffix}", title="Paper index", source="x",
            source_type="paper",
        )
        session.add(project)
        session.flush()
        source = PaperSource(
            project_id=project.id,
            original_filename="paper.pdf",
            source_hash=suffix.ljust(64, "0")[:64],
            relative_path="projects/test/source/original.pdf",
        )
        session.add(source)
        first = PaperSeries(project_id=project.id, audience="generalist")
        second = PaperSeries(project_id=project.id, audience="expert")
        session.add(first)
        session.add(second)
        session.flush()
        session.add(Job(project_id=project.id, paper_series_id=first.id,
                        task="paper_plan", status="running"))
        session.add(Job(project_id=project.id, paper_series_id=second.id,
                        task="paper_plan", status="running"))
        session.commit()

        session.add(Job(project_id=project.id, paper_series_id=first.id,
                        task="paper_plan", status="queued"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        names = {
            row[1] for row in session.exec(text("PRAGMA index_list('job')")).all()
        }
        assert {
            "uq_active_root_task",
            "uq_active_paper_series_task",
            "uq_active_paper_part_task",
        } <= names
        version = session.exec(text(
            "SELECT MAX(version) FROM schema_version"
        )).one()[0]
        assert version == 3

        # The application test database is shared with later integration
        # modules.  Do not leave intentionally-created active jobs behind and
        # make backup behavior depend on pytest collection order.
        for job in session.exec(select(Job).where(
            Job.project_id == project.id,
            Job.status.in_(("queued", "running")),
        )).all():
            job.status = "done"
            session.add(job)
        session.commit()


def test_v2_upgrade_adds_paper_scope_without_rewriting_old_rows(tmp_path):
    upgrade_engine = create_engine(f"sqlite:///{tmp_path / 'v2.sqlite3'}")
    with upgrade_engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE schema_version (version INTEGER NOT NULL, "
            "applied TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql("INSERT INTO schema_version(version) VALUES (2)")
        connection.exec_driver_sql(
            "CREATE TABLE project (id INTEGER PRIMARY KEY, slug VARCHAR, title VARCHAR, "
            "source VARCHAR, source_type VARCHAR, status VARCHAR, deleting BOOLEAN, "
            "created DATETIME)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE artifact (id INTEGER PRIMARY KEY, project_id INTEGER, type VARCHAR, "
            "title VARCHAR, path VARCHAR, media_path VARCHAR, provider VARCHAR, model VARCHAR, "
            "input_hash VARCHAR DEFAULT '', config_hash VARCHAR DEFAULT '', "
            "provenance VARCHAR DEFAULT '{}', restricted BOOLEAN DEFAULT 0, "
            "repository_derived BOOLEAN DEFAULT 0, created DATETIME, updated DATETIME)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE job (id INTEGER PRIMARY KEY, project_id INTEGER, task VARCHAR, "
            "status VARCHAR, progress VARCHAR, error VARCHAR, celery_id VARCHAR, "
            "parent_job_id INTEGER, options VARCHAR DEFAULT '{}', started DATETIME, "
            "finished DATETIME, heartbeat DATETIME, created DATETIME, updated DATETIME)"
        )
        connection.exec_driver_sql(
            "INSERT INTO project(id,slug,title,source,source_type,status,deleting) "
            "VALUES (1,'legacy','Legacy','x','url','done',0)"
        )
        connection.exec_driver_sql(
            "INSERT INTO job(id,project_id,task,status) VALUES (1,1,'summarize','done')"
        )
    SQLModel.metadata.create_all(upgrade_engine)
    with upgrade_engine.begin() as connection:
        _migrate(connection)
        artifact_columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info('artifact')")
        }
        job_columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info('job')")
        }
        assert {"paper_series_id", "paper_part_id", "cloud_sync_excluded"} <= artifact_columns
        assert {"paper_series_id", "paper_part_id"} <= job_columns
        assert connection.exec_driver_sql(
            "SELECT status FROM job WHERE id=1"
        ).scalar() == "done"
        assert connection.exec_driver_sql(
            "SELECT MAX(version) FROM schema_version"
        ).scalar() == 3
