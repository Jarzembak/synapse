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

MAX_TAG_WORDS = 4   # a real tag is 1-4 words; longer = a model run-on phrase
MAX_TAG_LEN = 40    # word count is the primary signal; length is a backstop


def clean_tag(name: str) -> str | None:
    """Normalize one model-proposed tag, or None if it's junk.

    Local models occasionally loop tokens ("apis apis apis…" or a longer
    "apis networking apis networking" cycle) or emit a whole run-on phrase as a
    single tag; slugified verbatim these pollute the vocabulary. Collapse
    consecutive repeats and any repeated cycle, then reject over-long / multi-
    word run-ons so they never enter the vocabulary."""
    if not name or not name.strip():
        return None
    slug = library.make_slug(name)
    if slug in ("", "untitled"):  # make_slug's fallback for empty/garbage input
        return None
    parts = slug.split("-")
    # 1) drop consecutive repeats: apis-apis-control-plane -> apis-control-plane
    dedup: list[str] = []
    for p in parts:
        if not dedup or dedup[-1] != p:
            dedup.append(p)
    # 2) collapse a repeated cycle (single- or multi-token loops):
    #    apis-apis-apis -> apis ; apis-networking-apis-networking -> apis-networking.
    #    Genuine tags that merely reuse a word (day-to-day, end-to-end) aren't a
    #    clean repetition of a base, so they're left intact.
    n = len(dedup)
    collapsed = dedup
    for period in range(1, n // 2 + 1):
        if n % period == 0 and dedup[:period] * (n // period) == dedup:
            collapsed = dedup[:period]
            break
    slug = "-".join(collapsed)
    if not slug or len(collapsed) > MAX_TAG_WORDS or len(slug) > MAX_TAG_LEN:
        return None
    return slug


def sanitize_tags(names: list[str]) -> list[str]:
    """clean_tag over a list, dropping junk and de-duplicating, order-preserving."""
    out: list[str] = []
    seen: set[str] = set()
    for n in names:
        c = clean_tag(n) if isinstance(n, str) else None
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def tag_text(session: Session, title: str, doc_type: str, body: str, *,
             local_only: bool = False) -> list[str]:
    """One LLM tagging call over a document; returns tag names (not applied)."""
    rules = advanced("pipeline")
    max_tags = int(rules.get("max_tags", 8))
    allow_new = bool(rules.get("allow_new_tags", True))

    system = get_prompt("tag") + f"\nPick 3-{max_tags} tags for the document."
    if allow_new:
        system += ("\nOnly invent a new tag when no existing tag fits; new tags "
                   "must be short, lowercase, hyphenated.")
    else:
        system += "\nYou MUST use existing vocabulary tags only — never invent new ones."

    vocabulary_query = select(Tag.name)
    if not local_only:
        vocabulary_query = vocabulary_query.where(Tag.restricted == False)  # noqa: E712
    vocab = session.exec(vocabulary_query).all()
    doc = body[:12000]
    result = llm.complete_json(
        "tag",
        system,
        f"Existing vocabulary:\n{', '.join(sorted(vocab))}\n\n"
        f"Document (type={doc_type}, title={title!r}):\n{doc}\n\n"
        'Reply as {"tags": ["...", ...]}',
        # cap output so a looping local model can't run away generating tags
        max_tokens=512,
        local_only=local_only,
    )
    raw = [n for n in result.get("tags", []) if isinstance(n, str)]
    known = set(vocab)
    # Existing vocabulary tags are trusted verbatim — a user may have created a
    # long/multi-word tag on purpose, so the sanitizer's length/word caps apply
    # only to NEW tags the model proposes, and only when new tags are allowed.
    names: list[str] = []
    seen: set[str] = set()
    for n in raw:
        slug = library.make_slug(n)
        if slug in known:
            cleaned = slug
        elif allow_new:
            cleaned = clean_tag(n)
        else:
            continue
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            names.append(cleaned)
    return names[:max_tags]


def tag_artifact(session: Session, artifact: Artifact, body: str) -> list[str]:
    """Tag one artifact from its own content (used for quick-ref docs, whose
    content is their own; project artifacts share a project-level tag set)."""
    names = tag_text(
        session, artifact.title, artifact.type, body,
        local_only=bool(
            getattr(artifact, "restricted", False)
            or library.artifact_is_repository_derived(session, artifact)),
    )
    library.apply_tags(session, artifact, names)
    return names
