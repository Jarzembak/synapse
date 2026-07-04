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


def test_model_override(client):
    r = client.put("/api/settings/models/summarize",
                   json={"provider": "anthropic", "model": "claude-haiku-4-5"})
    assert r.status_code == 200
    models = client.get("/api/settings/models").json()["functions"]
    assert models["summarize"] == {"provider": "anthropic", "model": "claude-haiku-4-5"}
    assert client.put("/api/settings/models/nope",
                      json={"provider": "x", "model": "y"}).status_code == 400
