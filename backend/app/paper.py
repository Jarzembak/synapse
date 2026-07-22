"""Local PDF extraction and page-grounded paper evidence utilities.

The paper pipeline treats the source PDF and its extracted evidence as
immutable inputs.  Model-produced maps and reductions are stored separately in
``PaperSynthesisCache`` (see :mod:`app.tasks.paper`).  This module intentionally
does not import Docling at module import time: the API and ordinary workers can
start without the heavyweight parser runtime, while the dedicated paper worker
loads it when an extraction job runs.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from sqlalchemy import text
from sqlmodel import Session, select

from .config import settings


PAPER_EVIDENCE_SCHEMA_VERSION = 1
DOCLING_VERSION = "2.113.0"
PARSER_VERSION = f"docling-{DOCLING_VERSION}/paper-evidence-{PAPER_EVIDENCE_SCHEMA_VERSION}"
MAX_PAPER_FILE_BYTES = 250 * 1024 * 1024
MAX_PAPER_PAGES = 500
MAX_PAPER_EXTRACTED_CHARACTERS = 5_000_000
MAX_EVIDENCE_CHARACTERS = 12_000
ALLOWED_OCR_LANGUAGES = frozenset({"eng", "spa", "fra", "deu"})
DEFAULT_OCR_LANGUAGES = ("eng",)

_VISIBLE_CITATION = re.compile(r"\[P:([A-Za-z0-9][A-Za-z0-9_.:-]{0,160})\]")
_HIDDEN_CITATION = re.compile(
    r"<!--\s*P:([A-Za-z0-9][A-Za-z0-9_.:-]{0,160})\s*-->")
_DEFINITION = re.compile(
    r"^\s*(?:definition|def\.|terminology|definici[oó]n|d[eé]finition)"
    r"\s*(?:\d+(?:\.\d+)*)?\s*[:.\-]",
    re.IGNORECASE,
)


class PaperExtractionError(RuntimeError):
    """A paper could not be admitted or converted without losing coverage."""


class PaperAnalysisBlocked(RuntimeError):
    """Extraction quality review is required before model analysis."""


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def file_sha256(path: Path, chunk_bytes: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk_bytes):
            digest.update(block)
    return digest.hexdigest()


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, type(default)):
        return value
    try:
        parsed = json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _grade(value: Any) -> str:
    if value is None:
        return "UNKNOWN"
    if hasattr(value, "value"):
        value = value.value
    rendered = str(value).rsplit(".", 1)[-1].upper()
    return rendered if rendered in {"POOR", "FAIR", "GOOD", "EXCELLENT"} else "UNKNOWN"


@dataclass(frozen=True)
class PaperExtractionConfig:
    """Hard-bounded v1 parser settings included in every provenance hash."""

    ocr_languages: tuple[str, ...] = DEFAULT_OCR_LANGUAGES
    max_file_bytes: int = MAX_PAPER_FILE_BYTES
    max_pages: int = MAX_PAPER_PAGES
    max_extracted_characters: int = MAX_PAPER_EXTRACTED_CHARACTERS
    max_evidence_characters: int = MAX_EVIDENCE_CHARACTERS
    nontrivial_page_characters: int = 40
    artifacts_path: str = "/opt/docling/models"
    document_timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        languages = tuple(dict.fromkeys(str(v).strip().lower()
                                        for v in self.ocr_languages if str(v).strip()))
        unknown = sorted(set(languages) - ALLOWED_OCR_LANGUAGES)
        if not languages:
            raise ValueError("at least one OCR language is required")
        if unknown:
            raise ValueError("unsupported OCR language(s): " + ", ".join(unknown))
        if not 1 <= self.max_file_bytes <= MAX_PAPER_FILE_BYTES:
            raise ValueError(f"paper file limit must be 1-{MAX_PAPER_FILE_BYTES} bytes")
        if not 1 <= self.max_pages <= MAX_PAPER_PAGES:
            raise ValueError(f"paper page limit must be 1-{MAX_PAPER_PAGES}")
        if not 1 <= self.max_extracted_characters <= MAX_PAPER_EXTRACTED_CHARACTERS:
            raise ValueError(
                "paper extracted-character limit must be 1-"
                f"{MAX_PAPER_EXTRACTED_CHARACTERS}")
        if not 1_000 <= self.max_evidence_characters <= 48_000:
            raise ValueError("evidence block limit must be 1,000-48,000 characters")
        object.__setattr__(self, "ocr_languages", languages)

    @property
    def config_hash(self) -> str:
        data = asdict(self)
        data["ocr_languages"] = sorted(self.ocr_languages)
        data.update({
            "parser_version": PARSER_VERSION,
            "remote_services": False,
            "external_plugins": False,
            "picture_description": False,
            "table_mode": "accurate",
            "cpu_only": True,
        })
        return _digest(data)


@dataclass(frozen=True)
class ParsedBlock:
    body: str
    page_number: int
    kind: str = "prose"
    section_path: tuple[str, ...] = ()
    bbox: dict[str, Any] | None = None
    extraction_method: str = "docling-layout"
    quality_grade: str = "UNKNOWN"
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParsedPageQuality:
    page_number: int
    grade: str = "UNKNOWN"
    scores: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedPaper:
    page_count: int
    blocks: tuple[ParsedBlock, ...]
    page_quality: tuple[ParsedPageQuality, ...] = ()
    document_grade: str = "UNKNOWN"
    confidence: dict[str, Any] = field(default_factory=dict)
    parser_name: str = "injected"
    parser_version: str = "test"
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PaperEvidence:
    chunk_index: int
    evidence_id: str
    page_number: int
    section_path: tuple[str, ...]
    bbox: dict[str, Any] | None
    kind: str
    body: str
    body_hash: str
    extraction_method: str
    quality_grade: str
    flags: tuple[str, ...]
    estimated_tokens: int

    def as_dict(self, *, include_body: bool = True) -> dict[str, Any]:
        value = asdict(self)
        if not include_body:
            value.pop("body", None)
        return value


@dataclass(frozen=True)
class PaperExtractionResult:
    source_hash: str
    size_bytes: int
    page_count: int
    extracted_characters: int
    parser_name: str
    parser_version: str
    parser_config_hash: str
    document_grade: str
    evidence: tuple[PaperEvidence, ...]
    quality_report: dict[str, Any]
    coverage_report: dict[str, Any]
    warnings: tuple[str, ...] = ()


class PaperParser(Protocol):
    def parse(self, path: Path, config: PaperExtractionConfig) -> ParsedPaper: ...


def _model_dump(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    for method in ("model_dump", "dict"):
        fn = getattr(value, method, None)
        if callable(fn):
            try:
                return fn(mode="json") if method == "model_dump" else fn()
            except TypeError:
                return fn()
    return {}


def _filtered_model(model_type, **values):
    """Construct a Docling pydantic model across minor API field changes."""
    fields = getattr(model_type, "model_fields", {})
    accepted = {key: value for key, value in values.items()
                if not fields or key in fields}
    return model_type(**accepted)


def _bbox_dict(value: Any) -> dict[str, Any] | None:
    raw = _model_dump(value)
    if not raw:
        raw = {name: getattr(value, name) for name in ("l", "t", "r", "b")
               if hasattr(value, name)} if value is not None else {}
    if not raw:
        return None
    output: dict[str, Any] = {}
    for key in ("l", "t", "r", "b"):
        number = raw.get(key)
        if isinstance(number, (int, float)) and math.isfinite(float(number)):
            output[key] = round(float(number), 3)
    origin = raw.get("coord_origin") or raw.get("origin")
    if hasattr(origin, "value"):
        origin = origin.value
    if origin:
        output["origin"] = str(origin)
    return output or None


def _label_name(item: Any) -> str:
    value = getattr(item, "label", "") or item.__class__.__name__
    if hasattr(value, "value"):
        value = value.value
    return str(value).rsplit(".", 1)[-1].replace("-", "_").lower()


def _table_text(item: Any, document: Any) -> str:
    for target in (item, getattr(item, "data", None)):
        if target is None:
            continue
        for method in ("export_to_markdown", "to_markdown"):
            fn = getattr(target, method, None)
            if not callable(fn):
                continue
            for args in ((document,), (),):
                try:
                    value = fn(*args)
                    if value:
                        return str(value)
                except (TypeError, AttributeError):
                    continue
    return str(getattr(item, "text", "") or "")


def _caption_text(item: Any, document: Any) -> str:
    for method in ("caption_text", "get_caption_text"):
        fn = getattr(item, method, None)
        if callable(fn):
            try:
                value = fn(document)
                if value:
                    return str(value)
            except (TypeError, AttributeError):
                pass
    return str(getattr(item, "text", "") or "")


def _kind(label: str, body: str, section_path: tuple[str, ...]) -> str:
    if label in {"title", "section_header", "heading", "subtitle"}:
        return "heading"
    if label in {"table"} or "table" in label:
        return "table"
    if label in {"formula", "equation"} or "formula" in label:
        return "equation"
    if label in {"caption", "picture", "figure", "image"} or any(
            token in label for token in ("picture", "figure", "image")):
        return "caption" if label == "caption" else "visual"
    if "footnote" in label or label in {"page_footer", "foot_note"}:
        return "footnote"
    if "reference" in label or any(
            heading.casefold().strip() in {
                "references", "bibliography", "referencias", "bibliografía",
                "références", "bibliographie", "literatur", "literaturverzeichnis",
                "quellen",
            }
            for heading in section_path[-1:]):
        return "reference"
    if _DEFINITION.match(body):
        return "definition"
    return "prose"


def _docling_page_quality(confidence: Any) -> tuple[str, list[ParsedPageQuality], dict]:
    raw = _model_dump(confidence)
    document_grade = _grade(
        getattr(confidence, "low_grade", None)
        or raw.get("low_grade") or raw.get("mean_grade"))
    pages_value = getattr(confidence, "pages", None)
    if pages_value is None:
        pages_value = raw.get("pages") or {}
    if isinstance(pages_value, dict):
        rows = list(pages_value.items())
    elif isinstance(pages_value, list):
        rows = list(enumerate(pages_value, 1))
    else:
        rows = []
    pages: list[ParsedPageQuality] = []
    for fallback_number, report in rows:
        report_raw = _model_dump(report)
        try:
            page_number = int(
                report_raw.get("page_no") or report_raw.get("page_number")
                or getattr(report, "page_no", None) or fallback_number)
        except (TypeError, ValueError):
            continue
        if page_number == 0:
            page_number = int(fallback_number) + 1
        grade = _grade(
            getattr(report, "low_grade", None) or report_raw.get("low_grade")
            or report_raw.get("mean_grade"))
        pages.append(ParsedPageQuality(page_number, grade, report_raw))
    return document_grade, pages, raw


class DoclingPaperParser:
    """Accuracy-oriented, CPU-only Docling parser with Tesseract OCR fallback."""

    name = "docling"
    version = PARSER_VERSION

    def parse(self, path: Path, config: PaperExtractionConfig) -> ParsedPaper:
        try:
            from docling.datamodel.accelerator_options import (
                AcceleratorDevice, AcceleratorOptions,
            )
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import (
                PdfPipelineOptions, TableFormerMode, TesseractCliOcrOptions,
            )
            from docling.document_converter import DocumentConverter, PdfFormatOption
        except ImportError as exc:
            raise PaperExtractionError(
                "Docling is not installed in this worker; route paper_extract to "
                "the dedicated paper-worker image") from exc

        artifacts_path = Path(config.artifacts_path)
        if not artifacts_path.is_dir():
            raise PaperExtractionError(
                f"pinned Docling model directory is missing: {artifacts_path}")

        # Do not rely on DocumentConverter.max_num_pages as an admission
        # check: converter limits may return a successful prefix. Count pages
        # independently first, then require Docling to return the same count.
        # pypdfium2 ships with Docling's PDF extra and performs no network I/O.
        pdf_document = None
        try:
            from pypdfium2 import PdfDocument

            pdf_document = PdfDocument(str(path))
            preflight_page_count = len(pdf_document)
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            if "password" in message.casefold() or "encrypt" in message.casefold():
                raise PaperExtractionError(
                    "encrypted/password-protected PDFs are not supported") from exc
            raise PaperExtractionError(
                f"could not read the complete PDF page index: {message}") from exc
        finally:
            if pdf_document is not None:
                pdf_document.close()
        if not 1 <= preflight_page_count <= config.max_pages:
            raise PaperExtractionError(
                f"PDF has {preflight_page_count} pages; the configured limit is "
                f"{config.max_pages}; no partial paper analysis was stored")

        ocr = _filtered_model(
            TesseractCliOcrOptions,
            lang=list(config.ocr_languages),
            force_full_page_ocr=False,
            tesseract_cmd="tesseract",
        )
        pipeline = _filtered_model(
            PdfPipelineOptions,
            artifacts_path=artifacts_path,
            enable_remote_services=False,
            allow_external_plugins=False,
            document_timeout=config.document_timeout_seconds,
            accelerator_options=AcceleratorOptions(
                num_threads=max(1, int(os.environ.get("OMP_NUM_THREADS", "4"))),
                device=AcceleratorDevice.CPU,
            ),
            do_ocr=True,
            do_table_structure=True,
            do_formula_enrichment=True,
            do_code_enrichment=False,
            do_picture_description=False,
            do_picture_classification=False,
            generate_picture_images=False,
            generate_page_images=False,
            ocr_options=ocr,
        )
        table_options = getattr(pipeline, "table_structure_options", None)
        if table_options is not None and hasattr(table_options, "mode"):
            table_options.mode = TableFormerMode.ACCURATE

        # These variables make accidental runtime downloads fail closed.  The
        # image bakes all parser model artifacts during its build.
        old_hf_offline = os.environ.get("HF_HUB_OFFLINE")
        old_transformers_offline = os.environ.get("TRANSFORMERS_OFFLINE")
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        try:
            converter = DocumentConverter(
                allowed_formats=[InputFormat.PDF],
                format_options={
                    InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline),
                },
            )
            result = converter.convert(
                path,
                raises_on_error=True,
                max_num_pages=config.max_pages,
                max_file_size=config.max_file_bytes,
            )
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            if "password" in message.casefold() or "encrypt" in message.casefold():
                raise PaperExtractionError(
                    "encrypted/password-protected PDFs are not supported") from exc
            raise PaperExtractionError(f"Docling could not extract this PDF: {message}") from exc
        finally:
            if old_hf_offline is None:
                os.environ.pop("HF_HUB_OFFLINE", None)
            else:
                os.environ["HF_HUB_OFFLINE"] = old_hf_offline
            if old_transformers_offline is None:
                os.environ.pop("TRANSFORMERS_OFFLINE", None)
            else:
                os.environ["TRANSFORMERS_OFFLINE"] = old_transformers_offline

        document = getattr(result, "document", None)
        if document is None:
            raise PaperExtractionError("Docling returned no document")
        raw_status = getattr(result, "status", "success")
        status = str(getattr(raw_status, "value", raw_status)).rsplit(".", 1)[-1].casefold()
        if status != "success":
            raise PaperExtractionError(
                f"Docling conversion status was {status}; partial paper extraction "
                "is not allowed")

        pages = getattr(document, "pages", {}) or {}
        page_count = len(pages)
        if page_count != preflight_page_count:
            raise PaperExtractionError(
                f"Docling returned {page_count} of {preflight_page_count} pages; "
                "partial paper extraction is not allowed")
        blocks: list[ParsedBlock] = []
        headings: list[tuple[int, str]] = []
        iterator = document.iterate_items()
        for item, level in iterator:
            label = _label_name(item)
            level = int(level or 0)
            if label in {"title", "section_header", "heading", "subtitle"}:
                heading_text = str(getattr(item, "text", "") or "").strip()
                if heading_text:
                    headings = [(depth, title) for depth, title in headings
                                if depth < level]
                    headings.append((level, heading_text))
            section_path = tuple(title for _depth, title in headings)
            if "table" in label:
                body = _table_text(item, document)
            elif any(token in label for token in ("picture", "figure", "image")):
                body = _caption_text(item, document)
            else:
                body = str(getattr(item, "text", "") or "")
            provenance = list(getattr(item, "prov", None) or [])
            if not provenance:
                provenance = [None]
            for prov in provenance:
                page_number = int(getattr(prov, "page_no", 1) or 1)
                bbox = _bbox_dict(getattr(prov, "bbox", None))
                block_kind = _kind(label, body, section_path)
                flags: list[str] = []
                if block_kind in {"visual", "caption"}:
                    flags.append("visual_review_needed")
                # Preserve an otherwise-textless visual location without
                # inventing a description of the visual itself.
                block_body = body
                if block_kind == "visual" and not block_body.strip():
                    block_body = "Visual element (not interpreted; review the source PDF)."
                if not block_body.strip():
                    continue
                method = "docling-layout"
                source_name = str(
                    getattr(prov, "source", "") or getattr(item, "orig", "")
                ).casefold()
                if "ocr" in source_name:
                    method = "tesseract-ocr"
                blocks.append(ParsedBlock(
                    body=block_body,
                    page_number=page_number,
                    kind=block_kind,
                    section_path=section_path,
                    bbox=bbox,
                    extraction_method=method,
                    flags=tuple(flags),
                ))

        document_grade, page_quality, confidence = _docling_page_quality(
            getattr(result, "confidence", None))
        quality_by_page = {row.page_number: row.grade for row in page_quality}
        blocks = [ParsedBlock(
            body=block.body,
            page_number=block.page_number,
            kind=block.kind,
            section_path=block.section_path,
            bbox=block.bbox,
            extraction_method=block.extraction_method,
            quality_grade=quality_by_page.get(block.page_number, "UNKNOWN"),
            flags=block.flags,
        ) for block in blocks]
        if page_count == 0 and blocks:
            page_count = max(block.page_number for block in blocks)
        errors = tuple(
            str(error) for error in (getattr(result, "errors", None) or []) if error)
        return ParsedPaper(
            page_count=page_count,
            blocks=tuple(blocks),
            page_quality=tuple(page_quality),
            document_grade=document_grade,
            confidence=confidence,
            parser_name=self.name,
            parser_version=self.version,
            warnings=errors,
        )


def _split_complete(body: str, max_characters: int) -> list[str]:
    """Boundary-aware split that never drops or repeats a source character."""
    if len(body) <= max_characters:
        return [body]
    parts: list[str] = []
    start = 0
    while start < len(body):
        hard_end = min(len(body), start + max_characters)
        end = hard_end
        if hard_end < len(body):
            minimum = start + max(max_characters // 2, 1)
            candidates = [
                body.rfind("\n\n", minimum, hard_end),
                body.rfind("\n", minimum, hard_end),
                body.rfind(" ", minimum, hard_end),
            ]
            boundary = max(candidates)
            if boundary >= minimum:
                # Keep delimiters in exactly one output segment.
                end = boundary + (2 if body[boundary:boundary + 2] == "\n\n" else 1)
        if end <= start:
            end = hard_end
        parts.append(body[start:end])
        start = end
    assert "".join(parts) == body
    return parts


def _stable_evidence_id(
    source_hash: str,
    *,
    page_number: int,
    kind: str,
    section_path: tuple[str, ...],
    bbox: dict[str, Any] | None,
    body_hash: str,
    part_number: int,
    occurrence: int,
) -> str:
    identity = {
        "schema": PAPER_EVIDENCE_SCHEMA_VERSION,
        "source": source_hash,
        "page": page_number,
        "kind": kind,
        "section": section_path,
        "bbox": bbox,
        "body_hash": body_hash,
        "part": part_number,
        "occurrence": occurrence,
    }
    return f"P{page_number:04d}-{_digest(identity)[:20].upper()}"


def _normalized_kind(value: str, body: str, section: tuple[str, ...]) -> str:
    known = {
        "prose", "heading", "definition", "equation", "table", "caption",
        "visual", "footnote", "reference",
    }
    candidate = str(value or "prose").strip().lower().replace("-", "_")
    if candidate == "prose" and _DEFINITION.match(body):
        return "definition"
    return candidate if candidate in known else _kind(candidate, body, section)


def extract_pdf(
    path: str | Path,
    config: PaperExtractionConfig | None = None,
    *,
    parser: PaperParser | Callable[[Path, PaperExtractionConfig], ParsedPaper] | None = None,
) -> PaperExtractionResult:
    """Extract all admitted evidence from one immutable PDF.

    No representative sampling or prefix slicing occurs.  If the PDF exceeds
    any v1 admission limit, the entire operation fails before rows are written.
    A parser may be injected for deterministic fixtures; production defaults to
    the offline Docling/Tesseract adapter.
    """
    config = config or PaperExtractionConfig()
    source_path = Path(path).resolve()
    if not source_path.is_file():
        raise PaperExtractionError(f"paper source does not exist: {source_path}")
    if source_path.suffix.casefold() != ".pdf":
        raise PaperExtractionError("paper ingestion supports PDF files only")
    size_bytes = source_path.stat().st_size
    if size_bytes > config.max_file_bytes:
        raise PaperExtractionError(
            f"PDF is {size_bytes} bytes; the configured limit is {config.max_file_bytes}")
    with source_path.open("rb") as handle:
        signature = handle.read(1024)
    if not signature.lstrip().startswith(b"%PDF-"):
        raise PaperExtractionError("file does not have a valid PDF signature")
    source_hash = file_sha256(source_path)

    selected_parser = parser or DoclingPaperParser()
    parsed = (selected_parser(source_path, config)
              if callable(selected_parser) and not hasattr(selected_parser, "parse")
              else selected_parser.parse(source_path, config))
    if not isinstance(parsed, ParsedPaper):
        raise TypeError("paper parser must return ParsedPaper")
    if not 1 <= parsed.page_count <= config.max_pages:
        raise PaperExtractionError(
            f"PDF has {parsed.page_count} pages; the configured limit is {config.max_pages}")

    quality_by_page = {
        row.page_number: _grade(row.grade) for row in parsed.page_quality
    }
    page_characters: Counter[int] = Counter()
    page_structured: set[int] = set()
    method_counts: Counter[str] = Counter()
    kind_counts: Counter[str] = Counter()
    evidence: list[PaperEvidence] = []
    identities: Counter[str] = Counter()
    extracted_characters = 0
    for parsed_block in parsed.blocks:
        page = int(parsed_block.page_number)
        if not 1 <= page <= parsed.page_count:
            raise PaperExtractionError(
                f"parser returned out-of-range page {page} for {parsed.page_count}-page PDF")
        body = str(parsed_block.body or "").replace("\x00", "").replace("\r\n", "\n")
        if not body.strip():
            continue
        section = tuple(str(value).strip() for value in parsed_block.section_path
                        if str(value).strip())
        kind = _normalized_kind(parsed_block.kind, body, section)
        bbox = _bbox_dict(parsed_block.bbox)
        method = str(parsed_block.extraction_method or "unknown")
        grade = _grade(parsed_block.quality_grade)
        if grade == "UNKNOWN":
            grade = quality_by_page.get(page, "UNKNOWN")
        base_flags = {str(value) for value in parsed_block.flags if str(value)}
        if kind in {"visual", "caption"}:
            base_flags.add("visual_review_needed")
        if kind in {"table", "equation"} and (grade in {"POOR", "FAIR"} or not bbox):
            base_flags.add("unreliable_extraction")
        segments = _split_complete(body, config.max_evidence_characters)
        for part_number, segment in enumerate(segments, 1):
            body_hash = hashlib.sha256(segment.encode("utf-8")).hexdigest()
            identity_base = _digest({
                "page": page, "kind": kind, "section": section, "bbox": bbox,
                "body_hash": body_hash, "part": part_number,
            })
            occurrence = identities[identity_base]
            identities[identity_base] += 1
            evidence_id = _stable_evidence_id(
                source_hash,
                page_number=page,
                kind=kind,
                section_path=section,
                bbox=bbox,
                body_hash=body_hash,
                part_number=part_number,
                occurrence=occurrence,
            )
            flags = set(base_flags)
            if len(segments) > 1:
                flags.add(f"source_block_part_{part_number}_of_{len(segments)}")
            item = PaperEvidence(
                chunk_index=len(evidence),
                evidence_id=evidence_id,
                page_number=page,
                section_path=section,
                bbox=bbox,
                kind=kind,
                body=segment,
                body_hash=body_hash,
                extraction_method=method,
                quality_grade=grade,
                flags=tuple(sorted(flags)),
                estimated_tokens=max(1, math.ceil(len(segment) / 4)),
            )
            evidence.append(item)
            extracted_characters += len(segment)
            page_characters[page] += len(segment)
            method_counts[method] += 1
            kind_counts[kind] += 1
            if kind in {"table", "equation", "caption", "visual"}:
                page_structured.add(page)
            if extracted_characters > config.max_extracted_characters:
                raise PaperExtractionError(
                    "extracted text exceeds the configured "
                    f"{config.max_extracted_characters}-character limit; no partial "
                    "paper analysis was stored")
    if not evidence:
        raise PaperExtractionError("the PDF produced no readable evidence blocks")

    nontrivial_pages = sorted(
        page for page in range(1, parsed.page_count + 1)
        if page_characters[page] >= config.nontrivial_page_characters
        or page in page_structured)
    poor_pages = sorted(
        page for page in nontrivial_pages if quality_by_page.get(page) == "POOR")
    document_grade = _grade(parsed.document_grade)
    document_poor_without_pages = document_grade == "POOR" and not poor_pages
    blocked_reasons: list[dict[str, Any]] = [
        {"kind": "page", "page_number": page, "grade": "POOR"}
        for page in poor_pages
    ]
    if document_poor_without_pages:
        blocked_reasons.append({
            "kind": "document", "grade": "POOR",
            "message": "Docling reported POOR document quality without page detail",
        })
    page_reports = []
    scores_by_page = {row.page_number: row.scores for row in parsed.page_quality}
    for page in range(1, parsed.page_count + 1):
        page_reports.append({
            "page_number": page,
            "grade": quality_by_page.get(page, "UNKNOWN"),
            "extracted_characters": page_characters[page],
            "nontrivial": page in nontrivial_pages,
            "scores": scores_by_page.get(page, {}),
        })
    quality_report = {
        "document_grade": document_grade,
        "analysis_blocked": bool(blocked_reasons),
        "blocked_reasons": blocked_reasons,
        "poor_pages": poor_pages,
        "pages": page_reports,
        "confidence": parsed.confidence,
        "review_policy": {
            "poor_nontrivial_pages_block": True,
            "critical_claims_may_not_rely_only_on_acknowledged_gaps": True,
        },
    }
    visual_ids = [item.evidence_id for item in evidence
                  if "visual_review_needed" in item.flags]
    unreliable_ids = [item.evidence_id for item in evidence
                      if "unreliable_extraction" in item.flags]
    coverage_report = {
        "source_hash": source_hash,
        "source_bytes": size_bytes,
        "page_count": parsed.page_count,
        "pages_with_evidence": len(page_characters),
        "evidence_block_count": len(evidence),
        "extracted_characters": extracted_characters,
        "kind_counts": dict(sorted(kind_counts.items())),
        "extraction_method_counts": dict(sorted(method_counts.items())),
        "visual_review_evidence_ids": visual_ids,
        "unreliable_evidence_ids": unreliable_ids,
        "unmapped_evidence_count": len(evidence),
        "limits": {
            "max_file_bytes": config.max_file_bytes,
            "max_pages": config.max_pages,
            "max_extracted_characters": config.max_extracted_characters,
            "max_evidence_characters": config.max_evidence_characters,
        },
        "sampling": False,
        "prefix_truncation": False,
    }
    return PaperExtractionResult(
        source_hash=source_hash,
        size_bytes=size_bytes,
        page_count=parsed.page_count,
        extracted_characters=extracted_characters,
        parser_name=parsed.parser_name,
        parser_version=parsed.parser_version,
        parser_config_hash=config.config_hash,
        document_grade=document_grade,
        evidence=tuple(evidence),
        quality_report=quality_report,
        coverage_report=coverage_report,
        warnings=tuple(parsed.warnings),
    )


def paper_source_path(source: Any) -> Path:
    """Resolve a stored source path, refusing escapes outside the library."""
    relative = Path(str(getattr(source, "relative_path", "") or ""))
    if relative.is_absolute():
        raise PaperExtractionError("paper source path must be library-relative")
    root = settings.library_dir.resolve()
    resolved = (root / relative).resolve()
    if resolved != root and root not in resolved.parents:
        raise PaperExtractionError("paper source path escapes the library directory")
    return resolved


def paper_source_for_project(session: Session, project_id: int):
    from .models import PaperSource

    return session.exec(select(PaperSource).where(
        PaperSource.project_id == project_id)).first()


def persist_extraction(session: Session, source: Any,
                       result: PaperExtractionResult) -> list[Any]:
    """Atomically replace derived extraction rows for the same immutable PDF."""
    from .models import (
        PaperChunk, PaperChunkEmbedding, PaperSynthesisCache, utcnow,
    )

    if str(getattr(source, "source_hash", "") or "") not in {"", result.source_hash}:
        raise PaperExtractionError(
            "stored paper source hash does not match the extracted PDF; import a "
            "revised PDF as a new project")
    existing_chunks = session.exec(select(PaperChunk).where(
        PaperChunk.source_id == source.id)).all()
    session.exec(text(
        "DELETE FROM paper_chunk_fts WHERE source_id=:source_id"
    ).bindparams(source_id=source.id))
    chunk_ids = [chunk.id for chunk in existing_chunks if chunk.id is not None]
    if chunk_ids:
        embeddings = session.exec(select(PaperChunkEmbedding).where(
            PaperChunkEmbedding.chunk_id.in_(chunk_ids))).all()
        for embedding in embeddings:
            session.delete(embedding)
    for chunk in existing_chunks:
        session.delete(chunk)
    caches = session.exec(select(PaperSynthesisCache).where(
        PaperSynthesisCache.source_id == source.id)).all()
    for cache in caches:
        session.delete(cache)
    session.flush()

    rows = []
    for item in result.evidence:
        row = PaperChunk(
            source_id=source.id,
            chunk_index=item.chunk_index,
            evidence_id=item.evidence_id,
            page_number=item.page_number,
            section_path=json.dumps(item.section_path, ensure_ascii=False),
            bbox=json.dumps(item.bbox, sort_keys=True) if item.bbox else "{}",
            kind=item.kind,
            body=item.body,
            body_hash=item.body_hash,
            extraction_method=item.extraction_method,
            quality_grade=item.quality_grade,
            flags=json.dumps(item.flags),
            estimated_tokens=item.estimated_tokens,
        )
        session.add(row)
        session.flush()
        session.exec(text(
            "INSERT INTO paper_chunk_fts("
            "body, chunk_id, source_id, project_id, page_number, evidence_id"
            ") VALUES (:body, :chunk_id, :source_id, :project_id, :page, :evidence_id)"
        ).bindparams(
            body=item.body,
            chunk_id=row.id,
            source_id=source.id,
            project_id=source.project_id,
            page=item.page_number,
            evidence_id=item.evidence_id,
        ))
        rows.append(row)
    source.source_hash = result.source_hash
    source.size_bytes = result.size_bytes
    source.page_count = result.page_count
    source.extracted_characters = result.extracted_characters
    source.parser_version = result.parser_version
    source.parser_config_hash = result.parser_config_hash
    source.status = ("review_required" if result.quality_report["analysis_blocked"]
                     else "ready")
    source.quality_grade = result.document_grade
    source.quality_report = json.dumps(result.quality_report, sort_keys=True)
    source.coverage_report = json.dumps(result.coverage_report, sort_keys=True)
    source.updated = utcnow()
    session.add(source)
    session.commit()
    for row in rows:
        session.refresh(row)
    return rows


def acknowledged_page_numbers(source: Any) -> set[int]:
    value = getattr(source, "acknowledged_pages", "[]")
    try:
        raw = value if isinstance(value, (dict, list)) else json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        raw = []
    values = raw.keys() if isinstance(raw, dict) else raw
    output = set()
    for value in values:
        if isinstance(value, dict):
            value = value.get("page") or value.get("page_number")
        try:
            output.add(int(value))
        except (TypeError, ValueError):
            continue
    return output


def extraction_blockers(source: Any) -> list[dict[str, Any]]:
    report = _json(getattr(source, "quality_report", "{}"), {})
    acknowledged = acknowledged_page_numbers(source)
    blockers = []
    for reason in report.get("blocked_reasons", []):
        if not isinstance(reason, dict):
            continue
        page = reason.get("page_number")
        if page is not None and int(page) in acknowledged:
            continue
        blockers.append(reason)
    return blockers


def require_analysis_ready(source: Any) -> None:
    status = str(getattr(source, "status", "") or "")
    if status not in {"extracted", "review_required", "analyzed", "ready"}:
        raise PaperAnalysisBlocked(
            f"paper extraction is not ready for analysis (status={status or 'unknown'})")
    blockers = extraction_blockers(source)
    if blockers:
        pages = [str(reason.get("page_number")) for reason in blockers
                 if reason.get("page_number") is not None]
        suffix = f" on page(s) {', '.join(pages)}" if pages else ""
        raise PaperAnalysisBlocked(
            "paper analysis is blocked by POOR extraction quality" + suffix
            + "; replace the PDF or acknowledge each named page with a reason")


def list_paper_evidence(session: Session, source_id: int,
                        *, include_body: bool = True) -> list[dict[str, Any]]:
    from .models import PaperChunk

    chunks = session.exec(select(PaperChunk).where(
        PaperChunk.source_id == source_id).order_by(PaperChunk.chunk_index)).all()
    output = []
    for chunk in chunks:
        row = {
            "chunk_id": chunk.id,
            "evidence_id": chunk.evidence_id,
            "page_number": chunk.page_number,
            "section_path": _json(chunk.section_path, []),
            "bbox": _json(chunk.bbox, {}),
            "kind": chunk.kind,
            "body_hash": chunk.body_hash,
            "extraction_method": chunk.extraction_method,
            "quality_grade": chunk.quality_grade,
            "flags": _json(chunk.flags, []),
            "estimated_tokens": chunk.estimated_tokens,
        }
        if include_body:
            row["body"] = chunk.body
        output.append(row)
    return output


def validate_paper_citations(session: Session, source_id: int,
                             evidence_ids: Iterable[str]) -> dict[str, Any]:
    from .models import PaperChunk

    requested = sorted({str(value) for value in evidence_ids if value})
    if not requested:
        return {"valid": {}, "invalid": []}
    rows = session.exec(select(PaperChunk).where(
        PaperChunk.source_id == source_id,
        PaperChunk.evidence_id.in_(requested),
    )).all()
    valid = {row.evidence_id: row for row in rows}
    return {"valid": valid, "invalid": sorted(set(requested) - set(valid))}


def source_citation(project_id: int, source: Any, chunk: Any) -> dict[str, Any]:
    section = _json(getattr(chunk, "section_path", "[]"), [])
    excerpt = re.sub(r"\s+", " ", str(getattr(chunk, "body", ""))).strip()[:500]
    page = int(getattr(chunk, "page_number", 1) or 1)
    return {
        "kind": "paper",
        "source_hash": str(getattr(source, "source_hash", "")),
        "evidence_id": str(getattr(chunk, "evidence_id", "")),
        "page": page,
        "section": section,
        "bounding_box": _json(getattr(chunk, "bbox", "{}"), {}),
        "excerpt": excerpt,
        "url": f"/api/papers/{project_id}/source#page={page}",
    }


def validate_and_render_citations(
    body: str,
    *,
    project_id: int,
    source: Any,
    evidence: Iterable[Any],
    require: bool = True,
) -> tuple[str, int]:
    """Validate ``[P:evidence-id]`` tokens and render clickable page links."""
    evidence_rows = list(evidence)
    known = {str(getattr(item, "evidence_id", None) or item.get("evidence_id")): item
             for item in evidence_rows}
    visible = _VISIBLE_CITATION.findall(body)
    hidden = _HIDDEN_CITATION.findall(body)
    cited = visible + hidden
    unknown = sorted(set(cited) - set(known))
    if unknown:
        raise RuntimeError(
            "model returned invalid paper evidence citation(s): "
            + ", ".join(unknown[:10]))
    if require and evidence_rows and not cited:
        raise RuntimeError("paper document contained no validated evidence citations")

    def get(item: Any, name: str, default: Any = None) -> Any:
        return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)

    def replace(match: re.Match) -> str:
        evidence_id = match.group(1)
        item = known[evidence_id]
        page = int(get(item, "page_number", 1) or 1)
        section = get(item, "section_path", [])
        if isinstance(section, str):
            section = _json(section, [])
        label = f"p. {page}"
        if section:
            label += " · " + str(section[-1]).replace("]", "")[:80]
        return (
            f"[{label}](/api/papers/{project_id}/source#page={page})"
            f"<!--P:{evidence_id}-->")

    return _VISIBLE_CITATION.sub(replace, body), len(set(cited))
