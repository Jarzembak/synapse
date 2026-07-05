"""Integration tests through the FastAPI app: library writes, FTS search, tags."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.db import get_session
from app import library
from app.models import Project


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def seed_artifact(title: str, body: str, type: str = "summary", tags=None):
    with get_session() as session:
        project = Project(slug="demo", title="Demo video", source="x", source_type="url")
        existing = session.exec(
            __import__("sqlmodel").select(Project).where(Project.slug == "demo")
        ).first()
        if existing:
            project = existing
        else:
            session.add(project)
            session.commit()
            session.refresh(project)
        art = library.write_artifact(
            session, project_id=project.id, project_slug="demo",
            type=type, title=title, body=body,
            rel_path=f"projects/demo/{library.make_slug(title)}.md",
        )
        if tags:
            library.apply_tags(session, art, tags)
        return art.id


def test_health_and_seeded_tags(client):
    assert client.get("/api/health").json() == {"ok": True}
    tags = client.get("/api/tags").json()
    assert any(t["name"] == "nmap" for t in tags)


def test_project_create_and_steps(client):
    r = client.post("/api/projects", json={
        "source": "https://example.com/v?x=1", "source_type": "url", "title": "Test Video",
    })
    assert r.status_code == 200, r.text
    assert r.json()["slug"] == "test-video"
    steps = client.get("/api/projects/steps").json()
    assert steps[0]["name"] == "ingest" and steps[-1]["name"] == "mindmap"
    assert steps[1]["name"] == "download"
    detail = client.get(f"/api/projects/{r.json()['id']}").json()
    assert len(detail["steps"]) == 13


def test_fts_search_and_filters(client):
    aid = seed_artifact("Wireguard notes", "Set up wireguard with wg genkey and firewall rules.",
                        tags=["networking"])
    hits = client.get("/api/library/search", params={"q": "genkey"}).json()
    assert [h["id"] for h in hits] == [aid]
    assert hits[0]["tags"] == ["networking"]
    # tag filter
    assert client.get("/api/library/search", params={"tag": "networking"}).json()
    assert client.get("/api/library/search", params={"tag": "kubernetes"}).json() == []
    # type filter
    assert client.get("/api/library/search", params={"q": "genkey", "type": "transcript"}).json() == []


def test_artifact_view_and_tag_edit(client):
    aid = seed_artifact("Tag edit target", "some body")
    r = client.get(f"/api/artifacts/{aid}").json()
    assert r["body"] == "some body"
    r = client.put(f"/api/artifacts/{aid}/tags", json={"tags": ["Ansible", "linux"]})
    assert sorted(r.json()["tags"]) == ["ansible", "linux"]
    # frontmatter on disk reflects the edit
    meta, _ = library.read_doc(f"projects/demo/{library.make_slug('Tag edit target')}.md")
    assert sorted(meta["tags"]) == ["ansible", "linux"]


def test_tag_rename_propagates(client):
    aid = seed_artifact("Rename target", "body", tags=["oldname"])
    tag = next(t for t in client.get("/api/tags").json() if t["name"] == "oldname")
    r = client.put(f"/api/tags/{tag['id']}", json={"name": "newname"})
    assert r.status_code == 200
    assert "newname" in client.get(f"/api/artifacts/{aid}").json()["tags"]


def test_glossary_roundtrip(client):
    client.put("/api/settings/glossary", json={"terms": ["Fortigate", "  RKE2 ", "Fortigate"]})
    assert client.get("/api/settings/glossary").json()["terms"] == ["Fortigate", "RKE2"]


def test_download_prefs_roundtrip(client):
    assert client.get("/api/settings/download").json() == {"max_height": 1080}
    assert client.put("/api/settings/download", json={"max_height": 0}).status_code == 200
    assert client.get("/api/settings/download").json() == {"max_height": 0}
    assert client.put("/api/settings/download", json={"max_height": -1}).status_code == 400
    client.put("/api/settings/download", json={"max_height": 1080})  # restore


def test_media_prefix_serving(client):
    from app.config import settings as app_settings

    media_file = app_settings.media_dir / "demo" / "source_audio.m4a"
    media_file.parent.mkdir(parents=True, exist_ok=True)
    media_file.write_bytes(b"fake-m4a-bytes")

    with get_session() as session:
        from sqlmodel import select
        project = session.exec(select(Project).where(Project.slug == "demo")).first()
        art = library.write_artifact(
            session, project_id=project.id if project else None, project_slug="demo",
            type="source_audio", title="Source audio — Demo",
            body="archived copy", rel_path="projects/demo/source_audio.md",
            media_rel="media:demo/source_audio.m4a",
        )
        aid = art.id

    r = client.get(f"/api/media/{aid}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/mp4")
    assert r.content == b"fake-m4a-bytes"


def test_quickref_detail_includes_sources(client):
    """Regression: detail endpoint must serialize like the list endpoint —
    a missing 'sources' key white-screened the QuickRefs page."""
    from sqlmodel import select
    from app.models import QuickRef, QuickRefSource

    seed_artifact("qr seed", "ensures the demo project exists")
    library._write_doc("tools/testtool.md", {"title": "testtool"}, "quick ref body")
    with get_session() as session:
        project = session.exec(select(Project).where(Project.slug == "demo")).first()
        ref = QuickRef(kind="tool", slug="testtool", title="testtool",
                       path="tools/testtool.md", aliases='["test tool"]')
        session.add(ref)
        session.commit()
        session.refresh(ref)
        session.add(QuickRefSource(quickref_id=ref.id, project_id=project.id))
        session.commit()
        rid, pid, ptitle = ref.id, project.id, project.title

    detail = client.get(f"/api/quickrefs/{rid}").json()
    assert detail["ref"]["sources"] == [{"id": pid, "title": ptitle}]
    assert detail["ref"]["aliases"] == ["test tool"]
    listed = next(r for r in client.get("/api/quickrefs").json() if r["slug"] == "testtool")
    assert listed["sources"] == detail["ref"]["sources"]


def test_prompt_editor_roundtrip(client):
    prompts = client.get("/api/settings/prompts").json()
    assert "deepdive" in prompts and not prompts["deepdive"]["modified"]
    default = prompts["deepdive"]["value"]

    r = client.put("/api/settings/prompts/deepdive", json={"value": "custom prompt"})
    assert r.status_code == 200
    after = client.get("/api/settings/prompts").json()["deepdive"]
    assert after["modified"] and after["value"] == "custom prompt"

    # saving the default text back clears the override
    client.put("/api/settings/prompts/deepdive", json={"value": default})
    assert not client.get("/api/settings/prompts").json()["deepdive"]["modified"]

    r = client.delete("/api/settings/prompts/deepdive")
    assert r.json()["default"] == default
    assert client.put("/api/settings/prompts/nope", json={"value": "x"}).status_code == 400


def test_params_and_advanced_roundtrip(client):
    r = client.put("/api/settings/params/summarize",
                   json={"temperature": 0.2, "max_tokens": 2048})
    assert r.status_code == 200
    assert client.get("/api/settings/params").json()["summarize"] == \
        {"temperature": 0.2, "max_tokens": 2048}
    from app.llm import resolve_params
    assert resolve_params("summarize") == (0.2, 2048)
    client.put("/api/settings/params/summarize", json={})

    adv = client.get("/api/settings/advanced").json()
    assert adv["groups"]["pipeline"]["chunk_chars"] == 24000
    r = client.put("/api/settings/advanced/pipeline",
                   json={"values": {"chunk_chars": 12000, "bogus_key": 1}})
    assert r.status_code == 200
    got = client.get("/api/settings/advanced").json()["groups"]["pipeline"]
    assert got["chunk_chars"] == 12000
    assert "bogus_key" not in got
    client.put("/api/settings/advanced/pipeline", json={"values": {}})
    assert client.put("/api/settings/advanced/nope", json={"values": {}}).status_code == 400


def test_cloud_settings_masking(client):
    r = client.put("/api/settings/cloud", json={
        "provider": "s3",
        "config": {"endpoint": "https://minio.local:9000", "bucket": "synapse",
                   "access_key_id": "AK", "secret_access_key": "supersecret"},
        "remote_base": "synapse", "auto": False,
    })
    assert r.status_code == 200
    got = client.get("/api/settings/cloud").json()
    assert got["config"]["secret_access_key"] == "•set•"      # masked
    assert "supersecret" not in str(got)
    assert got["config"]["endpoint"] == "https://minio.local:9000"

    # writing back the mask keeps the stored secret
    client.put("/api/settings/cloud", json={
        "provider": "s3",
        "config": {**got["config"]}, "remote_base": "synapse", "auto": False,
    })
    from app.settings_store import get_setting
    assert get_setting("cloud.config")["secret_access_key"] == "supersecret"
    assert client.put("/api/settings/cloud", json={
        "provider": "nope", "config": {}, "remote_base": "x", "auto": False,
    }).status_code == 400


def test_rclone_config_builder():
    from app.tasks.cloud import build_config

    s3 = build_config("s3", {"endpoint": "https://x", "access_key_id": "a",
                             "secret_access_key": "s", "region": "us-east-1"})
    assert "type = s3" in s3 and "endpoint = https://x" in s3 and "region = us-east-1" in s3
    dav = build_config("webdav", {"url": "https://nc/remote.php/dav/files/lee",
                                  "user": "lee", "_obscured_password": "xyz"})
    assert "type = webdav" in dav and "vendor = nextcloud" in dav and "pass = xyz" in dav
    drv = build_config("drive", {"token": '{"access_token":"t"}'})
    assert "type = drive" in drv and "scope = drive" in drv
    import pytest as _pytest
    with _pytest.raises(ValueError):
        build_config("nope", {})


def test_quickref_concept_kind(client):
    """concept docs land in concepts/ with the quickref_concept artifact type."""
    from sqlmodel import select
    from app.models import QuickRef

    library._write_doc("concepts/least-privilege.md", {"title": "least privilege"}, "body")
    with get_session() as session:
        ref = QuickRef(kind="concept", slug="least-privilege", title="least privilege",
                       path="concepts/least-privilege.md", aliases="[]")
        session.add(ref)
        session.commit()
        art = library.write_artifact(
            session, project_id=None, project_slug=None, type="quickref_concept",
            title="least privilege", body="body",
            rel_path="concepts/least-privilege.md",
        )
        library.apply_tags(session, art, ["hardening"])
    refs = client.get("/api/quickrefs?kind=concept").json()
    hit = next(r for r in refs if r["slug"] == "least-privilege")
    assert hit["tags"] == ["hardening"]
    assert hit["updated"] is not None


def test_tag_project_propagates_and_caches(client, monkeypatch):
    """Project-level tagging: one LLM call from the richest doc, propagated to
    all project artifacts (metadata artifacts inherit rather than re-tag)."""
    from app.tasks.generate import tag_project
    from app import llm
    from app.models import Artifact

    with get_session() as session:
        project = Project(slug="tagdemo", title="Tag demo", source="x", source_type="url")
        session.add(project)
        session.commit()
        session.refresh(project)
        pid = project.id
        t = library.write_artifact(
            session, project_id=pid, project_slug="tagdemo", type="transcript",
            title="Transcript — Tag demo", body="[00:00:01] all about wireguard tunnels",
        )
        v = library.write_artifact(
            session, project_id=pid, project_slug="tagdemo", type="source_video",
            title="Source video — Tag demo", body="Archived video download.",
        )
        tid, vid = t.id, v.id

    calls = []
    # "Wireguard"/"wireguard" slugify identically — regression for the
    # IntegrityError this used to raise on the artifacttag primary key
    monkeypatch.setattr(llm, "complete_json",
                        lambda *a, **k: (calls.append(1) or
                                         {"tags": ["Wireguard", "wireguard", "networking"]}))
    tag_project(pid)
    assert len(calls) == 1

    with get_session() as session:
        assert library.current_tags(session, tid) == ["networking", "wireguard"]
        # metadata artifact inherited the same set, no independent LLM call
        assert library.current_tags(session, vid) == ["networking", "wireguard"]

    # second run: cached marker → no new LLM call, tags intact
    monkeypatch.setattr(llm, "complete_json",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not re-call LLM")))
    tag_project(pid)
    with get_session() as session:
        assert library.current_tags(session, vid) == ["networking", "wireguard"]

    # manual edits survive later propagations
    with get_session() as session:
        art = session.get(Artifact, vid)
        library.apply_tags(session, art, ["my-custom-tag"])
    tag_project(pid)
    with get_session() as session:
        assert library.current_tags(session, vid) == ["my-custom-tag"]
        assert library.current_tags(session, tid) == ["networking", "wireguard"]


def test_logging_and_tail_endpoint(client):
    """Central logging writes a rotating file per service; /api/logs tails it."""
    import logging

    logging.getLogger("synapse.test").info("hello from the test suite")
    for h in logging.getLogger().handlers:
        h.flush()

    listed = client.get("/api/logs").json()
    assert listed["file_logging"] is True
    assert "test" in listed["services"]

    tail = client.get("/api/logs/test?lines=50").json()
    assert any("hello from the test suite" in line for line in tail["lines"])

    assert client.get("/api/logs/nosuchservice").status_code == 404
    assert client.get("/api/logs/..%2Fetc").status_code in (400, 404)


def test_combined_title():
    from app.tasks.ingest import combined_title

    assert combined_title({"title": "Kubernetes in 100s", "uploader": "Fireship"}) \
        == "Fireship - Kubernetes in 100s"
    assert combined_title({"title": "Solo talk", "uploader": ""}) == "Solo talk"
    assert combined_title({"title": "", "uploader": "X"}) is None
    assert combined_title({}) is None


def test_create_auto_names_url(client, monkeypatch):
    # the create endpoint does `from ..tasks.ingest import fetch_url_metadata`
    # at call time, so patching the module attribute takes effect
    from app.tasks import ingest as ingest_mod
    monkeypatch.setattr(ingest_mod, "fetch_url_metadata",
                        lambda url, project_slug=None: {"title": "Recon Basics",
                                                        "uploader": "HackerTalks"})
    r = client.post("/api/projects", json={
        "source": "https://youtube.com/watch?v=abc", "source_type": "url",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "HackerTalks - Recon Basics"
    assert body["slug"] == "hackertalks-recon-basics"

    # explicit title wins, no metadata fetch
    monkeypatch.setattr(ingest_mod, "fetch_url_metadata",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch")))
    r2 = client.post("/api/projects", json={
        "source": "https://youtube.com/watch?v=xyz", "source_type": "url",
        "title": "My Title",
    })
    assert r2.json()["title"] == "My Title"


def test_rename_project(client):
    r = client.post("/api/projects", json={
        "source": "https://e.com/r", "source_type": "url", "title": "Old Name"})
    pid = r.json()["id"]
    slug = r.json()["slug"]

    resp = client.patch(f"/api/projects/{pid}", json={"title": "New Name"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "New Name"
    assert resp.json()["slug"] == slug  # slug unchanged → no orphaned files

    assert client.patch(f"/api/projects/{pid}", json={"title": "  "}).status_code == 400
    assert client.patch("/api/projects/99999", json={"title": "x"}).status_code == 404


def test_delete_project_removes_files(client):
    from app.config import settings

    r = client.post("/api/projects", json={
        "source": "https://e.com/d", "source_type": "url", "title": "Doomed"})
    pid = r.json()["id"]
    slug = r.json()["slug"]

    # give it an on-disk artifact + a media file
    with get_session() as session:
        library.write_artifact(
            session, project_id=pid, project_slug=slug, type="summary",
            title="Summary — Doomed", body="body")
    media_file = settings.media_dir / slug / "source_video.mp4"
    media_file.parent.mkdir(parents=True, exist_ok=True)
    media_file.write_bytes(b"x")
    proj_dir = settings.library_dir / "projects" / slug
    assert proj_dir.exists()

    resp = client.delete(f"/api/projects/{pid}")
    assert resp.status_code == 200
    assert not proj_dir.exists()
    assert not (settings.media_dir / slug).exists()
    assert client.get(f"/api/projects/{pid}").status_code == 404
    assert client.delete(f"/api/projects/{pid}").status_code == 404


def test_step_graph_is_sound():
    """Every dependency is a real step and the graph has no cycles."""
    from app.tasks.orchestrate import HARD_DEPS, RUN_DEPS, STEP_NAMES, STEP_OUTPUT

    for deps in (HARD_DEPS, RUN_DEPS):
        assert set(deps) == STEP_NAMES
        for step, ds in deps.items():
            assert ds <= STEP_NAMES, f"{step} depends on unknown step(s) {ds - STEP_NAMES}"
        # topological check: repeatedly remove steps with no remaining deps
        remaining = {s: set(d) for s, d in deps.items()}
        while remaining:
            ready = [s for s, d in remaining.items() if not d]
            assert ready, f"dependency cycle among {sorted(remaining)}"
            for s in ready:
                del remaining[s]
            for d in remaining.values():
                d.difference_update(ready)
    assert set(STEP_OUTPUT) == STEP_NAMES


def test_transitive_dependents():
    from app.tasks.orchestrate import HARD_DEPS, RUN_DEPS, transitive_dependents

    downstream = transitive_dependents("deepdive_claude", RUN_DEPS)
    assert "merge" in downstream and "tts" in downstream and "mindmap" in downstream
    assert "deepdive_gemini" not in downstream  # independent branch survives
    assert "trim" not in downstream

    # failure-skip uses HARD deps: a failed correction pass must NOT skip the
    # deep dives — they fall back to the raw transcript
    hard_downstream = transitive_dependents("correct", HARD_DEPS)
    assert hard_downstream == set()


def test_dep_satisfied_soft_vs_hard_failures():
    from app.tasks.orchestrate import dep_satisfied

    # correct failed → summarize (soft dep on correct) may still launch
    assert dep_satisfied("summarize", "correct",
                         done=set(), pending=set(), running=set(), failed={"correct"})
    # merge hard-requires the deep dives → a failed one blocks it
    assert not dep_satisfied("merge", "deepdive_claude",
                             done=set(), pending=set(), running=set(),
                             failed={"deepdive_claude"})
    # still pending/running → wait
    assert not dep_satisfied("summarize", "correct",
                             done=set(), pending={"correct"}, running=set(), failed=set())
    # finished before this run started → satisfied
    assert dep_satisfied("summarize", "correct",
                         done=set(), pending=set(), running=set(), failed=set())


def test_prerequisite_gating(client):
    r = client.post("/api/projects", json={
        "source": "https://example.com/gating", "source_type": "url", "title": "Gating demo",
    })
    pid = r.json()["id"]
    detail = client.get(f"/api/projects/{pid}").json()
    steps = {s["name"]: s for s in detail["steps"]}

    assert not steps["ingest"]["blocked"]
    assert not steps["download"]["blocked"]
    assert steps["transcribe"]["blocked"]
    assert steps["transcribe"]["missing"] == ["Ingest media"]
    assert steps["merge"]["missing"] == ["Deep dive (Claude)", "Deep dive (Gemini)"]
    assert steps["tts"]["missing"] == ["Podcast script"]
    assert detail["remaining"] == 13  # url project: every step applicable
    assert detail["run_all_active"] is False

    # manual run of a blocked step is refused with the prerequisite named
    resp = client.post(f"/api/projects/{pid}/run/merge")
    assert resp.status_code == 409
    assert "Deep dive" in resp.json()["detail"]

    # local project: download not applicable
    r2 = client.post("/api/projects", json={
        "source": "talks/x.mp4", "source_type": "local", "title": "Gating local",
    })
    d2 = client.get(f"/api/projects/{r2.json()['id']}").json()
    dl = next(s for s in d2["steps"] if s["name"] == "download")
    assert dl["not_applicable"] is True
    assert d2["remaining"] == 12


def test_run_all_endpoint(client, monkeypatch):
    from app.tasks.celery_app import celery

    class FakeResult:
        id = "fake-celery-id"

    sent = []
    monkeypatch.setattr(celery, "send_task",
                        lambda name, args=None, **k: (sent.append((name, args)) or FakeResult()))

    r = client.post("/api/projects", json={
        "source": "https://example.com/runall", "source_type": "url", "title": "Runall demo",
    })
    pid = r.json()["id"]
    resp = client.post(f"/api/projects/{pid}/run_all")
    assert resp.status_code == 200
    assert sent == [("run_all", [resp.json()["id"], pid])]

    # a queued/running job blocks a second run_all
    resp2 = client.post(f"/api/projects/{pid}/run_all")
    assert resp2.status_code == 409

    # ... and run-all is global: another project can't start one concurrently
    r3 = client.post("/api/projects", json={
        "source": "https://example.com/runall2", "source_type": "url", "title": "Runall two",
    })
    resp3 = client.post(f"/api/projects/{r3.json()['id']}/run_all")
    assert resp3.status_code == 409
    assert "one at a time" in resp3.json()["detail"]

    # recovery hatch: reset stuck jobs, then run_all is allowed again
    reset = client.post(f"/api/projects/{pid}/reset_jobs")
    assert reset.json()["reset"] == 1
    resp4 = client.post(f"/api/projects/{pid}/run_all")
    assert resp4.status_code == 200


def test_model_override(client):
    r = client.put("/api/settings/models/summarize",
                   json={"provider": "anthropic", "model": "claude-haiku-4-5"})
    assert r.status_code == 200
    models = client.get("/api/settings/models").json()["functions"]
    assert models["summarize"] == {"provider": "anthropic", "model": "claude-haiku-4-5"}
    assert client.put("/api/settings/models/nope",
                      json={"provider": "x", "model": "y"}).status_code == 400
