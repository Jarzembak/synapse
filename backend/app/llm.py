"""Single entry point for all chat-LLM calls.

complete(function, ...) looks up the (provider, model) configured for that
pipeline function — Settings-table override first, config.py default second —
and dispatches to a local provider (Ollama's native API, or any
OpenAI-compatible server that isn't OpenAI itself, such as LM Studio /
llama.cpp / vLLM) or a frontier API (Anthropic, Gemini, OpenAI).

complete_json() is the structured variant: local providers get native JSON
enforcement (Ollama `format`, OpenAI-compatible `response_format`), and the
parse retries and strips fences/prose until the output parses, since local
models are fence-happy.

Local-provider behavior (context window, keep-alive, thinking, timeout, JSON
mode) is tuned from Settings → Advanced → Local models (`advanced("local")`).
"""
from __future__ import annotations

import copy
import ipaddress
import json
import logging
import math
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

import httpx

from .config import FUNCTION_DEFAULTS, advanced, settings
from .settings_store import get_setting

log = logging.getLogger("synapse.llm")

MAX_TOKENS = 16384
LOCAL_PROVIDERS = {"ollama", "openai_compat"}
# Automatic context growth is bounded so one small request cannot reserve a
# model's entire advertised window. Explicitly configured larger minima remain
# valid and native model metadata is always the final ceiling.
LOCAL_CONTEXT_BUCKETS = (
    2_048, 4_096, 8_192, 12_288, 16_384, 24_576, 32_768, 40_960, 49_152, 65_536,
)
LOCAL_AUTOMATIC_CONTEXT_CAP = 65_536
# Backward-compatible name consumed by older safety/provenance code. It is now
# an automatic planning cap, not an unconditional context floor.
REPOSITORY_NUM_CTX = 65536
REPOSITORY_TIMEOUT_SECONDS = 1800.0
LOCAL_CONTEXT_POLICY_VERSION = 2
_usage: ContextVar[tuple[int, int]] = ContextVar("llm_usage", default=(0, 0))
_project_scope: ContextVar[tuple[int | None, bool | None]] = ContextVar(
    "llm_project_scope", default=(None, None))
_last_diagnostics: ContextVar[dict | None] = ContextVar(
    "llm_last_call_diagnostics", default=None)


@contextmanager
def project_scope(project_id: int | None, *, local_only: bool | None = None):
    """Attach project privacy to calls that do not have a persisted Job.

    Normal pipeline calls are discoverable through ``current_job_id``.  Small
    follow-on tasks such as auto-tagging carry only an artifact/project id, so
    they use this explicit scope to keep the same local-only boundary.
    """
    token = _project_scope.set((project_id, local_only))
    try:
        yield
    finally:
        _project_scope.reset(token)


def _project_local_only() -> bool:
    """Return the active project's source-neutral local-processing policy.

    Repository behavior is unchanged. Paper projects use their own sticky
    ``PaperSource.local_only`` decision, which is locked by the first
    processing job. Missing privacy metadata for either sensitive source type
    fails closed instead of silently becoming cloud eligible.
    """
    scoped_project_id, scoped_policy = _project_scope.get()
    if scoped_policy is True:
        return True
    project_id = scoped_project_id
    try:
        if project_id is None:
            from .context import current_job_id
            from .db import get_session
            from .models import Job

            job_id = current_job_id.get()
            if not job_id:
                return False
            with get_session() as session:
                job = session.get(Job, job_id)
                project_id = job.project_id if job else None
        if project_id is None:
            return False

        from .db import get_session
        from .models import Project

        with get_session() as session:
            project = session.get(Project, project_id)
            if not project:
                return False
            from sqlmodel import select

            if project.source_type == "github":
                from .models import RepositorySource

                source = session.exec(
                    select(RepositorySource).where(
                        RepositorySource.project_id == project_id)
                ).first()
                # A GitHub project whose policy row is missing or unreadable
                # must not become cloud-eligible by accident.
                if source is None:
                    return True
                private = bool(getattr(source, "is_private",
                                       getattr(source, "private", False)))
                return private or bool(getattr(source, "local_only", False))
            if project.source_type == "paper":
                from .models import PaperSource

                source = session.exec(
                    select(PaperSource).where(PaperSource.project_id == project_id)
                ).first()
                return True if source is None else bool(source.local_only)
            return False
    except ImportError:
        # During an old-schema startup there cannot yet be a valid GitHub
        # project.  A scoped repository id is nevertheless treated as private.
        return project_id is not None


def _repository_local_only() -> bool:
    """Backward-compatible alias for callers/tests from the repository release."""
    return _project_local_only()


def _local_model(function: str) -> str:
    if function == "repository_reduce":
        key = "repository.reduce_model"
        default = settings.repository_reduce_model
    else:
        key = "repository.local_model"
        default = settings.repository_local_model
    configured = get_setting(key, default) or default
    if isinstance(configured, dict):
        provider = configured.get("provider", "ollama")
        model = configured.get("model", "")
        if provider != "ollama":
            raise RuntimeError(f"{key} must use the ollama provider")
    else:
        model = str(configured or "")
    try:
        return validate_local_ollama_model(model)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


def validate_local_ollama_model(value: str) -> str:
    """Reject model identifiers that can invoke Ollama cloud offload."""
    model = str(value or "").strip()
    if (not model or len(model) > 200 or any(ord(char) < 32 for char in model)
            or any(char.isspace() for char in model)
            or "://" in model or "\\" in model
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*", model)):
        raise ValueError("model must name a valid local Ollama model")
    if "cloud" in re.split(r"[._/:@-]+", model.casefold()):
        raise ValueError("local-only processing cannot use an Ollama cloud model")
    return model


def require_local_ollama_endpoint() -> None:
    """Fail closed if private data would be sent to a remote Ollama server."""
    try:
        parsed = urlsplit(settings.ollama_base_url)
        host = (parsed.hostname or "").rstrip(".").lower()
        parsed.port
    except ValueError as exc:
        raise RuntimeError("OLLAMA_BASE_URL is invalid for local-only processing") from exc
    allowed_names = {"localhost", "ollama", "host.docker.internal"}
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if (parsed.scheme not in {"http", "https"} or not (host in allowed_names or loopback)
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise RuntimeError(
            "local-only processing requires OLLAMA_BASE_URL to use the "
            "local Ollama service, localhost, or a loopback address"
        )


def _enforce_local_provider(function: str, provider: str, model: str,
                            *, local_only: bool = False) -> tuple[str, str]:
    # ASR is unrelated to static repository projects. Repository TTS is pinned
    # here as well as at execution so effective provenance and actual synthesis
    # agree even when the global model matrix selects Gemini.
    if function == "asr":
        return provider, model
    restricted = bool(local_only or _project_local_only())
    if function == "tts":
        return ("piper", "en_US-ryan-medium") if restricted else (provider, model)
    if restricted:
        require_local_ollama_endpoint()
        return "ollama", _local_model(function)
    if provider == "ollama":
        return provider, model
    return provider, model


class LLMHTTPError(RuntimeError):
    """HTTP failure from a provider, with the status preserved so the
    transient-retry logic can classify it."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class EmptyResponseError(RuntimeError):
    """Model returned no usable text — local models do this sporadically, so
    it is treated as transient and retried."""


class OutputBudgetError(RuntimeError):
    """Generation stopped because the output-token budget ran out, not
    because the model finished. Deterministic for a given prompt and budget,
    so it is not transient."""

    def __init__(self, *, model: str, max_tokens: int, visible_output: bool):
        self.max_tokens = max_tokens
        self.visible_output = visible_output
        # No settings path in this message: pipeline steps pass explicit
        # budgets that Settings → Advanced → Generation parameters cannot
        # override, so naming that knob would send operators to a dead end.
        outcome = ("its reply was cut off mid-generation" if visible_output
                   else "no usable output was produced within the budget "
                        "(hidden reasoning or non-content tokens consumed it)")
        super().__init__(
            f"{model} hit the {max_tokens:,}-token output budget before "
            f"finishing — {outcome}; raise this step's output-token budget "
            "or send less input")


class ContextWindowError(RuntimeError):
    """A prompt cannot fit without truncation in the model's usable window."""

    def __init__(
        self,
        *,
        required_context: int,
        native_context: int,
        effective_context: int,
    ):
        self.required_context = required_context
        self.native_context = native_context
        self.effective_context = effective_context
        ceiling = native_context or effective_context
        super().__init__(
            f"the request needs approximately {required_context:,} context "
            f"tokens, but the model can use only {ceiling:,}; subdivide the "
            "input or lower the output-token budget"
        )


def last_call_diagnostics() -> dict:
    """Return a detached snapshot of the most recent call in this context.

    The context-local storage keeps concurrent worker tasks isolated. Existing
    callers can ignore it; repository orchestration can persist the snapshot on
    a Job after a failure or a successful adaptive batch.
    """
    return copy.deepcopy(_last_diagnostics.get() or {})


def _update_diagnostics(**values) -> None:
    current = copy.deepcopy(_last_diagnostics.get() or {})
    current.update(values)
    _last_diagnostics.set(current)


def _append_attempt(value: dict) -> None:
    current = copy.deepcopy(_last_diagnostics.get() or {})
    current.setdefault("attempts", []).append(value)
    _last_diagnostics.set(current)


def _record_call(function: str, provider: str, model: str, started: float,
                 input_chars: int, output: str, error: Exception | None) -> None:
    try:
        from .context import current_job_id
        from .db import get_session
        from .models import LLMCall

        input_tokens, output_tokens = _usage.get()
        with get_session() as session:
            session.add(LLMCall(
                job_id=current_job_id.get(), function=function, provider=provider,
                model=model, input_chars=input_chars, output_chars=len(output),
                input_tokens=input_tokens, output_tokens=output_tokens,
                duration_seconds=round(time.monotonic() - started, 3),
                status="error" if error else "ok",
                error=str(error)[:1000] if error else "",
            ))
            session.commit()
    except Exception:
        log.warning("could not record LLM usage", exc_info=True)


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, EmptyResponseError):
        return True
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in {408, 409, 429} or isinstance(status, int) and status >= 500:
        return True
    return exc.__class__.__name__ in {
        "APITimeoutError", "APIConnectionError", "InternalServerError",
        "RateLimitError", "ServiceUnavailableError", "ConnectError", "ReadTimeout",
        "ConnectTimeout", "RemoteProtocolError",
    }


def resolve_model(function: str) -> tuple[str, str]:
    override = get_setting(f"model.{function}")
    if override:
        provider, model = override["provider"], override["model"]
        return _enforce_local_provider(function, provider, model)
    d = FUNCTION_DEFAULTS[function]
    return _enforce_local_provider(function, d["provider"], d["model"])


def resolve_params(function: str) -> tuple[float | None, int, bool]:
    """Per-function generation params (Settings → Advanced, key params.<fn>).

    The third element reports whether max_tokens was explicitly configured —
    the module-wide MAX_TOKENS fallback is a ceiling local planning may fit
    to the model, while a configured value is a demand."""
    p = get_setting(f"params.{function}") or {}
    temperature = p.get("temperature")
    explicit = bool(p.get("max_tokens"))
    max_tokens = int(p.get("max_tokens") or MAX_TOKENS)
    return temperature, max_tokens, explicit


def complete(
    function: str,
    system: str,
    user: str,
    *,
    max_tokens: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    json_format: bool = False,
    local_only: bool = False,
    transient_attempts: int | None = None,
) -> str:
    restricted = bool(local_only or _project_local_only())
    if provider is None or model is None:
        provider, model = resolve_model(function)
    provider, model = _enforce_local_provider(
        function, provider, model, local_only=restricted)
    temperature, cfg_max, cfg_explicit = resolve_params(function)
    default_budget = False
    if max_tokens is None:
        max_tokens = cfg_max
        # Untuned functions fall back to the module-wide MAX_TOKENS ceiling;
        # local context planning may fit that ceiling to the model instead of
        # charging all 16k output tokens against a small context window.
        default_budget = not cfg_explicit

    log.debug("completing %s via %s/%s (max_tokens=%s, temperature=%s)",
              function, provider, model, max_tokens, temperature)
    started = time.monotonic()
    output = ""
    error: Exception | None = None
    _usage.set((0, 0))
    _last_diagnostics.set({
        "function": function,
        "provider": provider,
        "model": model,
        "requested_context": None,
        "effective_context": None,
        "native_context": None,
        "timeout_seconds": None,
        "max_output_tokens": max_tokens,
        "attempts": [],
    })
    if transient_attempts is None:
        attempts = max(
            1,
            min(int(get_setting("llm.transient_attempts", 3) or 3), 5),
        )
    else:
        if isinstance(transient_attempts, bool):
            raise ValueError("transient_attempts must be an integer from 1 to 5")
        attempts = int(transient_attempts)
        if not 1 <= attempts <= 5:
            raise ValueError("transient_attempts must be an integer from 1 to 5")
    try:
        for attempt in range(attempts):
            attempt_started = time.monotonic()
            try:
                if provider == "ollama":
                    output = _ollama(system, user, model, max_tokens, temperature,
                                     json_format, restricted=restricted,
                                     function=function,
                                     default_budget=default_budget)
                elif provider == "openai_compat":
                    output = _openai_compat(system, user, model, max_tokens,
                                            temperature, json_format)
                elif provider == "openai":
                    output = _openai(system, user, model, max_tokens, temperature,
                                     json_format)
                elif provider == "anthropic":
                    output = _anthropic(system, user, model, max_tokens, temperature)
                elif provider == "gemini":
                    output = _gemini(system, user, model, max_tokens, temperature)
                else:
                    raise ValueError(
                        f"unknown provider {provider!r} for function {function!r}")
                if not output.strip():
                    raise EmptyResponseError(
                        f"{provider}/{model} returned an empty response")
                _append_attempt({
                    "attempt": attempt + 1,
                    "status": "ok",
                    "duration_seconds": round(
                        time.monotonic() - attempt_started, 3),
                    "transient": False,
                    "retry_delay_seconds": 0,
                })
                return output
            except Exception as exc:
                error = exc
                transient = _is_transient(exc)
                will_retry = attempt + 1 < attempts and transient
                delay = min(8, 2 ** attempt) if will_retry else 0
                status = getattr(exc, "status_code", None)
                if status is None:
                    status = getattr(
                        getattr(exc, "response", None), "status_code", None)
                _append_attempt({
                    "attempt": attempt + 1,
                    "status": "error",
                    "duration_seconds": round(
                        time.monotonic() - attempt_started, 3),
                    "transient": transient,
                    "error_type": exc.__class__.__name__,
                    "error": str(exc)[:1000],
                    "error_message": str(exc)[:1000],
                    "status_code": status,
                    "retry_delay_seconds": delay,
                })
                if not will_retry:
                    raise
                log.warning("transient %s failure (%s); retrying in %ss",
                            function, exc, delay)
                time.sleep(delay)
    finally:
        # mirror the emptiness check above: a whitespace-only reply raised, so
        # it must not be recorded as a successful call
        _record_call(function, provider, model, started, len(system) + len(user),
                     output, error if not output.strip() else None)


def complete_json(function: str, system: str, user: str, *, retries: int = 2, **kw):
    system = system + "\nRespond with ONLY valid JSON. No prose, no code fences."
    last_err: Exception | None = None
    for _ in range(retries + 1):
        raw = complete(function, system, user, json_format=True, **kw)
        try:
            return _extract_json(raw)
        except json.JSONDecodeError as e:
            last_err = e
            log.warning("%s produced invalid JSON (retrying): %s", function, e)
            user = user + "\n\nYour previous reply was not valid JSON. JSON only."
    raise ValueError(f"{function}: model never produced valid JSON: {last_err}")


def _strip_think(raw: str) -> str:
    """Drop <think>…</think> reasoning blocks some local models emit inline.

    An unclosed <think> means the model never finished reasoning — everything
    from the tag on is thinking, so nothing usable remains after it.
    """
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    i = raw.find("<think>")
    if i != -1:
        raw = raw[:i]
    return raw.strip()


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if m:
        return m.group(1).strip()
    # some models prefix prose; grab from the earliest { or [ so a top-level
    # array isn't truncated to its first element object
    starts = [i for i in (raw.find(opener) for opener in "{[") if i != -1]
    if starts:
        return raw[min(starts):]
    return raw


def _extract_json(raw: str):
    """Parse a model reply into JSON, tolerating fences, leading prose, and
    trailing prose ('{...} Hope this helps!') — all common with local models."""
    cleaned = _strip_fences(_strip_think(raw))
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        value, _end = json.JSONDecoder().raw_decode(cleaned)
        return value


def _local_cfg() -> dict:
    return advanced("local")


def _estimate_input_tokens(system: str, user: str) -> int:
    """Conservatively estimate mixed prose/code tokens without a tokenizer."""
    text = system + "\n" + user
    if not text:
        return 1
    # Code, JSON, CJK text, emoji, and identifiers tokenize more densely than
    # ordinary English prose. UTF-8 bytes protect non-ASCII inputs while the
    # 2.5-byte bound leaves headroom for punctuation-heavy source code.
    byte_estimate = math.ceil(len(text.encode("utf-8")) / 2.5)
    word_estimate = math.ceil(len(re.findall(r"\S+", text)) * 1.35)
    return max(1, byte_estimate, word_estimate)


def _bucket_context(target: int, *, configured_minimum: int) -> int:
    cap = max(configured_minimum, LOCAL_AUTOMATIC_CONTEXT_CAP)
    for bucket in LOCAL_CONTEXT_BUCKETS:
        if bucket >= target and bucket >= configured_minimum:
            return min(bucket, cap)
    return cap


def _known_native_context(model: str) -> int:
    """Read installed-model metadata before resource assessment when possible."""
    try:
        from .local_model_safety import inspect_model

        inventory, row = inspect_model(model)
        if inventory.get("ok") and row:
            return max(0, int(row.get("native_context_tokens") or 0))
    except Exception:
        log.debug("could not inspect %s native context", model, exc_info=True)
    return 0


def _ollama_context_plan(
    system: str,
    user: str,
    max_tokens: int,
    *,
    configured_context: int,
    native_context: int = 0,
    flexible_output: bool = False,
) -> dict[str, int]:
    input_tokens = _estimate_input_tokens(system, user)
    output_tokens = max(1, int(max_tokens))
    configured_minimum = max(1_024, int(configured_context))
    if flexible_output and native_context:
        # The module-default budget (MAX_TOKENS) is a ceiling for functions
        # nobody has tuned, not a demand: charged in full it exceeds any
        # <=16k-context model's whole window, refusing every call with
        # "subdivide the input" advice that cannot help. Fit the default to
        # the window the planner can actually grant — requested context is
        # bucketed and capped, so a 128k-native model still admits at most
        # the automatic cap; fitting against raw native would leave mid-size
        # prompts refused with a self-contradictory message. Solving
        # input + out + margin(out) <= ceiling with margin >= out/10 gives
        # out <= (ceiling - input - prompt margin)/1.1.
        ceiling = min(
            native_context,
            max(configured_minimum, LOCAL_AUTOMATIC_CONTEXT_CAP),
        )
        prompt_margin = max(512, math.ceil(input_tokens * 0.10))
        fitted = math.floor((ceiling - input_tokens - prompt_margin) / 1.1)
        if fitted >= 256:
            output_tokens = min(output_tokens, fitted)
    safety_margin = max(
        512,
        math.ceil(input_tokens * 0.10),
        math.ceil(output_tokens * 0.10),
    )
    required = input_tokens + output_tokens + safety_margin
    requested = _bucket_context(
        max(configured_minimum, required),
        configured_minimum=configured_minimum,
    )
    effective = min(requested, native_context) if native_context else requested
    return {
        "estimated_input_tokens": input_tokens,
        "safety_margin_tokens": safety_margin,
        "required_context": required,
        "requested_context": requested,
        "effective_context": effective,
        "native_context": native_context,
        "planned_output_tokens": output_tokens,
    }


def _ollama(system: str, user: str, model: str, max_tokens: int,
            temperature: float | None, json_format: bool = False,
            *, restricted: bool = False, function: str = "",
            default_budget: bool = False) -> str:
    """Ollama's native /api/chat. The native API (unlike its OpenAI-compat
    shim) accepts per-call options — critically num_ctx, without which long
    transcript chunks are silently truncated at the server's default window."""
    cfg = _local_cfg()
    native_context = _known_native_context(model)
    context_plan = _ollama_context_plan(
        system,
        user,
        max_tokens,
        configured_context=int(cfg["num_ctx"]),
        native_context=native_context,
        flexible_output=default_budget,
    )
    options: dict = {
        "num_predict": context_plan["planned_output_tokens"],
        "num_ctx": context_plan["effective_context"],
    }
    timeout_seconds = float(cfg["timeout_seconds"])
    if restricted:
        # Repository and local-only paper work may run with CPU offload. Keep
        # their established long-request timeout, while sizing memory from the
        # actual prompt and output budget instead of reserving 65k every time.
        timeout_seconds = max(timeout_seconds, REPOSITORY_TIMEOUT_SECONDS)
    _update_diagnostics(
        function=function,
        requested_context=context_plan["requested_context"],
        effective_context=context_plan["effective_context"],
        native_context=context_plan["native_context"] or None,
        timeout_seconds=timeout_seconds,
        max_output_tokens=context_plan["planned_output_tokens"],
        estimated_input_tokens=context_plan["estimated_input_tokens"],
        safety_margin_tokens=context_plan["safety_margin_tokens"],
        required_context=context_plan["required_context"],
    )
    if context_plan["required_context"] > context_plan["effective_context"]:
        # When native metadata already proves the request cannot fit, expose a
        # subdividable context error before resource admission can mask it.
        raise ContextWindowError(
            required_context=context_plan["required_context"],
            native_context=context_plan["native_context"],
            effective_context=context_plan["effective_context"],
        )
    # Recheck immediately before the model call.  Assignment-time checks can be
    # stale after another application consumes memory or a tag is updated to a
    # different digest.  Remote Ollama hosts retain capability checking but
    # report resource status as unavailable rather than using this host's RAM.
    from .local_model_safety import LocalModelSafetyError, ensure_model_safe

    try:
        safety = ensure_model_safe(
            model,
            role="completion",
            requested_context=options["num_ctx"],
            refresh=True,
        )
    except LocalModelSafetyError as exc:
        failed_assessment = exc.assessment if isinstance(exc.assessment, dict) else {}
        _update_diagnostics(
            model_digest=exc.digest or None,
            safety_assessment=exc.assessment,
            resident_transition=failed_assessment.get("resident_transition"),
        )
        raise
    _update_diagnostics(
        model_digest=safety.get("digest") or None,
        safety_assessment=safety.get("assessment"),
        resident_transition=safety.get("resident_transition"),
    )
    vetted_context = int(options["num_ctx"])
    fresh_native_context = int(safety.get("native_context_tokens") or 0)
    discovered_native = fresh_native_context or native_context
    if discovered_native and discovered_native != native_context:
        context_plan = _ollama_context_plan(
            system,
            user,
            max_tokens,
            configured_context=int(cfg["num_ctx"]),
            native_context=discovered_native,
            flexible_output=default_budget,
        )
        options["num_predict"] = context_plan["planned_output_tokens"]
        options["num_ctx"] = context_plan["effective_context"]
        if options["num_ctx"] != vetted_context:
            # Reassess resources against the context Ollama will actually
            # use. Compare against the context the first admission vetted,
            # not the plan's own requested bucket — a discovered-larger
            # native window can grow the KV reservation past what was
            # admitted while landing exactly on its new bucket.
            try:
                safety = ensure_model_safe(
                    model,
                    role="completion",
                    requested_context=options["num_ctx"],
                )
            except LocalModelSafetyError as exc:
                failed_assessment = (
                    exc.assessment if isinstance(exc.assessment, dict) else {})
                _update_diagnostics(
                    model_digest=exc.digest or None,
                    safety_assessment=exc.assessment,
                    resident_transition=failed_assessment.get(
                        "resident_transition"),
                )
                raise
            _update_diagnostics(
                model_digest=safety.get("digest") or None,
                safety_assessment=safety.get("assessment"),
                resident_transition=safety.get("resident_transition"),
            )
    _update_diagnostics(
        function=function,
        requested_context=context_plan["requested_context"],
        effective_context=context_plan["effective_context"],
        native_context=context_plan["native_context"] or None,
        timeout_seconds=timeout_seconds,
        max_output_tokens=context_plan["planned_output_tokens"],
        estimated_input_tokens=context_plan["estimated_input_tokens"],
        safety_margin_tokens=context_plan["safety_margin_tokens"],
        required_context=context_plan["required_context"],
    )
    if context_plan["required_context"] > context_plan["effective_context"]:
        raise ContextWindowError(
            required_context=context_plan["required_context"],
            native_context=context_plan["native_context"],
            effective_context=context_plan["effective_context"],
        )
    if temperature is not None:
        options["temperature"] = temperature
    payload: dict = {
        "model": model,
        "stream": False,
        "options": options,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    keep_alive = cfg.get("keep_alive")
    if keep_alive not in ("", None):
        # Ollama parses string values with Go's time.ParseDuration, which
        # rejects unit-less numbers — the seconds / negative-means-forever
        # semantics only apply to JSON numbers, so coerce "-1"/"300" to those.
        try:
            payload["keep_alive"] = (float(keep_alive) if "." in str(keep_alive)
                                     else int(keep_alive))
        except (TypeError, ValueError):
            payload["keep_alive"] = keep_alive
    think = cfg.get("think", "auto")
    if restricted:
        # Repository (local-only) steps: Qwen3 enables thinking by default,
        # but deterministic repository JSON and cited guides need the bounded
        # completion budget for visible output, not hidden reasoning tokens.
        payload["think"] = False
    elif think in ("on", "off"):
        payload["think"] = think == "on"
    if json_format and cfg.get("json_mode", True):
        payload["format"] = "json"
    # Ollama is a local transport boundary. Never let HTTP(S)_PROXY redirect
    # private prompts through an outbound proxy even when the URL hostname is
    # local-looking (for example `ollama` in Docker Compose).
    for _ in range(2):
        response = httpx.post(
            f"{settings.ollama_base_url}/api/chat", json=payload,
            timeout=httpx.Timeout(timeout_seconds, connect=10),
            trust_env=False,
        )
        if response.status_code >= 400:
            try:
                detail = response.json().get("error") or response.text
            except ValueError:
                detail = response.text
            # models without the thinking capability reject the flag outright;
            # drop it and retry once (mirrors the response_format fallback in
            # _openai_compat)
            if (response.status_code == 400 and "think" in payload
                    and "think" in str(detail).lower()):
                log.warning("%s rejected the think flag (%s); retrying without",
                            model, str(detail)[:200])
                payload.pop("think")
                continue
            raise LLMHTTPError(
                f"ollama returned {response.status_code}: {detail[:500]}",
                response.status_code)
        break
    data = response.json()
    _usage.set((data.get("prompt_eval_count") or 0, data.get("eval_count") or 0))
    # thinking arrives in message.thinking when supported; <think> tags in
    # content still show up from GGUF imports without a structured template
    output = _strip_think((data.get("message") or {}).get("content") or "")
    if data.get("done_reason") == "length":
        # num_predict ran out mid-generation. Returning the truncated text
        # would corrupt artifacts silently, and retrying with the same budget
        # re-bills the identical truncation, so fail loud and non-transient.
        raise OutputBudgetError(
            model=model, max_tokens=int(options["num_predict"]),
            visible_output=bool(output))
    return output


def _openai_compat(system: str, user: str, model: str, max_tokens: int,
                   temperature: float | None, json_format: bool = False) -> str:
    """Any OpenAI-compatible server that isn't OpenAI itself: LM Studio,
    llama.cpp, vLLM, … (OpenAI's own API is the separate "openai" provider).
    The max_completion_tokens fallback below stays useful here regardless —
    OpenAI-fronting proxies and newer compat servers want it too."""
    from openai import OpenAI

    cfg = _local_cfg()
    base = (settings.openai_compat_base_url or "").rstrip("/")
    if not base:
        raise RuntimeError(
            "the openai_compat provider needs OPENAI_COMPAT_BASE_URL in .env "
            "(e.g. http://host.docker.internal:1234/v1 for LM Studio)")
    client = OpenAI(base_url=base, api_key=settings.openai_compat_api_key or "local",
                    timeout=float(cfg["timeout_seconds"]), max_retries=0)
    kw: dict = {"max_tokens": max_tokens}
    if temperature is not None:
        kw["temperature"] = temperature
    if json_format and cfg.get("json_mode", True):
        kw["response_format"] = {"type": "json_object"}
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    # Two per-call fallbacks, each dropped at most once on a 400: OpenAI's
    # reasoning models take max_completion_tokens instead of max_tokens, and
    # not every local server implements response_format.
    for _ in range(3):
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages, **kw)
            break
        except Exception as exc:
            if getattr(exc, "status_code", None) == 400:
                if ("max_tokens" in kw
                        and "max_completion_tokens" in str(exc)):
                    log.warning("%s wants max_completion_tokens (%s); retrying",
                                base, exc)
                    kw["max_completion_tokens"] = kw.pop("max_tokens")
                    continue
                if "response_format" in kw:
                    log.warning("%s rejected response_format (%s); retrying without",
                                base, exc)
                    kw.pop("response_format")
                    continue
            raise
    if resp.usage:
        _usage.set((resp.usage.prompt_tokens or 0, resp.usage.completion_tokens or 0))
    return _strip_think(resp.choices[0].message.content or "")


def _openai(system: str, user: str, model: str, max_tokens: int,
            temperature: float | None, json_format: bool = False) -> str:
    """OpenAI's own API — a frontier provider like anthropic/gemini, distinct
    from openai_compat (which targets compatible servers that aren't OpenAI)."""
    from openai import OpenAI

    if not settings.openai_api_key:
        raise RuntimeError("the openai provider needs OPENAI_API_KEY in .env")
    client = OpenAI(api_key=settings.openai_api_key, timeout=180, max_retries=2)
    kw = {} if temperature is None else {"temperature": temperature}
    if json_format:
        kw["response_format"] = {"type": "json_object"}
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    # max_tokens is deprecated on current OpenAI models (reasoning models
    # reject it outright); local servers behind openai_compat still expect
    # it, which is why the two providers differ here
    try:
        resp = client.chat.completions.create(
            model=model, max_completion_tokens=max_tokens, messages=messages, **kw)
    except Exception as exc:
        # reasoning models (gpt-5*, o*) 400 on any non-default temperature; a
        # Settings override tuned for another provider shouldn't brick the step
        if "temperature" in kw and getattr(exc, "status_code", None) == 400 \
                and "temperature" in str(exc).lower():
            log.warning("%s rejected temperature=%s; retrying without",
                        model, kw["temperature"])
            kw.pop("temperature")
            resp = client.chat.completions.create(
                model=model, max_completion_tokens=max_tokens, messages=messages,
                **kw)
        else:
            raise
    if resp.usage:
        _usage.set((resp.usage.prompt_tokens or 0, resp.usage.completion_tokens or 0))
    choice = resp.choices[0]
    content = choice.message.content or ""
    if not content.strip() and getattr(choice, "finish_reason", "") == "length":
        # the whole budget went to hidden reasoning tokens — deterministic, so
        # raising here (non-transient) beats the empty-response retry loop
        # re-billing the same reasoning three times
        raise RuntimeError(
            f"{model} spent the entire token budget on reasoning and returned "
            "no visible output — raise this function's max tokens under "
            "Settings → Advanced → Generation parameters")
    return content


def _anthropic(system: str, user: str, model: str, max_tokens: int,
               temperature: float | None) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key,
                                 timeout=180, max_retries=2)
    kw = {} if temperature is None else {"temperature": temperature}
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        **kw,
    )
    if getattr(resp, "usage", None):
        _usage.set((resp.usage.input_tokens or 0, resp.usage.output_tokens or 0))
    return "".join(b.text for b in resp.content if b.type == "text")


def _gemini(system: str, user: str, model: str, max_tokens: int,
            temperature: float | None) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)
    resp = client.models.generate_content(
        model=model,
        contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system, max_output_tokens=max_tokens,
            temperature=temperature,
        ),
    )
    usage = getattr(resp, "usage_metadata", None)
    if usage:
        _usage.set((getattr(usage, "prompt_token_count", 0) or 0,
                    getattr(usage, "candidates_token_count", 0) or 0))
    return resp.text or ""


def chunk_text(body: str, max_chars: int = 24000, overlap: int = 500) -> list[str]:
    """Split long transcripts on line boundaries with a little overlap."""
    if len(body) <= max_chars:
        return [body]
    lines = body.splitlines(keepends=True)
    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    for line in lines:
        if size + len(line) > max_chars and cur:
            chunks.append("".join(cur))
            # carry a small tail into the next chunk for context
            tail: list[str] = []
            tsize = 0
            for prev in reversed(cur):
                if tsize + len(prev) > overlap:
                    break
                tail.insert(0, prev)
                tsize += len(prev)
            cur = tail
            size = tsize
        cur.append(line)
        size += len(line)
    if cur:
        chunks.append("".join(cur))
    return chunks
