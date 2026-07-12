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
    created: datetime = Field(default_factory=utcnow)
    updated: datetime = Field(default_factory=utcnow)


class Tag(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    kind: str = "topic"  # topic | tool | technique | tech | domain


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
