"""Single entry point for all chat-LLM calls.

complete(function, ...) looks up the (provider, model) configured for that
pipeline function — Settings-table override first, config.py default second —
and dispatches to a local provider (Ollama's native API, or any
OpenAI-compatible server such as LM Studio / llama.cpp / vLLM), Anthropic,
or Gemini.

complete_json() is the structured variant: local providers get native JSON
enforcement (Ollama `format`, OpenAI-compatible `response_format`), and the
parse retries and strips fences/prose until the output parses, since local
models are fence-happy.

Local-provider behavior (context window, keep-alive, thinking, timeout, JSON
mode) is tuned from Settings → Advanced → Local models (`advanced("local")`).
"""
from __future__ import annotations

import json
import logging
import re
import time
from contextvars import ContextVar

import httpx

from .config import FUNCTION_DEFAULTS, advanced, settings
from .settings_store import get_setting

log = logging.getLogger("synapse.llm")

MAX_TOKENS = 16384
LOCAL_PROVIDERS = {"ollama", "openai_compat"}
_usage: ContextVar[tuple[int, int]] = ContextVar("llm_usage", default=(0, 0))


class LLMHTTPError(RuntimeError):
    """HTTP failure from a provider, with the status preserved so the
    transient-retry logic can classify it."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class EmptyResponseError(RuntimeError):
    """Model returned no usable text — local models do this sporadically, so
    it is treated as transient and retried."""


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
        return override["provider"], override["model"]
    d = FUNCTION_DEFAULTS[function]
    return d["provider"], d["model"]


def resolve_params(function: str) -> tuple[float | None, int]:
    """Per-function generation params (Settings → Advanced, key params.<fn>)."""
    p = get_setting(f"params.{function}") or {}
    temperature = p.get("temperature")
    max_tokens = int(p.get("max_tokens") or MAX_TOKENS)
    return temperature, max_tokens


def complete(
    function: str,
    system: str,
    user: str,
    *,
    max_tokens: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    json_format: bool = False,
) -> str:
    if provider is None or model is None:
        provider, model = resolve_model(function)
    temperature, cfg_max = resolve_params(function)
    if max_tokens is None:
        max_tokens = cfg_max

    log.debug("completing %s via %s/%s (max_tokens=%s, temperature=%s)",
              function, provider, model, max_tokens, temperature)
    started = time.monotonic()
    output = ""
    error: Exception | None = None
    _usage.set((0, 0))
    attempts = max(1, min(int(get_setting("llm.transient_attempts", 3) or 3), 5))
    try:
        for attempt in range(attempts):
            try:
                if provider == "ollama":
                    output = _ollama(system, user, model, max_tokens, temperature,
                                     json_format)
                elif provider == "openai_compat":
                    output = _openai_compat(system, user, model, max_tokens,
                                            temperature, json_format)
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
                return output
            except Exception as exc:
                error = exc
                if attempt + 1 >= attempts or not _is_transient(exc):
                    raise
                delay = min(8, 2 ** attempt)
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


def _ollama(system: str, user: str, model: str, max_tokens: int,
            temperature: float | None, json_format: bool = False) -> str:
    """Ollama's native /api/chat. The native API (unlike its OpenAI-compat
    shim) accepts per-call options — critically num_ctx, without which long
    transcript chunks are silently truncated at the server's default window."""
    cfg = _local_cfg()
    options: dict = {"num_predict": max_tokens, "num_ctx": int(cfg["num_ctx"])}
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
    if think in ("on", "off"):
        payload["think"] = think == "on"
    if json_format and cfg.get("json_mode", True):
        payload["format"] = "json"
    response = httpx.post(
        f"{settings.ollama_base_url}/api/chat", json=payload,
        timeout=httpx.Timeout(float(cfg["timeout_seconds"]), connect=10),
    )
    if response.status_code >= 400:
        try:
            detail = response.json().get("error") or response.text
        except ValueError:
            detail = response.text
        raise LLMHTTPError(f"ollama returned {response.status_code}: {detail[:500]}",
                           response.status_code)
    data = response.json()
    _usage.set((data.get("prompt_eval_count") or 0, data.get("eval_count") or 0))
    # thinking arrives in message.thinking when supported; <think> tags in
    # content still show up from GGUF imports without a structured template
    return _strip_think((data.get("message") or {}).get("content") or "")


def _openai_compat(system: str, user: str, model: str, max_tokens: int,
                   temperature: float | None, json_format: bool = False) -> str:
    """Any OpenAI-compatible local server: LM Studio, llama.cpp, vLLM, …"""
    from openai import OpenAI

    cfg = _local_cfg()
    base = (settings.openai_compat_base_url or "").rstrip("/")
    if not base:
        raise RuntimeError(
            "the openai_compat provider needs OPENAI_COMPAT_BASE_URL in .env "
            "(e.g. http://host.docker.internal:1234/v1 for LM Studio)")
    client = OpenAI(base_url=base, api_key=settings.openai_compat_api_key or "local",
                    timeout=float(cfg["timeout_seconds"]), max_retries=0)
    kw = {} if temperature is None else {"temperature": temperature}
    if json_format and cfg.get("json_mode", True):
        kw["response_format"] = {"type": "json_object"}
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    try:
        resp = client.chat.completions.create(
            model=model, max_tokens=max_tokens, messages=messages, **kw)
    except Exception as exc:
        # not every server implements response_format; drop it and retry once
        if "response_format" in kw and getattr(exc, "status_code", None) == 400:
            log.warning("%s rejected response_format (%s); retrying without", base, exc)
            kw.pop("response_format")
            resp = client.chat.completions.create(
                model=model, max_tokens=max_tokens, messages=messages, **kw)
        else:
            raise
    if resp.usage:
        _usage.set((resp.usage.prompt_tokens or 0, resp.usage.completion_tokens or 0))
    return _strip_think(resp.choices[0].message.content or "")


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
