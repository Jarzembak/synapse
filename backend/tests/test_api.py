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


def test_model_override(client):
    r = client.put("/api/settings/models/summarize",
                   json={"provider": "anthropic", "model": "claude-haiku-4-5"})
    assert r.status_code == 200
    models = client.get("/api/settings/models").json()["functions"]
    assert models["summarize"] == {"provider": "anthropic", "model": "claude-haiku-4-5"}
    assert client.put("/api/settings/models/nope",
                      json={"provider": "x", "model": "y"}).status_code == 400
