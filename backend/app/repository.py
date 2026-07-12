"""Safe, static-only GitHub repository acquisition and evidence indexing.

Repository contents are untrusted input.  This module never invokes Git,
package managers, hooks, tests, builds, submodules, Git LFS, or repository code.
It downloads an archive pinned to an immutable commit, extracts regular files
under strict limits, and builds line-addressed evidence for local analysis.
"""
from __future__ import annotations

import fnmatch
import hashlib
import itertools
import json
import logging
import math
import os
import re
import shutil
import stat
import tarfile
import tempfile
import zipfile
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select, text

from .config import settings
from .db import get_session
from .models import (
    Job,
    Project,
    RepositoryChunk,
    RepositoryFile,
    RepositorySnapshot,
    RepositorySource,
    utcnow,
)
from .settings_store import get_setting, set_setting

log = logging.getLogger(__name__)

GITHUB_CREDENTIAL_KEY = "github.credentials.default"
GITHUB_API = "https://api.github.com"
ALLOWED_GITHUB_HOSTS = {
    "github.com",
    "api.github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
}
SCANNER_VERSION = "2"
MAX_TOTAL_FACTS = 2500
MAX_SUBMODULE_DECLARATIONS = 1000
MAX_FACT_EVIDENCE_CHUNKS_PER_FILE = 32
MAX_OMITTED_LINK_FACT_PATHS = 50
MAX_OMITTED_LINK_FACT_CHARS = 4_000

DEFAULT_EXCLUSIONS = [
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    "node_modules",
    "vendor",
    "vendors",
    ".venv",
    "venv",
    "site-packages",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".next",
    ".nuxt",
    ".cache",
    "coverage",
    ".coverage",
    "dist",
    "build",
    "out",
    "obj",
]

_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class RepositoryError(RuntimeError):
    """Safe user-facing repository failure (never contains credentials)."""


class RepositoryLimitError(RepositoryError):
    pass


class RepositoryCanceled(RepositoryError):
    pass


@dataclass(frozen=True)
class GitHubRepository:
    owner: str
    repository: str
    canonical_url: str


def parse_github_url(value: str) -> GitHubRepository:
    """Normalize ``owner/repo`` or a plain GitHub HTTPS repository URL."""
    raw = (value or "").strip()
    if not raw:
        raise ValueError("a GitHub repository URL is required")
    if _CONTROL_RE.search(raw):
        raise ValueError("the GitHub repository URL contains invalid characters")

    if "://" not in raw:
        parts = raw.strip("/").split("/")
        if len(parts) != 2:
            raise ValueError("use a GitHub URL or owner/repository")
        owner, repository = parts
    else:
        parsed = urlsplit(raw)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise ValueError("GitHub repository URLs must use https://")
        if parsed.hostname.rstrip(".").lower() != "github.com" or parsed.port:
            raise ValueError("only github.com repositories are supported")
        if parsed.username or parsed.password:
            raise ValueError("credentials must not be embedded in a repository URL")
        if parsed.query or parsed.fragment:
            raise ValueError("repository URLs cannot contain a query string or fragment")
        decoded = unquote(parsed.path)
        if "%" in decoded or "\\" in decoded:
            raise ValueError("the repository URL path is invalid")
        parts = [part for part in decoded.strip("/").split("/") if part]
        if len(parts) != 2:
            raise ValueError("use the repository root URL, without tree/blob paths")
        owner, repository = parts

    if repository.lower().endswith(".git"):
        repository = repository[:-4]
    if not _OWNER_RE.fullmatch(owner) or not _REPO_RE.fullmatch(repository):
        raise ValueError("the GitHub owner or repository name is invalid")
    if repository in {".", ".."}:
        raise ValueError("the GitHub repository name is invalid")
    canonical = f"https://github.com/{owner}/{repository}"
    return GitHubRepository(owner=owner, repository=repository, canonical_url=canonical)


def validate_github_ref(value: str | None) -> str:
    """Validate a branch, tag, or commit without interpreting it as a path."""
    ref = (value or "").strip()
    if not ref:
        return ""
    if len(ref) > 255 or _CONTROL_RE.search(ref):
        raise ValueError("the GitHub ref is invalid")
    if any(char in ref for char in " ~^:?*[\\") or ".." in ref or "@{" in ref:
        raise ValueError("the GitHub ref contains characters Git does not allow")
    if ref.startswith(("/", ".")) or ref.endswith(("/", ".")) or "//" in ref \
            or any(part.endswith(".lock") for part in ref.split("/")):
        raise ValueError("the GitHub ref is invalid")
    return ref.lower() if _SHA_RE.fullmatch(ref) else ref


def normalize_scope_patterns(values: Iterable[str] | None) -> list[str]:
    clean: list[str] = []
    for value in values or []:
        if not isinstance(value, str):
            raise ValueError("repository scope patterns must be text")
        pattern = (value or "").strip().replace("\\", "/").strip("/")
        if not pattern:
            continue
        if len(pattern) > 500:
            raise ValueError("repository scope patterns cannot exceed 500 characters")
        pure = PurePosixPath(pattern)
        if pure.is_absolute() or ".." in pure.parts or _CONTROL_RE.search(pattern):
            raise ValueError(f"unsafe repository scope pattern: {value!r}")
        if re.match(r"^[A-Za-z]:", pattern):
            raise ValueError(f"unsafe repository scope pattern: {value!r}")
        if pattern not in clean:
            clean.append(pattern)
            if len(clean) > 200:
                raise ValueError("repository scope cannot contain more than 200 patterns")
    return clean


def _matches_scope(path: str, pattern: str) -> bool:
    normalized = path.strip("/")
    base = pattern.rstrip("/")
    return (
        normalized == base
        or normalized.startswith(base + "/")
        or fnmatch.fnmatchcase(normalized, pattern)
    )


def path_in_scope(path: str, include_paths: Iterable[str] | None,
                  exclude_paths: Iterable[str] | None) -> bool:
    includes = list(include_paths or [])
    excludes = list(exclude_paths or [])
    if includes and not any(_matches_scope(path, pattern) for pattern in includes):
        return False
    return not any(_matches_scope(path, pattern) for pattern in excludes)


def set_github_token(token: str) -> None:
    candidate = (token or "").strip()
    if not candidate or len(candidate) > 512 or _CONTROL_RE.search(candidate) \
            or any(char.isspace() for char in candidate):
        raise ValueError("enter a valid fine-grained GitHub token")
    if not candidate.startswith("github_pat_"):
        raise ValueError("use a fine-grained GitHub token beginning with github_pat_")
    set_setting(GITHUB_CREDENTIAL_KEY, {"token": candidate})


def get_github_token(*, required: bool = False) -> str:
    stored = get_setting(GITHUB_CREDENTIAL_KEY) or {}
    token = stored.get("token", "") if isinstance(stored, dict) else ""
    if required and not token:
        raise RepositoryError(
            "this repository requires a configured read-only GitHub credential")
    return token


def delete_github_token() -> None:
    set_setting(GITHUB_CREDENTIAL_KEY, None)


def github_token_configured() -> bool:
    return bool(get_github_token())


def repository_scan_settings() -> dict:
    configured = get_setting("repository.scan") or {}
    defaults = {
        "max_download_bytes": settings.max_repository_download_bytes,
        "max_unpacked_bytes": settings.max_repository_unpacked_bytes,
        "max_files": settings.max_repository_files,
        "max_file_bytes": settings.max_repository_file_bytes,
        "max_text_file_bytes": settings.max_repository_text_file_bytes,
        "max_indexed_bytes": settings.max_repository_indexed_bytes,
        "chunk_lines": settings.repository_chunk_lines,
        "chunk_chars": settings.repository_chunk_chars,
        "max_compression_ratio": settings.repository_max_compression_ratio,
        "max_map_chunks": settings.repository_max_map_chunks,
        "max_map_input_chars": settings.repository_max_map_input_chars,
        "default_exclusions": list(DEFAULT_EXCLUSIONS),
    }
    for key, value in configured.items():
        if key in defaults:
            defaults[key] = value
    return defaults


def validate_repository_local_model(value: str) -> str:
    from .llm import validate_local_ollama_model

    return validate_local_ollama_model(value)


def repository_local_model() -> str:
    return validate_repository_local_model(
        str(get_setting("repository.local_model") or settings.repository_local_model))


def repository_scan_config_hash(
    source: RepositorySource, *, settings_snapshot: dict | None = None,
) -> str:
    payload = {
        "scanner_version": SCANNER_VERSION,
        "settings": settings_snapshot if settings_snapshot is not None
        else repository_scan_settings(),
        "include_paths": source.include_paths,
        "exclude_paths": source.exclude_paths,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        .encode("utf-8")
    ).hexdigest()


def _github_headers(token: str = "") -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Synapse-repository-analyzer",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _validate_github_transport_url(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").rstrip(".").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise RepositoryError("GitHub returned an unsafe archive location") from exc
    if (parsed.scheme.lower() != "https" or host not in ALLOWED_GITHUB_HOSTS
            or port not in {None, 443} or parsed.fragment):
        raise RepositoryError("GitHub returned an unsafe archive location")
    if parsed.username or parsed.password:
        raise RepositoryError("GitHub returned an unsafe archive location")
    return value


def _safe_github_error(status: int, *, private_hint: bool = False) -> RepositoryError:
    if status in {401, 403}:
        return RepositoryError(
            "GitHub denied access; check that the fine-grained token has read-only Contents access")
    if status == 404:
        suffix = " or configure its read-only credential" if private_hint else ""
        return RepositoryError(f"GitHub repository or ref was not found{suffix}")
    if status == 429:
        return RepositoryError("GitHub rate-limited this request; try again later")
    return RepositoryError(f"GitHub request failed with status {status}")


def _github_json(url: str, *, token: str = "") -> dict:
    _validate_github_transport_url(url)
    try:
        with httpx.Client(timeout=httpx.Timeout(30, connect=10), follow_redirects=False) as client:
            response = client.get(url, headers=_github_headers(token))
    except httpx.HTTPError as exc:
        raise RepositoryError("could not connect to GitHub") from exc
    if response.status_code >= 400:
        raise _safe_github_error(response.status_code, private_hint=not bool(token))
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise RepositoryError("GitHub returned an invalid response") from exc
    if not isinstance(payload, dict):
        raise RepositoryError("GitHub returned an invalid response")
    return payload


def _parse_github_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def resolve_repository_ref(repo: GitHubRepository, ref: str = "", *,
                           token: str = "") -> dict:
    chosen = validate_github_ref(ref) or "HEAD"
    encoded = quote(chosen, safe="")
    payload = _github_json(
        f"{GITHUB_API}/repos/{quote(repo.owner, safe='')}/"
        f"{quote(repo.repository, safe='')}/commits/{encoded}", token=token)
    sha = str(payload.get("sha") or "").lower()
    if not _SHA_RE.fullmatch(sha):
        raise RepositoryError("GitHub did not resolve the ref to a commit")
    commit = payload.get("commit") or {}
    committer = commit.get("committer") or commit.get("author") or {}
    return {
        "sha": sha,
        "commit_url": f"{repo.canonical_url}/commit/{sha}",
        "commit_time": _parse_github_time(committer.get("date")),
    }


def _preview_exclusion(path: str, size: int, include_paths: list[str],
                       exclude_paths: list[str], limits: dict) -> str:
    if not path_in_scope(path, include_paths, exclude_paths):
        return "outside_scope"
    parts = {part.casefold() for part in PurePosixPath(path).parts[:-1]}
    if parts.intersection({item.casefold() for item in limits["default_exclusions"]}):
        return "vendor_or_generated_directory"
    if _secret_prone_path(path):
        return "secret_prone"
    if size > limits["max_text_file_bytes"]:
        return "large_file"
    return ""


def preflight_repository(value: str, ref: str = "", *,
                         include_paths: Iterable[str] | None = None,
                         exclude_paths: Iterable[str] | None = None,
                         token: str | None = None, include_tree: bool = True) -> dict:
    """Resolve metadata/SHA and return an approximate, non-downloading preview."""
    repo = parse_github_url(value)
    clean_ref = validate_github_ref(ref)
    includes = normalize_scope_patterns(include_paths)
    excludes = normalize_scope_patterns(exclude_paths)
    credential = get_github_token() if token is None else token
    metadata = _github_json(
        f"{GITHUB_API}/repos/{quote(repo.owner, safe='')}/{quote(repo.repository, safe='')}",
        token=credential,
    )
    default_branch = validate_github_ref(str(metadata.get("default_branch") or ""))
    requested = clean_ref or default_branch
    resolved = resolve_repository_ref(repo, requested, token=credential)
    private = bool(metadata.get("private"))
    if private and not credential:
        raise RepositoryError(
            "this private repository requires a configured read-only GitHub credential")

    limits = repository_scan_settings()
    preview = {
        "available": False,
        "tree_truncated": False,
        "total_files": None,
        "total_bytes": None,
        "eligible_files": None,
        "eligible_bytes": None,
        "submodule_count": None,
        "excluded": {},
    }
    if include_tree:
        try:
            tree = _github_json(
                f"{GITHUB_API}/repos/{quote(repo.owner, safe='')}/"
                f"{quote(repo.repository, safe='')}/git/trees/{resolved['sha']}?recursive=1",
                token=credential,
            )
            reasons: Counter[str] = Counter()
            total_files = total_bytes = eligible_files = eligible_bytes = submodules = 0
            for entry in tree.get("tree") or []:
                if not isinstance(entry, dict):
                    continue
                kind = entry.get("type")
                path = str(entry.get("path") or "")
                if kind == "commit" or entry.get("mode") == "160000":
                    submodules += 1
                    continue
                if kind != "blob" or not path:
                    continue
                size = max(0, int(entry.get("size") or 0))
                total_files += 1
                total_bytes += size
                reason = _preview_exclusion(path, size, includes, excludes, limits)
                if reason:
                    reasons[reason] += 1
                else:
                    eligible_files += 1
                    eligible_bytes += size
            preview = {
                "available": True,
                "tree_truncated": bool(tree.get("truncated")),
                "total_files": total_files,
                "total_bytes": total_bytes,
                "eligible_files": eligible_files,
                "eligible_bytes": eligible_bytes,
                "submodule_count": submodules,
                "excluded": dict(sorted(reasons.items())),
            }
        except RepositoryError:
            # Metadata/SHA preflight is authoritative.  Tree preview is useful
            # but not required (GitHub can truncate/deny it for giant repos).
            pass

    return {
        "provider": "github",
        "host": "github.com",
        "owner": repo.owner,
        "repository": repo.repository,
        "canonical_url": repo.canonical_url,
        "title": str(metadata.get("name") or repo.repository),
        "description": str(metadata.get("description") or ""),
        "default_branch": default_branch,
        "requested_ref": requested,
        "resolved_sha": resolved["sha"],
        "commit_url": resolved["commit_url"],
        "commit_time": resolved["commit_time"],
        "is_private": private,
        # Repository analysis is deliberately local-only for v1, regardless
        # of GitHub visibility. This is a durable source policy, not merely a
        # UI/provider preference.
        "local_only": True,
        "github_size_kib": max(0, int(metadata.get("size") or 0)),
        "include_paths": includes,
        "exclude_paths": excludes,
        "coverage_preview": preview,
        "limits": limits,
        "warnings": [
            "Static analysis only: repository code, hooks, submodules, and Git LFS are not executed or fetched.",
            "All repository-derived model processing and artifacts remain local in this release.",
        ],
    }


def _refresh_repository_visibility(session: Session, source: RepositorySource,
                                   token: str) -> dict:
    """Refresh mutable GitHub visibility before any new source is accepted.

    A public repository can become private without changing its URL. Privacy
    therefore cannot be trusted from import-time metadata: the transition must
    become local-only before a token-authenticated archive can be downloaded or
    any downstream model sees it.
    """
    metadata = _github_json(
        f"{GITHUB_API}/repos/{quote(source.owner, safe='')}/"
        f"{quote(source.repository, safe='')}",
        token=token,
    )
    visibility = metadata.get("private")
    if not isinstance(visibility, bool):
        raise RepositoryError("GitHub returned repository metadata without visibility")
    prior_private = bool(source.is_private)
    prior_local_only = bool(source.local_only)
    source.is_private = visibility
    source.local_only = True
    if visibility:
        if not token:
            raise RepositoryError(
                "this private repository requires a configured read-only GitHub credential")
        source.credential_ref = GITHUB_CREDENTIAL_KEY
        if (not prior_private and not prior_local_only and
                (get_setting("cloud.provider") or get_setting("cloud.last_sync"))):
            source.cloud_purge_pending = True
    source.updated = utcnow()
    session.add(source)
    session.flush()
    if source.local_only or visibility:
        from . import library

        newly_restricted = library.mark_project_restricted(
            session, source.project_id)
        # Persist privacy escalation before any subsequent ref/archive network
        # operation. A later failure must not roll the project back to cloud-
        # eligible state after GitHub has declared it private.
        session.commit()
        session.refresh(source)
        # Rewriting an already-sanitized document would also invalidate its
        # semantic chunks/embeddings on every update check. Only perform the
        # vault rewrite when durable artifact policy actually escalated.
        if newly_restricted:
            library.sanitize_project_artifacts(source.project_id)
        if source.cloud_purge_pending:
            try:
                from .tasks.cloud import enqueue_privacy_purge

                enqueue_privacy_purge(source.id)
            except Exception:
                # The durable flag is an outbox; startup/worker recovery will
                # retry without ever reporting the purge as complete.
                log.exception("could not queue pending cloud privacy purge")
    return {
        "is_private": visibility,
        "local_only": bool(source.local_only or visibility),
        "privacy_changed": prior_private != visibility,
        "cloud_purge_pending": bool(source.cloud_purge_pending),
    }


def check_repository_update(source: RepositorySource, *,
                            session: Session | None = None) -> dict:
    if session is None:
        with get_session() as owned:
            stored = owned.get(RepositorySource, source.id) if source.id else None
            target_source = stored or source
            result = check_repository_update(target_source, session=owned)
            owned.commit()
            source.is_private = target_source.is_private
            source.local_only = target_source.local_only
            source.credential_ref = target_source.credential_ref
            source.cloud_purge_pending = target_source.cloud_purge_pending
            return result
    repo = GitHubRepository(source.owner, source.repository, source.canonical_url)
    token = get_github_token()
    privacy = _refresh_repository_visibility(session, source, token)
    resolved = resolve_repository_ref(repo, source.requested_ref or source.default_branch,
                                      token=token)
    current_sha = ""
    current = current_repository_snapshot(session, source.project_id)
    if current:
        current_sha = current.resolved_sha
    changed = bool(current_sha and resolved["sha"] != current_sha) or not current_sha
    result = {
        "changed": changed,
        "has_update": changed,
        "current_sha": current_sha or None,
        "analyzed_sha": current_sha or None,
        "target_sha": resolved["sha"],
        "latest_sha": resolved["sha"],
        "commit_sha": resolved["sha"],
        "resolved_ref": resolved["sha"],
        "requested_ref": source.requested_ref or source.default_branch,
        "commit_url": resolved["commit_url"],
        "commit_time": resolved["commit_time"],
        "ahead_by": None,
        "changed_files": None,
        **privacy,
    }
    if current_sha and changed:
        try:
            comparison = _github_json(
                f"{GITHUB_API}/repos/{quote(source.owner, safe='')}/"
                f"{quote(source.repository, safe='')}/compare/{current_sha}...{resolved['sha']}",
                token=token,
            )
            result["ahead_by"] = max(0, int(comparison.get("ahead_by") or 0))
            result["changed_files"] = len(comparison.get("files") or [])
        except RepositoryError:
            pass
    return result


def repository_processing_policy(project_id: int) -> bool:
    """Refresh public visibility before derived content can reach a model.

    Known private/local-only sources remain usable offline. An unverifiable
    formerly-public source fails closed for this operation and is forced
    through the local provider without pretending its stored visibility was
    refreshed successfully.
    """
    with get_session() as session:
        project = session.get(Project, project_id)
        if not project or project.source_type != "github":
            return False
        source = repository_source_for_project(session, project_id)
        if source is None:
            return True
        if source.is_private or source.local_only:
            return True
        try:
            policy = _refresh_repository_visibility(
                session, source, get_github_token())
            return bool(policy["local_only"] or policy["is_private"])
        except Exception as exc:
            session.rollback()
            log.warning(
                "could not refresh GitHub visibility for project %s; "
                "forcing this operation local-only: %s", project_id, exc)
            return True


def _report(progress: Callable | None, message: str, current: int | None = None,
            total: int | None = None) -> None:
    if not progress:
        return
    try:
        progress(message, current, total)
    except TypeError:
        progress(message)


def _check_canceled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled and cancelled():
        raise RepositoryCanceled("repository snapshot was canceled")


def _download_archive(repo: GitHubRepository, sha: str, destination: Path, *,
                      token: str, limits: dict, progress: Callable | None,
                      cancelled: Callable[[], bool] | None) -> tuple[str, str, int]:
    url = (
        f"{GITHUB_API}/repos/{quote(repo.owner, safe='')}/"
        f"{quote(repo.repository, safe='')}/tarball/{sha}"
    )
    digest = hashlib.sha256()
    total = 0
    try:
        with httpx.Client(timeout=httpx.Timeout(900, connect=15), follow_redirects=False) as client:
            for _redirect in range(6):
                _check_canceled(cancelled)
                _validate_github_transport_url(url)
                request_token = (
                    token if (urlsplit(url).hostname or "").rstrip(".").lower()
                    == "api.github.com" else ""
                )
                with client.stream(
                        "GET", url, headers=_github_headers(request_token)) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location", "")
                        if not location:
                            raise RepositoryError("GitHub returned an invalid archive redirect")
                        url = _validate_github_transport_url(urljoin(url, location))
                        continue
                    if response.status_code >= 400:
                        raise _safe_github_error(response.status_code, private_hint=not bool(token))
                    declared = int(response.headers.get("content-length") or 0)
                    if declared > limits["max_download_bytes"]:
                        raise RepositoryLimitError("repository archive exceeds the download limit")
                    with destination.open("wb") as handle:
                        for chunk in response.iter_bytes(1024 * 1024):
                            _check_canceled(cancelled)
                            total += len(chunk)
                            if total > limits["max_download_bytes"]:
                                raise RepositoryLimitError(
                                    "repository archive exceeds the download limit")
                            handle.write(chunk)
                            digest.update(chunk)
                            _report(progress, "Downloading pinned repository snapshot", total,
                                    declared or None)
                        handle.flush()
                        os.fsync(handle.fileno())
                    break
            else:
                raise RepositoryError("GitHub returned too many archive redirects")
    except httpx.HTTPError as exc:
        raise RepositoryError("could not download the repository archive") from exc
    if not total:
        raise RepositoryError("GitHub returned an empty repository archive")
    if zipfile.is_zipfile(destination):
        archive_type = "zip"
    elif tarfile.is_tarfile(destination):
        archive_type = "tar"
    else:
        raise RepositoryError("GitHub returned an unsupported repository archive")
    return digest.hexdigest(), archive_type, total


def _archive_name(name: str) -> tuple[str, PurePosixPath | None]:
    if not name or "\x00" in name or "\\" in name or name.startswith("/"):
        raise RepositoryError("repository archive contains an unsafe path")
    pure = PurePosixPath(name.rstrip("/"))
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise RepositoryError("repository archive contains an unsafe path")
    if re.match(r"^[A-Za-z]:", pure.parts[0]):
        raise RepositoryError("repository archive contains an unsafe path")
    root = pure.parts[0]
    relative = PurePosixPath(*pure.parts[1:]) if len(pure.parts) > 1 else None
    return root, relative


def _expected_archive_root(root: str, repo: GitHubRepository, sha: str) -> bool:
    folded = root.casefold()
    prefixes = {
        f"{repo.owner}-{repo.repository}-".casefold(),
        f"{repo.repository}-".casefold(),
    }
    suffix = root.rsplit("-", 1)[-1].casefold()
    return any(folded.startswith(prefix) for prefix in prefixes) \
        and len(suffix) >= 7 and sha.casefold().startswith(suffix)


def _safe_destination(root: Path, relative: PurePosixPath) -> Path:
    destination = root.joinpath(*relative.parts)
    try:
        destination.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise RepositoryError("repository archive attempts to escape its snapshot") from exc
    return destination


def _track_archive_path(relative: PurePosixPath, kind: str,
                        seen: dict[str, tuple[str, str]]) -> None:
    name = relative.as_posix()
    # Record implicit parents as directories.  Besides detecting ordinary
    # file/directory collisions, this prevents a malicious archive from
    # placing materialized children below an omitted symbolic-link entry (or
    # declaring a symbolic link after children were already extracted).
    for depth in range(1, len(relative.parts)):
        parent = PurePosixPath(*relative.parts[:depth]).as_posix()
        previous_parent = seen.get(parent.casefold())
        if previous_parent and (
                previous_parent[0] != parent
                or previous_parent[1] != "directory"):
            raise RepositoryError(
                "repository archive contains duplicate or case-colliding paths")
        if previous_parent is None:
            seen[parent.casefold()] = (parent, "directory")
    folded = name.casefold()
    previous = seen.get(folded)
    if previous and (previous[0] != name or previous[1] != "directory" or kind != "directory"):
        raise RepositoryError("repository archive contains duplicate or case-colliding paths")
    seen[folded] = (name, kind)


def _extract_tar(archive: Path, destination: Path, repo: GitHubRepository, sha: str,
                 *, limits: dict, compressed_bytes: int, progress: Callable | None,
                 cancelled: Callable[[], bool] | None) -> tuple[int, int, list[str]]:
    root_name = ""
    seen: dict[str, tuple[str, str]] = {}
    omitted_links: list[str] = []
    files = total = entries = 0
    with tarfile.open(archive, mode="r:*") as handle:
        for member in handle:
            _check_canceled(cancelled)
            entries += 1
            if entries > limits["max_files"] * 2 + 1000:
                raise RepositoryLimitError("repository archive contains too many entries")
            root, relative = _archive_name(member.name)
            if not root_name:
                root_name = root
                if not _expected_archive_root(root, repo, sha):
                    raise RepositoryError("repository archive has an unexpected root directory")
            if root != root_name:
                raise RepositoryError("repository archive must contain exactly one root directory")
            if relative is None:
                if not member.isdir():
                    raise RepositoryError("repository archive root must be a directory")
                continue
            if member.isdir():
                _track_archive_path(relative, "directory", seen)
                _safe_destination(destination, relative).mkdir(parents=True, exist_ok=True)
                continue
            if member.issym():
                if files + len(omitted_links) + 1 > limits["max_files"]:
                    raise RepositoryLimitError("repository archive contains too many files")
                _track_archive_path(relative, "symlink", seen)
                # Git symbolic links are metadata only in this static workflow.
                # Never resolve their target or create a filesystem link.
                omitted_links.append(relative.as_posix())
                continue
            if not member.isreg():
                raise RepositoryError("repository archive contains an unsafe special file")
            if member.size < 0 or member.size > limits["max_file_bytes"]:
                raise RepositoryLimitError("repository archive contains an oversized file")
            files += 1
            total += member.size
            if files + len(omitted_links) > limits["max_files"]:
                raise RepositoryLimitError("repository archive contains too many files")
            if total > limits["max_unpacked_bytes"]:
                raise RepositoryLimitError("repository archive exceeds the unpacked size limit")
            if compressed_bytes and total > 1024 * 1024 \
                    and total / compressed_bytes > limits["max_compression_ratio"]:
                raise RepositoryLimitError(
                    "repository archive exceeds the compression-ratio limit")
            _track_archive_path(relative, "file", seen)
            target = _safe_destination(destination, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            source = handle.extractfile(member)
            if source is None:
                raise RepositoryError("repository archive contains an unreadable file")
            written = 0
            with source, target.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    _check_canceled(cancelled)
                    written += len(chunk)
                    if written > member.size or written > limits["max_file_bytes"]:
                        raise RepositoryLimitError("repository archive file exceeded its declared size")
                    actual_total = total - member.size + written
                    if compressed_bytes and actual_total > 1024 * 1024 \
                            and actual_total / compressed_bytes > limits["max_compression_ratio"]:
                        raise RepositoryLimitError(
                            "repository archive exceeds the compression-ratio limit")
                    output.write(chunk)
            if written != member.size:
                raise RepositoryError("repository archive contains a truncated file")
            _report(progress, "Safely extracting repository snapshot", files,
                    limits["max_files"])
    if not root_name:
        raise RepositoryError("repository archive is empty")
    if compressed_bytes and total > 1024 * 1024 \
            and total / compressed_bytes > limits["max_compression_ratio"]:
        raise RepositoryLimitError("repository archive exceeds the compression-ratio limit")
    return files, total, omitted_links


def _zip_kind(info: zipfile.ZipInfo) -> str:
    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    if file_type == stat.S_IFLNK:
        return "symlink"
    if info.is_dir():
        return "directory"
    if file_type not in {0, stat.S_IFREG}:
        return "special"
    return "file"


def _extract_zip(archive: Path, destination: Path, repo: GitHubRepository, sha: str,
                 *, limits: dict, compressed_bytes: int, progress: Callable | None,
                 cancelled: Callable[[], bool] | None) -> tuple[int, int, list[str]]:
    root_name = ""
    seen: dict[str, tuple[str, str]] = {}
    omitted_links: list[str] = []
    files = total = 0
    with zipfile.ZipFile(archive) as handle:
        infos = handle.infolist()
        if len(infos) > limits["max_files"] * 2 + 1000:
            raise RepositoryLimitError("repository archive contains too many entries")
        for info in infos:
            _check_canceled(cancelled)
            if info.flag_bits & 0x1:
                raise RepositoryError("encrypted repository archive entries are not supported")
            root, relative = _archive_name(info.filename)
            if not root_name:
                root_name = root
                if not _expected_archive_root(root, repo, sha):
                    raise RepositoryError("repository archive has an unexpected root directory")
            if root != root_name:
                raise RepositoryError("repository archive must contain exactly one root directory")
            kind = _zip_kind(info)
            if kind == "special":
                raise RepositoryError("repository archive contains an unsafe special file")
            if relative is None:
                if kind != "directory":
                    raise RepositoryError("repository archive root must be a directory")
                continue
            _track_archive_path(relative, kind, seen)
            target = _safe_destination(destination, relative)
            if kind == "directory":
                target.mkdir(parents=True, exist_ok=True)
                continue
            if kind == "symlink":
                if files + len(omitted_links) + 1 > limits["max_files"]:
                    raise RepositoryLimitError("repository archive contains too many files")
                # The entry payload is the target on Unix-created zip files.
                # It is intentionally neither opened nor materialized.
                omitted_links.append(relative.as_posix())
                continue
            if info.file_size < 0 or info.file_size > limits["max_file_bytes"]:
                raise RepositoryLimitError("repository archive contains an oversized file")
            if info.file_size > 1024 * 1024 and \
                    info.file_size / max(1, info.compress_size) > limits["max_compression_ratio"]:
                raise RepositoryLimitError("repository archive contains a compression bomb")
            files += 1
            total += info.file_size
            if files + len(omitted_links) > limits["max_files"]:
                raise RepositoryLimitError("repository archive contains too many files")
            if total > limits["max_unpacked_bytes"]:
                raise RepositoryLimitError("repository archive exceeds the unpacked size limit")
            if compressed_bytes and total > 1024 * 1024 \
                    and total / compressed_bytes > limits["max_compression_ratio"]:
                raise RepositoryLimitError(
                    "repository archive exceeds the compression-ratio limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with handle.open(info) as source, target.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    _check_canceled(cancelled)
                    written += len(chunk)
                    if written > info.file_size or written > limits["max_file_bytes"]:
                        raise RepositoryLimitError("repository archive file exceeded its declared size")
                    actual_total = total - info.file_size + written
                    if compressed_bytes and actual_total > 1024 * 1024 \
                            and actual_total / compressed_bytes > limits["max_compression_ratio"]:
                        raise RepositoryLimitError(
                            "repository archive exceeds the compression-ratio limit")
                    output.write(chunk)
            if written != info.file_size:
                raise RepositoryError("repository archive contains a truncated file")
            _report(progress, "Safely extracting repository snapshot", files,
                    limits["max_files"])
    if not root_name:
        raise RepositoryError("repository archive is empty")
    if compressed_bytes and total > 1024 * 1024 \
            and total / compressed_bytes > limits["max_compression_ratio"]:
        raise RepositoryLimitError("repository archive exceeds the compression-ratio limit")
    return files, total, omitted_links


def _remove_tree_safely(path: Path, root: Path) -> None:
    try:
        resolved = path.resolve()
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise RepositoryError("refused to clean a path outside repository staging") from exc
    if resolved != root.resolve():
        shutil.rmtree(resolved, ignore_errors=True)


def cleanup_repository_staging(source_id: int | None = None) -> int:
    """Remove abandoned acquisitions without touching an active snapshot job."""
    staging = (settings.repository_dir / ".staging").resolve()
    if not staging.exists():
        return 0
    active_sources: set[int] = set()
    if source_id is None:
        with get_session() as session:
            project_ids = session.exec(
                select(Job.project_id).where(
                    Job.task == "repo_snapshot",
                    Job.status.in_(("queued", "running")),
                )
            ).all()
            if project_ids:
                active_sources = set(session.exec(
                    select(RepositorySource.id).where(
                        RepositorySource.project_id.in_(project_ids))
                ).all())
    removed = 0
    for candidate in list(staging.iterdir()):
        prefix = candidate.name.split("-", 1)[0]
        candidate_source = int(prefix) if prefix.isdigit() else None
        if source_id is not None and candidate_source != source_id:
            continue
        if source_id is None and candidate_source in active_sources:
            continue
        resolved = candidate.resolve()
        try:
            resolved.relative_to(staging)
        except ValueError as exc:
            raise RepositoryError("unsafe repository staging entry") from exc
        if candidate.is_symlink() or candidate.is_file():
            candidate.unlink(missing_ok=True)
        else:
            shutil.rmtree(candidate)
        removed += 1
    return removed


_BINARY_SUFFIXES = {
    ".7z", ".a", ".avi", ".bin", ".bmp", ".class", ".db", ".dll",
    ".dylib", ".eot", ".exe", ".flac", ".gif", ".gz", ".ico", ".jar",
    ".jpeg", ".jpg", ".lockb", ".m4a", ".mov", ".mp3", ".mp4", ".o",
    ".ogg", ".otf", ".pdf", ".png", ".pyc", ".so", ".sqlite", ".tar",
    ".tiff", ".ttf", ".wav", ".webm", ".webp", ".woff", ".woff2", ".xz",
    ".zip",
}

_LANGUAGES = {
    ".py": "Python", ".pyi": "Python", ".js": "JavaScript",
    ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".java": "Java",
    ".kt": "Kotlin", ".kts": "Kotlin", ".go": "Go", ".rs": "Rust",
    ".c": "C", ".h": "C/C++", ".cc": "C++", ".cpp": "C++",
    ".cs": "C#", ".fs": "F#", ".rb": "Ruby", ".php": "PHP",
    ".swift": "Swift", ".dart": "Dart", ".scala": "Scala",
    ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell", ".ps1": "PowerShell",
    ".sql": "SQL", ".tf": "Terraform", ".hcl": "HCL", ".vue": "Vue",
    ".svelte": "Svelte", ".html": "HTML", ".css": "CSS", ".scss": "SCSS",
    ".md": "Markdown", ".rst": "reStructuredText", ".json": "JSON",
    ".jsonc": "JSON", ".toml": "TOML", ".yaml": "YAML", ".yml": "YAML",
    ".xml": "XML", ".ini": "INI", ".cfg": "Configuration",
    ".properties": "Configuration", ".gradle": "Gradle", ".proto": "Protocol Buffers",
}

_MANIFEST_NAMES = {
    "package.json", "pyproject.toml", "setup.cfg", "setup.py", "requirements.txt",
    "pipfile", "poetry.lock", "uv.lock", "cargo.toml", "cargo.lock", "go.mod",
    "go.sum", "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
    "gemfile", "gemfile.lock", "composer.json", "composer.lock", "pubspec.yaml",
    "pubspec.lock", "mix.exs", "package.swift", "dockerfile", "compose.yml",
    "compose.yaml", "docker-compose.yml", "docker-compose.yaml", "makefile",
    "justfile", ".gitmodules", ".gitattributes",
}

_LOCK_NAMES = {
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
    "bun.lock", "bun.lockb", "poetry.lock", "uv.lock", "pipfile.lock",
    "cargo.lock", "go.sum", "gemfile.lock", "composer.lock", "pubspec.lock",
}

_HIGH_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bglpat-[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bnpm_[0-9A-Za-z]{30,}\b"),
    re.compile(r"\bpypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."
               r"[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp|https?)://"
        r"[^\s/@:]+:[^@\s/]+@[^\s]+"),
]

_SECRET_NAME = (
    r"api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"private[_-]?key|signing[_-]?key|webhook(?:[_-]?url)?|"
    r"database[_-]?(?:url|uri)|connection[_-]?string|credential|dsn|"
    r"password|passwd|secret|token"
)
_SECRET_IDENTIFIER = (
    rf"(?:[A-Za-z][A-Za-z0-9]*[_-])*(?:{_SECRET_NAME})"
    rf"(?:[_-][A-Za-z0-9]+)*"
)
_QUOTED_SECRET_RE = re.compile(
    rf"(?i)(?P<prefix>['\"]?(?:{_SECRET_IDENTIFIER})['\"]?\s*[:=]\s*)"
    r"(?P<quote>['\"])(?P<value>[^'\"\r\n]{8,})(?P=quote)"
)
_UNQUOTED_SECRET_RE = re.compile(
    rf"(?im)(?P<prefix>^\s*(?:export\s+)?(?:{_SECRET_IDENTIFIER})\s*[:=]\s*)"
    r"(?P<value>[^\s#'\"][^\r\n#]{7,})(?P<suffix>\s*(?:#.*)?)$"
)


def _secret_prone_path(path: str) -> bool:
    pure = PurePosixPath(path)
    name = pure.name.casefold()
    suffix = pure.suffix.casefold()
    examples = (".example", ".sample", ".template", ".dist")
    if name == ".env" or (name.startswith(".env.") and not name.endswith(examples)):
        return True
    if suffix in {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"}:
        return True
    if name in {
        "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".npmrc", ".pypirc",
        "credentials", "credentials.json", "service-account.json", "secrets.json",
        "auth.json", "terraform.tfstate", "terraform.tfstate.backup",
    }:
        return True
    return any(part.casefold() in {".secrets", "secrets"} for part in pure.parts[:-1])


def _path_vendor_or_generated(path: str, exclusions: Iterable[str]) -> tuple[bool, bool]:
    pure = PurePosixPath(path)
    directory_parts = {part.casefold() for part in pure.parts[:-1]}
    excluded_dirs = {part.casefold() for part in exclusions}
    vendor = bool(directory_parts.intersection({
        "vendor", "vendors", "node_modules", "site-packages", ".venv", "venv",
    }))
    generated_dir = bool(directory_parts.intersection(excluded_dirs)) and not vendor
    name = pure.name.casefold()
    generated_file = name.endswith((
        ".min.js", ".min.css", ".map", ".generated.js", ".generated.ts",
        ".generated.cs", ".g.cs", ".designer.cs",
    ))
    return vendor, generated_dir or generated_file


def _language(path: str) -> str:
    name = PurePosixPath(path).name.casefold()
    if name == "dockerfile" or name.startswith("dockerfile."):
        return "Dockerfile"
    if name == "makefile":
        return "Makefile"
    return _LANGUAGES.get(PurePosixPath(path).suffix.casefold(), "Text")


def _role(path: str) -> str:
    pure = PurePosixPath(path)
    name = pure.name.casefold()
    folded = path.casefold()
    if name in _MANIFEST_NAMES or name in _LOCK_NAMES or name.startswith("requirements"):
        return "manifest" if name not in _LOCK_NAMES else "lockfile"
    if name.startswith(("readme", "contributing", "architecture", "security", "license")) \
            or pure.suffix.casefold() in {".md", ".rst"}:
        return "documentation"
    if folded.startswith(".github/workflows/"):
        return "ci"
    if any(part.casefold() in {"test", "tests", "spec", "specs", "__tests__"}
           for part in pure.parts[:-1]):
        return "test"
    if pure.suffix.casefold() in {".yaml", ".yml", ".toml", ".ini", ".cfg", ".json"}:
        return "configuration"
    return "source"


def _analysis_priority(role: str) -> int:
    return {
        "manifest": 100,
        "documentation": 90,
        "configuration": 80,
        "ci": 75,
        "source": 60,
        "test": 45,
        "lockfile": 10,
    }.get(role, 20)


def _hash_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _is_binary(path: str, raw: bytes) -> bool:
    if PurePosixPath(path).suffix.casefold() in _BINARY_SUFFIXES or b"\x00" in raw[:8192]:
        return True
    try:
        raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return True
    return False


_ENTROPY_CANDIDATE_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_+/=-]{32,200}(?![A-Za-z0-9])")


def _entropy_secret_count(body: str) -> int:
    count = 0
    safe_context = re.compile(
        r"(?i)(?:sha(?:1|256|512)?|hash|digest|checksum|integrity|example|fixture|placeholder)"
        r"[^\r\n]{0,40}$")
    named_secret_context = re.compile(
        rf"(?i)(?:{_SECRET_IDENTIFIER})\s*[:=]\s*$")
    named_secret_candidate = re.compile(
        rf"(?i)^(?:{_SECRET_IDENTIFIER})\s*[:=]")
    for match in _ENTROPY_CANDIDATE_RE.finditer(body):
        value = match.group(0).rstrip("=")
        context = body[max(0, match.start() - 80):match.start()]
        # Named credential assignments are handled by the literal redactor.
        # Counting the combined ``API_KEY=value`` token as entropy would drop
        # the entire file before its safely redacted structure can be indexed.
        if (safe_context.search(context)
                or named_secret_context.search(context)
                or named_secret_candidate.search(value)
                or len(set(value)) < 12):
            continue
        classes = sum(bool(re.search(pattern, value)) for pattern in (
            r"[a-z]", r"[A-Z]", r"[0-9]", r"[_+/=-]"))
        if classes < 3:
            continue
        frequencies = Counter(value)
        entropy = -sum(
            (amount / len(value)) * math.log2(amount / len(value))
            for amount in frequencies.values()
        )
        if entropy >= 4.3:
            count += 1
    return count


def _high_secret_count(body: str) -> int:
    return (sum(len(pattern.findall(body)) for pattern in _HIGH_SECRET_PATTERNS)
            + _entropy_secret_count(body))


def _redact_suspicious_literals(body: str) -> tuple[str, int]:
    count = 0

    def placeholder(value: str) -> bool:
        lowered = value.casefold()
        return any(marker in lowered for marker in (
            "getenv", "process.env", "os.environ", "example", "replace", "your_",
            "your-", "changeme", "placeholder", "<", "${", "{{", "secrets.",
        ))

    def replace_quoted(match: re.Match) -> str:
        nonlocal count
        value = match.group("value")
        if placeholder(value):
            return match.group(0)
        count += 1
        return f"{match.group('prefix')}{match.group('quote')}[REDACTED]{match.group('quote')}"

    def replace_unquoted(match: re.Match) -> str:
        nonlocal count
        if placeholder(match.group("value")):
            return match.group(0)
        count += 1
        return f"{match.group('prefix')}[REDACTED]{match.group('suffix')}"

    redacted = _QUOTED_SECRET_RE.sub(replace_quoted, body)
    redacted = _UNQUOTED_SECRET_RE.sub(replace_unquoted, redacted)
    return redacted, count


def _safe_fact_text(value: object, limit: int = 1000) -> str:
    body = str(value or "").strip().replace("\x00", "")[:limit]
    body, _ = _redact_suspicious_literals(body)
    # Never retain URL credentials or signed query strings in inventory facts.
    if "://" in body:
        try:
            parsed = urlsplit(body)
            if parsed.username or parsed.password or parsed.query:
                return "[REDACTED URL]"
        except ValueError:
            return "[REDACTED URL]"
    return body


_FACT_TOKEN_RE = re.compile(r"[A-Za-z0-9_@./:+~<>=!-]{1,300}")


def _fact_line_index(body: str) -> dict[str, int]:
    """Build one bounded token→line map instead of rescanning a manifest."""
    index: dict[str, int] = {}
    for line_number, line in itertools.islice(enumerate(body.splitlines(), 1), 20_000):
        for match in _FACT_TOKEN_RE.finditer(line):
            index.setdefault(match.group(0), line_number)
            if len(index) >= 50_000:
                return index
    return index


def _line_number(index: dict[str, int], needle: str) -> int:
    clean = str(needle).strip().strip("'\"")
    if clean in index:
        return index[clean]
    for match in _FACT_TOKEN_RE.finditer(clean):
        if match.group(0) in index:
            return index[match.group(0)]
    return 1


def _append_fact(facts: dict, kind: str, value: dict) -> None:
    if int(facts.get("_fact_count", 0)) >= MAX_TOTAL_FACTS:
        facts.setdefault("fact_limits", {})["total"] = (
            f"truncated_at_{MAX_TOTAL_FACTS}")
        return
    if len(facts.get(kind, [])) >= 500:
        facts.setdefault("fact_limits", {})[kind] = "truncated_at_500"
        return
    marker = json.dumps(value, sort_keys=True, ensure_ascii=True)
    seen = facts.setdefault("_seen", set())
    key = (kind, marker)
    if key not in seen:
        seen.add(key)
        facts.setdefault(kind, []).append(value)
        facts["_fact_count"] = int(facts.get("_fact_count", 0)) + 1


def _bounded_items(value: object, limit: int = 500):
    return itertools.islice(value.items(), limit) if isinstance(value, dict) else ()


def _dependency(facts: dict, *, ecosystem: str, name: object, version: object,
                scope: str, path: str, line: int) -> None:
    clean_name = _safe_fact_text(name, 300)
    if clean_name:
        _append_fact(facts, "dependencies", {
            "ecosystem": ecosystem,
            "name": clean_name,
            "version": _safe_fact_text(version, 300),
            "scope": scope,
            "path": path,
            "line": line,
        })


def _command(facts: dict, *, command: object, kind: str, path: str, line: int) -> None:
    clean = _safe_fact_text(command)
    if clean:
        _append_fact(facts, "commands", {
            "command": clean,
            "kind": kind,
            "path": path,
            "line": line,
        })


def _extract_manifest_facts(path: str, body: str, facts: dict) -> None:
    pure = PurePosixPath(path)
    name = pure.name.casefold()
    role = _role(path)
    line_index: dict[str, int] | None = None

    def line_for(needle: str) -> int:
        nonlocal line_index
        if line_index is None:
            line_index = _fact_line_index(body)
        return _line_number(line_index, needle)
    if role in {"manifest", "lockfile"}:
        _append_fact(facts, "manifests", {
            "path": path, "kind": name, "line": 1,
        })

    if name == "package.json":
        try:
            package = json.loads(body)
        except json.JSONDecodeError:
            package = {}
        if isinstance(package, dict):
            for section, scope in (
                ("dependencies", "runtime"), ("devDependencies", "development"),
                ("peerDependencies", "peer"), ("optionalDependencies", "optional"),
            ):
                values = package.get(section) or {}
                if isinstance(values, dict):
                    for dependency, version in _bounded_items(values):
                        _dependency(facts, ecosystem="npm", name=dependency, version=version,
                                    scope=scope, path=path,
                                    line=line_for(f'"{dependency}"'))
            scripts = package.get("scripts") or {}
            if isinstance(scripts, dict):
                for script, command in _bounded_items(scripts):
                    _command(facts, command=f"npm run {script}", kind="package_script",
                             path=path, line=line_for(f'"{script}"'))
                    _append_fact(facts, "script_definitions", {
                        "name": _safe_fact_text(script, 200),
                        "definition": _safe_fact_text(command),
                        "path": path,
                        "line": line_for(f'"{script}"'),
                    })
            engines = package.get("engines") or {}
            if isinstance(engines, dict):
                for runtime, version in _bounded_items(engines):
                    _append_fact(facts, "runtimes", {
                        "name": _safe_fact_text(runtime, 100),
                        "version": _safe_fact_text(version, 200),
                        "path": path,
                        "line": line_for(f'"{runtime}"'),
                    })

    elif name == "pyproject.toml":
        try:
            import tomllib
            parsed = tomllib.loads(body)
        except (ValueError, TypeError):
            parsed = {}
        project = parsed.get("project") or {}
        if isinstance(project, dict):
            requires_python = project.get("requires-python")
            if requires_python:
                _append_fact(facts, "runtimes", {
                    "name": "python", "version": _safe_fact_text(requires_python, 200),
                    "path": path,
                    "line": line_for("requires-python"),
                })
            for spec in itertools.islice(project.get("dependencies") or [], 500):
                match = re.match(r"\s*([A-Za-z0-9_.-]+)\s*(.*)", str(spec))
                if match:
                    _dependency(facts, ecosystem="python", name=match.group(1),
                                version=match.group(2), scope="runtime", path=path,
                                line=line_for(str(spec)))
            optional = project.get("optional-dependencies") or {}
            if isinstance(optional, dict):
                for group, specs in _bounded_items(optional, 100):
                    for spec in itertools.islice(specs or [], 100):
                        match = re.match(r"\s*([A-Za-z0-9_.-]+)\s*(.*)", str(spec))
                        if match:
                            _dependency(facts, ecosystem="python", name=match.group(1),
                                        version=match.group(2), scope=f"optional:{group}",
                                        path=path, line=line_for(str(spec)))
            for command_name in itertools.islice((project.get("scripts") or {}), 500):
                _command(facts, command=command_name, kind="python_entrypoint", path=path,
                         line=line_for(command_name))
        poetry = ((parsed.get("tool") or {}).get("poetry") or {})
        if isinstance(poetry, dict):
            for dependency, version in _bounded_items(poetry.get("dependencies") or {}):
                if dependency.casefold() == "python":
                    _append_fact(facts, "runtimes", {
                        "name": "python", "version": _safe_fact_text(version, 200),
                        "path": path,
                        "line": line_for(dependency),
                    })
                else:
                    _dependency(facts, ecosystem="python", name=dependency, version=version,
                                scope="runtime", path=path,
                                line=line_for(dependency))

    elif name.startswith("requirements") and pure.suffix.casefold() in {"", ".txt", ".in"}:
        for line_number, line in itertools.islice(enumerate(body.splitlines(), 1), 2000):
            clean = line.strip()
            if not clean or clean.startswith(("#", "-", "http://", "https://")):
                continue
            match = re.match(r"([A-Za-z0-9_.-]+)\s*(.*)", clean)
            if match:
                _dependency(facts, ecosystem="python", name=match.group(1),
                            version=match.group(2), scope="declared", path=path,
                            line=line_number)

    elif name == "cargo.toml":
        try:
            import tomllib
            parsed = tomllib.loads(body)
        except (ValueError, TypeError):
            parsed = {}
        for section, scope in (("dependencies", "runtime"),
                               ("dev-dependencies", "development"),
                               ("build-dependencies", "build")):
            for dependency, version in _bounded_items(parsed.get(section) or {}):
                _dependency(facts, ecosystem="cargo", name=dependency, version=version,
                            scope=scope, path=path, line=line_for(dependency))

    elif name == "go.mod":
        module_mode = False
        for line_number, line in itertools.islice(enumerate(body.splitlines(), 1), 5000):
            clean = line.strip()
            if clean == "require (":
                module_mode = True
                continue
            if module_mode and clean == ")":
                module_mode = False
                continue
            if clean.startswith("go "):
                _append_fact(facts, "runtimes", {
                    "name": "go", "version": clean[3:].strip(), "path": path,
                    "line": line_number,
                })
            candidate = clean[8:].strip() if clean.startswith("require ") else clean if module_mode else ""
            match = re.match(r"([^\s]+)\s+([^\s]+)", candidate)
            if match:
                _dependency(facts, ecosystem="go", name=match.group(1),
                            version=match.group(2), scope="runtime", path=path,
                            line=line_number)

    elif name == "composer.json":
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {}
        for section, scope in (("require", "runtime"), ("require-dev", "development")):
            for dependency, version in _bounded_items(parsed.get(section) or {}):
                _dependency(facts, ecosystem="composer", name=dependency, version=version,
                            scope=scope, path=path, line=line_for(dependency))

    elif name in {"dockerfile"} or name.startswith("dockerfile."):
        for line_number, line in itertools.islice(enumerate(body.splitlines(), 1), 5000):
            clean = line.strip()
            match = re.match(r"(?i)^FROM\s+([^\s]+)", clean)
            if match:
                _append_fact(facts, "containers", {
                    "image": _safe_fact_text(match.group(1), 300),
                    "path": path, "line": line_number,
                })
            match = re.match(r"(?i)^EXPOSE\s+(.+)", clean)
            if match:
                _append_fact(facts, "ports", {
                    "value": _safe_fact_text(match.group(1), 200),
                    "path": path, "line": line_number,
                })

    elif name in {"makefile", "justfile"}:
        for line_number, line in itertools.islice(enumerate(body.splitlines(), 1), 5000):
            match = re.match(r"^([A-Za-z0-9_.-]+)\s*:(?![=])", line)
            if match and not match.group(1).startswith("."):
                prefix = "make" if name == "makefile" else "just"
                _command(facts, command=f"{prefix} {match.group(1)}", kind="task",
                         path=path, line=line_number)

    if name in {"compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml"}:
        for line_number, line in itertools.islice(enumerate(body.splitlines(), 1), 5000):
            image = re.match(r"\s*image:\s*['\"]?([^'\"#\s]+)", line)
            if image:
                _append_fact(facts, "containers", {
                    "image": _safe_fact_text(image.group(1), 300),
                    "path": path, "line": line_number,
                })
            port = re.match(r"\s*-\s*['\"]?(\d+(?::\d+)?(?:/(?:tcp|udp))?)", line)
            if port:
                _append_fact(facts, "ports", {
                    "value": port.group(1), "path": path, "line": line_number,
                })


_ENV_PATTERNS = [
    re.compile(r"(?:os\.getenv|os\.environ\.get|getenv|Deno\.env\.get)\(\s*['\"]([A-Z][A-Z0-9_]{1,})['\"]"),
    re.compile(r"\bprocess\.env\.([A-Z][A-Z0-9_]{1,})\b"),
    re.compile(r"\$\{([A-Z][A-Z0-9_]{1,})(?::[-?][^}]*)?\}"),
]


def _extract_general_facts(path: str, body: str, facts: dict) -> None:
    lines = body.splitlines()
    newline_offsets = [match.start() for match in re.finditer("\n", body)]
    for pattern in _ENV_PATTERNS:
        for match in itertools.islice(pattern.finditer(body), 500):
            line = bisect_right(newline_offsets, match.start()) + 1
            _append_fact(facts, "environment", {
                "name": match.group(1), "path": path, "line": line,
            })
    if PurePosixPath(path).name.casefold().startswith(".env"):
        for line_number, line in itertools.islice(enumerate(lines, 1), 5000):
            match = re.match(r"\s*(?:export\s+)?([A-Z][A-Z0-9_]{1,})\s*=", line)
            if match:
                _append_fact(facts, "environment", {
                    "name": match.group(1), "path": path, "line": line_number,
                })
    if _role(path) in {"documentation", "ci"}:
        command_re = re.compile(
            r"^\s*(?:\$\s*)?((?:npm|pnpm|yarn|bun|pipx?|python|python3|uv|poetry|"
            r"docker(?:\s+compose)?|make|just|cargo|go|dotnet|mvnw?|gradlew?|terraform|"
            r"ansible-playbook|kubectl)\b[^\r\n]{0,950})"
        )
        for line_number, line in itertools.islice(enumerate(lines, 1), 5000):
            match = command_re.match(line)
            if match:
                _command(facts, command=match.group(1), kind="documented", path=path,
                         line=line_number)


def _parse_submodules(path: str, body: str, facts: dict) -> list[str]:
    if PurePosixPath(path).name.casefold() != ".gitmodules":
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for line_number, line in enumerate(body.splitlines(), 1):
        match = re.match(r"\s*path\s*=\s*(.+?)\s*$", line)
        if not match:
            continue
        try:
            clean = normalize_scope_patterns([match.group(1)])[0]
        except (ValueError, IndexError):
            continue
        folded = clean.casefold()
        if folded in seen:
            continue
        if len(paths) >= MAX_SUBMODULE_DECLARATIONS:
            facts.setdefault("fact_limits", {})["submodules"] = (
                f"truncated_at_{MAX_SUBMODULE_DECLARATIONS}")
            break
        seen.add(folded)
        paths.append(clean)
        _append_fact(facts, "submodules", {"path": clean, "source": path,
                                             "line": line_number, "fetched": False})
    return paths


def _chunks(body: str, *, max_lines: int, max_chars: int) -> list[tuple[int, int, str]]:
    lines = body.splitlines()
    if not lines and body:
        lines = [body]
    output: list[tuple[int, int, str]] = []
    start = 0
    while start < len(lines):
        if len(lines[start]) > max_chars:
            for offset in range(0, len(lines[start]), max_chars):
                output.append((start + 1, start + 1,
                               lines[start][offset:offset + max_chars]))
            start += 1
            continue
        end = start
        characters = 0
        while end < len(lines) and end - start < max_lines:
            added = len(lines[end]) + (1 if end > start else 0)
            if end > start and characters + added > max_chars:
                break
            characters += added
            end += 1
        chunk_body = "\n".join(lines[start:end])
        output.append((start + 1, end, chunk_body))
        start = end
    return output


def _fact_items_for_path(facts: dict, path: str) -> list[dict]:
    """Return structured facts whose evidence originates in ``path``."""
    folded = path.casefold()
    output: list[dict] = []
    for value in facts.values():
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            source_path = item.get("source") or item.get("path")
            if isinstance(source_path, str) and source_path.casefold() == folded:
                output.append(item)
    return output


def _fact_needles(item: dict) -> list[str]:
    """Pick bounded, non-metadata values useful for selecting a line fragment."""
    output: list[str] = []
    for key in ("name", "command", "definition", "image", "value", "version"):
        value = item.get(key)
        if isinstance(value, (str, int, float)):
            clean = str(value).strip()
            if 1 < len(clean) <= 1000:
                output.append(clean)
    return output


def _bounded_fact_evidence(body: str, items: list[dict], *,
                           max_chars: int) -> list[tuple[int, int, str]]:
    """Build small redacted excerpts for otherwise facts-only files.

    A lockfile may be very large or even a single very long line.  Only lines
    referenced by extracted facts are considered, and only a fixed number of
    bounded fragments can become evidence.
    """
    width = max(1, int(max_chars))
    lines = body.splitlines()
    if not lines and body:
        lines = [body]
    if not lines:
        return []
    by_line: dict[int, list[dict]] = {}
    for item in items:
        try:
            line = max(1, int(item.get("line") or 1))
        except (TypeError, ValueError):
            line = 1
        if line <= len(lines):
            by_line.setdefault(line, []).append(item)
    if not by_line:
        by_line[1] = []

    output: list[tuple[int, int, str]] = []
    for line_number in sorted(by_line):
        line = lines[line_number - 1]
        fragments = [line[offset:offset + width]
                     for offset in range(0, max(1, len(line)), width)]
        needles = [needle.casefold() for item in by_line[line_number]
                   for needle in _fact_needles(item)]
        matching = [fragment for fragment in fragments
                    if any(needle in fragment.casefold() for needle in needles)]
        selected = matching or fragments[:1]
        for fragment in selected:
            output.append((line_number, line_number, fragment))
            if len(output) >= MAX_FACT_EVIDENCE_CHUNKS_PER_FILE:
                return output
    return output


def _attach_fact_evidence(facts: dict,
                          evidence_by_path: dict[str, list[dict]]) -> None:
    """Attach a real line-addressed evidence id to each mappable fact."""
    for value in facts.values():
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            source_path = item.get("source") or item.get("path")
            if not isinstance(source_path, str):
                continue
            candidates = evidence_by_path.get(source_path.casefold(), [])
            try:
                line = max(1, int(item.get("line") or 1))
            except (TypeError, ValueError):
                line = 1
            candidates = [candidate for candidate in candidates
                          if candidate["start_line"] <= line <= candidate["end_line"]]
            if not candidates:
                continue
            needles = [needle.casefold() for needle in _fact_needles(item)]
            matching = [candidate for candidate in candidates
                        if any(needle in candidate["body"].casefold()
                               for needle in needles)]
            item["evidence_id"] = (matching or candidates)[0]["evidence_id"]


def _catalogued_snapshot_links(snapshot: RepositorySnapshot) -> list[str]:
    """Validate persisted archive link metadata before it reaches inventory."""
    try:
        raw_links = json.loads(snapshot.omitted_links or "[]")
    except (TypeError, json.JSONDecodeError) as exc:
        raise RepositoryError("repository snapshot link metadata is invalid") from exc
    if not isinstance(raw_links, list):
        raise RepositoryError("repository snapshot link metadata is invalid")
    links: list[str] = []
    seen: set[str] = set()
    for value in raw_links:
        if (not isinstance(value, str) or len(value) > 4096
                or value.startswith("/") or "\\" in value
                or _CONTROL_RE.search(value)):
            raise RepositoryError("repository snapshot link metadata is invalid")
        root, relative = _archive_name(f"snapshot/{value}")
        if (root != "snapshot" or relative is None
                or re.match(r"^[A-Za-z]:", relative.parts[0])):
            raise RepositoryError("repository snapshot link metadata is invalid")
        clean = relative.as_posix()
        folded = clean.casefold()
        if folded in seen:
            raise RepositoryError("repository snapshot link metadata contains duplicates")
        seen.add(folded)
        links.append(clean)
    return sorted(links, key=str.casefold)


def _bounded_omitted_link_facts(links: list[str]) -> tuple[list[str], int]:
    """Keep link coverage useful without letting path metadata own the prompt."""
    selected: list[str] = []
    for path in links:
        if len(selected) >= MAX_OMITTED_LINK_FACT_PATHS:
            break
        candidate = selected + [path]
        if len(json.dumps(candidate, ensure_ascii=True)) > MAX_OMITTED_LINK_FACT_CHARS:
            break
        selected.append(path)
    return selected, len(links) - len(selected)


def repository_snapshot_path(snapshot: RepositorySnapshot) -> Path:
    if not snapshot.relative_path:
        raise RepositoryError("repository snapshot has no local path")
    root = settings.repository_dir.resolve()
    raw_path = root / snapshot.relative_path
    if raw_path.is_symlink():
        raise RepositoryError("repository snapshot path is a symbolic link")
    path = raw_path.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RepositoryError("repository snapshot path is unsafe") from exc
    return path


def scan_repository_snapshot(session: Session, source: RepositorySource,
                             snapshot: RepositorySnapshot, *,
                             progress: Callable | None = None,
                             cancelled: Callable[[], bool] | None = None) -> RepositorySnapshot:
    """Build a deterministic, secret-aware static inventory and evidence index."""
    root = repository_snapshot_path(snapshot)
    if not root.is_dir():
        raise RepositoryError("repository snapshot directory is missing")
    limits = repository_scan_settings()
    scan_config_hash = repository_scan_config_hash(
        source, settings_snapshot=limits)
    try:
        include_paths = normalize_scope_patterns(json.loads(source.include_paths or "[]"))
        exclude_paths = normalize_scope_patterns(json.loads(source.exclude_paths or "[]"))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RepositoryError("repository scope settings are invalid") from exc

    # Content-addressed reuse survives a branch moving to a new commit (and a
    # safe retry of this scanner).  Capture it before replacing current rows.
    prior_summary_by_hash: dict[str, tuple[str, str, str]] = {}
    prior_rows = session.exec(
        select(
            RepositoryChunk.content_hash,
            RepositoryChunk.summary_text,
            RepositoryChunk.summary_json,
            RepositoryChunk.summary_config_hash,
        )
        .join(RepositoryFile, RepositoryFile.id == RepositoryChunk.file_id)
        .join(RepositorySnapshot, RepositorySnapshot.id == RepositoryFile.snapshot_id)
        .where(RepositorySnapshot.source_id == source.id,
               RepositoryChunk.summary_text != "")
        .order_by(RepositoryChunk.created.desc())
    ).all()
    for content_hash, summary_text, summary_json, summary_config_hash in prior_rows:
        prior_summary_by_hash.setdefault(
            content_hash,
            (summary_text, summary_json, summary_config_hash),
        )

    # A rescan must not expose a ready snapshot while its evidence rows are
    # being replaced. Publish only after the final manifest is complete.
    snapshot.status = "pending"
    snapshot.error = ""
    source.pending_sha = snapshot.resolved_sha
    if source.current_snapshot_id == snapshot.id:
        parent = session.get(RepositorySnapshot, snapshot.parent_snapshot_id) \
            if snapshot.parent_snapshot_id else None
        source.current_snapshot_id = (
            parent.id if parent and parent.status == "ready" else None
        )
        session.add(source)
    session.add(snapshot)
    session.commit()
    session.refresh(snapshot)

    existing_files = session.exec(
        select(RepositoryFile).where(RepositoryFile.snapshot_id == snapshot.id)
    ).all()
    existing_ids = [row.id for row in existing_files if row.id is not None]
    if existing_ids:
        chunks = session.exec(
            select(RepositoryChunk).where(RepositoryChunk.file_id.in_(existing_ids))
        ).all()
        for chunk in chunks:
            session.delete(chunk)
        for row in existing_files:
            session.delete(row)
        session.flush()
    try:
        session.exec(text("DELETE FROM repository_chunk_fts WHERE snapshot_id = :sid")
                     .bindparams(sid=snapshot.id))
    except Exception:
        # init_db creates the FTS table; tolerate direct unit-level scanner use.
        log.debug("repository FTS table is not initialized yet")
    session.commit()

    facts: dict = {"scanner_version": SCANNER_VERSION, "static_only": True,
                   "_seen": set(), "_fact_count": 0}
    omitted_links = _catalogued_snapshot_links(snapshot)
    languages: Counter[str] = Counter()
    manifest_entries: list[dict] = []
    submodule_paths: list[str] = []
    evidence_by_path: dict[str, list[dict]] = {}
    file_count = total_bytes = indexed_files = indexed_bytes = excluded_files = 0
    exclusion_reasons: Counter[str] = Counter()
    secret_findings = 0
    materialized_paths: dict[str, tuple[str, str]] = {}
    files_to_scan: list[tuple[str, Path]] = []

    def store_evidence_chunk(file_row: RepositoryFile, relative: str, *,
                             chunk_index: int, start_line: int, end_line: int,
                             chunk_body: str, kind: str) -> None:
        body_hash = hashlib.sha256(chunk_body.encode("utf-8")).hexdigest()
        evidence_id = "E" + hashlib.sha256(
            f"{source.id}\0{snapshot.resolved_sha}\0{relative}\0"
            f"{start_line}\0{end_line}\0{body_hash}"
            .encode("utf-8")).hexdigest()[:16].upper()
        chunk = RepositoryChunk(
            file_id=file_row.id,
            chunk_index=chunk_index,
            evidence_id=evidence_id,
            start_line=start_line,
            end_line=end_line,
            kind=kind,
            body=chunk_body,
            body_hash=body_hash,
            content_hash=body_hash,
            estimated_tokens=max(1, len(chunk_body) // 4),
        )
        cached = prior_summary_by_hash.get(body_hash)
        if cached:
            chunk.summary_text, chunk.summary_json, chunk.summary_config_hash = cached
        session.add(chunk)
        session.flush()
        evidence_by_path.setdefault(relative.casefold(), []).append({
            "evidence_id": evidence_id,
            "start_line": start_line,
            "end_line": end_line,
            "body": chunk_body,
        })
        try:
            session.exec(text(
                "INSERT INTO repository_chunk_fts"
                "(body, chunk_id, file_id, snapshot_id, project_id) "
                "VALUES (:body, :cid, :fid, :sid, :pid)"
            ).bindparams(body=chunk_body, cid=chunk.id, fid=file_row.id,
                         sid=snapshot.id, pid=source.project_id))
        except Exception:
            log.debug("repository FTS table is not initialized yet")

    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        _check_canceled(cancelled)
        directory_path = Path(directory)
        for dirname in list(dirnames):
            candidate = directory_path / dirname
            if candidate.is_symlink():
                raise RepositoryError("repository snapshot contains a symbolic link")
            relative_directory = candidate.relative_to(root).as_posix()
            _track_archive_path(
                PurePosixPath(relative_directory), "directory", materialized_paths)
        dirnames.sort(key=str.casefold)
        filenames.sort(key=str.casefold)
        for filename in filenames:
            path = directory_path / filename
            if path.is_symlink() or not path.is_file():
                raise RepositoryError("repository snapshot contains a link or special file")
            relative = path.relative_to(root).as_posix()
            _track_archive_path(PurePosixPath(relative), "file", materialized_paths)
            files_to_scan.append((relative, path))

    for link in omitted_links:
        _track_archive_path(PurePosixPath(link), "symlink", materialized_paths)

    if len(files_to_scan) + len(omitted_links) > limits["max_files"]:
        raise RepositoryLimitError("repository snapshot contains too many files")

    for link in omitted_links:
        row = RepositoryFile(
            snapshot_id=snapshot.id,
            path=link,
            role="symlink",
            excluded=True,
            exclusion_reason="symlink_not_followed",
            symlink=True,
        )
        session.add(row)
        file_count += 1
        excluded_files += 1
        exclusion_reasons["symlink_not_followed"] += 1
        manifest_entries.append({
            "path": link, "hash": "", "size": 0,
            "excluded": True, "reason": "symlink_not_followed",
        })
    if omitted_links:
        session.commit()

    for index, (relative, path) in enumerate(files_to_scan, 1):
        _check_canceled(cancelled)
        size = path.stat().st_size
        if size > limits["max_file_bytes"]:
            raise RepositoryLimitError("repository snapshot contains an oversized file")
        file_count += 1
        total_bytes += size
        if total_bytes > limits["max_unpacked_bytes"]:
            raise RepositoryLimitError("repository snapshot exceeds the unpacked size limit")

        content_hash = _hash_path(path)
        role = _role(relative)
        vendor, generated = _path_vendor_or_generated(
            relative, limits["default_exclusions"])
        secret_path = _secret_prone_path(relative)
        in_scope = path_in_scope(relative, include_paths, exclude_paths)
        raw = path.read_bytes() if size <= limits["max_text_file_bytes"] else b""
        lfs_pointer = bool(raw.startswith(
            b"version https://git-lfs.github.com/spec/v1\n"))
        binary = (not lfs_pointer) and (
            bool(raw and _is_binary(relative, raw))
            or PurePosixPath(relative).suffix.casefold() in _BINARY_SUFFIXES)
        body = ""
        if raw and not binary:
            body = raw.decode("utf-8-sig")
        high_secrets = _high_secret_count(body) if body else 0
        redacted_body, literal_secrets = (
            _redact_suspicious_literals(body) if body else ("", 0)
        )
        # High-confidence token formats and secret-prone files are excluded.
        # Named credential assignments remain useful after value redaction,
        # but the raw file viewer is disabled by the sticky restricted flag.
        restricted = secret_path or high_secrets > 0 or literal_secrets > 0
        secret_findings += high_secrets + literal_secrets + (1 if secret_path else 0)

        reason = ""
        if not in_scope:
            reason = "outside_scope"
        elif secret_path:
            reason = "secret_prone"
        elif high_secrets:
            reason = "secret_detected"
        elif vendor:
            reason = "vendored"
        elif generated:
            reason = "generated"
        elif binary:
            reason = "binary"
        elif lfs_pointer:
            reason = "git_lfs_pointer"
        elif role == "lockfile":
            reason = "facts_only"
        elif size > limits["max_text_file_bytes"]:
            reason = "large_file"
        elif indexed_bytes + size > limits["max_indexed_bytes"]:
            reason = "index_budget"

        excluded = bool(reason)
        if excluded:
            excluded_files += 1
            exclusion_reasons[reason] += 1
        line_count = len(body.splitlines()) if body else 0
        language = _language(relative)
        file_row = RepositoryFile(
            snapshot_id=snapshot.id,
            path=relative,
            content_hash=content_hash,
            size_bytes=size,
            line_count=line_count,
            language=language,
            role=role,
            binary=binary,
            generated=generated,
            vendor=vendor,
            restricted=restricted,
            excluded=excluded,
            exclusion_reason=reason,
            analysis_priority=_analysis_priority(role),
            lfs_pointer=lfs_pointer,
        )
        session.add(file_row)
        session.flush()
        # Publish the pending inventory row, then release SQLite while parsing
        # potentially large untrusted manifests in memory.
        session.commit()
        manifest_entries.append({
            "path": relative,
            "hash": content_hash,
            "size": size,
            "excluded": excluded,
            "reason": reason,
        })
        if reason == "facts_only":
            _append_fact(facts, "facts_only_files", {
                "path": relative, "role": role, "reason": "lockfile", "line": 1,
            })
        if reason == "git_lfs_pointer":
            _append_fact(facts, "git_lfs", {
                "path": relative, "fetched": False, "line": 1,
            })

        if reason == "facts_only":
            _extract_manifest_facts(relative, redacted_body, facts)

        if not excluded:
            indexed_files += 1
            indexed_bytes += len(redacted_body.encode("utf-8"))
            languages[language] += size
            _extract_manifest_facts(relative, redacted_body, facts)
            _extract_general_facts(relative, redacted_body, facts)
            submodule_paths.extend(_parse_submodules(relative, redacted_body, facts))
            for chunk_index, (start_line, end_line, chunk_body) in enumerate(
                    _chunks(redacted_body, max_lines=limits["chunk_lines"],
                            max_chars=limits["chunk_chars"])):
                store_evidence_chunk(
                    file_row, relative, chunk_index=chunk_index,
                    start_line=start_line, end_line=end_line,
                    chunk_body=chunk_body, kind=role)

        if reason in {"facts_only", "git_lfs_pointer"} \
                and redacted_body and not restricted:
            fact_items = _fact_items_for_path(facts, relative)
            for chunk_index, (start_line, end_line, chunk_body) in enumerate(
                    _bounded_fact_evidence(
                        redacted_body, fact_items,
                        max_chars=limits["chunk_chars"])):
                store_evidence_chunk(
                    file_row, relative, chunk_index=chunk_index,
                    start_line=start_line, end_line=end_line,
                    chunk_body=chunk_body, kind="fact")

        # Keep SQLite write leases short. The progress callback updates Job in
        # another connection, so it must run only after this batch commits.
        session.commit()
        if index % 25 == 0 or index == len(files_to_scan):
            _report(progress, "Indexing repository files", index, len(files_to_scan))

    existing_paths = {entry["path"].casefold() for entry in manifest_entries}
    for submodule_path in sorted(set(submodule_paths), key=str.casefold):
        if submodule_path.casefold() in existing_paths:
            continue
        if file_count >= limits["max_files"]:
            facts.setdefault("fact_limits", {})["submodule_inventory"] = (
                "truncated_at_repository_file_limit")
            break
        row = RepositoryFile(
            snapshot_id=snapshot.id,
            path=submodule_path,
            role="submodule",
            excluded=True,
            exclusion_reason="submodule_not_fetched",
            submodule=True,
        )
        session.add(row)
        file_count += 1
        excluded_files += 1
        exclusion_reasons["submodule_not_fetched"] += 1
        manifest_entries.append({
            "path": submodule_path, "hash": "", "size": 0,
            "excluded": True, "reason": "submodule_not_fetched",
        })

    facts["languages"] = [
        {"language": language, "bytes": byte_count}
        for language, byte_count in sorted(languages.items(), key=lambda item: (-item[1], item[0]))
    ]
    dependencies = facts.get("dependencies", [])
    known_frameworks = {
        "react", "next", "vue", "svelte", "angular", "fastapi", "django", "flask",
        "spring-boot", "express", "nestjs", "rails", "laravel", "sqlmodel",
    }
    facts["frameworks"] = sorted({
        item["name"] for item in dependencies
        if item.get("name", "").casefold() in known_frameworks
    }, key=str.casefold)
    bounded_links, omitted_link_paths_omitted = _bounded_omitted_link_facts(
        omitted_links)
    facts["coverage"] = {
        "file_count": file_count,
        "total_bytes": total_bytes,
        "indexed_file_count": indexed_files,
        "indexed_bytes": indexed_bytes,
        "excluded_file_count": excluded_files,
        "files_with_evidence": len(evidence_by_path),
        "evidence_chunk_count": sum(len(items) for items in evidence_by_path.values()),
        "exclusion_reason_counts": {
            reason: count for reason, count in sorted(exclusion_reasons.items())
        },
        "omitted_link_count": len(omitted_links),
        "omitted_link_paths": bounded_links,
        "omitted_link_paths_omitted": omitted_link_paths_omitted,
    }
    _attach_fact_evidence(facts, evidence_by_path)
    facts.pop("_seen", None)
    facts.pop("_fact_count", None)
    manifest_entries.sort(key=lambda item: item["path"].casefold())
    manifest_hash = hashlib.sha256(
        json.dumps(manifest_entries, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")).hexdigest()

    snapshot.file_count = file_count
    snapshot.total_bytes = total_bytes
    snapshot.indexed_file_count = indexed_files
    snapshot.indexed_bytes = indexed_bytes
    snapshot.excluded_file_count = excluded_files
    snapshot.secret_finding_count = secret_findings
    snapshot.manifest_hash = manifest_hash
    snapshot.facts = json.dumps(facts, sort_keys=True)
    snapshot.scanner_version = SCANNER_VERSION
    # Persist the exact policy captured before reading the first file. Settings
    # may be edited while a long static scan is running; a later policy value
    # must never be stamped onto evidence produced under the earlier one.
    snapshot.scan_config_hash = scan_config_hash
    snapshot.omitted_links = json.dumps(
        omitted_links, sort_keys=True, separators=(",", ":"))
    snapshot.status = "ready"
    snapshot.error = ""
    snapshot.completed = utcnow()
    source.current_snapshot_id = snapshot.id
    if (source.pending_sha or "").lower() == snapshot.resolved_sha:
        source.pending_sha = ""
    source.local_only = bool(source.is_private) or source.local_only
    source.updated = utcnow()
    session.add(snapshot)
    session.add(source)
    session.commit()
    session.refresh(snapshot)
    return snapshot


def acquire_repository_snapshot(session: Session, source: RepositorySource,
                                sha: str | None = None, *,
                                force_rescan: bool = False,
                                progress: Callable | None = None,
                                cancelled: Callable[[], bool] | None = None) -> RepositorySnapshot:
    """Download, safely extract, atomically publish, and index one commit."""
    if source.id is None:
        raise RepositoryError("repository source must be stored before acquisition")
    target = (sha or source.pending_sha or "").lower()
    repo = GitHubRepository(source.owner, source.repository, source.canonical_url)
    token = get_github_token()
    _refresh_repository_visibility(session, source, token)
    if not target:
        target = resolve_repository_ref(
            repo, source.requested_ref or source.default_branch, token=token)["sha"]
    if not _SHA_RE.fullmatch(target):
        raise RepositoryError("repository target is not an immutable commit SHA")

    existing = session.exec(
        select(RepositorySnapshot).where(
            RepositorySnapshot.source_id == source.id,
            RepositorySnapshot.resolved_sha == target,
        )
    ).first()
    if existing and existing.status == "ready":
        try:
            if (repository_snapshot_path(existing).is_dir()
                    and not force_rescan
                    and existing.scan_config_hash
                    == repository_scan_config_hash(source)):
                source.current_snapshot_id = existing.id
                if (source.pending_sha or "").lower() == existing.resolved_sha:
                    source.pending_sha = ""
                session.add(source)
                session.commit()
                session.refresh(existing)
                return existing
        except RepositoryError:
            pass

    resolved = resolve_repository_ref(repo, target, token=token)
    if resolved["sha"] != target:
        raise RepositoryError("GitHub resolved the pinned commit unexpectedly")
    parent_id = source.current_snapshot_id
    snapshot = existing or RepositorySnapshot(
        source_id=source.id,
        parent_snapshot_id=parent_id,
        requested_ref=source.requested_ref or source.default_branch,
        resolved_sha=target,
    )
    snapshot.status = "pending"
    snapshot.error = ""
    snapshot.commit_url = resolved["commit_url"]
    snapshot.commit_time = resolved["commit_time"]
    session.add(snapshot)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        concurrent = session.exec(
            select(RepositorySnapshot).where(
                RepositorySnapshot.source_id == source.id,
                RepositorySnapshot.resolved_sha == target,
            )
        ).first()
        if concurrent and concurrent.status == "ready":
            return concurrent
        raise RepositoryError("this repository snapshot is already being acquired")
    session.refresh(snapshot)

    limits = repository_scan_settings()
    repository_root = settings.repository_dir.resolve()
    source_root = repository_root / str(source.id)
    final_path = source_root / target
    staging_root = repository_root / ".staging"
    source_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f"{source.id}-{target[:8]}-", dir=staging_root))
    archive = temporary / "repository.archive"
    unpacked = temporary / "snapshot"
    unpacked.mkdir()
    try:
        _check_canceled(cancelled)
        archive_hash, archive_type, compressed_bytes = _download_archive(
            repo, target, archive, token=token, limits=limits,
            progress=progress, cancelled=cancelled)
        if archive_type == "zip":
            _files, _bytes, omitted_links = _extract_zip(
                archive, unpacked, repo, target, limits=limits,
                compressed_bytes=compressed_bytes, progress=progress,
                cancelled=cancelled)
        else:
            _files, _bytes, omitted_links = _extract_tar(
                archive, unpacked, repo, target, limits=limits,
                compressed_bytes=compressed_bytes, progress=progress,
                cancelled=cancelled)
        _check_canceled(cancelled)
        previous = temporary / "previous"
        if final_path.is_symlink() or (final_path.exists() and not final_path.is_dir()):
            raise RepositoryError("repository snapshot destination is invalid")
        if final_path.exists():
            os.replace(final_path, previous)
            try:
                os.replace(unpacked, final_path)
            except Exception:
                if not final_path.exists() and previous.exists():
                    os.replace(previous, final_path)
                raise
        else:
            os.replace(unpacked, final_path)
        snapshot.archive_sha256 = archive_hash
        snapshot.archive_bytes = compressed_bytes
        snapshot.omitted_links = json.dumps(
            sorted(omitted_links, key=str.casefold), separators=(",", ":"))
        snapshot.relative_path = final_path.relative_to(repository_root).as_posix()
        session.add(snapshot)
        session.commit()
        session.refresh(snapshot)
        return scan_repository_snapshot(
            session, source, snapshot, progress=progress, cancelled=cancelled)
    except RepositoryError as exc:
        snapshot.status = "error"
        snapshot.error = str(exc)[:1000]
        session.add(snapshot)
        session.commit()
        raise
    except Exception as exc:
        log.exception("repository snapshot failed for %s/%s at %s",
                      source.owner, source.repository, target)
        snapshot.status = "error"
        snapshot.error = "repository snapshot failed safely"
        session.add(snapshot)
        session.commit()
        raise RepositoryError("repository snapshot failed safely") from exc
    finally:
        _remove_tree_safely(temporary, staging_root)


def repository_source_for_project(session: Session, project_id: int) -> RepositorySource | None:
    return session.exec(
        select(RepositorySource).where(RepositorySource.project_id == project_id)
    ).first()


def current_repository_snapshot(session: Session, project_id: int, *,
                                require_ready: bool = True) -> RepositorySnapshot | None:
    source = repository_source_for_project(session, project_id)
    if not source:
        return None
    snapshot = session.get(RepositorySnapshot, source.current_snapshot_id) \
        if source.current_snapshot_id else None
    if snapshot and (not require_ready or snapshot.status == "ready"):
        return snapshot
    query = select(RepositorySnapshot).where(RepositorySnapshot.source_id == source.id)
    if require_ready:
        query = query.where(RepositorySnapshot.status == "ready")
    return session.exec(query.order_by(RepositorySnapshot.created.desc())).first()


def ensure_snapshot(project_id: int, force: bool = False,
                    expected_sha: str | None = None, *,
                    progress: Callable | None = None,
                    cancelled: Callable[[], bool] | None = None) -> RepositorySnapshot:
    """Idempotent worker entry point for the ``repo_snapshot`` pipeline step."""
    with get_session() as session:
        project = session.get(Project, project_id)
        source = repository_source_for_project(session, project_id)
        if not project or project.source_type != "github" or not source:
            raise RepositoryError("repository project was not found")
        token = get_github_token()
        # Re-check visibility even when the immutable snapshot itself is being
        # reused. This closes the public-to-private transition before a forced
        # downstream analysis can resolve a cloud model. A previously known
        # private snapshot remains locally usable after its token is removed.
        if token or not source.is_private:
            _refresh_repository_visibility(session, source, token)
            session.commit()
            session.refresh(source)
        target = (expected_sha or source.pending_sha or "").lower()
        if expected_sha and not _SHA_RE.fullmatch(target):
            raise RepositoryError("expected repository SHA is invalid")
        current = current_repository_snapshot(session, project_id)
        if current and (not target or current.resolved_sha == target):
            needs_rescan = bool(
                force or current.scan_config_hash != repository_scan_config_hash(source)
            )
            if needs_rescan:
                if source.is_private and not token:
                    raise RepositoryError(
                        "restore the GitHub token to reacquire and rescan this "
                        "private repository's pinned commit")
                else:
                    current = acquire_repository_snapshot(
                        session, source, current.resolved_sha, force_rescan=True,
                        progress=progress, cancelled=cancelled)
            session.refresh(current)
            session.expunge(current)
            return current
        if not target:
            repo = GitHubRepository(source.owner, source.repository, source.canonical_url)
            target = resolve_repository_ref(
                repo, source.requested_ref or source.default_branch,
                token=get_github_token(required=source.is_private),
            )["sha"]
            source.pending_sha = target
            source.updated = utcnow()
            session.add(source)
            session.commit()
            session.refresh(source)
        snapshot = acquire_repository_snapshot(
            session, source, target, force_rescan=force,
            progress=progress, cancelled=cancelled)
        session.refresh(snapshot)
        session.expunge(snapshot)
        return snapshot


def list_repository_files(session: Session, snapshot_id: int, *,
                          included_only: bool = False) -> list[RepositoryFile]:
    query = select(RepositoryFile).where(RepositoryFile.snapshot_id == snapshot_id)
    if included_only:
        query = query.where(RepositoryFile.excluded == False)  # noqa: E712
    return list(session.exec(query.order_by(RepositoryFile.path)).all())


def list_repository_evidence(session: Session, snapshot_id: int, *,
                             include_body: bool = True) -> list[dict]:
    """Return evidence rows, optionally as lightweight budget metadata only."""
    if not include_body:
        rows = session.exec(
            select(
                RepositoryChunk.id,
                RepositoryFile.id,
                RepositoryChunk.evidence_id,
                RepositoryFile.path,
                RepositoryChunk.start_line,
                RepositoryChunk.end_line,
                RepositoryChunk.body_hash,
                RepositoryChunk.content_hash,
                RepositoryChunk.kind,
                RepositoryChunk.symbol,
                RepositoryFile.analysis_priority,
                func.length(RepositoryChunk.body),
            )
            .join(RepositoryFile, RepositoryFile.id == RepositoryChunk.file_id)
            .where(RepositoryFile.snapshot_id == snapshot_id)
            .order_by(RepositoryFile.path, RepositoryChunk.chunk_index)
        ).all()
        return [{
            "chunk_id": chunk_id,
            "file_id": file_id,
            "snapshot_id": snapshot_id,
            "evidence_id": evidence_id,
            "path": path,
            "start_line": start_line,
            "end_line": end_line,
            "body_hash": body_hash,
            "content_hash": content_hash,
            "kind": kind,
            "symbol": symbol,
            "analysis_priority": analysis_priority,
            "body_chars": int(body_chars or 0),
        } for (
            chunk_id, file_id, evidence_id, path, start_line, end_line,
            body_hash, content_hash, kind, symbol, analysis_priority, body_chars,
        ) in rows]
    rows = session.exec(
        select(RepositoryChunk, RepositoryFile)
        .join(RepositoryFile, RepositoryFile.id == RepositoryChunk.file_id)
        .where(RepositoryFile.snapshot_id == snapshot_id)
        .order_by(RepositoryFile.path, RepositoryChunk.chunk_index)
    ).all()
    return [{
        "chunk_id": chunk.id,
        "file_id": file.id,
        "snapshot_id": snapshot_id,
        "evidence_id": chunk.evidence_id,
        "path": file.path,
        "start_line": chunk.start_line,
        "end_line": chunk.end_line,
        "body": chunk.body,
        "body_hash": chunk.body_hash,
        "content_hash": chunk.content_hash,
        "kind": chunk.kind,
        "symbol": chunk.symbol,
        "summary_text": chunk.summary_text,
        "summary_json": chunk.summary_json,
        "summary_config_hash": chunk.summary_config_hash,
        "analysis_priority": file.analysis_priority,
        "body_chars": len(chunk.body),
    } for chunk, file in rows]


def read_repository_file(snapshot: RepositorySnapshot, path: str, *,
                         start_line: int | None = None,
                         end_line: int | None = None) -> str:
    clean = normalize_scope_patterns([path])
    if len(clean) != 1 or any(char in clean[0] for char in "*?["):
        raise RepositoryError("repository file path is invalid")
    root = repository_snapshot_path(snapshot)
    target = (root / Path(*PurePosixPath(clean[0]).parts)).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as exc:
        raise RepositoryError("repository file path is unsafe") from exc
    if not target.is_file() or target.is_symlink():
        raise RepositoryError("repository file was not found")
    if target.stat().st_size > 5 * 1024 * 1024:
        raise RepositoryError(
            "repository file is too large for the bounded source viewer")
    try:
        body = target.read_text(encoding="utf-8-sig")
    except (UnicodeDecodeError, OSError) as exc:
        raise RepositoryError("repository file is not readable text") from exc
    if start_line is None and end_line is None:
        return body
    lines = body.splitlines()
    start = max(1, start_line or 1)
    end = min(len(lines), end_line or len(lines))
    if start > end:
        raise RepositoryError("repository line range is invalid")
    return "\n".join(lines[start - 1:end])


def validate_repository_citations(session: Session, snapshot_id: int,
                                  evidence_ids: Iterable[str]) -> dict[str, RepositoryChunk]:
    requested = list(dict.fromkeys(str(item) for item in evidence_ids))
    if not requested:
        return {}
    rows = session.exec(
        select(RepositoryChunk, RepositoryFile)
        .join(RepositoryFile, RepositoryFile.id == RepositoryChunk.file_id)
        .where(RepositoryFile.snapshot_id == snapshot_id,
               RepositoryChunk.evidence_id.in_(requested))
    ).all()
    found = {chunk.evidence_id: chunk for chunk, _file in rows}
    missing = [item for item in requested if item not in found]
    if missing:
        raise ValueError(f"unknown repository evidence id(s): {', '.join(missing[:10])}")
    for chunk, file in rows:
        if chunk.start_line < 1 or chunk.end_line < chunk.start_line \
                or (file.line_count and chunk.end_line > file.line_count):
            raise ValueError(f"invalid repository citation range for {chunk.evidence_id}")
    return found


def get_chunk_summary(chunk: RepositoryChunk, config_hash: str) -> dict | None:
    if not chunk.summary_text or chunk.summary_config_hash != config_hash:
        return None
    try:
        data = json.loads(chunk.summary_json or "{}")
    except json.JSONDecodeError:
        data = {}
    return {"text": chunk.summary_text, "data": data, "config_hash": config_hash}


def set_chunk_summary(session: Session, chunk_id: int, *, text_value: str,
                      data: dict | None, config_hash: str) -> RepositoryChunk:
    chunk = session.get(RepositoryChunk, chunk_id)
    if not chunk:
        raise ValueError("repository chunk was not found")
    chunk.summary_text = text_value
    chunk.summary_json = json.dumps(data or {}, sort_keys=True)
    chunk.summary_config_hash = config_hash
    session.add(chunk)
    session.flush()
    return chunk


def search_repository_chunks(session: Session, query: str, *,
                             project_id: int | None = None,
                             limit: int = 20) -> list[dict]:
    """FTS query helper returning the same evidence shape as the pipeline."""
    clean = (query or "").strip()
    if not clean:
        return []
    sql = (
        "SELECT chunk_id FROM repository_chunk_fts WHERE repository_chunk_fts MATCH :q "
        + ("AND project_id = :pid " if project_id is not None else "")
        + "ORDER BY rank LIMIT :lim"
    )
    params = {"q": clean, "lim": max(1, min(int(limit), 100))}
    if project_id is not None:
        params["pid"] = project_id
    ids = [row[0] for row in session.exec(text(sql).bindparams(**params)).all()]
    if not ids:
        return []
    rows = session.exec(
        select(RepositoryChunk, RepositoryFile)
        .join(RepositoryFile, RepositoryFile.id == RepositoryChunk.file_id)
        .where(RepositoryChunk.id.in_(ids))
    ).all()
    by_id = {chunk.id: (chunk, file) for chunk, file in rows}
    output = []
    for chunk_id in ids:
        pair = by_id.get(chunk_id)
        if not pair:
            continue
        chunk, file = pair
        output.append({
            "chunk_id": chunk.id, "file_id": file.id,
            "snapshot_id": file.snapshot_id, "evidence_id": chunk.evidence_id,
            "path": file.path, "start_line": chunk.start_line,
            "end_line": chunk.end_line, "body": chunk.body,
            "body_hash": chunk.body_hash, "content_hash": chunk.content_hash,
            "kind": chunk.kind, "symbol": chunk.symbol,
        })
    return output
