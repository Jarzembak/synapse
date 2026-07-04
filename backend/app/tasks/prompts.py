"""Prompt templates for the generation pipeline."""

CORRECT_SYSTEM = """You repair speech-to-text transcripts of technical videos for a \
cybersecurity/sysadmin audience. Fix ONLY transcription errors: misheard words, \
mangled shell commands, wrong acronyms, misrendered tool/product names, broken \
technical terms. Use the glossary of known-correct domain terms. Keep timestamps, \
line structure, wording and meaning otherwise identical. Never summarize, never \
editorialize. Output the corrected transcript text only."""

SUMMARY_SYSTEM = """Write a short summary (150-250 words) of this video transcript: \
what it covers, the key tools/technologies named, and who would benefit from watching. \
Markdown, no heading."""

DEEPDIVE_SYSTEM = """You are writing a deep-dive study document from a video transcript.

Focus on the CORE CONCEPTS, TOOLS, and TECHNOLOGIES the video covers. For each: what
it is, how it works, how it's used in practice, and how it relates to the others.

CRITICAL RULE — procedures: if the video contains procedural content (a step-by-step
tutorial, a walk-through of a methodology, a configuration recipe, an attack/defense
sequence), you MUST capture the procedure in full and flesh it out: every step in
order, the exact commands/settings involved, WHY each step is done, and its expected
result. Never compress a procedure into a summary sentence.

Structure (markdown):
# Deep Dive: <topic>
## Overview
## Core Concepts        (### per concept)
## Tools & Technologies (### per tool/tech)
## Procedures           (### per procedure; numbered steps: action, command/detail, why, expected result)
## How It Fits Together
## Further Study

Go deep — expand on what the transcript says using your own knowledge where it adds
accuracy or context, and mark speculation as such."""

MERGE_SYSTEM = """You are merging two deep-dive documents (one by Claude, one by Gemini)
about the same video into ONE unified deep dive.

Rules:
- Remove redundancy: where both cover the same point, keep the better/clearer telling
  and fold in anything unique from the other.
- Preserve the UNION of all procedures. Only merge two procedures if they describe the
  exact same steps; if they differ, keep both, noting the difference. Never drop steps.
- Unify structure to: Overview / Core Concepts / Tools & Technologies / Procedures /
  How It Fits Together / Further Study.
- Keep the deepest level of technical detail present in either source.
Output the merged markdown document only."""

EXTRACT_ENTITIES_SYSTEM = """From this deep-dive document, list the concrete TOOLS
(software, hardware, services with a name, e.g. nmap, Wireshark, Kubernetes) and
TECHNIQUES (named methods/procedures, e.g. dns-tunneling, memory-forensics) covered
in substantive depth — not passing mentions."""

QUICKREF_NEW_SYSTEM = """Create a quick-reference document for the given {kind}, based on
this deep-dive material. Structure (markdown):
# <name>
## What it is        (2-3 sentences)
## Key usage / syntax (commands, flags, config snippets where applicable)
## Examples           (concrete examples drawn from the source material, cited as 'From: <video title>')
## Tips & gotchas
Keep it a fast-recall reference, not an essay."""

QUICKREF_MERGE_SYSTEM = """Update an existing quick-reference document with new material
from another video's deep dive. Integrate — do not append a new section per video.
Merge new usage examples into ## Examples (keep the 'From: <video title>' attributions),
add genuinely new flags/tips in place, dedupe, keep the existing structure and voice.
Output the full updated document only."""

PODCAST_OUTLINE_SYSTEM = """Plan a two-host podcast episode covering this deep-dive
document for technically-minded listeners. Host A is the lead/explainer; Host B asks
sharp questions, adds color, and summarizes. Produce a JSON outline:
{"title": ..., "segments": [{"heading": ..., "points": ["..."]}]}
8-14 segments, covering ALL major concepts, tools and EVERY procedure in the source."""

PODCAST_SEGMENT_SYSTEM = """Write the podcast dialogue for ONE segment of a two-host show.
Format strictly as alternating lines:
HOST_A: ...
HOST_B: ...
Natural spoken style — contractions, brief reactions, analogies — but technically
precise; walk through any procedure steps concretely. No stage directions, no markdown.
Continue smoothly from the previous segment; do not re-introduce the show."""

TRIM_SPANS_SYSTEM = """You are given a timestamped transcript. Identify spans that are NOT
topic-relevant content: intro chatter, sponsor reads, subscribe/like requests, housekeeping,
off-topic tangents, outro filler. Reply as JSON:
{"remove": [{"start": "HH:MM:SS", "end": "HH:MM:SS", "reason": "..."}]}
Be conservative — when unsure, keep the content. Spans must not overlap."""

MINDMAP_SYSTEM = """Build a topic graph of this deep-dive document for an interactive
mind map. Reply as JSON:
{"nodes": [{"id": "slug", "label": "...", "kind": "concept|tool|technique|technology",
            "summary": "1-2 sentence description"}],
 "edges": [{"source": "slug", "target": "slug", "label": "relationship"}]}
15-40 nodes. The graph should read as 'how this domain fits together': tools connect to
the concepts they implement, techniques to the tools they use, etc."""
