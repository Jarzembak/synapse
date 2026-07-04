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
    source_type: str  # "url" | "local"
    status: str = "new"
    created: datetime = Field(default_factory=utcnow)


class Artifact(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    project_id: int | None = Field(default=None, foreign_key="project.id", index=True)
    # transcript | corrected | summary | deepdive_claude | deepdive_gemini |
    # deepdive_merged | podcast_script | podcast_audio | trimmed_audio |
    # mindmap | quickref_tool | quickref_technique
    type: str = Field(index=True)
    title: str
    path: str  # library-relative path of the .md (or sidecar .md for binaries)
    media_path: str | None = None  # library-relative path of binary payload
    provider: str | None = None
    model: str | None = None
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
    kind: str = Field(index=True)  # tool | technique
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
    status: str = "queued"  # queued | running | done | error
    progress: str = ""
    error: str = ""
    celery_id: str = ""
    created: datetime = Field(default_factory=utcnow)
    updated: datetime = Field(default_factory=utcnow)


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str = ""  # JSON-encoded
