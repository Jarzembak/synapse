# Research paper analysis

Choose **Projects → Research paper** to upload a PDF or import one from the
read-only `/host-media` mount.

## Input limits

The first release supports PDF only, with these defaults:

- 250 MiB maximum source size;
- 500 pages;
- five million extracted characters; and
- English, Spanish, French, and German OCR languages selected per import.

The PDF is copied immutably into the project's library directory for citations
and backups. It is always excluded from cloud sync. A revised PDF is imported
as a new project rather than replacing its predecessor.

## Evidence extraction

Extraction runs locally in the concurrency-one `paper-worker` using Docling and
Tesseract. It records every admitted:

- prose block and heading;
- definition;
- equation;
- table;
- caption;
- footnote; and
- reference.

Each evidence block has a stable ID, source hash, page, section path, bounding
box, extraction method, and quality metadata. The pipeline maps every admitted
block within the configured limits; it does not representative-sample or take
only a prefix.

Tables and formulas receive structural extraction and are flagged when
unreliable. Charts and diagrams retain captions and page locations but are
marked **visual review needed** rather than being interpreted.

## Quality review

A POOR document grade or POOR nontrivial page blocks analysis. You can:

- replace the source with a new paper project; or
- acknowledge each named page with a reason.

Acknowledged gaps remain visible in coverage reports and cannot be the sole
support for a critical claim.

## Shared analysis

After extraction review, complete leaf maps and recursive
evidence-preserving reductions produce:

- a source, extraction, and coverage report;
- a structural claim and argument map;
- a whole-paper mind map;
- quick references derived from all mapped evidence; and
- paper-grounded Library search and Q&A.

The analysis maps definitions, claims, hypotheses, methods, data or materials,
results and uncertainty, assumptions, limitations, prerequisites, bibliography
relationships, and referenced figures or tables.

## Audience tracks and multipart plans

Select any combination of Generalist, Practitioner, and Expert tracks. They
reuse the shared evidence map but are planned, approved, generated, and
deleted independently.

Each track contains one to five sequential parts. The default target is 50
minutes per part, constrained to 40–60 minutes. A cohesive paper can remain a
single part.

Synapse drafts a prerequisite-aware teaching arc and stops for review. The plan
editor requires every admitted evidence block to have:

- one primary part; or
- a recorded omission reason.

Critical and major evidence cannot be silently omitted. Lower-priority evidence
may be omitted only with a reason, and demoting critical material also requires
an explanation.

Before production you can edit part count, order, title, focus, and evidence
assignments. Once a part is complete its structure is locked; future
ungenerated parts remain editable and affected outputs become stale.

## Generated track and part artifacts

An approved audience track produces:

- an overview;
- methods and reproducibility guidance;
- evidence and results guidance;
- prerequisite knowledge and terminology;
- balanced limitations and critique;
- an explanatory deep dive;
- a critical-methodology deep dive; and
- a merged definitive study guide.

Each part adds:

- a cited study guide that doubles as show notes;
- a segment-based two-host script; and
- podcast audio using the configured voices.

Page citations stay out of spoken dialogue. Segment and guide metadata retains
validated evidence IDs and clickable page/section links.

## Series memory

Every finalized script produces an immutable memory revision containing:

- terminology and pronunciations;
- introduced, completed, and deferred topics;
- claims and examples already covered;
- stories and analogies already used;
- open questions and promised callbacks;
- handoff notes; and
- evidence IDs.

The UI exposes this series bible and accepts separate user guidance without
rewriting generated facts. Scripts generate sequentially from the prior memory
revision; independent study guides can generate in parallel. Audio does not
change memory.

Regenerating Part N creates a new memory revision and preserves but marks later
scripts and audio stale. **Rebuild this and following** performs that
regeneration explicitly. An audio-only rerun does not make following parts
stale.

## Privacy

Paper processing is local-only by default, covering chat models, embeddings,
tagging, Q&A, TTS, and cloud sync. Encrypted backups are required while a
local-only paper exists.

A cloud-enabled paper may use configured providers and merge eligible quick
references into the cross-project library. Its original PDF never syncs.

The first release does not perform external literature lookup or generative
visual interpretation. GitHub multipart series are planned for a later
release.
