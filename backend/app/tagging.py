"""Controlled-vocabulary auto-tagging.

The tagger sees the existing vocabulary and must choose from it; whether it may
propose new tags, and how many tags per artifact, are Settings → Advanced knobs.
"""
from __future__ import annotations

from sqlmodel import Session, select

from . import library, llm
from .config import advanced
from .models import Artifact, Tag
from .tasks.prompts import get_prompt


def tag_artifact(session: Session, artifact: Artifact, body: str) -> list[str]:
    rules = advanced("pipeline")
    max_tags = int(rules.get("max_tags", 8))
    allow_new = bool(rules.get("allow_new_tags", True))

    system = get_prompt("tag") + f"\nPick 3-{max_tags} tags for the document."
    if allow_new:
        system += ("\nOnly invent a new tag when no existing tag fits; new tags "
                   "must be short, lowercase, hyphenated.")
    else:
        system += "\nYou MUST use existing vocabulary tags only — never invent new ones."

    vocab = session.exec(select(Tag.name)).all()
    doc = body[:12000]
    result = llm.complete_json(
        "tag",
        system,
        f"Existing vocabulary:\n{', '.join(sorted(vocab))}\n\n"
        f"Document (type={artifact.type}, title={artifact.title!r}):\n{doc}\n\n"
        'Reply as {"tags": ["...", ...]}',
    )
    names = [n for n in result.get("tags", []) if isinstance(n, str)]
    if not allow_new:
        known = set(vocab)
        names = [n for n in names if library.make_slug(n) in known]
    library.apply_tags(session, artifact, names[:max_tags])
    return names[:max_tags]
