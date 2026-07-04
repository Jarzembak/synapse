"""Single entry point for all chat-LLM calls.

complete(function, ...) looks up the (provider, model) configured for that
pipeline function — Settings-table override first, config.py default second —
and dispatches to Ollama (OpenAI-compatible), Anthropic, or Gemini.

complete_json() is the structured variant: it retries and strips fences until
the output parses, since local models are fence-happy.
"""
from __future__ import annotations

import json
import re

from .config import FUNCTION_DEFAULTS, settings
from .settings_store import get_setting

MAX_TOKENS = 16384


def resolve_model(function: str) -> tuple[str, str]:
    override = get_setting(f"model.{function}")
    if override:
        return override["provider"], override["model"]
    d = FUNCTION_DEFAULTS[function]
    return d["provider"], d["model"]


def complete(
    function: str,
    system: str,
    user: str,
    *,
    max_tokens: int = MAX_TOKENS,
    provider: str | None = None,
    model: str | None = None,
) -> str:
    if provider is None or model is None:
        provider, model = resolve_model(function)

    if provider == "ollama":
        return _ollama(system, user, model, max_tokens)
    if provider == "anthropic":
        return _anthropic(system, user, model, max_tokens)
    if provider == "gemini":
        return _gemini(system, user, model, max_tokens)
    raise ValueError(f"unknown provider {provider!r} for function {function!r}")


def complete_json(function: str, system: str, user: str, *, retries: int = 2, **kw):
    system = system + "\nRespond with ONLY valid JSON. No prose, no code fences."
    last_err: Exception | None = None
    for _ in range(retries + 1):
        raw = complete(function, system, user, **kw)
        try:
            return json.loads(_strip_fences(raw))
        except json.JSONDecodeError as e:
            last_err = e
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


def _ollama(system: str, user: str, model: str, max_tokens: int) -> str:
    from openai import OpenAI

    client = OpenAI(base_url=f"{settings.ollama_base_url}/v1", api_key="ollama")
    resp = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return resp.choices[0].message.content or ""


def _anthropic(system: str, user: str, model: str, max_tokens: int) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


def _gemini(system: str, user: str, model: str, max_tokens: int) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)
    resp = client.models.generate_content(
        model=model,
        contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system, max_output_tokens=max_tokens
        ),
    )
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
