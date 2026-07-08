"""Integration tests through the FastAPI app: library writes, FTS search, tags."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.db import get_session
from app import library
from app.models import Job, Project


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


def test_quickref_categories_builtins(client):
    cats = client.get("/api/quickrefs/categories").json()
    assert [c["key"] for c in cats[:4]] == ["tool", "technique", "concept", "technology"]
    tech = cats[3]
    assert tech["builtin"] and tech["dir"] == "technologies" and tech["plural"] == "Technologies"
    # the technology kind has a doc prompt in the registry / prompt editor
    assert "quickref_technology" in client.get("/api/settings/prompts").json()


def test_custom_category_crud(client):
    from app import categories

    new = {"label": "Framework", "plural": "Frameworks", "icon": "🧭",
           "description": "a named methodology practitioners align work to",
           "prompt": "Write a framework quick-ref for the given subject."}
    r = client.post("/api/quickrefs/categories", json=new)
    assert r.status_code == 200, r.text
    assert r.json()["key"] == "framework" and r.json()["dir"] == "frameworks"

    cats = client.get("/api/quickrefs/categories").json()
    mine = next(c for c in cats if c["key"] == "framework")
    assert not mine["builtin"] and mine["count"] == 0

    # duplicates and built-in collisions are refused
    assert client.post("/api/quickrefs/categories", json=new).status_code == 409
    assert client.post("/api/quickrefs/categories", json={
        **new, "label": "Tool", "plural": "Tools2"}).status_code == 409
    # required fields
    assert client.post("/api/quickrefs/categories", json={
        **new, "label": "Empty desc", "plural": "EDs", "description": " "}).status_code == 400

    # update: fields change, key and dir stay fixed; blanks are rejected, not
    # silently dropped
    r = client.put("/api/quickrefs/categories/framework",
                   json={"label": "Frameworkz", "description": "updated"})
    assert r.status_code == 200
    assert r.json()["label"] == "Frameworkz" and r.json()["dir"] == "frameworks"
    assert client.put("/api/quickrefs/categories/framework",
                      json={"description": "  "}).status_code == 400
    assert client.put("/api/quickrefs/categories/tool",
                      json={"label": "x"}).status_code == 400
    assert client.put("/api/quickrefs/categories/nope",
                      json={"label": "x"}).status_code == 404

    # the pipeline helpers pick the category up
    from app.tasks.quickref import doc_prompt, extraction_system

    cat_map = categories.category_map()
    assert doc_prompt("framework", cat_map) == "Write a framework quick-ref for the given subject."
    assert doc_prompt("tool", cat_map)  # builtin routes through the prompt registry
    system = extraction_system([], categories.all_categories())
    assert "FRAMEWORKZ" in system and "updated" in system
    assert '"tool|technique|concept|technology|framework"' in system
    assert categories.kind_dir("framework") == "frameworks"
    assert categories.kind_dir("technology") == "technologies"

    # deletion is blocked while docs still use the category; deleting the doc
    # through the API (files + DB rows + FTS) unblocks it
    from app.models import Artifact, QuickRef

    library._write_doc("frameworks/mitre-attack.md", {"title": "MITRE ATT&CK"}, "kb body")
    with get_session() as session:
        ref = QuickRef(kind="framework", slug="mitre-attack", title="MITRE ATT&CK",
                       path="frameworks/mitre-attack.md", aliases="[]")
        session.add(ref)
        session.commit()
        rid = ref.id
        art = library.write_artifact(
            session, project_id=None, project_slug=None, type="quickref_framework",
            title="MITRE ATT&CK", body="kb body",
            rel_path="frameworks/mitre-attack.md",
        )
        aid = art.id
    assert client.delete("/api/quickrefs/categories/framework").status_code == 409

    assert client.delete(f"/api/quickrefs/{rid}").status_code == 200
    assert not library.lib_path("frameworks/mitre-attack.md").exists()
    with get_session() as session:
        assert session.get(Artifact, aid) is None
    assert client.delete(f"/api/quickrefs/{rid}").status_code == 404

    assert client.delete("/api/quickrefs/categories/framework").status_code == 200
    assert client.delete("/api/quickrefs/categories/framework").status_code == 404
    assert client.delete("/api/quickrefs/categories/tool").status_code == 400
    assert all(c["key"] != "framework"
               for c in client.get("/api/quickrefs/categories").json())


def test_delete_project_keeps_shared_quickref_artifacts(client):
    """Regression: deleting a project must not wipe the Artifact row / FTS index
    of a shared quick-ref doc that happens to keep this project's id."""
    from sqlmodel import select
    from app.models import Artifact

    r = client.post("/api/projects", json={
        "source": "https://e.com/shared", "source_type": "url", "title": "Shared creator"})
    pid = r.json()["id"]
    slug = r.json()["slug"]

    with get_session() as session:
        # a project-owned artifact (should be deleted) …
        library.write_artifact(
            session, project_id=pid, project_slug=slug, type="summary",
            title="Summary — Shared", body="own artifact")
        # … and a shared quick-ref doc whose project_id is this project (kept)
        library.write_artifact(
            session, project_id=pid, project_slug=slug, type="quickref_tool",
            title="sharedtool", body="cross-project doc",
            rel_path="tools/sharedtool.md")

    assert client.delete(f"/api/projects/{pid}").status_code == 200
    with get_session() as session:
        rows = session.exec(
            select(Artifact).where(Artifact.path == "tools/sharedtool.md")).all()
        assert len(rows) == 1  # survived
    # still full-text searchable
    hits = client.get("/api/library/search", params={"q": "cross-project"}).json()
    assert any(h["path"] == "tools/sharedtool.md" for h in hits)


def test_library_search_multi_tag_no_duplicates(client):
    """Regression: an artifact carrying several requested tags must appear once."""
    aid = seed_artifact("Multi tag doc", "wireguard and firewalls",
                        tags=["networking", "linux"])
    hits = client.get("/api/library/search",
                      params={"tag": "networking,linux"}).json()
    matching = [h for h in hits if h["id"] == aid]
    assert len(matching) == 1


def test_quickref_detail_missing_file_returns_410(client):
    """A QuickRef row whose doc file was removed on disk yields 410, not 500."""
    from app.models import QuickRef

    with get_session() as session:
        ref = QuickRef(kind="tool", slug="ghosttool", title="ghosttool",
                       path="tools/ghosttool-missing.md", aliases="[]")
        session.add(ref)
        session.commit()
        session.refresh(ref)
        rid = ref.id
    assert client.get(f"/api/quickrefs/{rid}").status_code == 410


def test_correct_chunking_uses_no_overlap(client, monkeypatch):
    """Regression: the correction pass must not duplicate chunk-boundary text."""
    from app.tasks import generate
    from app import llm

    calls = []
    monkeypatch.setattr(llm, "resolve_model", lambda fn: ("test", "m"))
    # echo each chunk back unchanged so we can detect duplicated lines
    monkeypatch.setattr(llm, "complete", lambda fn, sys, chunk, **k: chunk)
    monkeypatch.setattr(generate, "_write",
                        lambda pid, t, tp, body, **k: calls.append(body) or 1)
    monkeypatch.setattr(generate, "advanced", lambda g: {"chunk_chars": 200})

    r = client.post("/api/projects", json={
        "source": "https://e.com/corr", "source_type": "url", "title": "Corr demo"})
    pid = r.json()["id"]
    lines = "".join(f"[00:00:{i:02d}] line number {i}\n" for i in range(40))
    with get_session() as session:
        library.write_artifact(
            session, project_id=pid, project_slug=r.json()["slug"], type="transcript",
            title="Transcript — Corr", body=lines)

    generate.correct.run(0, pid)  # .run bypasses celery dispatch
    joined = calls[-1]
    # every distinct source line appears exactly once — no boundary duplication
    import re as _re
    for i in range(40):
        assert len(_re.findall(rf"line number {i}\b", joined)) == 1


def test_upsert_quickref_keeps_existing_kind(client, monkeypatch):
    """When the LLM re-classifies an existing doc under a different kind, the
    merge must keep the doc's established kind — a mismatched quickref_<kind>
    artifact type at the same path would fork a second Artifact row."""
    from app import categories, llm
    from app.models import Artifact, QuickRef
    from app.tasks import quickref as qr
    from sqlmodel import select

    with get_session() as session:
        project = Project(slug="qkind", title="Kind demo", source="x", source_type="url")
        session.add(project)
        session.commit()
        session.refresh(project)
        pid, pslug, ptitle = project.id, project.slug, project.title
        library.write_artifact(
            session, project_id=pid, project_slug=pslug, type="quickref_tool",
            title="Docker", body="tool manual", rel_path="tools/docker.md")
        session.add(QuickRef(kind="tool", slug="docker", title="Docker",
                             path="tools/docker.md", aliases="[]"))
        session.commit()

    monkeypatch.setattr(llm, "resolve_model", lambda fn: ("test", "test-model"))
    monkeypatch.setattr(llm, "complete", lambda *a, **k: "merged body")
    monkeypatch.setattr(qr, "auto_tag", lambda *a, **k: None)  # no celery broker
    qr._upsert_quickref(pid, pslug, ptitle, "Docker", "technology", "docker",
                        "deep dive text", categories.category_map())

    with get_session() as session:
        arts = session.exec(
            select(Artifact).where(Artifact.path == "tools/docker.md")).all()
        assert len(arts) == 1 and arts[0].type == "quickref_tool"
        ref = session.exec(select(QuickRef).where(QuickRef.slug == "docker")).one()
        assert ref.kind == "tool"
    _, body = library.read_doc("tools/docker.md")
    assert body.startswith("merged body")


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


def test_project_list_progress(client):
    """The projects list carries a derived pipeline status (done/total + a
    macro-state), not the vestigial ingest/transcribe string."""
    from datetime import timedelta
    from sqlmodel import select
    from app.models import Job, utcnow

    r = client.post("/api/projects", json={
        "source": "https://example.com/prog", "source_type": "url", "title": "Progress demo"})
    pid, slug = r.json()["id"], r.json()["slug"]

    def prog():
        return next(p["progress"] for p in client.get("/api/projects").json()
                    if p["id"] == pid)

    p0 = prog()
    assert p0["total"] == 13 and p0["done"] == 0          # url project: every step applies
    assert p0["status"] == "new" and p0["last_activity"] is None

    # a transcript implies ingest happened, so it counts toward two steps
    # (ingest + transcribe); nothing running → partial
    with get_session() as session:
        library.write_artifact(session, project_id=pid, project_slug=slug,
                               type="transcript", title="T — Progress demo",
                               body="[00:00:01] hello")
        session.add(Job(project_id=pid, task="transcribe", status="done", updated=utcnow()))
        session.commit()
    p1 = prog()
    assert p1["done"] == 2 and p1["status"] == "partial" and p1["last_activity"] is not None

    # a running job wins and names the concrete step
    with get_session() as session:
        session.add(Job(project_id=pid, task="summarize", status="running",
                        updated=utcnow() + timedelta(minutes=1)))
        session.commit()
    p2 = prog()
    assert p2["status"] == "running" and p2["detail"] == "Summary"

    # most-recent job errored, nothing running → failed, names the failed step
    with get_session() as session:
        run = session.exec(select(Job).where(Job.project_id == pid,
                                             Job.task == "summarize")).first()
        run.status, run.updated = "done", utcnow() + timedelta(minutes=2)
        session.add(run)
        session.add(Job(project_id=pid, task="merge", status="error",
                        updated=utcnow() + timedelta(minutes=3)))
        session.commit()
    p3 = prog()
    assert p3["status"] == "failed" and p3["detail"] == "Merge deep dives"

    # a cancellation (leftover step jobs errored as 'run-all canceled') reads as
    # canceled, not failed
    with get_session() as session:
        session.add(Job(project_id=pid, task="mindmap", status="error",
                        error="run-all canceled", updated=utcnow() + timedelta(minutes=4)))
        session.commit()
    p4 = prog()
    assert p4["status"] == "canceled"


def test_project_progress_run_all_label(client):
    """A queued run-all (waiting its turn) reads as running with a friendly
    label, never the raw 'run_all' task name."""
    from sqlmodel import select
    from app.models import Job

    r = client.post("/api/projects", json={
        "source": "https://example.com/ralabel", "source_type": "url", "title": "RA label"})
    pid = r.json()["id"]
    with get_session() as session:
        session.add(Job(project_id=pid, task="run_all", status="queued"))
        session.commit()
    try:
        p = next(x["progress"] for x in client.get("/api/projects").json() if x["id"] == pid)
        assert p["status"] == "running" and p["detail"] == "Run all steps"
    finally:
        # don't leave a queued run_all in the shared DB — it would hijack the
        # global run-all queue other tests exercise
        with get_session() as session:
            for j in session.exec(select(Job).where(Job.project_id == pid)).all():
                session.delete(j)
            session.commit()


def test_system_stats_shape(client):
    """The monitor endpoint returns host CPU/RAM plus (possibly empty) GPU and
    Ollama lists — never 500s when nvidia-smi or Ollama are absent."""
    r = client.get("/api/system/stats")
    assert r.status_code == 200
    s = r.json()
    assert 0 <= s["cpu_percent"] <= 100 * (s["cpu_count"] or 1)
    assert s["mem_total_mb"] > 0 and 0 <= s["mem_percent"] <= 100
    assert isinstance(s["gpus"], list) and isinstance(s["ollama_models"], list)
    assert len(s["cpu_per_core"]) == s["cpu_count"]


def test_piper_voice_urls():
    from app.tasks.audio import _piper_urls

    onnx, cfg = _piper_urls("en_US-amy-medium")
    assert onnx.endswith("/en/en_US/amy/medium/en_US-amy-medium.onnx")
    assert cfg == onnx + ".json"
    # a different lang/region derives the right nested path
    onnx2, _ = _piper_urls("de_DE-thorsten-high")
    assert onnx2.endswith("/de/de_DE/thorsten/high/de_DE-thorsten-high.onnx")


def test_synthesize_lines_parallel_preserves_order(tmp_path):
    """Parallel synthesis must still emit line/gap concat entries in script
    order regardless of which line finishes first."""
    from app.tasks.audio import _synthesize_lines

    lines = [("HOST_A", "one"), ("HOST_B", "two"), ("HOST_A", "three")]
    rendered = []

    def render(i, speaker, text, out):
        out.write_text(f"{i}:{text}")           # stand in for a wav
        rendered.append((i, speaker, text))

    entries = _synthesize_lines(lines, tmp_path, workers=3,
                                on_progress=lambda m: None, render=render)
    assert entries == [
        "file 'line_00000.wav'", "file 'gap.wav'",
        "file 'line_00001.wav'", "file 'gap.wav'",
        "file 'line_00002.wav'", "file 'gap.wav'",
    ]
    assert (tmp_path / "line_00000.wav").read_text() == "0:one"
    assert (tmp_path / "line_00002.wav").read_text() == "2:three"
    assert len(rendered) == 3


def test_tts_worker_count(client, monkeypatch):
    from app.tasks import audio

    monkeypatch.setattr(audio, "advanced", lambda g: {"tts_workers": 0})
    # 0 = auto, clamped to the engine default and the core count
    assert audio._tts_workers(1) == 1
    assert audio._tts_workers(4) == min(4, __import__("os").cpu_count() or 1)
    monkeypatch.setattr(audio, "advanced", lambda g: {"tts_workers": 6})
    assert audio._tts_workers(1) == 6          # explicit override wins


def test_download_concurrent_is_corruption_safe(tmp_path, monkeypatch):
    """Several threads cold-fetching the same model file must each write a
    complete file via a private temp — never a shared, interleaved .part that
    persists as a corrupt model (regression: parallel Kokoro workers)."""
    import threading
    from app.tasks import audio

    payload = b"MODEL" * 20000

    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_bytes(self):
            for i in range(0, len(payload), 997):  # small chunks → interleave
                yield payload[i:i + 997]

    monkeypatch.setattr(audio.httpx, "stream", lambda *a, **k: FakeStream())
    dest = tmp_path / "kokoro.onnx"
    errors: list = []

    def go():
        try:
            audio._download("http://model", dest)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=go) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert dest.read_bytes() == payload           # complete, not interleaved
    assert not list(tmp_path.glob("*.part"))       # every private temp cleaned up


def test_tts_dispatch_routes_to_provider(client, monkeypatch):
    """The tts task picks the synth backend from the configured provider."""
    from app.tasks import audio
    from app import llm

    seed_artifact("Podcast script — demo", "HOST_A: hi\nHOST_B: hey there",
                  type="podcast_script")
    with get_session() as session:
        from sqlmodel import select
        pid = session.exec(select(Project).where(Project.slug == "demo")).first().id

    calls = []
    for name in ("_tts_piper", "_tts_kokoro"):
        monkeypatch.setattr(audio, name,
                            lambda *a, _n=name, **k: (calls.append(_n) or __import__("pathlib").Path("x.mp3")))
    monkeypatch.setattr(audio, "_store_audio", lambda *a, **k: None)
    monkeypatch.setattr(llm, "resolve_model", lambda fn: ("piper", "piper"))
    audio.tts(job_id=_a_job(pid), project_id=pid)
    assert calls == ["_tts_piper"]


def _a_job(pid: int) -> int:
    from app.models import Job
    with get_session() as session:
        j = Job(project_id=pid, task="tts", status="queued")
        session.add(j)
        session.commit()
        session.refresh(j)
        return j.id


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


def test_run_all_queues_and_chains(client, monkeypatch):
    from app.tasks.celery_app import celery
    from app.tasks.orchestrate import maybe_start_next_run_all

    class FakeResult:
        id = "fake-celery-id"

    sent = []
    monkeypatch.setattr(celery, "send_task",
                        lambda name, args=None, **k: (sent.append((name, args)) or FakeResult()))

    r1 = client.post("/api/projects", json={
        "source": "https://example.com/ra1", "source_type": "url", "title": "RA one"})
    p1 = r1.json()["id"]
    r2 = client.post("/api/projects", json={
        "source": "https://example.com/ra2", "source_type": "url", "title": "RA two"})
    p2 = r2.json()["id"]

    # first run-all starts immediately (CAS → running → dispatched)
    a = client.post(f"/api/projects/{p1}/run_all")
    assert a.status_code == 200 and a.json()["status"] == "running"
    assert sent == [("run_all", [a.json()["id"], p1])]

    # same project can't be double-queued
    assert client.post(f"/api/projects/{p1}/run_all").status_code == 409

    # second project QUEUES (no 409 anymore) and does NOT dispatch yet
    b = client.post(f"/api/projects/{p2}/run_all")
    assert b.status_code == 200 and b.json()["status"] == "queued"
    assert len(sent) == 1  # still only the first was dispatched

    # simulate the first finishing → the queued one auto-chains
    with get_session() as session:
        j1 = session.get(Job, a.json()["id"])
        j1.status = "done"
        session.add(j1)
        session.commit()
    maybe_start_next_run_all()
    assert sent == [("run_all", [a.json()["id"], p1]),
                    ("run_all", [b.json()["id"], p2])]
    assert client.get("/api/jobs", params={"project_id": p2}).json()[0]["status"] == "running"


def test_cancel_job(client, monkeypatch):
    from app.tasks.celery_app import celery

    monkeypatch.setattr(celery, "send_task", lambda *a, **k: type("R", (), {"id": "x"})())
    monkeypatch.setattr(celery.control, "revoke", lambda *a, **k: None)

    r = client.post("/api/projects", json={
        "source": "https://example.com/cancel", "source_type": "url", "title": "Cancel demo"})
    pid = r.json()["id"]
    job = client.post(f"/api/projects/{pid}/run_all").json()

    resp = client.post(f"/api/jobs/{job['id']}/cancel")
    assert resp.status_code == 200
    assert client.get("/api/jobs", params={"project_id": pid}).json()[0]["status"] == "canceled"
    # cancelling an already-terminal job is a 409
    assert client.post(f"/api/jobs/{job['id']}/cancel").status_code == 409
    assert client.post("/api/jobs/99999/cancel").status_code == 404


def test_jobs_list_enriched(client, monkeypatch):
    from app.tasks.celery_app import celery

    monkeypatch.setattr(celery, "send_task", lambda *a, **k: type("R", (), {"id": "x"})())
    r = client.post("/api/projects", json={
        "source": "https://example.com/enrich", "source_type": "url", "title": "Enrich demo"})
    pid = r.json()["id"]
    client.post(f"/api/projects/{pid}/run_all")
    jobs = client.get("/api/jobs", params={"project_id": pid}).json()
    assert jobs[0]["task_label"] == "Run all steps"
    assert jobs[0]["project_title"] == "Enrich demo"


def test_model_override(client):
    r = client.put("/api/settings/models/summarize",
                   json={"provider": "anthropic", "model": "claude-haiku-4-5"})
    assert r.status_code == 200
    models = client.get("/api/settings/models").json()["functions"]
    assert models["summarize"] == {"provider": "anthropic", "model": "claude-haiku-4-5"}
    assert client.put("/api/settings/models/nope",
                      json={"provider": "x", "model": "y"}).status_code == 400
