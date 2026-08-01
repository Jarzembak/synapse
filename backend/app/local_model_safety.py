"""Ollama model inventory, annotations, compatibility, and resource safeguards.

The Ollama registry name is not a resource contract.  This module combines the
installed model's immutable digest and metadata with the resources visible to
the execution environment.  Resource estimates are intentionally conservative:
they are used to identify clearly impossible or disruptive loads, not to promise
that a model will achieve a particular throughput.

Annotations are keyed by model name so a user's friendly label survives a model
update.  Benchmarks and unsafe-resource acknowledgements are keyed by digest so
new weights cannot inherit an old compatibility result or override.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from threading import Lock
from urllib.parse import urlsplit

import httpx
import psutil

from .config import advanced, settings
from .settings_store import get_setting, set_setting

PROFILE_KEY = "ollama.model_profiles"
PROFILE_VERSION = 1
BENCHMARK_PROMPT_VERSION = 1
INVENTORY_TTL_SECONDS = 15.0
GIB = 1024 ** 3

_inventory_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = Lock()


class LocalModelSafetyError(RuntimeError):
    """A stable, non-transient model-safety rejection.

    ``http_status`` is deliberately not named ``status_code``: the LLM retry
    classifier treats HTTP 409 as transient, while a safety rejection must not
    retry an expensive model load.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int,
        model: str,
        digest: str = "",
        assessment: dict | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.model = model
        self.digest = digest
        self.assessment = assessment

    def detail(self) -> dict:
        out = {
            "code": self.code,
            "message": str(self),
            "model": self.model,
        }
        if self.digest:
            out["digest"] = self.digest
        if self.assessment is not None:
            out["assessment"] = self.assessment
        return out


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _base_url() -> str:
    return settings.ollama_base_url.rstrip("/")


def _endpoint_is_local(base_url: str | None = None) -> bool:
    """Return whether API-container resource data describes the Ollama host."""
    try:
        parsed = urlsplit(base_url or _base_url())
        host = (parsed.hostname or "").rstrip(".").casefold()
        if parsed.scheme not in {"http", "https"}:
            return False
        if host in {"localhost", "ollama", "host.docker.internal"}:
            return True
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        return False


def _profiles() -> dict:
    raw = get_setting(PROFILE_KEY) or {}
    if not isinstance(raw, dict) or raw.get("version") != PROFILE_VERSION:
        return {
            "version": PROFILE_VERSION,
            "annotations": {},
            "acknowledgements": {},
            "benchmarks": {},
        }
    return {
        "version": PROFILE_VERSION,
        "annotations": dict(raw.get("annotations") or {}),
        "acknowledgements": dict(raw.get("acknowledgements") or {}),
        "benchmarks": dict(raw.get("benchmarks") or {}),
    }


def _save_profiles(value: dict) -> None:
    set_setting(PROFILE_KEY, value)


def _annotation_for(name: str) -> dict:
    value = _profiles()["annotations"].get(name, {})
    return {
        "label": str(value.get("label") or ""),
        "notes": str(value.get("notes") or ""),
        "labels": [str(item) for item in value.get("labels", []) if str(item)],
    }


def save_annotation(model: str, *, label: str, notes: str, labels: list[str]) -> dict:
    model = model.strip()
    if not model:
        raise ValueError("model is required")
    label = label.strip()
    notes = notes.strip()
    if len(label) > 80:
        raise ValueError("label must be 80 characters or fewer")
    if len(notes) > 1000:
        raise ValueError("notes must be 1000 characters or fewer")
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in labels:
        value = str(item).strip()
        if not value:
            continue
        if len(value) > 32:
            raise ValueError("each model label must be 32 characters or fewer")
        folded = value.casefold()
        if folded not in seen:
            cleaned.append(value)
            seen.add(folded)
    if len(cleaned) > 12:
        raise ValueError("a model can have at most 12 labels")
    profiles = _profiles()
    if label or notes or cleaned:
        profiles["annotations"][model] = {
            "label": label,
            "notes": notes,
            "labels": cleaned,
        }
    else:
        profiles["annotations"].pop(model, None)
    _save_profiles(profiles)
    return _annotation_for(model)


def clear_acknowledgement(digest: str) -> bool:
    digest = digest.strip()
    profiles = _profiles()
    existed = digest in profiles["acknowledgements"]
    profiles["acknowledgements"].pop(digest, None)
    if existed:
        _save_profiles(profiles)
    return existed


def _gpu_memory() -> tuple[int, int]:
    """Return total and currently free NVIDIA VRAM visible to this container."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return 0, 0
    try:
        proc = subprocess.run(
            [
                exe,
                "--query-gpu=memory.total,memory.used",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if proc.returncode != 0:
            return 0, 0
        total = used = 0
        for line in proc.stdout.strip().splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 2:
                continue
            total += max(0, int(float(parts[0]))) * 1_048_576
            used += max(0, int(float(parts[1]))) * 1_048_576
        return total, max(0, total - used)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0, 0


def runtime_resources() -> dict:
    local = _endpoint_is_local()
    if not local:
        return {
            "available": False,
            "reason": "remote Ollama resources are not visible to Synapse",
            "ram_total_bytes": 0,
            "ram_available_bytes": 0,
            "vram_total_bytes": 0,
            "vram_free_bytes": 0,
        }
    vm = psutil.virtual_memory()
    vram_total, vram_free = _gpu_memory()
    return {
        "available": True,
        "reason": "",
        "ram_total_bytes": int(vm.total),
        "ram_available_bytes": int(vm.available),
        "vram_total_bytes": vram_total,
        "vram_free_bytes": vram_free,
    }


def _runtime_fingerprint(resources: dict) -> str:
    data = {
        "base": _base_url(),
        "ram_total_bytes": resources.get("ram_total_bytes", 0),
        "vram_total_bytes": resources.get("vram_total_bytes", 0),
    }
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _context_length(model: dict) -> int:
    details = model.get("details") or {}
    candidates = [
        model.get("native_context_tokens"),
        details.get("context_length"),
    ]
    info = model.get("model_info") or {}
    candidates.extend(
        value for key, value in info.items()
        if key.endswith(".context_length")
    )
    for value in candidates:
        try:
            number = int(value)
            if number > 0:
                return number
        except (TypeError, ValueError):
            continue
    return 0


def _model_number(info: dict, suffix: str) -> int:
    for key, value in info.items():
        if key.endswith(suffix):
            try:
                number = int(value)
                if number > 0:
                    return number
            except (TypeError, ValueError):
                pass
    return 0


def _estimated_context_bytes(model: dict, requested_context: int) -> int:
    """Estimate an f16 KV cache, with a conservative metadata-free fallback."""
    info = model.get("model_info") or {}
    blocks = _model_number(info, ".block_count")
    kv_heads = _model_number(info, ".attention.head_count_kv")
    key_length = _model_number(info, ".attention.key_length")
    value_length = _model_number(info, ".attention.value_length")
    if blocks and kv_heads and key_length and value_length:
        return requested_context * blocks * kv_heads * (
            key_length + value_length
        ) * 2
    size = int(model.get("size_bytes") or 0)
    # Architecture-free estimates are not exact.  Scale from 8% of weights per
    # 16k tokens and retain a 256 MiB minimum so small models are not treated as
    # free.  Blocking remains limited to clearly impossible configurations.
    return max(
        256 * 1024 ** 2,
        math.ceil(size * 0.08 * max(1.0, requested_context / 16_384)),
    )


def resource_assessment(
    model: dict,
    *,
    requested_context: int,
    resources: dict | None = None,
) -> dict:
    resources = resources or runtime_resources()
    digest = str(model.get("digest") or "")
    size = max(0, int(model.get("size_bytes") or 0))
    weight = math.ceil(size * 1.10)
    context = _estimated_context_bytes(model, requested_context)
    working = max(512 * 1024 ** 2, math.ceil(size * 0.05))
    total = weight + context + working
    if not resources.get("available"):
        return {
            "tier": "unavailable",
            "message": resources.get("reason") or "resource information is unavailable",
            "requested_context_tokens": requested_context,
            "estimated_weight_bytes": weight,
            "estimated_context_bytes": context,
            "estimated_total_bytes": total,
            "acknowledged": False,
        }

    ram_total = int(resources.get("ram_total_bytes") or 0)
    ram_available = int(resources.get("ram_available_bytes") or 0)
    vram_total = int(resources.get("vram_total_bytes") or 0)
    vram_free = int(resources.get("vram_free_bytes") or 0)
    ram_reserve = min(ram_available, max(2 * GIB, math.ceil(ram_total * 0.15)))
    vram_reserve = min(vram_free, max(1 * GIB, math.ceil(vram_total * 0.12))) \
        if vram_total else 0
    usable_ram = max(0, ram_available - ram_reserve)
    usable_vram = max(0, vram_free - vram_reserve)
    usable_combined = usable_ram + usable_vram

    if size <= 0:
        tier = "unavailable"
        message = "the model size is unavailable; resource fit cannot be verified"
    elif total > usable_combined:
        tier = "blocked"
        message = (
            "estimated model and context memory exceed the currently safe "
            "combined RAM and VRAM budget"
        )
    elif total > usable_combined * 0.75 or usable_combined - total < 2 * GIB:
        tier = "resource_intensive"
        message = (
            "the model should fit only by consuming most currently available "
            "resources; close other GPU and memory intensive applications"
        )
    elif usable_vram and total <= usable_vram * 0.85:
        tier = "recommended"
        message = "the model is expected to fit in currently free VRAM"
    else:
        tier = "hybrid"
        message = (
            "the model should fit, but Ollama may use system RAM and CPU offload"
            if vram_total
            else "the model should fit in system RAM and will run on CPU"
        )

    profiles = _profiles()
    acknowledgement = profiles["acknowledgements"].get(digest, {}) if digest else {}
    acknowledged = bool(
        acknowledgement
        and acknowledgement.get("runtime_fingerprint")
        == _runtime_fingerprint(resources)
        and int(acknowledgement.get("context_tokens") or 0) >= requested_context
    )
    return {
        "tier": tier,
        "message": message,
        "requested_context_tokens": requested_context,
        "estimated_weight_bytes": weight,
        "estimated_context_bytes": context,
        "estimated_total_bytes": total,
        "acknowledged": acknowledged,
    }


def _model_aliases(value: str) -> set[str]:
    """Return equivalent Ollama names without mistaking a registry port for a tag."""
    value = value.strip()
    if not value:
        return set()
    aliases = {value}
    final_component = value.rsplit("/", 1)[-1]
    if ":" not in final_component:
        aliases.add(value + ":latest")
    elif final_component.endswith(":latest"):
        aliases.add(value[:-len(":latest")])
    return aliases


def _canonical_model(rows: list[dict], requested: str) -> dict | None:
    requested_names = _model_aliases(requested)
    for row in rows:
        names: set[str] = set()
        for value in (row.get("name"), row.get("model")):
            names.update(_model_aliases(str(value or "")))
        if requested_names & names:
            return row
    return None


def _resident_capacity(residents: list[dict] | dict | None) -> dict[str, object]:
    """Describe memory Ollama can reclaim while scheduling another model."""
    if isinstance(residents, dict):
        residents = [residents]
    rows = residents or []
    ram_bytes = 0
    vram_bytes = 0
    models: list[str] = []
    for resident in rows:
        resident_size = max(0, int(resident.get("size") or 0))
        resident_vram = max(0, int(resident.get("size_vram") or 0))
        if resident_size:
            resident_vram = min(resident_vram, resident_size)
        resident_ram = max(0, resident_size - resident_vram)
        ram_bytes += resident_ram
        vram_bytes += resident_vram
        name = str(resident.get("name") or resident.get("model") or "").strip()
        if name and name not in models:
            models.append(name)
    return {
        "models": models,
        "ram_bytes": ram_bytes,
        "vram_bytes": vram_bytes,
    }


def _resources_with_resident_capacity(
    resources: dict,
    residents: list[dict] | dict | None,
) -> dict:
    """Restore allocations that remain under Ollama scheduler control.

    Runtime free-memory counters already include pressure from external
    applications. Only allocations reported by Ollama's ``/api/ps`` are added
    back, because Ollama can serialize a model transition and evict those
    runners. Physical totals cap the restored values.
    """
    if not resources.get("available") or not residents:
        return resources
    capacity = _resident_capacity(residents)
    adjusted = dict(resources)
    adjusted["vram_free_bytes"] = min(
        int(adjusted.get("vram_total_bytes") or 0),
        int(adjusted.get("vram_free_bytes") or 0)
        + int(capacity["vram_bytes"]),
    )
    adjusted["ram_available_bytes"] = min(
        int(adjusted.get("ram_total_bytes") or 0),
        int(adjusted.get("ram_available_bytes") or 0)
        + int(capacity["ram_bytes"]),
    )
    return adjusted


def _merge_model(tag: dict, shown: dict | None = None) -> dict:
    shown = shown or {}
    details = {**(tag.get("details") or {}), **(shown.get("details") or {})}
    capabilities = shown.get("capabilities") or tag.get("capabilities") or []
    info = shown.get("model_info") or tag.get("model_info") or {}
    row = {
        "name": tag.get("name") or tag.get("model") or shown.get("name") or "",
        "model": tag.get("model") or tag.get("name") or shown.get("model") or "",
        "digest": tag.get("digest") or shown.get("digest") or "",
        "size_bytes": int(tag.get("size") or shown.get("size") or 0),
        "modified_at": tag.get("modified_at") or shown.get("modified_at") or "",
        "details": {
            "family": details.get("family") or "",
            "families": details.get("families") or [],
            "parameter_size": details.get("parameter_size") or "",
            "quantization_level": details.get("quantization_level") or "",
        },
        "capabilities": sorted({str(value) for value in capabilities if value}),
        "model_info": info,
    }
    row["native_context_tokens"] = _context_length({
        **row,
        "details": {**details, **row["details"]},
    })
    return row


def _fetch_inventory(*, refresh: bool = False) -> dict:
    base = _base_url()
    now = time.monotonic()
    with _cache_lock:
        cached = _inventory_cache.get(base)
        if cached and not refresh and now - cached[0] < INVENTORY_TTL_SECONDS:
            return cached[1]
    try:
        with httpx.Client(trust_env=False, timeout=httpx.Timeout(3, connect=2)) as client:
            response = client.get(f"{base}/api/tags")
            response.raise_for_status()
            tags = response.json().get("models", [])
            running_models: list[dict] = []
            try:
                running_response = client.get(f"{base}/api/ps")
                running_response.raise_for_status()
                running_models = list(running_response.json().get("models", []))
            except Exception:
                # Older Ollama servers may not expose /api/ps. Installed-model
                # safety remains usable; residency is optional observability.
                running_models = []
            rows: list[dict] = []
            for tag in tags:
                shown = None
                if not tag.get("capabilities") or not _context_length(tag):
                    try:
                        detail_response = client.post(
                            f"{base}/api/show",
                            json={"model": tag.get("name") or tag.get("model")},
                        )
                        detail_response.raise_for_status()
                        shown = detail_response.json()
                    except Exception:
                        shown = None
                rows.append(_merge_model(tag, shown))
        result = {
            "configured": True,
            "ok": True,
            "local": _endpoint_is_local(base),
            "detail": "",
            "models": sorted(rows, key=lambda item: item["name"].casefold()),
            "running_models": running_models,
        }
    except Exception as exc:
        result = {
            "configured": True,
            "ok": False,
            "local": _endpoint_is_local(base),
            "detail": str(exc)[:300],
            "models": [],
            "running_models": [],
        }
    with _cache_lock:
        _inventory_cache[base] = (now, result)
    return result


def clear_inventory_cache() -> None:
    with _cache_lock:
        _inventory_cache.clear()


def inspect_model(model: str, *, refresh: bool = False) -> tuple[dict, dict]:
    inventory = _fetch_inventory(refresh=refresh)
    row = _canonical_model(inventory["models"], model) if inventory["ok"] else None
    return inventory, row


def model_catalog(*, refresh: bool = False) -> dict:
    inventory = _fetch_inventory(refresh=refresh)
    resources = runtime_resources()
    result = {**inventory, "resources": resources}
    general_context = max(1024, int(advanced("local")["num_ctx"]))
    profiles = _profiles()
    running = inventory.get("running_models") or []
    assessment_resources = _resources_with_resident_capacity(resources, running)
    result["ollama_reclaimable_capacity"] = _resident_capacity(running)
    rows = []
    for model in inventory["models"]:
        digest = model.get("digest") or ""
        resident = _canonical_model(running, model["name"])
        row = {
            key: value for key, value in model.items()
            if key != "model_info"
        }
        row["annotation"] = _annotation_for(model["name"])
        row["benchmark"] = profiles["benchmarks"].get(digest) if digest else None
        row["assessment"] = resource_assessment(
            model,
            requested_context=general_context,
            resources=assessment_resources,
        )
        row["repository_assessment"] = resource_assessment(
            model,
            requested_context=max(general_context, 32_768),
            resources=assessment_resources,
        )
        row["restricted_assessment"] = resource_assessment(
            model,
            requested_context=max(general_context, 65_536),
            resources=assessment_resources,
        )
        if resident:
            resident_size = int(resident.get("size") or 0)
            resident_vram = int(resident.get("size_vram") or 0)
            if resident_vram <= 0:
                processor = "cpu"
            elif resident_size and resident_vram >= resident_size * 0.95:
                processor = "gpu"
            else:
                processor = "hybrid"
            row["residency"] = {
                "loaded": True,
                "size_bytes": resident_size,
                "size_vram_bytes": resident_vram,
                "context_length": int(resident.get("context_length") or 0),
                "expires_at": str(resident.get("expires_at") or ""),
                "processor": processor,
            }
        else:
            row["residency"] = {
                "loaded": False,
                "size_bytes": 0,
                "size_vram_bytes": 0,
                "context_length": 0,
                "expires_at": "",
                "processor": "",
            }
        rows.append(row)
    result["models"] = rows
    return result


def unload_model(model: str) -> dict:
    """Release one installed model from Ollama-managed RAM and VRAM."""
    inventory, row = inspect_model(model, refresh=True)
    if not inventory["ok"]:
        raise ValueError(
            inventory["detail"] or "Ollama is unreachable; the model cannot be unloaded"
        )
    if row is None:
        raise ValueError("the model is not installed")
    name = str(row.get("name") or model).strip()
    try:
        with httpx.Client(
            trust_env=False,
            timeout=httpx.Timeout(30, connect=5),
        ) as client:
            response = client.post(
                f"{_base_url()}/api/generate",
                json={"model": name, "keep_alive": 0},
            )
            response.raise_for_status()
    except Exception as exc:
        raise ValueError(f"Ollama could not unload {name!r}: {exc}") from exc
    clear_inventory_cache()
    return {"ok": True, "model": name}


def acknowledge_blocked_model(
    model: str,
    *,
    digest: str,
    confirmation: str,
    reason: str,
) -> dict:
    if confirmation != model:
        raise ValueError("confirmation must exactly match the model name")
    reason = reason.strip()
    if len(reason) < 10:
        raise ValueError(
            "a blocked-model override reason must be at least 10 characters")
    if len(reason) > 500:
        raise ValueError("reason must be 500 characters or fewer")
    inventory, row = inspect_model(model, refresh=True)
    if not inventory["ok"]:
        raise ValueError("Ollama is unreachable; the model cannot be acknowledged")
    if row is None:
        raise ValueError("the model is not installed")
    actual_digest = str(row.get("digest") or "")
    if not actual_digest or digest != actual_digest:
        raise ValueError("the model digest changed; refresh the model catalog")
    running = inventory.get("running_models") or []
    resources = _resources_with_resident_capacity(runtime_resources(), running)
    if not resources["available"]:
        raise ValueError(
            "remote Ollama resource overrides require a configured remote resource profile"
        )
    assessment = resource_assessment(
        row,
        requested_context=max(65_536, int(advanced("local")["num_ctx"])),
        resources=resources,
    )
    if assessment["tier"] != "blocked":
        raise ValueError("only a currently blocked model needs this override")
    profiles = _profiles()
    profiles["acknowledgements"][actual_digest] = {
        "model": row["name"],
        "reason": reason,
        "acknowledged_at": _utcnow(),
        "runtime_fingerprint": _runtime_fingerprint(resources),
        "context_tokens": assessment["requested_context_tokens"],
    }
    _save_profiles(profiles)
    return resource_assessment(
        row,
        requested_context=assessment["requested_context_tokens"],
        resources=resources,
    )


def _validated_model_metadata(
    row: dict,
    *,
    role: str,
    requested_context: int,
) -> tuple[set[str], int, str]:
    """Validate one fresh installed-model row for its requested execution."""
    capabilities = set(row.get("capabilities") or [])
    required = "embedding" if role == "embedding" else "completion"
    if capabilities and required not in capabilities:
        raise LocalModelSafetyError(
            "ollama_capability_mismatch",
            f"Ollama model {row['name']!r} does not advertise the {required!r} capability",
            http_status=422,
            model=row["name"],
            digest=row.get("digest") or "",
        )
    native_context = int(row.get("native_context_tokens") or 0)
    warning = ""
    if native_context and native_context < requested_context:
        warning = (
            f"the model advertises {native_context:,} context tokens, below "
            f"the requested {requested_context:,}"
        )
    return capabilities, native_context, warning


def ensure_model_safe(
    model: str,
    *,
    role: str,
    requested_context: int,
    refresh: bool = False,
) -> dict:
    """Validate an installed model and reject an unacknowledged impossible load.

    An unreachable server is allowed through so the actual Ollama request can
    report its normal connectivity error.  Remote servers still receive
    capability checks when reachable, but resource status is explicitly
    unavailable because local Docker statistics do not describe that host.
    """
    inventory, row = inspect_model(model, refresh=refresh)
    if not inventory["ok"]:
        return {
            "available": False,
            "warning": "Ollama could not be inspected before use",
            "assessment": {
                "tier": "unavailable",
                "message": inventory["detail"] or "Ollama is unreachable",
                "acknowledged": False,
            },
        }
    if row is None:
        raise LocalModelSafetyError(
            "ollama_model_not_installed",
            f"Ollama model {model!r} is not installed",
            http_status=422,
            model=model,
        )
    capabilities, native_context, warning = _validated_model_metadata(
        row,
        role=role,
        requested_context=requested_context,
    )
    resources = runtime_resources()
    assessment = resource_assessment(
        row,
        requested_context=requested_context,
        resources=resources,
    )
    resident_transition: dict[str, object] | None = None
    if assessment["tier"] == "blocked" and inventory.get("local"):
        # A cached inventory can predate the model that just finished a call.
        # Refresh only after an apparent rejection, then assess the capacity
        # available after Ollama replaces any managed resident runner. This
        # does not forgive pressure from external processes and does not
        # forcibly unload a model another request may still be using.
        if refresh:
            refreshed_inventory, refreshed_row = inventory, row
        else:
            refreshed_inventory, refreshed_row = inspect_model(model, refresh=True)
        if refreshed_inventory.get("ok") and refreshed_row is None:
            raise LocalModelSafetyError(
                "ollama_model_not_installed",
                f"Ollama model {model!r} is not installed",
                http_status=422,
                model=model,
            )
        if refreshed_inventory.get("ok") and refreshed_row is not None:
            inventory, row = refreshed_inventory, refreshed_row
            capabilities, native_context, warning = _validated_model_metadata(
                row,
                role=role,
                requested_context=requested_context,
            )
            running = inventory.get("running_models") or []
            capacity = _resident_capacity(running)
            resources = _resources_with_resident_capacity(
                runtime_resources(), running)
            assessment = resource_assessment(
                row,
                requested_context=requested_context,
                resources=resources,
            )
            other_models: list[str] = []
            for resident_row in running:
                if _canonical_model([resident_row], row["name"]) is not None:
                    continue
                resident_name = str(
                    resident_row.get("name")
                    or resident_row.get("model")
                    or ""
                ).strip()
                if resident_name and resident_name not in other_models:
                    other_models.append(resident_name)
            resident_transition = {
                "required": bool(other_models),
                "resident_models": list(capacity["models"]),
                "replaced_models": other_models,
                "reclaimable_ram_bytes": int(capacity["ram_bytes"]),
                "reclaimable_vram_bytes": int(capacity["vram_bytes"]),
            }
            assessment = {
                **assessment,
                "resident_transition": resident_transition,
            }
    if assessment["tier"] == "blocked" and not assessment["acknowledged"]:
        raise LocalModelSafetyError(
            "ollama_model_blocked",
            assessment["message"],
            http_status=409,
            model=row["name"],
            digest=row.get("digest") or "",
            assessment=assessment,
        )
    return {
        "available": True,
        "model": row["name"],
        "digest": row.get("digest") or "",
        "capabilities": sorted(capabilities),
        "native_context_tokens": native_context,
        "warning": warning,
        "assessment": assessment,
        "resident_transition": resident_transition,
    }


def record_benchmark(model: str, digest: str, result: dict) -> dict:
    profiles = _profiles()
    stored = {
        "prompt_version": BENCHMARK_PROMPT_VERSION,
        "completion": bool(result.get("completion")),
        "structured_json": bool(result.get("structured_json")),
        "checked_at": _utcnow(),
        "error": str(result.get("error") or "")[:300],
        "model": model,
    }
    profiles["benchmarks"][digest] = stored
    _save_profiles(profiles)
    return stored


def run_compatibility_benchmark(model: str) -> dict:
    """Run one bounded completion/JSON probe and unload the model afterwards."""
    inspected = ensure_model_safe(
        model,
        role="completion",
        requested_context=2_048,
        refresh=True,
    )
    digest = inspected.get("digest") or ""
    payload = {
        "model": inspected.get("model") or model,
        "stream": False,
        "format": "json",
        "keep_alive": 0,
        "options": {
            "num_ctx": 2_048,
            "num_predict": 48,
            "temperature": 0,
        },
        "messages": [
            {
                "role": "system",
                "content": "Return only the requested JSON object.",
            },
            {
                "role": "user",
                "content": 'Return exactly {"synapse_compatibility":true}.',
            },
        ],
    }
    result = {"completion": False, "structured_json": False, "error": ""}
    try:
        with httpx.Client(trust_env=False) as client:
            response = client.post(
                f"{_base_url()}/api/chat",
                json=payload,
                timeout=httpx.Timeout(90, connect=10),
            )
            response.raise_for_status()
            content = str((response.json().get("message") or {}).get("content") or "")
            result["completion"] = bool(content.strip())
            parsed = json.loads(content)
            result["structured_json"] = (
                isinstance(parsed, dict)
                and parsed.get("synapse_compatibility") is True
            )
            if not result["structured_json"]:
                result["error"] = "model did not return the requested JSON object"
    except Exception as exc:
        result["error"] = str(exc)[:300]
    stored = record_benchmark(
        inspected.get("model") or model,
        digest,
        result,
    )
    if not stored["completion"] or not stored["structured_json"]:
        raise RuntimeError(stored["error"] or "model compatibility benchmark failed")
    return stored
