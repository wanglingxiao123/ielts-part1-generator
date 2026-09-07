"""The single creation point for the model client (design.md §2).

GPT-5.6 is Responses-API only, so this uses ``OpenAIResponsesModel``. ``OpenAIModel`` speaks
Chat Completions and would fail against every model in this family.

Two credential paths, both verified against the live endpoint on 2026-07-28:

``mantle`` (default, and what production must use)
    Pass ``bedrock_mantle_config={"region": ...}``. Strands resolves the ``/openai/v1`` base
    path for ``openai.gpt-5.*`` and mints a fresh bearer token per call from the ambient AWS
    credential chain. There is deliberately no token cache, expiry check or refresh logic in
    this file -- writing one would duplicate what the SDK already does per request and would
    then have its own staleness bug.

``bearer`` (development only)
    Uses a pre-minted ``AWS_BEARER_TOKEN_BEDROCK`` with an explicit ``base_url``. This exists
    because minting requires *valid SigV4 credentials*: on a machine whose SigV4 has expired,
    ``bedrock_mantle_config`` raises 401 ``invalid_api_key`` even though a previously minted
    bearer token still works. That is exactly the state of the development machine this was
    built on, and it is worth naming: a plain "401" from the mantle path means the AWS
    credentials expired, not that model access was revoked.

The two are mutually exclusive by construction -- ``client_args`` carrying ``api_key`` or
``base_url`` alongside ``bedrock_mantle_config`` raises ``ValueError`` inside Strands. This
module never sets both.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterator, List, Optional

from strands.models.openai_responses import OpenAIResponsesModel, resolve_bedrock_client_args

__all__ = [
    "AUTH_MODE", "MODEL_ID", "REGION", "build_model", "describe", "list_models",
    "resolve_model_id", "use_model",
]

# Same-family switch (-terra / -sol / -luna) without a rebuild.
MODEL_ID = os.environ.get("IELTS_MODEL_ID", "openai.gpt-5.6-terra")
# GPT-5.6 has no cross-region inference, so the Runtime must be deployed in this region.
REGION = os.environ.get("IELTS_MODEL_REGION", os.environ.get("AWS_REGION", "us-east-1"))
AUTH_MODE = os.environ.get("IELTS_MODEL_AUTH", "mantle")

SUPPORTED_REGIONS = ("us-east-1", "us-east-2")
_MANTLE_URL = "https://bedrock-mantle.%s.api.aws/openai/v1"
_MODEL_CACHE_TTL = float(os.environ.get("IELTS_MODEL_LIST_TTL", "300"))
_MODEL_CONTEXT: ContextVar[Optional[str]] = ContextVar("ielts_model_id", default=None)
_model_cache: Dict[str, Any] = {"at": 0.0, "models": None}


def _mantle_base_url(region: str) -> str:
    return _MANTLE_URL % region


def build_model(
    max_output_tokens: int,
    reasoning_effort: Optional[str] = None,
    model_id: Optional[str] = None,
) -> OpenAIResponsesModel:
    """Create a model client for one step.

    No ``temperature`` parameter. The design assumed per-step temperatures (high for generate,
    low for audit and revise), but the live endpoint rejects it outright:

        400 unsupported_parameter: 'temperature' is not supported with this model.

    ``reasoning.effort`` is the knob this family actually exposes, so step-level behaviour is
    tuned with that instead. Diversity across generations comes from the scenario prompt rather
    than from sampling temperature.
    """
    identifier = model_id or _MODEL_CONTEXT.get() or MODEL_ID
    params: Dict[str, Any] = {"max_output_tokens": max_output_tokens}
    if reasoning_effort:
        params["reasoning"] = {"effort": reasoning_effort}

    if AUTH_MODE == "bearer":
        token = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
        if not token:
            raise RuntimeError(
                "IELTS_MODEL_AUTH=bearer requires AWS_BEARER_TOKEN_BEDROCK in the environment"
            )
        return OpenAIResponsesModel(
            model_id=identifier,
            client_args={"api_key": token, "base_url": _mantle_base_url(REGION)},
            params=params,
        )

    # Mantle mints a token per call; never combine with client_args credentials.
    return OpenAIResponsesModel(
        model_id=identifier,
        bedrock_mantle_config={"region": REGION},
        params=params,
    )


def describe() -> Dict[str, Any]:
    """Configuration snapshot for logs and the batch summary event."""
    return {"model_id": MODEL_ID, "region": REGION, "auth_mode": AUTH_MODE}


@contextmanager
def use_model(model_id: str) -> Iterator[None]:
    """Bind one model to the current async request without affecting other requests."""
    token = _MODEL_CONTEXT.set(model_id)
    try:
        yield
    finally:
        _MODEL_CONTEXT.reset(token)


def _compatible(identifier: str) -> bool:
    return identifier.startswith("openai.gpt-")


def _remote_models() -> List[str]:
    if AUTH_MODE == "bearer":
        token = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
        if not token:
            raise RuntimeError(
                "IELTS_MODEL_AUTH=bearer requires AWS_BEARER_TOKEN_BEDROCK in the environment"
            )
        client_args = {"api_key": token, "base_url": _mantle_base_url(REGION)}
    else:
        client_args = resolve_bedrock_client_args(
            {"region": REGION}, model_id=MODEL_ID)
    from openai import OpenAI

    with OpenAI(**client_args, timeout=10.0) as client:
        rows = list(client.models.list().data)
    return sorted({
        str(getattr(row, "id", "") or "").strip()
        for row in rows if _compatible(str(getattr(row, "id", "") or ""))
    })


def list_models(force: bool = False) -> Dict[str, Any]:
    """Return the deployment-visible Responses-compatible GPT catalogue.

    A discovery outage does not disable the configured default model. It does, however, prevent
    arbitrary alternatives from being accepted until a trusted list is available again.
    """
    now = time.monotonic()
    cached = _model_cache.get("models")
    warning = None
    if force or cached is None or now - float(_model_cache.get("at") or 0) >= _MODEL_CACHE_TTL:
        try:
            cached = _remote_models()
            _model_cache.update({"at": now, "models": cached})
        except Exception as exc:  # noqa: BLE001 - fallback is part of the public contract
            warning = "模型列表暂时不可用，当前仅可使用部署默认模型。"
            cached = list(cached or [])
    identifiers = [identifier for identifier in (cached or []) if _compatible(identifier)]
    if MODEL_ID not in identifiers:
        identifiers.insert(0, MODEL_ID)
    models = [{"id": identifier, "label": identifier.removeprefix("openai.")}
              for identifier in identifiers]
    result: Dict[str, Any] = {"models": models, "default_model_id": MODEL_ID}
    if warning:
        result["warning"] = warning
    return result


def resolve_model_id(requested: Optional[str]) -> str:
    """Validate an explicit browser choice against the trusted deployment catalogue."""
    if not requested:
        return MODEL_ID
    identifier = str(requested).strip()
    allowed = {row["id"] for row in list_models().get("models", [])}
    if identifier not in allowed:
        raise ValueError("model %r is not available in this deployment" % identifier)
    return identifier


def assert_region_supported(region: Optional[str] = None) -> None:
    """Fail fast when deployed outside the model's regions.

    Called by the deploy script rather than at import time: an import-time check would make the
    container refuse to start (and fail ``/ping``) over a configuration problem that deserves a
    clear deployment-time error instead.
    """
    resolved = region or REGION
    if resolved not in SUPPORTED_REGIONS:
        raise ValueError(
            "%s is only available in %s and has no cross-region inference; "
            "Runtime and model must share a region (got %r)"
            % (MODEL_ID, " / ".join(SUPPORTED_REGIONS), resolved)
        )
