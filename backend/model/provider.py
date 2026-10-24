"""Model adapters for overseas Bedrock, independent of the application region.

Kimi K2.5 uses Mantle Chat Completions; K3 uses Bedrock Runtime Chat Completions.
Explicitly enabled GPT-5.6 models use Responses.
Strands owns profile token minting and refresh. Secret auth reads once per new model.
Profile auth never uses ambient credentials.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from contextlib import closing, contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterator, List, Optional

import boto3
import httpx
from botocore.config import Config
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import CredentialProvider
from strands.models.openai import OpenAIModel
from strands.models.openai_responses import OpenAIResponsesModel
from strands.types.exceptions import ContextWindowOverflowException, ModelThrottledException

__all__ = [
    "AUTH_MODE", "MODEL_ID", "REGION", "build_model", "describe", "list_models",
    "resolve_model_id", "use_model", "current_model_id",
]

MODEL_ID = os.environ.get("IELTS_MODEL_ID", "moonshotai.kimi-k2.5")
REGION = os.environ.get("IELTS_MODEL_REGION", "us-east-1")
AUTH_MODE = os.environ.get("IELTS_MODEL_AUTH", "bearer")
SUPPORTED_REGIONS = ("us-east-1", "us-east-2", "us-west-2")
_IMPLEMENTED_MODELS = (
    "moonshotai.kimi-k2.5",
    "us.moonshotai.kimi-k3",
    "openai.gpt-5.6-luna", "openai.gpt-5.6-sol", "openai.gpt-5.6-terra",
)
_MODEL_CACHE_TTL = float(os.environ.get("IELTS_MODEL_LIST_TTL", "300"))
_MODEL_CONTEXT: ContextVar[Optional[str]] = ContextVar("ielts_model_id", default=None)
_model_cache: Dict[str, Any] = {"at": 0.0, "models": None}
_SECRET_LOOKUP: ContextVar[bool] = ContextVar("ielts_secret_lookup", default=False)


class _SecretLookupLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Botocore DEBUG includes response bodies, transport/retry exceptions
        # and the runtime session token in the SigV4 canonical request.
        return not _SECRET_LOOKUP.get()


_secret_log_filter = _SecretLookupLogFilter()


@contextmanager
def _k3_credential_logs() -> Iterator[None]:
    for name in ("botocore.auth", "botocore.endpoint", "botocore.httpsession",
                 "botocore.parsers", "botocore.retryhandler", "botocore.retries.standard",
                 "openai._base_client"):
        logging.getLogger(name).addFilter(_secret_log_filter)
    context = _SECRET_LOOKUP.set(True)
    try:
        yield
    finally:
        _SECRET_LOOKUP.reset(context)


class _K3ProfileAuth(httpx.Auth):
    def __init__(self, credentials_provider, region: str):
        self._credentials_provider = credentials_provider
        self._region = region

    def _sign(self, request: httpx.Request) -> None:
        with _k3_credential_logs():
            if request.url.scheme != "https" or request.url.host != (
                f"bedrock-runtime.{self._region}.amazonaws.com"
            ):
                raise RuntimeError("Kimi K3 profile auth requires the configured Bedrock Runtime endpoint")
            credentials = self._credentials_provider.load().get_frozen_credentials()
            headers = dict(request.headers)
            headers.pop("authorization", None)  # Replace the OpenAI SDK's placeholder.
            signed = AWSRequest(
                method=request.method, url=str(request.url), data=request.content, headers=headers,
            )
            SigV4Auth(credentials, "bedrock", self._region).add_auth(signed)
            request.headers.update(dict(signed.headers))

    async def async_auth_flow(self, request):
        await request.aread()
        try:
            await asyncio.to_thread(self._sign, request)
        except Exception:
            raise RuntimeError("Unable to sign Kimi K3 profile request") from None
        yield request


class _K3OpenAIModel(OpenAIModel):
    def __init__(self, *, credentials_provider=None, model_region: str, **kwargs):
        self._credentials_provider = credentials_provider
        self._model_region = model_region
        super().__init__(**kwargs)

    def _resolve_client_args(self) -> dict:
        args = dict(super()._resolve_client_args())
        if self._credentials_provider is not None:
            # Strands opens/closes one OpenAI client per request. The OpenAI SDK
            # closes this HTTPX client with it, so no pool crosses event loops.
            args["api_key"] = "sigv4"
            args["http_client"] = httpx.AsyncClient(
                auth=_K3ProfileAuth(self._credentials_provider, self._model_region),
                timeout=args["timeout"], follow_redirects=False,
            )
        return args

    async def stream(self, *args, **kwargs):
        with _k3_credential_logs():
            try:
                async for event in super().stream(*args, **kwargs):
                    yield event
            except (ContextWindowOverflowException, ModelThrottledException) as exc:
                raise type(exc)(_k3_failure_message(exc)) from None
            except Exception as exc:
                raise RuntimeError(_k3_failure_message(exc)) from None


def _k3_failure_message(exc: Exception) -> str:
    details = type(exc).__name__
    status = getattr(exc, "status_code", None)
    if status is None and exc.__cause__ is not None:
        status = getattr(exc.__cause__, "status_code", None)
    if type(status) is int and 100 <= status <= 599:
        details += f", status_code={status}"
    return f"Kimi K3 Chat Completions request failed ({details})"


def _build_k3_model(max_output_tokens: int, reasoning_effort: Optional[str]) -> OpenAIModel:
    effort = "medium" if reasoning_effort is None else reasoning_effort
    if effort not in ("none", "low", "medium", "high", "xhigh", "max"):
        raise ValueError("Kimi K3 reasoning_effort must be none, low, medium, high, xhigh or max")
    client_args: Dict[str, Any] = {
        "base_url": f"https://bedrock-runtime.{REGION}.amazonaws.com/openai/v1",
        "timeout": httpx.Timeout(180, connect=5, write=30, pool=5),
        "max_retries": 1,
    }
    credentials_provider = None
    if AUTH_MODE == "profile":
        with _k3_credential_logs():
            credentials_provider = _ProfileCredentialsProvider(_profile_session())
            credentials_provider.load()
    elif AUTH_MODE in ("bearer", "secret"):
        token = _secret_token() if AUTH_MODE == "secret" else os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
        if not isinstance(token, str) or not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise RuntimeError("Kimi K3 requires a valid Bedrock bearer token")
        client_args["api_key"] = token
    else:
        raise RuntimeError("Kimi K3 requires explicit profile, bearer or secret auth")
    try:
        return _K3OpenAIModel(
            credentials_provider=credentials_provider, model_region=REGION,
            model_id="us.moonshotai.kimi-k3", client_args=client_args,
            params={"max_tokens": min(max_output_tokens, 32768), "reasoning_effort": effort},
        )
    except Exception:
        raise RuntimeError("Unable to initialize Kimi K3 Chat Completions") from None


def _configured_models() -> List[str]:
    if MODEL_ID not in _IMPLEMENTED_MODELS:
        raise ValueError("IELTS_MODEL_ID has no implemented model adapter")
    configured = os.environ.get("IELTS_ALLOWED_MODEL_IDS", MODEL_ID).split(",")
    return list(dict.fromkeys(
        [MODEL_ID] + [value.strip() for value in configured
                      if value.strip() in _IMPLEMENTED_MODELS]
    ))


def _validate_auth() -> None:
    if AUTH_MODE not in ("bearer", "profile", "mantle", "secret"):
        raise ValueError("IELTS_MODEL_AUTH must be bearer, profile, mantle or secret")
    if AUTH_MODE == "secret":
        _secret_reference()
    if AUTH_MODE == "profile" and not os.environ.get("IELTS_MODEL_PROFILE", "").strip():
        raise RuntimeError("IELTS_MODEL_AUTH=profile requires IELTS_MODEL_PROFILE")
    if AUTH_MODE == "mantle" and any(
        os.environ.get(key, "").startswith("cn-")
        for key in ("AWS_REGION", "AWS_DEFAULT_REGION")
    ):
        raise RuntimeError("China application regions require explicit overseas profile or bearer auth")


def _secret_reference() -> tuple[str, str]:
    """Require explicit account isolation; the role's exact ARN policy authorizes reads."""
    arn = os.environ.get("IELTS_MODEL_SECRET_ARN", "")
    if not arn:
        raise RuntimeError("IELTS_MODEL_AUTH=secret requires IELTS_MODEL_SECRET_ARN")
    match = re.fullmatch(
        r"arn:aws-cn:secretsmanager:(cn-north-1|cn-northwest-1):"
        r"([0-9]{12}):secret:[A-Za-z0-9/_+=.@-]{1,512}-[A-Za-z0-9]{6}",
        arn,
    )
    if match is None:
        raise ValueError("IELTS_MODEL_SECRET_ARN must be a full China Secrets Manager ARN")
    region, account = match.groups()
    app_region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if region != app_region:
        raise ValueError("IELTS_MODEL_SECRET_ARN region must match the China application region")
    expected_account = os.environ.get("IELTS_MODEL_SECRET_ACCOUNT_ID", "")
    if (
        re.fullmatch(r"[0-9]{12}", expected_account) is None or expected_account != account
    ):
        raise ValueError("IELTS_MODEL_SECRET_ACCOUNT_ID must match the secret ARN account")
    return arn, region


def _secret_token() -> str:
    """Resolve AWSCURRENT in memory, with no token cache or credential fallback."""
    arn, region = _secret_reference()
    for name in ("botocore.parsers", "botocore.endpoint", "botocore.retryhandler",
                 "botocore.retries.standard", "botocore.httpsession", "botocore.auth"):
        logging.getLogger(name).addFilter(_secret_log_filter)
    lookup_context = _SECRET_LOOKUP.set(True)
    try:
        session = boto3.Session(region_name=region)
        with closing(session.client(
            "secretsmanager",
            region_name=region,
            config=Config(
                connect_timeout=3,
                read_timeout=5,
                retries={"mode": "standard", "total_max_attempts": 2},
                ignore_configured_endpoint_urls=True,
            ),
        )) as client:
            response = client.get_secret_value(SecretId=arn, VersionStage="AWSCURRENT")
        if response.get("ARN") != arn:
            raise ValueError
        value = response.get("SecretString")
        if not isinstance(value, str):
            raise ValueError
        secret = json.loads(value)
        token = secret.get("token") if isinstance(secret, dict) else None
        # An API token must be a nonempty ASCII header value without whitespace.
        if not isinstance(token, str) or not token or any(
            ord(char) < 33 or ord(char) > 126 for char in token
        ):
            raise ValueError
    except Exception:
        # SDK and JSON errors may include response bodies. Suppress their traceback
        # chain as well as their message; never substitute ambient bearer/profile auth.
        raise RuntimeError("Unable to resolve model secret credentials") from None
    finally:
        _SECRET_LOOKUP.reset(lookup_context)
    return token


def _profile_session() -> boto3.Session:
    """Retain refreshable profile credentials while disabling ambient fallbacks."""
    _validate_auth()
    profile = os.environ.get("IELTS_MODEL_PROFILE", "").strip()
    if not profile:
        raise RuntimeError("Profile credentials require IELTS_MODEL_PROFILE")
    session = boto3.Session(profile_name=profile, region_name=REGION)
    # Explicit profile selection disables env keys, but botocore still appends ECS,
    # IMDS and legacy credential files. Remove those before any credential lookup.
    resolver = session._session.get_component("credential_provider")
    for method in ("env", "ec2-credentials-file", "boto-config", "container-role", "iam-role"):
        resolver.remove(method)
    profiles = session._session.full_config.get("profiles", {})
    visited = set()
    while profile and profile not in visited:
        visited.add(profile)
        config = profiles.get(profile, {})
        if config.get("credential_source"):
            raise RuntimeError("Model profiles must use explicit source_profile credentials, not credential_source")
        profile = config.get("source_profile")
    return session


class _ProfileCredentialsProvider(CredentialProvider):
    METHOD = "ielts-model-profile"

    def __init__(self, session: boto3.Session):
        self.session = session

    def load(self):
        # Return the live object, not frozen keys: SSO/role/login refresh remains SDK-owned.
        credentials = self.session.get_credentials()
        if credentials is None:
            raise RuntimeError("IELTS_MODEL_PROFILE has no usable credentials; ambient fallback is disabled")
        return credentials


def build_model(
    max_output_tokens: int,
    reasoning_effort: Optional[str] = None,
    model_id: Optional[str] = None,
) -> OpenAIModel | OpenAIResponsesModel:
    """Create one step's adapter without changing request-local model selection."""
    assert_region_supported()
    _validate_auth()
    identifier = model_id or _MODEL_CONTEXT.get() or MODEL_ID
    if identifier not in _configured_models():
        raise ValueError("model %r is not available in this deployment" % identifier)
    kimi = identifier == "moonshotai.kimi-k2.5"
    model_class = OpenAIModel if kimi else OpenAIResponsesModel
    if max_output_tokens <= 0:
        raise ValueError("max_output_tokens must be positive")
    if identifier == "us.moonshotai.kimi-k3":
        return _build_k3_model(max_output_tokens, reasoning_effort)
    params: Dict[str, Any] = (
        {"max_tokens": min(max_output_tokens, 16384)} if kimi
        else {"max_output_tokens": max_output_tokens}
    )
    if not kimi and reasoning_effort:
        params["reasoning"] = {"effort": reasoning_effort}
    if AUTH_MODE in ("bearer", "secret"):
        token = _secret_token() if AUTH_MODE == "secret" else os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
        if not token:
            raise RuntimeError(
                "IELTS_MODEL_AUTH=bearer requires AWS_BEARER_TOKEN_BEDROCK in the environment"
            )
        path = "v1" if kimi else "openai/v1"
        if AUTH_MODE == "secret":
            try:
                return model_class(
                    model_id=identifier,
                    client_args={"api_key": token, "base_url": f"https://bedrock-mantle.{REGION}.api.aws/{path}"},
                    params=params,
                )
            except Exception:
                raise RuntimeError("Unable to initialize model with secret credentials") from None
        return model_class(
            model_id=identifier,
            client_args={"api_key": token, "base_url": f"https://bedrock-mantle.{REGION}.api.aws/{path}"},
            params=params,
        )
    config: Dict[str, Any] = {"region": REGION}
    if AUTH_MODE == "profile":
        credentials_provider = _ProfileCredentialsProvider(_profile_session())
        credentials_provider.load()  # Fail before invocation if the explicit profile is empty.
        config["credentials_provider"] = credentials_provider
    return model_class(model_id=identifier, bedrock_mantle_config=config, params=params)


def describe() -> Dict[str, Any]:
    """Configuration snapshot with no credentials for logs and batch summaries."""
    return {"model_id": MODEL_ID, "region": REGION, "auth_mode": AUTH_MODE}


def current_model_id() -> str:
    """Return the request's model selection without catalogue or credential lookup."""
    return _MODEL_CONTEXT.get() or MODEL_ID


@contextmanager
def use_model(model_id: str) -> Iterator[None]:
    """Bind one model to the current async request without affecting other requests."""
    token = _MODEL_CONTEXT.set(model_id)
    try:
        yield
    finally:
        _MODEL_CONTEXT.reset(token)


def _remote_models() -> List[str]:
    # Only explicitly isolated profile credentials can perform catalogue discovery.
    # Secret, bearer and legacy mantle use configured IDs without a control-plane call.
    if AUTH_MODE != "profile":
        return _configured_models()
    session = _profile_session()
    _ProfileCredentialsProvider(session).load()
    rows = session.client("bedrock", region_name=REGION).list_foundation_models().get(
        "modelSummaries", []
    )
    return sorted({("us.moonshotai.kimi-k3" if row.get("modelId") == "moonshotai.kimi-k3"
                    else str(row.get("modelId") or "").strip())
                   for row in rows if isinstance(row, dict)})


def list_models(force: bool = False) -> Dict[str, Any]:
    """Configured adapter catalogue; listing is not proof of inference entitlement."""
    assert_region_supported()
    _validate_auth()
    configured = _configured_models()
    identifiers = configured
    warning = None
    if AUTH_MODE == "profile":
        now = time.monotonic()
        key = (REGION, AUTH_MODE, os.environ.get("IELTS_MODEL_PROFILE"), tuple(configured))
        if (force or _model_cache.get("key") != key or _model_cache.get("models") is None
                or now - _model_cache["at"] >= _MODEL_CACHE_TTL):
            try:
                remote = _remote_models()
                identifiers = [value for value in configured if value == MODEL_ID or value in remote]
            except Exception:  # noqa: BLE001 - configured fallback is the public contract
                warning = "模型目录查询失败，显示已配置模型；目录可见性不代表推理权限。"
            _model_cache.update({"key": key, "at": now, "models": identifiers, "warning": warning})
        identifiers = _model_cache["models"]
        warning = _model_cache.get("warning")
    result: Dict[str, Any] = {
        "models": [{"id": value, "label": value.removeprefix("us.").removeprefix("openai.").removeprefix("moonshotai.")}
                   for value in identifiers],
        "default_model_id": MODEL_ID,
    }
    if warning:
        result["warning"] = warning
    return result


def resolve_model_id(requested: Optional[str]) -> str:
    """Validate a browser choice against the configured deployment catalogue."""
    identifier = str(requested).strip() if requested else MODEL_ID
    allowed = {row["id"] for row in list_models()["models"]}
    if identifier not in allowed:
        raise ValueError("model %r is not available in this deployment" % identifier)
    return identifier


def assert_region_supported(region: Optional[str] = None) -> None:
    """Validate the overseas model region, independently of the Runtime region."""
    resolved = region or REGION
    if resolved not in SUPPORTED_REGIONS:
        raise ValueError(
            "IELTS_MODEL_REGION must be %s (got %r); it is independent of AWS_REGION"
            % (" / ".join(SUPPORTED_REGIONS), resolved)
        )
