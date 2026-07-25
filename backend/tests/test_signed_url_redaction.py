"""Regressions preventing signed media URLs from entering durable metadata."""
from __future__ import annotations

import hashlib
import json
import pickle
import traceback
import uuid

import pytest
from fastapi.testclient import TestClient

from app import library, provenance
from app.db import get_session
from app.main import app
from app.models import Job, Project
from app.tasks.common import PipelineTaskError, pipeline_task
from app.trusted_origin import (
    TRUSTED_ORIGIN_HEADER,
    TRUSTED_REQUEST_HOST_HEADER,
)


@pytest.fixture(scope="module")
def client():
    with TestClient(
        app,
        headers={
            TRUSTED_ORIGIN_HEADER: "http://localhost:8080",
            TRUSTED_REQUEST_HOST_HEADER: "localhost:8080",
        },
    ) as test_client:
        yield test_client


def test_url_artifact_redacts_source_after_extra_metadata_merge():
    suffix = uuid.uuid4().hex
    secret = f"signed-secret-{suffix}"
    source = (
        f"https://media.example.com/recordings/{suffix}"
        f"?token={secret}&expires=9999999999#viewer"
    )
    with get_session() as session:
        project = Project(
            slug=f"signed-artifact-{suffix}",
            title="Signed artifact fixture",
            source=source,
            source_type="url",
        )
        session.add(project)
        session.commit()
        session.refresh(project)
        artifact = library.write_artifact(
            session,
            project_id=project.id,
            project_slug=project.slug,
            type="source_video",
            title="Source video",
            body="Archived source.",
            extra_meta={
                # Central redaction must win even when a caller accidentally
                # supplies the authenticated URL after base metadata is built.
                "source_url": source,
                "duration_seconds": 60,
            },
        )
        artifact_path = artifact.path
        stored_provenance = artifact.provenance

    safe_source = "https://media.example.com"
    meta, _body = library.read_doc(artifact_path)
    raw_markdown = library.lib_path(artifact_path).read_text(encoding="utf-8")
    assert meta["source_url"] == safe_source
    assert meta["duration_seconds"] == 60
    assert secret not in raw_markdown
    assert f"/recordings/{suffix}" not in raw_markdown
    assert secret not in stored_provenance


def test_url_provenance_uses_full_source_digest_for_staleness():
    suffix = uuid.uuid4().hex
    first_source = (
        f"https://media.example.com/watch/{suffix}?token=first-{suffix}"
    )
    second_source = (
        f"https://media.example.com/watch/{suffix}?token=second-{suffix}"
    )
    with get_session() as session:
        project = Project(
            slug=f"signed-provenance-{suffix}",
            title="Signed provenance fixture",
            source=first_source,
            source_type="url",
        )
        session.add(project)
        session.commit()
        session.refresh(project)

        first_signature = provenance._source_signature(project)
        first_hash, _config_hash, first_detail = provenance.signatures(
            session, project, "download"
        )
        project.source = second_source
        session.add(project)
        session.commit()
        session.refresh(project)
        second_signature = provenance._source_signature(project)
        second_hash, _config_hash, second_detail = provenance.signatures(
            session, project, "download"
        )

    safe_source = "https://media.example.com"
    assert first_signature == {
        "source": safe_source,
        "source_digest": hashlib.sha256(first_source.encode("utf-8")).hexdigest(),
        "source_type": "url",
    }
    assert second_signature["source"] == safe_source
    assert second_signature["source_digest"] == hashlib.sha256(
        second_source.encode("utf-8")
    ).hexdigest()
    assert first_hash != second_hash
    durable_detail = json.dumps([first_detail, second_detail], sort_keys=True)
    assert f"first-{suffix}" not in durable_detail
    assert f"second-{suffix}" not in durable_detail
    assert f"/watch/{suffix}" not in durable_detail


def test_pending_url_title_does_not_copy_query_secrets(client, monkeypatch):
    from app.tasks import ingest as ingest_module

    monkeypatch.setattr(ingest_module, "fetch_url_metadata", lambda _source: {})
    suffix = uuid.uuid4().hex
    secret = f"title-secret-{suffix}"
    source = (
        f"https://media.example.com/pending/{suffix}"
        f"?access_token={secret}&expires=9999999999#player"
    )
    response = client.post("/api/projects", json={
        "source": source,
        "source_type": "url",
    })

    assert response.status_code == 200, response.text
    project = response.json()
    safe_source = "https://media.example.com"
    assert project["title"] == f"(pending: {safe_source[:60]})"
    assert secret not in project["title"]
    assert f"/pending/{suffix}" not in project["title"]
    assert "?" not in project["title"]


def test_pipeline_failure_rethrow_and_job_error_are_fully_sanitized():
    suffix = uuid.uuid4().hex
    secret = f"pipeline-secret-{suffix}"
    source_url = f"https://media.example.com/failure/{suffix}"
    safe_origin = "https://media.example.com"
    # Keep the secret near the end of a query longer than the traceback limit.
    # If code truncates before redacting, the retained tail no longer has a URL
    # scheme and the query credential leaks into the durable job error.
    signed_url = (
        f"HTTPS://media.example.com/failure/{suffix}?padding="
        f"{'x' * 2500}&access_token={secret}#viewer"
    )
    with get_session() as session:
        project = Project(
            slug=f"signed-pipeline-{suffix}",
            title="Signed pipeline failure fixture",
            source=source_url,
            source_type="url",
        )
        session.add(project)
        session.commit()
        session.refresh(project)
        job = Job(project_id=project.id, task="signed_failure")
        session.add(job)
        session.commit()
        session.refresh(job)
        project_id = project.id
        job_id = job.id

    @pipeline_task
    def signed_failure(_job_id: int, _project_id: int):
        raise ValueError(f"provider rejected {signed_url}")

    with pytest.raises(PipelineTaskError) as raised:
        signed_failure(job_id, project_id)

    with get_session() as session:
        durable_error = session.get(Job, job_id).error

    serialized_error = pickle.dumps(raised.value)
    rendered_error = "".join(
        traceback.format_exception(type(raised.value), raised.value,
                                   raised.value.__traceback__)
    )
    for text in (str(raised.value), rendered_error, durable_error):
        assert secret not in text
        assert "access_token=" not in text
        assert safe_origin in text
        assert f"/failure/{suffix}" not in text
    assert secret.encode() not in serialized_error
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
