from __future__ import annotations

import json
import types

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import get_session
from app.local_model_safety import PROFILE_KEY
from app.main import app
from app.models import Job
from app.settings_store import get_setting, set_setting
from app.trusted_origin import TRUSTED_ORIGIN_HEADER, TRUSTED_REQUEST_HOST_HEADER


TRUSTED_HEADERS = {
    TRUSTED_ORIGIN_HEADER: "http://localhost:8080",
    TRUSTED_REQUEST_HOST_HEADER: "localhost:8080",
}
GIB = 1024 ** 3


@pytest.fixture()
def client():
    with TestClient(app, headers=TRUSTED_HEADERS) as value:
        yield value


@pytest.fixture(autouse=True)
def reset_model_profiles():
    from app import local_model_safety as safety

    keys = (
        PROFILE_KEY,
        "model.correct",
        "search.semantic_enabled",
        "search.embedding_provider",
        "search.embedding_model",
        "repository.local_model",
        "repository.scan",
    )
    previous = {key: get_setting(key) for key in keys}
    set_setting(PROFILE_KEY, None)
    safety.clear_inventory_cache()
    yield
    for key, value in previous.items():
        set_setting(key, value)
    safety.clear_inventory_cache()


def _row(
    *,
    name: str = "safe:latest",
    digest: str = "a" * 64,
    size: int = 2 * GIB,
    capabilities: list[str] | None = None,
    context: int = 131_072,
) -> dict:
    return {
        "name": name,
        "model": name,
        "digest": digest,
        "size_bytes": size,
        "modified_at": "",
        "details": {
            "family": "fixture",
            "families": ["fixture"],
            "parameter_size": "4B",
            "quantization_level": "Q4_K_M",
        },
        "capabilities": capabilities or ["completion"],
        "model_info": {},
        "native_context_tokens": context,
    }


def _inventory(row: dict) -> dict:
    return {
        "configured": True,
        "ok": True,
        "local": True,
        "detail": "",
        "models": [row],
        "running_models": [],
    }


def _resources(*, available: bool = True) -> dict:
    if not available:
        return {
            "available": False,
            "reason": "the Ollama host's resources are not visible to Synapse",
            "ram_total_bytes": 0,
            "ram_available_bytes": 0,
            "vram_total_bytes": 0,
            "vram_free_bytes": 0,
        }
    return {
        "available": True,
        "reason": "",
        "ram_total_bytes": 16 * GIB,
        "ram_available_bytes": 12 * GIB,
        "vram_total_bytes": 8 * GIB,
        "vram_free_bytes": 7 * GIB,
    }


def _install_fixture(monkeypatch, row: dict, *, resources: dict | None = None):
    from app import local_model_safety as safety

    monkeypatch.setattr(
        safety,
        "_fetch_inventory",
        lambda refresh=False: _inventory(row),
    )
    monkeypatch.setattr(
        safety,
        "runtime_resources",
        lambda: resources or _resources(),
    )


def test_structured_catalog_and_persisted_annotation(client, monkeypatch):
    row = _row()
    _install_fixture(monkeypatch, row)

    response = client.get("/api/settings/ollama/models")
    assert response.status_code == 200
    payload = response.json()
    assert payload["models"][0]["digest"] == row["digest"]
    assert payload["models"][0]["size_bytes"] == 2 * GIB
    assert payload["models"][0]["capabilities"] == ["completion"]
    assert payload["models"][0]["assessment"]["tier"] == "recommended"
    assert payload["models"][0]["restricted_assessment"]["requested_context_tokens"] == 65_536

    saved = client.put(
        "/api/settings/ollama/annotation",
        json={
            "model": row["name"],
            "label": "Fast local default",
            "notes": "Use for transcript cleanup.",
            "labels": ["transcripts", "preferred", "Transcripts"],
        },
    )
    assert saved.status_code == 200
    assert saved.json()["annotation"] == {
        "label": "Fast local default",
        "notes": "Use for transcript cleanup.",
        "labels": ["transcripts", "preferred"],
    }
    assert client.get("/api/settings/ollama/models").json()["models"][0][
        "annotation"
    ]["label"] == "Fast local default"


def test_capability_mismatches_fail_assignment(client, monkeypatch):
    embedding = _row(
        name="embed:latest",
        capabilities=["embedding"],
    )
    _install_fixture(monkeypatch, embedding)
    response = client.put(
        "/api/settings/models/correct",
        json={"provider": "ollama", "model": embedding["name"]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "ollama_capability_mismatch"

    completion = _row(
        name="chat:latest",
        digest="b" * 64,
        capabilities=["completion"],
    )
    _install_fixture(monkeypatch, completion)
    response = client.put(
        "/api/settings/search",
        json={
            "semantic_enabled": True,
            "embedding_provider": "ollama",
            "embedding_model": completion["name"],
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "ollama_capability_mismatch"


def test_blocked_model_requires_digest_and_explicit_acknowledgement(
    client,
    monkeypatch,
):
    huge = _row(name="huge:671b", size=120 * GIB, digest="c" * 64)
    _install_fixture(monkeypatch, huge)

    response = client.put(
        "/api/settings/models/correct",
        json={"provider": "ollama", "model": huge["name"]},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ollama_model_blocked"
    assert response.json()["detail"]["assessment"]["acknowledged"] is False

    wrong = client.post(
        "/api/settings/ollama/acknowledge",
        json={
            "model": huge["name"],
            "digest": huge["digest"],
            "confirmation": "huge",
            "reason": "Dedicated test host.",
        },
    )
    assert wrong.status_code == 422

    short_reason = client.post(
        "/api/settings/ollama/acknowledge",
        json={
            "model": huge["name"],
            "digest": huge["digest"],
            "confirmation": huge["name"],
            "reason": "testing",
        },
    )
    assert short_reason.status_code == 422
    assert "at least 10 characters" in short_reason.text

    acknowledged = client.post(
        "/api/settings/ollama/acknowledge",
        json={
            "model": huge["name"],
            "digest": huge["digest"],
            "confirmation": huge["name"],
            "reason": "Dedicated test host.",
        },
    )
    assert acknowledged.status_code == 200
    assert acknowledged.json()["assessment"]["acknowledged"] is True
    assert client.put(
        "/api/settings/models/correct",
        json={"provider": "ollama", "model": huge["name"]},
    ).status_code == 200

    removed = client.delete(
        f"/api/settings/ollama/acknowledgements/{huge['digest']}"
    )
    assert removed.json() == {"ok": True, "removed": True}
    assert client.put(
        "/api/settings/models/correct",
        json={"provider": "ollama", "model": huge["name"]},
    ).status_code == 409


def test_repository_model_assignments_use_adaptive_admission_context(
    client,
    monkeypatch,
):
    from app.routers import repositories

    captured = []
    monkeypatch.setattr(
        repositories,
        "ensure_model_safe",
        lambda model, **kwargs: captured.append((model, kwargs)) or {},
    )
    response = client.put(
        "/api/repositories/settings",
        json={
            "local_model": "repository-safe:latest",
            "reduce_model": "repository-reducer-safe:latest",
        },
    )
    assert response.status_code == 200
    assert captured == [
        (
            "repository-safe:latest",
            {"role": "completion", "requested_context": 32_768},
        ),
        (
            "repository-reducer-safe:latest",
            {"role": "completion", "requested_context": 32_768},
        ),
    ]


def test_remote_ollama_does_not_use_local_resource_numbers(monkeypatch):
    from app import local_model_safety as safety

    huge = _row(name="remote-huge:latest", size=120 * GIB)
    _install_fixture(monkeypatch, huge, resources=_resources(available=False))
    result = safety.ensure_model_safe(
        huge["name"],
        role="completion",
        requested_context=65_536,
    )
    assert result["assessment"]["tier"] == "unavailable"
    assert "not visible" in result["assessment"]["message"]


def test_endpoint_is_local_excludes_host_docker_internal():
    from app import local_model_safety as safety

    assert safety._endpoint_is_local("http://ollama:11434")
    assert safety._endpoint_is_local("http://localhost:11434")
    assert safety._endpoint_is_local("http://127.0.0.1:11434")
    assert not safety._endpoint_is_local("http://host.docker.internal:11434")
    assert not safety._endpoint_is_local("http://10.0.0.5:11434")


def test_host_gateway_ollama_degrades_to_unavailable_instead_of_blocking(
    monkeypatch,
):
    """A daemon reached via host.docker.internal must not be scored with this
    container's RAM/GPU: Docker Desktop exposes the VM's memory and no
    nvidia-smi, which blocked models that fit comfortably on the host GPU."""
    from app import local_model_safety as safety

    monkeypatch.setattr(
        safety.settings, "ollama_base_url", "http://host.docker.internal:11434")
    huge = _row(name="host-gpu-model:latest", size=120 * GIB)
    monkeypatch.setattr(
        safety, "_fetch_inventory", lambda refresh=False: _inventory(huge))

    assert safety.runtime_resources()["available"] is False

    result = safety.ensure_model_safe(
        huge["name"], role="completion", requested_context=65_536)
    assert result["available"] is True
    assert result["assessment"]["tier"] == "unavailable"


def test_catalog_reports_residency_and_unload_releases_only_selected_model(
    monkeypatch,
):
    from app import local_model_safety as safety

    row = _row(name="resident:latest")
    inventory = _inventory(row)
    inventory["running_models"] = [{
        "name": row["name"],
        "size": 3 * GIB,
        "size_vram": 2 * GIB,
        "context_length": 16_384,
        "expires_at": "2026-07-26T12:00:00Z",
    }]
    monkeypatch.setattr(
        safety,
        "_fetch_inventory",
        lambda refresh=False: inventory,
    )
    monkeypatch.setattr(safety, "runtime_resources", _resources)
    resident = safety.model_catalog()["models"][0]["residency"]
    assert resident["loaded"] is True
    assert resident["processor"] == "hybrid"
    assert resident["size_vram_bytes"] == 2 * GIB

    posted = []

    class Response:
        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, *, json):
            posted.append((url, json))
            return Response()

    monkeypatch.setattr(safety.httpx, "Client", Client)
    assert safety.unload_model(row["name"]) == {
        "ok": True,
        "model": row["name"],
    }
    assert posted == [(
        f"{safety.settings.ollama_base_url.rstrip('/')}/api/generate",
        {"model": row["name"], "keep_alive": 0},
    )]


def test_resident_model_allocation_is_not_charged_twice(monkeypatch):
    from app import local_model_safety as safety

    row = _row(name="already-loaded:latest", size=5 * GIB)
    inventory = _inventory(row)
    inventory["running_models"] = [{
        "name": row["name"],
        "size": 5 * GIB,
        "size_vram": 5 * GIB,
        "context_length": 8_192,
    }]
    constrained = {
        "available": True,
        "reason": "",
        "ram_total_bytes": 16 * GIB,
        "ram_available_bytes": 3 * GIB,
        "vram_total_bytes": 8 * GIB,
        "vram_free_bytes": 2 * GIB,
    }
    monkeypatch.setattr(
        safety, "_fetch_inventory", lambda refresh=False: inventory)
    monkeypatch.setattr(safety, "runtime_resources", lambda: constrained)

    raw = safety.resource_assessment(
        row, requested_context=8_192, resources=constrained)
    assert raw["tier"] == "blocked"
    checked = safety.ensure_model_safe(
        row["name"], role="completion", requested_context=8_192)
    assert checked["assessment"]["tier"] != "blocked"


def test_cached_ghost_resident_does_not_create_admission_capacity(monkeypatch):
    from app import local_model_safety as safety

    row = _row(name="ghost:latest", size=5 * GIB)
    cached = _inventory(row)
    cached["running_models"] = [{
        "name": row["name"],
        "size": 5 * GIB,
        "size_vram": 5 * GIB,
        "context_length": 8_192,
    }]
    refreshed = _inventory(row)
    constrained = {
        "available": True,
        "reason": "",
        "ram_total_bytes": 16 * GIB,
        "ram_available_bytes": 3 * GIB,
        "vram_total_bytes": 8 * GIB,
        "vram_free_bytes": 2 * GIB,
    }

    monkeypatch.setattr(
        safety,
        "_fetch_inventory",
        lambda refresh=False: refreshed if refresh else cached,
    )
    monkeypatch.setattr(safety, "runtime_resources", lambda: constrained)

    with pytest.raises(safety.LocalModelSafetyError) as raised:
        safety.ensure_model_safe(
            row["name"], role="completion", requested_context=8_192)

    assert raised.value.code == "ollama_model_blocked"
    assert raised.value.assessment["resident_transition"] == {
        "required": False,
        "resident_models": [],
        "replaced_models": [],
        "reclaimable_ram_bytes": 0,
        "reclaimable_vram_bytes": 0,
    }


def test_different_ollama_resident_is_reclaimable_for_model_transition(
    monkeypatch,
):
    from app import local_model_safety as safety

    reducer = _row(name="reducer:latest", size=4 * GIB)
    inventory = _inventory(reducer)
    inventory["running_models"] = [{
        "name": "mapper:latest",
        "size": 6 * GIB,
        "size_vram": 5 * GIB,
        "context_length": 16_384,
    }]
    constrained = {
        "available": True,
        "reason": "",
        "ram_total_bytes": 16 * GIB,
        "ram_available_bytes": 3 * GIB,
        "vram_total_bytes": 8 * GIB,
        "vram_free_bytes": 1 * GIB,
    }
    refreshes: list[bool] = []

    def inventory_fixture(*, refresh=False):
        refreshes.append(refresh)
        return inventory

    monkeypatch.setattr(safety, "_fetch_inventory", inventory_fixture)
    monkeypatch.setattr(safety, "runtime_resources", lambda: constrained)

    raw = safety.resource_assessment(
        reducer, requested_context=16_384, resources=constrained)
    assert raw["tier"] == "blocked"

    checked = safety.ensure_model_safe(
        reducer["name"], role="completion", requested_context=16_384)

    assert refreshes == [False, True]
    assert checked["assessment"]["tier"] != "blocked"
    assert checked["resident_transition"] == {
        "required": True,
        "resident_models": ["mapper:latest"],
        "replaced_models": ["mapper:latest"],
        "reclaimable_ram_bytes": 1 * GIB,
        "reclaimable_vram_bytes": 5 * GIB,
    }


def test_oversized_model_remains_blocked_after_resident_reclaim(monkeypatch):
    from app import local_model_safety as safety

    oversized = _row(name="oversized:latest", size=400 * GIB)
    inventory = _inventory(oversized)
    inventory["running_models"] = [{
        "name": "mapper:latest",
        "size": 6 * GIB,
        "size_vram": 5 * GIB,
        "context_length": 16_384,
    }]
    constrained = {
        "available": True,
        "reason": "",
        "ram_total_bytes": 16 * GIB,
        "ram_available_bytes": 3 * GIB,
        "vram_total_bytes": 8 * GIB,
        "vram_free_bytes": 1 * GIB,
    }
    monkeypatch.setattr(
        safety, "_fetch_inventory", lambda refresh=False: inventory)
    monkeypatch.setattr(safety, "runtime_resources", lambda: constrained)

    with pytest.raises(safety.LocalModelSafetyError) as raised:
        safety.ensure_model_safe(
            oversized["name"], role="completion", requested_context=16_384)

    assert raised.value.code == "ollama_model_blocked"
    assert raised.value.assessment["tier"] == "blocked"
    assert raised.value.assessment["resident_transition"]["required"] is True


def test_refreshed_model_metadata_is_revalidated_before_transition(monkeypatch):
    from app import local_model_safety as safety

    stale = _row(name="mutable:latest", size=4 * GIB)
    refreshed = _row(
        name="mutable:latest",
        digest="b" * 64,
        size=4 * GIB,
        capabilities=["embedding"],
        context=2_048,
    )
    constrained = {
        "available": True,
        "reason": "",
        "ram_total_bytes": 16 * GIB,
        "ram_available_bytes": 3 * GIB,
        "vram_total_bytes": 8 * GIB,
        "vram_free_bytes": 1 * GIB,
    }

    def inventory_fixture(*, refresh=False):
        return _inventory(refreshed if refresh else stale)

    monkeypatch.setattr(safety, "_fetch_inventory", inventory_fixture)
    monkeypatch.setattr(safety, "runtime_resources", lambda: constrained)

    with pytest.raises(safety.LocalModelSafetyError) as raised:
        safety.ensure_model_safe(
            stale["name"], role="completion", requested_context=16_384)

    assert raised.value.code == "ollama_capability_mismatch"
    assert raised.value.digest == refreshed["digest"]


def test_canonical_model_handles_default_tags_and_registry_ports():
    from app import local_model_safety as safety

    simple = {"name": "team/model:latest"}
    registry = {"name": "registry:5000/team/model:latest"}

    assert safety._canonical_model([simple], "team/model") is simple
    assert safety._canonical_model([simple], "team/model:latest") is simple
    assert safety._canonical_model(
        [registry], "registry:5000/team/model") is registry


def test_embedding_transport_forces_fresh_model_safety(monkeypatch):
    from app import local_model_safety as safety
    from app import search

    admissions: list[dict] = []
    monkeypatch.setattr(search, "embedding_provider", lambda: "ollama")
    monkeypatch.setattr(
        safety,
        "ensure_model_safe",
        lambda *_args, **kwargs: admissions.append(kwargs) or {},
    )

    class Response:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"embeddings": [[1.0, 2.0]]}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def post(*_args, **_kwargs):
            return Response()

    monkeypatch.setattr(search.httpx, "Client", Client)

    assert search.embed_texts(["evidence"], model="embed:latest") == [[1.0, 2.0]]
    assert admissions == [{
        "role": "embedding",
        "requested_context": 2_048,
        "refresh": True,
    }]


def test_benchmark_is_single_bounded_json_probe_and_persisted(monkeypatch):
    from app import local_model_safety as safety

    row = _row()
    _install_fixture(monkeypatch, row)
    calls = []

    class Response:
        status_code = 200
        text = ""

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "message": {
                    "content": '{"synapse_compatibility":true}',
                }
            }

    class Client:
        def __init__(self, **kwargs):
            calls.append(("client", kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, *, json, timeout):
            calls.append(("post", url, json, timeout))
            return Response()

    monkeypatch.setattr(safety.httpx, "Client", Client)
    result = safety.run_compatibility_benchmark(row["name"])
    assert result["completion"] is True
    assert result["structured_json"] is True
    assert ("client", {"trust_env": False}) in calls
    posts = [entry for entry in calls if entry[0] == "post"]
    assert len(posts) == 1
    assert posts[0][2]["options"] == {
        "num_ctx": 2_048,
        "num_predict": 48,
        "temperature": 0,
    }
    assert posts[0][2]["keep_alive"] == 0
    # the probe must never spend its 48-token budget on hidden reasoning
    assert posts[0][2]["think"] is False
    assert safety.model_catalog()["models"][0]["benchmark"][
        "structured_json"
    ] is True


def test_benchmark_drops_think_flag_when_model_rejects_it(monkeypatch):
    """Non-thinking models 400 on the flag; one retry without it, mirroring
    llm._ollama's fallback."""
    from app import local_model_safety as safety

    row = _row()
    _install_fixture(monkeypatch, row)
    sent = []

    class Rejected:
        status_code = 400
        text = 'registry model does not support thinking'

        def raise_for_status(self):
            raise AssertionError("the rejected response must not be used")

        def json(self):
            return {"error": self.text}

    class Accepted:
        status_code = 200
        text = ""

        def raise_for_status(self):
            return None

        def json(self):
            return {"message": {"content": '{"synapse_compatibility":true}'}}

    class Client:
        def __init__(self, **kwargs):
            assert kwargs.get("trust_env") is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, _url, *, json, timeout):
            sent.append(dict(json))
            return Rejected() if "think" in json else Accepted()

    monkeypatch.setattr(safety.httpx, "Client", Client)
    result = safety.run_compatibility_benchmark(row["name"])
    assert result["completion"] is True
    assert result["structured_json"] is True
    assert len(sent) == 2
    assert sent[0]["think"] is False
    assert "think" not in sent[1]


def test_post_pull_benchmark_invalidates_inventory_and_queues_separate_job(
    monkeypatch,
):
    from app.tasks import localmodels

    row = _row(name="new:latest")
    events = []
    monkeypatch.setattr(
        localmodels,
        "clear_inventory_cache",
        lambda: events.append("cache-cleared"),
    )
    monkeypatch.setattr(
        localmodels,
        "inspect_model",
        lambda model, refresh=False: (_inventory(row), row),
    )
    sent = []
    monkeypatch.setattr(
        localmodels.celery,
        "send_task",
        lambda name, args=None, queue=None: sent.append((name, args, queue))
        or types.SimpleNamespace(id="benchmark-celery-id"),
    )

    localmodels._queue_automatic_benchmark(row["name"])
    assert events == ["cache-cleared"]
    with get_session() as session:
        job = session.exec(
            select(Job).where(
                Job.task == "ollama_benchmark",
                Job.progress == row["name"],
            ).order_by(Job.id.desc())
        ).first()
        assert job is not None
        assert json.loads(job.options) == {"automatic": True}
        assert job.celery_id == "benchmark-celery-id"
        # the probe generates on Ollama, so it must wait its turn behind any
        # running analysis instead of colliding with it
        assert job.queue == "local_llm"
        job_id = job.id
        job.status = "done"
        session.add(job)
        session.commit()
    assert sent == [("ollama_benchmark", [job_id, row["name"]], "local_llm")]
