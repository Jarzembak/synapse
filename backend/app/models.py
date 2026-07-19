from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Project(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    slug: str = Field(index=True, unique=True)
    title: str
    source: str  # URL or media-relative path
    source_type: str  # "url" | "local" | "upload"
    status: str = "new"
    deleting: bool = Field(default=False, index=True)
    created: datetime = Field(default_factory=utcnow)


class Artifact(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    project_id: int | None = Field(default=None, foreign_key="project.id", index=True)
    # transcript | corrected | summary | deepdive_claude | deepdive_gemini |
    # deepdive_merged | podcast_script | podcast_audio | trimmed_audio |
    # mindmap | quickref_<category kind> | source_video | source_audio
    type: str = Field(index=True)
    title: str
    path: str  # library-relative path of the .md (or sidecar .md for binaries)
    # binary payload location: library-relative, or MEDIA_DIR-relative when
    # prefixed with "media:" (large archived source files)
    media_path: str | None = None
    provider: str | None = None
    model: str | None = None
    input_hash: str = ""
    config_hash: str = ""
    provenance: str = "{}"  # JSON: upstream/config/source details
    # Sticky local-safety marker. Repository material must never be sent to
    # cloud sync/model providers, even if later merged with other material.
    restricted: bool = Field(default=False, index=True)
    # Sticky origin marker. Repository projects and their project-local or
    # shared derivatives are local-only in v1. Unlike mutable project and
    # QuickRefSource relationships, this survives contributor/project deletion
    # so a later full cloud sync cannot reclassify the retained document.
    repository_derived: bool = Field(default=False, index=True)
    created: datetime = Field(default_factory=utcnow)
    updated: datetime = Field(default_factory=utcnow)


class Tag(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    kind: str = "topic"  # topic | tool | technique | tech | domain
    # Sticky: a private-derived vocabulary term must never be included in a
    # later public document's cloud-model tagging prompt.
    restricted: bool = Field(default=False, index=True)


class ArtifactTag(SQLModel, table=True):
    artifact_id: int = Field(foreign_key="artifact.id", primary_key=True)
    tag_id: int = Field(foreign_key="tag.id", primary_key=True)


class QuickRef(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    kind: str = Field(index=True)  # tool | technique | concept | technology | custom category key
    slug: str = Field(index=True)
    title: str
    path: str
    aliases: str = ""  # JSON list of name variants seen in sources


class QuickRefSource(SQLModel, table=True):
    quickref_id: int = Field(foreign_key="quickref.id", primary_key=True)
    project_id: int = Field(foreign_key="project.id", primary_key=True)


class Job(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    project_id: int | None = Field(default=None, foreign_key="project.id", index=True)
    task: str
    status: str = "queued"  # queued | running | done | error | canceled
    progress: str = ""
    error: str = ""
    celery_id: str = ""
    parent_job_id: int | None = Field(default=None, index=True)
    options: str = "{}"  # JSON run options (profile / explicit step set)
    started: datetime | None = None
    finished: datetime | None = None
    heartbeat: datetime | None = None
    created: datetime = Field(default_factory=utcnow)
    updated: datetime = Field(default_factory=utcnow)


class SearchChunk(SQLModel, table=True):
    """Retrievable excerpt used by FTS, semantic search, and grounded Q&A."""

    id: int | None = Field(default=None, primary_key=True)
    artifact_id: int = Field(foreign_key="artifact.id", index=True)
    chunk_index: int
    body: str
    start_time: str = ""
    body_hash: str = Field(index=True)


class ChunkEmbedding(SQLModel, table=True):
    """Provider-neutral float32 vector for one SearchChunk."""

    chunk_id: int = Field(foreign_key="searchchunk.id", primary_key=True)
    model: str = Field(primary_key=True)
    dimensions: int
    vector: bytes
    body_hash: str = Field(index=True)


class LLMCall(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    job_id: int | None = Field(default=None, index=True)
    function: str = Field(index=True)
    provider: str = Field(index=True)
    model: str = Field(index=True)
    input_chars: int = 0
    output_chars: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_seconds: float = 0
    status: str = "ok"
    error: str = ""
    created: datetime = Field(default_factory=utcnow, index=True)


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str = ""  # JSON-encoded


class RepositorySource(SQLModel, table=True):
    """GitHub identity and mutable tracking state for one Project.

    Credentials never live in this row.  ``credential_ref`` is only the key of
    an encrypted backend setting. All repository analysis is local-only in v1.
    """

    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True, unique=True)
    provider: str = "github"
    host: str = "github.com"
    owner: str = Field(index=True)
    repository: str = Field(index=True)
    canonical_url: str
    description: str = ""
    requested_ref: str = ""
    default_branch: str = ""
    is_private: bool = Field(default=False, index=True)
    local_only: bool = Field(default=True, index=True)
    credential_ref: str = ""
    include_paths: str = "[]"  # JSON list of relative glob/prefix filters
    exclude_paths: str = "[]"  # JSON list of relative glob/prefix filters
    coverage_preview: str = "{}"  # JSON from metadata/tree preflight
    pending_sha: str = Field(default="", index=True)
    current_snapshot_id: int | None = Field(default=None, index=True)
    # Durable outbox flag: a public→private transition is not fully settled
    # until any formerly public cloud copies have been removed.
    cloud_purge_pending: bool = Field(default=False, index=True)
    created: datetime = Field(default_factory=utcnow)
    updated: datetime = Field(default_factory=utcnow)


class RepositorySnapshot(SQLModel, table=True):
    """Immutable, locally retained repository state pinned to one commit."""

    id: int | None = Field(default=None, primary_key=True)
    source_id: int = Field(foreign_key="repositorysource.id", index=True)
    parent_snapshot_id: int | None = Field(default=None, index=True)
    requested_ref: str = ""
    resolved_sha: str = Field(index=True)
    commit_url: str = ""
    commit_time: datetime | None = None
    archive_sha256: str = ""
    archive_bytes: int = 0
    manifest_hash: str = Field(default="", index=True)
    relative_path: str = ""
    status: str = Field(default="pending", index=True)  # pending | ready | error
    error: str = ""
    file_count: int = 0
    total_bytes: int = 0
    indexed_file_count: int = 0
    indexed_bytes: int = 0
    excluded_file_count: int = 0
    secret_finding_count: int = 0
    facts: str = "{}"  # deterministic inventory JSON; contains no secret values
    scanner_version: str = "1"
    scan_config_hash: str = Field(default="", index=True)
    omitted_links: str = "[]"  # JSON paths catalogued but never materialized
    created: datetime = Field(default_factory=utcnow)
    completed: datetime | None = None


class RepositoryFile(SQLModel, table=True):
    """Static inventory row for a file observed in an immutable snapshot."""

    id: int | None = Field(default=None, primary_key=True)
    snapshot_id: int = Field(foreign_key="repositorysnapshot.id", index=True)
    path: str = Field(index=True)
    content_hash: str = Field(default="", index=True)
    size_bytes: int = 0
    line_count: int = 0
    language: str = ""
    role: str = Field(default="source", index=True)
    binary: bool = False
    generated: bool = False
    vendor: bool = False
    restricted: bool = False
    excluded: bool = Field(default=False, index=True)
    exclusion_reason: str = ""
    analysis_priority: int = 0
    lfs_pointer: bool = False
    submodule: bool = False
    symlink: bool = False
    created: datetime = Field(default_factory=utcnow)


class RepositoryChunk(SQLModel, table=True):
    """Line-addressed evidence plus a deterministic per-chunk summary cache."""

    id: int | None = Field(default=None, primary_key=True)
    file_id: int = Field(foreign_key="repositoryfile.id", index=True)
    chunk_index: int
    evidence_id: str = Field(index=True)
    start_line: int
    end_line: int
    kind: str = "text"
    symbol: str = ""
    body: str
    body_hash: str = Field(index=True)
    content_hash: str = Field(index=True)
    estimated_tokens: int = 0
    summary_text: str = ""
    summary_json: str = "{}"
    summary_config_hash: str = Field(default="", index=True)
    created: datetime = Field(default_factory=utcnow)
