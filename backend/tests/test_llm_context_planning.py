from __future__ import annotations

import pytest

from app import llm
from app.config import FUNCTION_DEFAULTS


LOCAL_CFG = {
    "num_ctx": 8_192,
    "keep_alive": "",
    "think": "off",
    "timeout_seconds": 120,
    "json_mode": True,
}


class SuccessfulResponse:
    status_code = 200

    @staticmethod
    def json():
        return {
            "message": {"content": '{"ok": true}'},
            "prompt_eval_count": 9,
            "eval_count": 3,
        }


def _allow_model(monkeypatch, *, native_context: int = 0):
    from app import local_model_safety

    monkeypatch.setattr(llm, "_known_native_context", lambda _model: native_context)
    monkeypatch.setattr(
        local_model_safety,
        "ensure_model_safe",
        lambda *args, **kwargs: {
            "native_context_tokens": native_context,
        },
    )


def test_repository_reduce_has_independent_local_model_setting(monkeypatch):
    values = {
        "repository.local_model": "map-explicit:latest",
        "repository.reduce_model": "reduce-explicit:latest",
    }
    monkeypatch.setattr(
        llm,
        "get_setting",
        lambda key, default=None: values.get(key, default),
    )

    assert llm._local_model("repository_map") == "map-explicit:latest"
    assert llm._local_model("repository_reduce") == "reduce-explicit:latest"
    assert FUNCTION_DEFAULTS["repository_reduce"] == {
        "provider": "ollama",
        "model": "qwen3.5:4b-q4_K_M",
    }


def test_repository_models_use_new_fallback_without_overwriting_explicit_setting(
    monkeypatch,
):
    monkeypatch.setattr(llm, "get_setting", lambda _key, default=None: default)
    assert llm._local_model("repository_map") == "qwen3.5:4b-q4_K_M"
    assert llm._local_model("repository_reduce") == "qwen3.5:4b-q4_K_M"

    monkeypatch.setattr(
        llm,
        "get_setting",
        lambda key, default=None: (
            "existing-user-choice:latest"
            if key == "repository.local_model"
            else default
        ),
    )
    assert llm._local_model("repository_map") == "existing-user-choice:latest"


def test_context_plan_grows_to_smallest_sensible_bucket():
    plan = llm._ollama_context_plan(
        "system",
        "x" * 30_000,
        2_000,
        configured_context=8_192,
    )

    assert plan["estimated_input_tokens"] >= 10_000
    assert plan["required_context"] > 12_288
    assert plan["requested_context"] == 16_384
    assert plan["effective_context"] == 16_384


def test_context_plan_caps_at_known_native_window():
    plan = llm._ollama_context_plan(
        "system",
        "x" * 90_000,
        4_000,
        configured_context=8_192,
        native_context=32_768,
    )

    assert plan["requested_context"] > plan["native_context"]
    assert plan["effective_context"] == 32_768
    assert plan["required_context"] > plan["effective_context"]


def test_context_plan_accounts_for_dense_non_ascii_text():
    plan = llm._ollama_context_plan(
        "system",
        "漢" * 12_000,
        1_024,
        configured_context=4_096,
    )

    assert plan["estimated_input_tokens"] >= 14_000
    assert plan["requested_context"] >= 16_384


def test_ollama_rejects_truncating_plan_before_transport(
    monkeypatch,
):
    from app import local_model_safety

    _allow_model(monkeypatch, native_context=12_288)
    monkeypatch.setattr(llm, "advanced", lambda _group: dict(LOCAL_CFG))
    sent = []
    admissions = []
    monkeypatch.setattr(
        local_model_safety,
        "ensure_model_safe",
        lambda *args, **kwargs: admissions.append((args, kwargs)),
    )
    monkeypatch.setattr(
        llm.httpx,
        "post",
        lambda *args, **kwargs: sent.append((args, kwargs)),
    )

    with pytest.raises(llm.ContextWindowError) as raised:
        llm._ollama(
            "system",
            "x" * 30_000,
            "small-native:latest",
            2_000,
            None,
            restricted=True,
            function="repository_reduce",
        )

    assert raised.value.required_context > raised.value.native_context
    assert raised.value.native_context == 12_288
    assert sent == []
    assert admissions == []
    diagnostics = llm.last_call_diagnostics()
    assert diagnostics["requested_context"] == 16_384
    assert diagnostics["effective_context"] == 12_288
    assert diagnostics["native_context"] == 12_288


def test_complete_exposes_call_plan_and_attempt_diagnostics(
    monkeypatch,
):
    _allow_model(monkeypatch, native_context=40_960)
    monkeypatch.setattr(llm, "advanced", lambda _group: dict(LOCAL_CFG))
    monkeypatch.setattr(llm.httpx, "post", lambda *args, **kwargs: SuccessfulResponse())
    monkeypatch.setattr(llm, "_record_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(llm, "_project_local_only", lambda: False)
    monkeypatch.setattr(llm, "get_setting", lambda _key, default=None: default)
    monkeypatch.setattr(llm, "resolve_params", lambda _function: (None, 512))

    output = llm.complete(
        "repository_reduce",
        "system",
        "small reduction",
        provider="ollama",
        model="reduce-model:latest",
        local_only=True,
        transient_attempts=1,
    )

    assert output == '{"ok": true}'
    diagnostics = llm.last_call_diagnostics()
    assert diagnostics["provider"] == "ollama"
    assert diagnostics["model"] == "qwen3.5:4b-q4_K_M"
    assert diagnostics["requested_context"] == 8_192
    assert diagnostics["effective_context"] == 8_192
    assert diagnostics["native_context"] == 40_960
    assert diagnostics["timeout_seconds"] == llm.REPOSITORY_TIMEOUT_SECONDS
    assert diagnostics["max_output_tokens"] == 512
    assert diagnostics["attempts"] == [{
        "attempt": 1,
        "status": "ok",
        "duration_seconds": diagnostics["attempts"][0]["duration_seconds"],
        "transient": False,
        "retry_delay_seconds": 0,
    }]


def test_transient_attempt_override_prevents_identical_retry(monkeypatch):
    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        raise llm.LLMHTTPError("timed out", 500)

    monkeypatch.setattr(llm, "_ollama", fail)
    monkeypatch.setattr(llm, "_record_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(llm, "_project_local_only", lambda: False)
    monkeypatch.setattr(llm, "resolve_params", lambda _function: (None, 512))

    with pytest.raises(llm.LLMHTTPError):
        llm.complete(
            "repository_reduce",
            "system",
            "batch",
            provider="ollama",
            model="reduce-model:latest",
            transient_attempts=1,
        )

    assert len(calls) == 1
    diagnostics = llm.last_call_diagnostics()
    assert len(diagnostics["attempts"]) == 1
    assert diagnostics["attempts"][0]["transient"] is True
    assert diagnostics["attempts"][0]["retry_delay_seconds"] == 0


def test_context_window_error_is_not_transient():
    error = llm.ContextWindowError(
        required_context=20_000,
        native_context=16_384,
        effective_context=16_384,
    )
    assert llm._is_transient(error) is False
