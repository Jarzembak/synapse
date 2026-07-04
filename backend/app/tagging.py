"""Controlled-vocabulary auto-tagging.

The tagger sees the existing vocabulary and must choose from it; it may propose
new tags only when nothing fits. New tags enter the vocabulary so future runs
converge on consistent naming.
"""
from __future__ import annotations

from sqlmodel import Session, select

from . import library, llm
from .models import Artifact, Tag

SYSTEM = """You tag technical study artifacts for a cybersecurity/sysadmin knowledge library.
Pick 3-8 tags for the document. STRONGLY prefer tags from the existing vocabulary.
Only invent a new tag when no existing tag fits; new tags must be short, lowercase, hyphenated."""


def tag_artifact(session: Session, artifact: Artifact, body: str) -> list[str]:
    vocab = session.exec(select(Tag.name)).all()
    doc = body[:12000]
    result = llm.complete_json(
        "tag",
        SYSTEM,
        f"Existing vocabulary:\n{', '.join(sorted(vocab))}\n\n"
        f"Document (type={artifact.type}, title={artifact.title!r}):\n{doc}\n\n"
        'Reply as {"tags": ["...", ...]}',
    )
    names = [n for n in result.get("tags", []) if isinstance(n, str)][:8]
    library.apply_tags(session, artifact, names)
    return names
