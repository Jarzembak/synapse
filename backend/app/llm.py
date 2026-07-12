"""Single entry point for all chat-LLM calls.

complete(function, ...) looks up the (provider, model) configured for that
pipeline function — Settings-table override first, config.py default second —
and dispatches to Ollama (OpenAI-compatible), Anthropic, or Gemini.

complete_json() is the structured variant: it retries and strips fences until
the output parses, since local models are fence-happy.
"""
from __future__ import annotations

import json
import logging
import re
import time
from contextvars import ContextVar

from .config import FUNCTION_DEFAULTS, settings
from .settings_store import get_setting

log = logging.getLogger("synapse.llm")

MAX_TOKENS = 16384
_usage: ContextVar[tuple[int, int]] = ContextVar("llm_usage", default=(0, 0))


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
    status = getattr(exc, "status_code", None)
    if status in {408, 409, 429} or isinstance(status, int) and status >= 500:
        return True
    return exc.__class__.__name__ in {
        "APITimeoutError", "APIConnectionError", "InternalServerError",
        "RateLimitError", "ServiceUnavailableError", "ConnectError", "ReadTimeout",
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
                    output = _ollama(system, user, model, max_tokens, temperature)
                elif provider == "anthropic":
                    output = _anthropic(system, user, model, max_tokens, temperature)
                elif provider == "gemini":
                    output = _gemini(system, user, model, max_tokens, temperature)
                else:
                    raise ValueError(
                        f"unknown provider {provider!r} for function {function!r}")
                if not output.strip():
                    raise RuntimeError(f"{provider}/{model} returned an empty response")
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
        _record_call(function, provider, model, started,
                     len(system) + len(user), output, error if not output else None)


def complete_json(function: str, system: str, user: str, *, retries: int = 2, **kw):
    system = system + "\nRespond with ONLY valid JSON. No prose, no code fences."
    last_err: Exception | None = None
    for _ in range(retries + 1):
        raw = complete(function, system, user, **kw)
        try:
            return json.loads(_strip_fences(raw))
        except json.JSONDecodeError as e:
            last_err = e
            log.warning("%s produced invalid JSON (retrying): %s", function, e)
            user = user + "\n\nYour previous reply was not valid JSON. JSON only."
    raise ValueError(f"{function}: model never produced valid JSON: {last_err}")


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if m:
        return m.group(1).strip()
    # some models prefix prose; grab from first { or [
    for opener in "{[":
        i = raw.find(opener)
        if i != -1:
            return raw[i:]
    return raw


def _ollama(system: str, user: str, model: str, max_tokens: int,
            temperature: float | None) -> str:
    from openai import OpenAI

    client = OpenAI(base_url=f"{settings.ollama_base_url}/v1", api_key="ollama",
                    timeout=180, max_retries=2)
    kw = {} if temperature is None else {"temperature": temperature}
    resp = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        **kw,
    )
    if resp.usage:
        _usage.set((resp.usage.prompt_tokens or 0, resp.usage.completion_tokens or 0))
    return resp.choices[0].message.content or ""


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
