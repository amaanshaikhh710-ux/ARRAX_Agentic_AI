"""Optional, server-side Gemini provider and untrusted planning adapter.

The provider boundary is intentionally small.  It uses the native Gemini REST
API through :mod:`urllib`, keeps credentials in the server process, and does
not perform work during import or application construction.  A provider is
only contacted when verification or generation is explicitly requested.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import json
import math
import os
from pathlib import PurePosixPath, PureWindowsPath
import re
import socket
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .domain import PlanStep, RiskLevel
from .planner import DeterministicPlanner, PlanningError
from .tools import ToolInputError, UnknownToolError, ToolRegistry


GEMINI_API_ORIGIN = "https://generativelanguage.googleapis.com"
GEMINI_API_VERSION = "v1beta"
# Stable candidate from the current Gemini API model catalog. Availability is
# still verified against the configured account before it is used.
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
DEFAULT_CONNECT_TIMEOUT_SECONDS = 3.0
DEFAULT_READ_TIMEOUT_SECONDS = 20.0
DEFAULT_PROVIDER_ATTEMPTS = 3
DEFAULT_MAX_RESPONSE_CHARS = 16_384
MAX_PROVIDER_RESPONSE_BYTES = 1_048_576
MAX_PLAN_STEPS = 32
MAX_PLAN_ATTEMPTS = 5
MAX_PLAN_TIMEOUT_SECONDS = 10.0


class ProviderMode(str, Enum):
    """Configured planner/provider mode."""

    DETERMINISTIC = "deterministic"
    AUTO = "auto"
    GEMINI = "gemini"


class ProviderConfigError(ValueError):
    """Raised when provider configuration is malformed."""


@dataclass(frozen=True)
class ProviderConfig:
    """Validated provider configuration.

    ``api_key`` is deliberately not included in any public status object.  It
    is held only by the server-side provider instance that needs it for a REST
    request.
    """

    mode: ProviderMode = ProviderMode.AUTO
    api_key: str | None = None
    model: str = DEFAULT_GEMINI_MODEL
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS
    max_attempts: int = DEFAULT_PROVIDER_ATTEMPTS
    max_response_chars: int = DEFAULT_MAX_RESPONSE_CHARS

    def __post_init__(self) -> None:
        mode = self.mode if isinstance(self.mode, ProviderMode) else _parse_mode(self.mode)
        object.__setattr__(self, "mode", mode)

        key = _normalize_api_key(self.api_key)
        object.__setattr__(self, "api_key", key)
        model = _normalize_model(self.model)
        object.__setattr__(self, "model", model)

        for name in ("connect_timeout_seconds", "read_timeout_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ProviderConfigError(f"{name} must be a positive finite number")
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int) or not 1 <= self.max_attempts <= 5:
            raise ProviderConfigError("max_attempts must be an integer between one and five")
        if isinstance(self.max_response_chars, bool) or not isinstance(self.max_response_chars, int) or not 1 <= self.max_response_chars <= 131_072:
            raise ProviderConfigError("max_response_chars must be between one and 131072")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ProviderConfig":
        """Parse the documented environment without contacting a provider."""

        values = os.environ if environ is None else environ
        raw_mode = values.get("ARRAX_AI_MODE", "auto")
        raw_key = values.get("GEMINI_API_KEY")
        raw_model = values.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
        return cls(
            mode=_parse_mode(raw_mode),
            api_key=_normalize_api_key(raw_key),
            model=_normalize_model(raw_model),
        )

    @classmethod
    def from_values(
        cls,
        *,
        mode: str | ProviderMode | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> "ProviderConfig":
        """Build configuration from an application/config mapping."""

        return cls(
            mode=_parse_mode("auto" if mode is None else mode),
            api_key=_normalize_api_key(api_key),
            model=_normalize_model(DEFAULT_GEMINI_MODEL if model is None else model),
        )

    @property
    def configured(self) -> bool:
        return self.mode is not ProviderMode.DETERMINISTIC and self.api_key is not None

    @property
    def effective_mode(self) -> ProviderMode:
        if self.mode is ProviderMode.AUTO and self.api_key is None:
            return ProviderMode.DETERMINISTIC
        return self.mode


def _parse_mode(value: str | ProviderMode) -> ProviderMode:
    if isinstance(value, ProviderMode):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ProviderConfigError("ARRAX_AI_MODE must be deterministic, auto, or gemini")
    try:
        return ProviderMode(value.strip().lower())
    except ValueError as exc:
        raise ProviderConfigError("ARRAX_AI_MODE must be deterministic, auto, or gemini") from exc


def _normalize_api_key(value: str | None) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str) or len(value) > 512 or value != value.strip() or any(char.isspace() or ord(char) < 32 for char in value):
        raise ProviderConfigError("GEMINI_API_KEY must be a single non-empty value")
    return value


_MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def _normalize_model(value: str) -> str:
    if not isinstance(value, str):
        raise ProviderConfigError("GEMINI_MODEL must be a non-empty model name")
    model = value.strip()
    if model.startswith("models/"):
        model = model[7:]
    if not _MODEL_PATTERN.fullmatch(model):
        raise ProviderConfigError("GEMINI_MODEL must be a URL-safe model name")
    return model


@dataclass(frozen=True)
class ProviderStatus:
    """Safe provider state suitable for API and frontend responses."""

    mode: ProviderMode
    configured: bool
    verified: bool
    model: str
    message: str
    last_error: str | None = None
    last_mode: ProviderMode | None = None
    configured_mode: ProviderMode | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ProviderMode):
            object.__setattr__(self, "mode", _parse_mode(self.mode))
        if self.configured_mode is not None and not isinstance(self.configured_mode, ProviderMode):
            object.__setattr__(self, "configured_mode", _parse_mode(self.configured_mode))
        if self.last_mode is not None and not isinstance(self.last_mode, ProviderMode):
            object.__setattr__(self, "last_mode", _parse_mode(self.last_mode))
        if not isinstance(self.configured, bool) or not isinstance(self.verified, bool):
            raise ValueError("provider status flags must be boolean")
        if not isinstance(self.model, str) or not self.model:
            raise ValueError("provider status model must be non-empty")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("provider status message must be non-empty")
        if self.last_error is not None and not isinstance(self.last_error, str):
            raise ValueError("provider status last_error must be a category or null")

    def to_dict(self) -> dict[str, Any]:
        """Serialize only non-sensitive provider state."""

        return {
            "mode": self.mode.value,
            "configured_mode": (self.configured_mode or self.mode).value,
            "configured": self.configured,
            "verified": self.verified,
            "model": self.model,
            "message": self.message,
            "last_error": self.last_error,
            "last_mode": self.last_mode.value if self.last_mode is not None else None,
        }


class ProviderError(RuntimeError):
    """A bounded, safe provider failure classified for callers."""

    def __init__(self, category: str, message: str) -> None:
        self.category = category
        self.safe_message = message
        super().__init__(message)


@dataclass(frozen=True)
class HttpResponse:
    """Small transport result used by the real and fake HTTP transports."""

    status: int
    body: bytes | str


class ProviderTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        """Perform one bounded request and return a response without logging it."""


class UrllibTransport:
    """Native-library HTTPS transport with a bounded response read."""

    def __init__(self, *, max_response_bytes: int = MAX_PROVIDER_RESPONSE_BYTES) -> None:
        self.max_response_bytes = max_response_bytes

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
        read_timeout: float | None = None,
    ) -> HttpResponse:
        request = Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed HTTPS origin is enforced by caller
                if read_timeout is not None:
                    # urllib exposes one timeout at the public API. Once the
                    # HTTPS connection has been established, tighten the
                    # underlying socket for the response body read.
                    raw = getattr(getattr(response, "fp", None), "raw", None)
                    sock = getattr(raw, "_sock", None)
                    if sock is not None and callable(getattr(sock, "settimeout", None)):
                        sock.settimeout(read_timeout)
                return HttpResponse(response.status, response.read(self.max_response_bytes + 1))
        except HTTPError as exc:
            try:
                body_value = exc.read(self.max_response_bytes + 1)
            except OSError:
                body_value = b""
            return HttpResponse(exc.code, body_value)


@dataclass(frozen=True)
class ModelInfo:
    """Safe subset of a Gemini model-list response."""

    name: str
    supported_generation_methods: tuple[str, ...]


def _base_status(config: ProviderConfig, *, last_error: str | None = None, message: str | None = None) -> ProviderStatus:
    effective = config.effective_mode
    if message is None:
        if config.mode is ProviderMode.DETERMINISTIC:
            message = "Deterministic planner selected; Gemini is disabled."
        elif config.api_key is None:
            message = "No Gemini API key configured; using deterministic planner."
        else:
            message = "Gemini is configured but has not been verified."
    return ProviderStatus(
        mode=effective,
        configured=config.configured,
        verified=False,
        model=config.model,
        message=message,
        last_error=last_error or ("missing_key" if config.mode is not ProviderMode.DETERMINISTIC and config.api_key is None else None),
        last_mode=effective if effective is ProviderMode.DETERMINISTIC else None,
        configured_mode=config.mode,
    )


class GeminiProvider:
    """Server-side Gemini REST client with explicit verification."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        transport: ProviderTransport | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or UrllibTransport()
        self._sleep = sleeper or time.sleep
        self._status = _base_status(config)

    @property
    def verified(self) -> bool:
        return self._status.verified

    def status(self) -> ProviderStatus:
        return self._status

    def list_models(self) -> list[ModelInfo]:
        """List model metadata and return only the fields needed for verification."""

        self._require_key()
        models: list[ModelInfo] = []
        page_token: str | None = None
        for _page in range(3):
            query: dict[str, str | None] = {}
            if page_token:
                query["pageToken"] = page_token
            payload = self._request_json("GET", self._models_url(query), operation="models")
            raw_models = payload.get("models")
            if not isinstance(raw_models, list):
                raise ProviderError("invalid_response", "Gemini model list response was invalid.")
            for raw_model in raw_models:
                if not isinstance(raw_model, Mapping):
                    raise ProviderError("invalid_response", "Gemini model list response was invalid.")
                name = raw_model.get("name")
                methods = raw_model.get("supportedGenerationMethods", [])
                if not isinstance(name, str) or not name.startswith("models/") or not isinstance(methods, list) or any(not isinstance(item, str) for item in methods):
                    raise ProviderError("invalid_response", "Gemini model list response was invalid.")
                try:
                    normalized_name = _normalize_model(name)
                except ProviderConfigError as exc:
                    raise ProviderError("invalid_response", "Gemini model list response was invalid.") from exc
                models.append(ModelInfo(normalized_name, tuple(methods)))
            page_token = payload.get("nextPageToken")
            if page_token is None:
                break
            if not isinstance(page_token, str) or not page_token:
                raise ProviderError("invalid_response", "Gemini model list response was invalid.")
        return models

    def verify(self) -> ProviderStatus:
        """Verify the exact configured model and perform a minimal canary."""

        if self.config.mode is ProviderMode.DETERMINISTIC:
            self._status = _base_status(self.config)
            return self._status
        if self.config.api_key is None:
            self._status = _base_status(self.config, last_error="missing_key")
            return self._status
        try:
            models = self.list_models()
            target = next((model for model in models if model.name == self.config.model), None)
            if target is None or "generateContent" not in target.supported_generation_methods:
                raise ProviderError(
                    "model_unavailable",
                    "The configured Gemini model is unavailable or does not support generateContent.",
                )
            self._generate_text(
                "Reply with the single word OK.",
                max_output_chars=64,
                allow_unverified=True,
            )
        except ProviderError as exc:
            self._status = ProviderStatus(
                mode=self.config.mode,
                configured=True,
                verified=False,
                model=self.config.model,
                message=exc.safe_message,
                last_error=exc.category,
                last_mode=None,
                configured_mode=self.config.mode,
            )
            return self._status
        self._status = ProviderStatus(
            mode=ProviderMode.GEMINI,
            configured=True,
            verified=True,
            model=self.config.model,
            message="Gemini provider verified for the configured model.",
            last_error=None,
            last_mode=ProviderMode.GEMINI,
            configured_mode=self.config.mode,
        )
        return self._status

    def generate_text(self, prompt: str, *, max_output_chars: int | None = None) -> str:
        """Generate bounded text after provider verification."""

        return self._generate_text(prompt, max_output_chars=max_output_chars, allow_unverified=False)

    # ``generate`` is a convenient compatibility spelling for small fakes and
    # callers that use the generic provider vocabulary.
    def generate(self, prompt: str, *, max_output_chars: int | None = None) -> str:
        return self.generate_text(prompt, max_output_chars=max_output_chars)

    def _generate_text(self, prompt: str, *, max_output_chars: int | None, allow_unverified: bool) -> str:
        self._require_key()
        if not allow_unverified and not self._status.verified:
            raise ProviderError("not_verified", "Gemini provider has not been verified.")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ProviderError("invalid_request", "Gemini prompt must be non-empty.")
        if len(prompt) > 32_768:
            raise ProviderError("invalid_request", "Gemini prompt exceeds the configured limit.")
        output_limit = self.config.max_response_chars if max_output_chars is None else max_output_chars
        if isinstance(output_limit, bool) or not isinstance(output_limit, int) or not 1 <= output_limit <= self.config.max_response_chars:
            raise ProviderError("invalid_request", "Gemini response limit is invalid.")
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": min(2048, max(16, output_limit // 4))},
        }
        response = self._request_json(
            "POST",
            self._generate_url(),
            operation="generate",
            payload=payload,
        )
        text = self._extract_text(response)
        if not text:
            raise ProviderError("invalid_response", "Gemini returned no text content.")
        if len(text) > output_limit:
            raise ProviderError("invalid_response", "Gemini response exceeded the configured size limit.")
        return text

    def _require_key(self) -> None:
        if self.config.api_key is None:
            raise ProviderError("missing_key", "No Gemini API key is configured.")

    def _models_url(self, query: Mapping[str, str | None]) -> str:
        encoded = urlencode({key: value for key, value in query.items() if value is not None})
        suffix = f"?{encoded}" if encoded else ""
        return f"{GEMINI_API_ORIGIN}/{GEMINI_API_VERSION}/models{suffix}"

    def _generate_url(self) -> str:
        model_path = quote(self.config.model, safe="-._~")
        return f"{GEMINI_API_ORIGIN}/{GEMINI_API_VERSION}/models/{model_path}:generateContent"

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        headers = {"Accept": "application/json"}
        if self.config.api_key is not None:
            # Keep the credential out of URLs, which are more likely to be
            # copied into access logs, traces, proxies, or error reports.
            headers["x-goog-api-key"] = self.config.api_key
        if body is not None:
            headers["Content-Type"] = "application/json"
        last_status: int | None = None
        for attempt in range(1, self.config.max_attempts + 1):
            try:
                transport_kwargs = {
                    "headers": headers,
                    "body": body,
                    "timeout": self.config.connect_timeout_seconds,
                }
                if isinstance(self.transport, UrllibTransport):
                    transport_kwargs["read_timeout"] = self.config.read_timeout_seconds
                response = self.transport.request(method, url, **transport_kwargs)
                if not isinstance(response, HttpResponse):
                    if isinstance(response, tuple) and len(response) == 2:
                        response = HttpResponse(int(response[0]), response[1])
                    else:
                        raise ProviderError("invalid_response", "Gemini transport returned an invalid response.")
            except ProviderError:
                raise
            except (TimeoutError, socket.timeout) as exc:
                if attempt < self.config.max_attempts:
                    self._sleep(0.05 * attempt)
                    continue
                raise ProviderError("timeout", "Gemini provider request timed out.") from exc
            except (URLError, OSError, ConnectionError) as exc:
                if _is_timeout_error(exc):
                    category = "timeout"
                else:
                    category = "network"
                if attempt < self.config.max_attempts:
                    self._sleep(0.05 * attempt)
                    continue
                message = "Gemini provider request timed out." if category == "timeout" else "Gemini provider network request failed."
                raise ProviderError(category, message) from exc
            except Exception as exc:
                # A custom transport is an injection boundary. Do not let an
                # implementation-specific exception or its message cross the
                # provider boundary, and keep the retry count bounded.
                if attempt < self.config.max_attempts:
                    self._sleep(0.05 * attempt)
                    continue
                raise ProviderError("network", "Gemini provider network request failed.") from exc

            last_status = response.status
            if isinstance(response.status, bool) or not isinstance(response.status, int):
                raise ProviderError("invalid_response", "Gemini transport returned an invalid status.")
            if 200 <= response.status < 300:
                return self._decode_json(response.body)
            if response.status == 429 or response.status >= 500:
                if attempt < self.config.max_attempts:
                    self._sleep(0.05 * attempt)
                    continue
                if response.status == 429:
                    raise ProviderError("rate_limit", "Gemini provider rate limit was reached.")
                raise ProviderError("service_unavailable", "Gemini provider service is unavailable.")
            if response.status in {401, 403}:
                raise ProviderError("authentication", "Gemini provider authentication was rejected.")
            if response.status == 404 and operation == "generate":
                raise ProviderError("model_unavailable", "The configured Gemini model is unavailable.")
            raise ProviderError("provider_http", "Gemini provider returned an unexpected HTTP status.")
        # The loop is statically bounded by max_attempts; this guard keeps the
        # return type obvious if that invariant is ever changed.
        raise ProviderError("provider_http", f"Gemini provider request failed with status {last_status or 0}.")

    @staticmethod
    def _decode_json(body: bytes | str) -> dict[str, Any]:
        if isinstance(body, bytes):
            if len(body) > MAX_PROVIDER_RESPONSE_BYTES:
                raise ProviderError("invalid_response", "Gemini response exceeded the size limit.")
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ProviderError("invalid_response", "Gemini response was not valid JSON.") from exc
        elif isinstance(body, str):
            if len(body.encode("utf-8")) > MAX_PROVIDER_RESPONSE_BYTES:
                raise ProviderError("invalid_response", "Gemini response exceeded the size limit.")
            text = body
        else:
            raise ProviderError("invalid_response", "Gemini response was not valid JSON.")
        try:
            value = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProviderError("invalid_response", "Gemini response was not valid JSON.") from exc
        if not isinstance(value, dict):
            raise ProviderError("invalid_response", "Gemini response was not a JSON object.")
        return value

    @staticmethod
    def _extract_text(payload: Mapping[str, Any]) -> str:
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ProviderError("invalid_response", "Gemini response did not contain candidates.")
        candidate = candidates[0]
        if not isinstance(candidate, Mapping):
            raise ProviderError("invalid_response", "Gemini response content was invalid.")
        content = candidate.get("content")
        if not isinstance(content, Mapping):
            raise ProviderError("invalid_response", "Gemini response content was invalid.")
        parts = content.get("parts")
        if not isinstance(parts, list):
            raise ProviderError("invalid_response", "Gemini response content was invalid.")
        text_parts: list[str] = []
        for part in parts:
            if not isinstance(part, Mapping) or not isinstance(part.get("text"), str):
                raise ProviderError("invalid_response", "Gemini response content was invalid.")
            text_parts.append(part["text"])
        return "".join(text_parts)


def _is_timeout_error(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", None)
    return isinstance(exc, (TimeoutError, socket.timeout)) or isinstance(reason, (TimeoutError, socket.timeout))


def _coerce_status(value: Any, *, config_mode: ProviderMode, model: str) -> ProviderStatus:
    if isinstance(value, ProviderStatus):
        return value
    if isinstance(value, Mapping):
        try:
            mode = _parse_mode(value.get("mode", config_mode.value))
            configured_mode = _parse_mode(value.get("configured_mode", config_mode.value))
            last_mode_value = value.get("last_mode")
            last_mode = _parse_mode(last_mode_value) if last_mode_value else None
            return ProviderStatus(
                mode=mode,
                configured=bool(value.get("configured", False)),
                verified=bool(value.get("verified", False)),
                model=str(value.get("model") or model),
                message=str(value.get("message") or "Provider status unavailable."),
                last_error=value.get("last_error") if isinstance(value.get("last_error"), str) else None,
                last_mode=last_mode,
                configured_mode=configured_mode,
            )
        except (TypeError, ValueError):
            pass
    return ProviderStatus(
        mode=config_mode,
        configured=False,
        verified=False,
        model=model,
        message="Provider status unavailable.",
        last_error="invalid_status",
        configured_mode=config_mode,
    )


class StructuredProviderPlanner:
    """Turn strictly JSON-only provider output into validated ``PlanStep`` values."""

    _STEP_FIELDS = {
        "step_id",
        "id",
        "name",
        "tool_name",
        "tool",
        "input",
        "dependencies",
        "risk",
        "approval_required",
        "max_attempts",
        "timeout_seconds",
        "verifier_tool",
        "verification_input",
    }
    _FORBIDDEN_FIELDS = {
        "approved",
        "approval",
        "approval_granted",
        "permissions",
        "permission",
        "authorized",
        "authorization",
        "grant",
        "allow",
        "bypass",
    }
    _UNSAFE_INPUT_FIELDS = {
        "command",
        "shell",
        "script",
        "executable",
        "program",
        "url",
        "endpoint",
        "network",
    }
    _PATH_FIELDS = {"path", "file", "directory", "root", "report_path"}

    def __init__(
        self,
        provider: Any,
        registry: ToolRegistry,
        *,
        deterministic_planner: DeterministicPlanner | None = None,
        max_steps: int = MAX_PLAN_STEPS,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.deterministic_planner = deterministic_planner or DeterministicPlanner(
            workspace_enabled=any(name.startswith("workspace.") for name in registry.names())
        )
        self.max_steps = max_steps

    def plan(self, goal: Mapping[str, Any]) -> tuple[PlanStep, ...]:
        if not isinstance(goal, Mapping) or not goal:
            raise PlanningError("goal must be a non-empty object")
        try:
            serialized_goal = json.dumps(dict(goal), ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise PlanningError("goal must contain JSON values") from exc
        if len(serialized_goal) > 16_384:
            raise PlanningError("goal exceeds the provider planning limit")
        prompt = self._prompt(serialized_goal)
        try:
            generate = getattr(self.provider, "generate_text", None) or getattr(self.provider, "generate", None)
            if not callable(generate):
                raise ProviderError("invalid_provider", "Provider does not expose structured generation.")
            output_limit = min(16_384, getattr(getattr(self.provider, "config", None), "max_response_chars", 16_384))
            text = generate(prompt, max_output_chars=output_limit)
        except ProviderError as exc:
            raise PlanningError(f"Gemini planning failed: {exc.safe_message}") from exc
        except Exception as exc:
            raise PlanningError("Gemini planning failed: provider request failed.") from exc
        if not isinstance(text, str) or len(text) > 16_384:
            raise PlanningError("provider plan response exceeded the size limit")
        try:
            document = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PlanningError("provider plan must be valid JSON only") from exc
        return self._validate_document(document)

    def _prompt(self, serialized_goal: str) -> str:
        tools = [
            {"name": name, "minimum_risk": self.registry.risk_for(name).value}
            for name in self.registry.names()
        ]
        tool_json = json.dumps(tools, ensure_ascii=True, separators=(",", ":"))
        return (
            "Return JSON only. Do not use Markdown, prose, approval claims, or permission fields. "
            "Create a bounded plan using only the listed tools. The application, not the model, "
            "decides approval and permissions. The exact output shape is "
            '{"steps":[{"step_id":"id","name":"...","tool_name":"registered.tool",'
            '"input":{},"dependencies":[],"risk":"read_only","approval_required":false,'
            '"max_attempts":1,"timeout_seconds":1.0,"verifier_tool":"fixture.verify",'
            '"verification_input":{}}]}. Omit optional fields only when their defaults are safe. '
            f"Registered tools: {tool_json}. Goal JSON: {serialized_goal}"
        )

    def _validate_document(self, document: Any) -> tuple[PlanStep, ...]:
        if not isinstance(document, Mapping) or set(document) != {"steps"}:
            raise PlanningError("provider plan must be an object containing only steps")
        raw_steps = document.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps or len(raw_steps) > self.max_steps:
            raise PlanningError("provider plan steps must be a bounded non-empty list")
        normalized_steps: list[dict[str, Any]] = []
        for index, raw_step in enumerate(raw_steps, start=1):
            if not isinstance(raw_step, Mapping):
                raise PlanningError(f"provider plan step {index} must be an object")
            keys = set(raw_step)
            forbidden = keys & self._FORBIDDEN_FIELDS
            if forbidden:
                raise PlanningError("provider plan cannot grant approval or permissions")
            unknown = keys - self._STEP_FIELDS
            if unknown:
                raise PlanningError("provider plan contains unsupported fields")
            if "step_id" in raw_step and "id" in raw_step:
                raise PlanningError("provider plan step cannot define both step_id and id")
            if "tool_name" in raw_step and "tool" in raw_step:
                raise PlanningError("provider plan step cannot define both tool_name and tool")
            step = dict(raw_step)
            if "id" in step:
                step["step_id"] = step.pop("id")
            if "tool" in step:
                step["tool_name"] = step.pop("tool")
            if "verification_input" in step:
                verification_input = step["verification_input"]
                if verification_input is not None and not isinstance(verification_input, Mapping):
                    raise PlanningError("verification_input must be an object")
                if isinstance(verification_input, Mapping) and "action_output" in verification_input:
                    raise PlanningError("provider cannot supply verification evidence")
                self._validate_safe_value(verification_input or {}, "verification_input")
            self._validate_step_shape(step, index)
            normalized_steps.append(step)

        structured_goal = {"type": "structured_plan", "steps": normalized_steps}
        try:
            plan = self.deterministic_planner.plan(structured_goal)
        except (PlanningError, ValueError) as exc:
            raise PlanningError("provider plan was rejected by the deterministic contract") from exc
        self._validate_plan(plan)
        self._validate_dependencies(plan)
        return plan

    def _validate_step_shape(self, step: Mapping[str, Any], index: int) -> None:
        for field_name in ("step_id", "name"):
            if field_name in step and (not isinstance(step[field_name], str) or not step[field_name].strip()):
                raise PlanningError(f"provider plan {field_name} must be a non-empty string")
        dependencies = step.get("dependencies", [])
        if not isinstance(dependencies, list) or any(not isinstance(item, str) or not item.strip() for item in dependencies):
            raise PlanningError("provider plan dependencies must be a list of step ids")
        tool_name = step.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise PlanningError(f"provider plan step {index} requires tool_name")
        try:
            self.registry.get(tool_name)
        except UnknownToolError as exc:
            raise PlanningError("provider plan selected an unregistered tool") from exc
        action_input = step.get("input", {})
        if not isinstance(action_input, Mapping):
            raise PlanningError(f"provider plan step {index} input must be an object")
        self._validate_safe_value(action_input, f"step {index} input")
        try:
            self.registry.validate(tool_name, action_input)
        except (ToolInputError, UnknownToolError) as exc:
            raise PlanningError("provider plan input was rejected by the tool contract") from exc
        risk = step.get("risk", RiskLevel.READ_ONLY.value)
        try:
            requested_risk = risk if isinstance(risk, RiskLevel) else RiskLevel(risk)
        except (TypeError, ValueError) as exc:
            raise PlanningError("provider plan contains an invalid risk level") from exc
        required_risk = self.registry.risk_for(tool_name)
        if _risk_rank(requested_risk) < _risk_rank(required_risk):
            raise PlanningError("provider plan risk is below the registry minimum")
        if "approval_required" in step and not isinstance(step["approval_required"], bool):
            raise PlanningError("approval_required must be boolean")
        max_attempts = step.get("max_attempts", 1)
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= MAX_PLAN_ATTEMPTS:
            raise PlanningError("max_attempts is outside the provider plan bound")
        timeout = step.get("timeout_seconds", 1.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= MAX_PLAN_TIMEOUT_SECONDS:
            raise PlanningError("timeout_seconds is outside the provider plan bound")
        verifier_name = step.get("verifier_tool", "fixture.verify")
        if not isinstance(verifier_name, str) or not verifier_name.strip():
            raise PlanningError("verifier_tool must be a non-empty string")
        try:
            verifier = self.registry.get(verifier_name)
        except UnknownToolError as exc:
            raise PlanningError("provider plan selected an unknown verifier") from exc
        if not getattr(verifier, "is_verifier", False) or self.registry.risk_for(verifier_name) is not RiskLevel.READ_ONLY:
            raise PlanningError("provider plan verifier is not an approved read-only verifier")
        verification_input = step.get("verification_input") or {}
        try:
            self.registry.validate(
                verifier_name,
                {"action_output": {}, **dict(verification_input)},
            )
        except (ToolInputError, UnknownToolError) as exc:
            raise PlanningError("provider verification input was rejected") from exc

    def _validate_plan(self, plan: tuple[PlanStep, ...]) -> None:
        ids = {step.step_id for step in plan}
        for step in plan:
            missing = set(step.dependencies) - ids
            if missing:
                raise PlanningError("provider plan has missing dependencies")
            required = self.registry.risk_for(step.tool_name)
            if _risk_rank(step.risk) < _risk_rank(required):
                raise PlanningError("provider plan risk is below the registry minimum")

    @staticmethod
    def _validate_dependencies(plan: tuple[PlanStep, ...]) -> None:
        graph = {step.step_id: set(step.dependencies) for step in plan}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise PlanningError("provider plan dependencies contain a cycle")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency in graph[step_id]:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in graph:
            visit(step_id)

    def _validate_safe_value(self, value: Any, path: str, depth: int = 0) -> None:
        if depth > 12:
            raise PlanningError("provider plan input is nested too deeply")
        if value is None or isinstance(value, (str, bool, int)):
            return
        if isinstance(value, float):
            if not math.isfinite(value):
                raise PlanningError("provider plan input contains a non-finite number")
            return
        if isinstance(value, Mapping):
            for key, nested in value.items():
                if not isinstance(key, str):
                    raise PlanningError("provider plan input keys must be strings")
                lowered = key.casefold()
                if lowered in self._FORBIDDEN_FIELDS or lowered in self._UNSAFE_INPUT_FIELDS:
                    raise PlanningError("provider plan contains a forbidden policy field")
                if lowered in self._PATH_FIELDS and isinstance(nested, str):
                    self._validate_relative_path(nested)
                self._validate_safe_value(nested, f"{path}.{key}", depth + 1)
            return
        if isinstance(value, list):
            for index, nested in enumerate(value):
                self._validate_safe_value(nested, f"{path}[{index}]", depth + 1)
            return
        raise PlanningError("provider plan input contains an unsupported value")

    @staticmethod
    def _validate_relative_path(value: str) -> None:
        if not value.strip() or PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute() or PureWindowsPath(value).drive:
            raise PlanningError("provider plan path must be relative")
        if any(part == ".." for part in PurePosixPath(value).parts) or any(part == ".." for part in PureWindowsPath(value).parts):
            raise PlanningError("provider plan path traversal is not allowed")


def _risk_rank(risk: RiskLevel) -> int:
    return {
        RiskLevel.READ_ONLY: 0,
        RiskLevel.REVERSIBLE: 1,
        RiskLevel.SENSITIVE: 2,
        RiskLevel.DESTRUCTIVE: 3,
    }[risk]


class HybridPlanner:
    """Use verified Gemini when allowed, with visible deterministic fallback."""

    def __init__(
        self,
        deterministic_planner: DeterministicPlanner | None = None,
        provider: Any | None = None,
        *,
        registry: ToolRegistry | None = None,
        mode: ProviderMode | str | None = None,
    ) -> None:
        self.deterministic_planner = deterministic_planner or DeterministicPlanner()
        self.provider = provider
        provider_config = getattr(provider, "config", None)
        configured_mode = mode or getattr(provider_config, "mode", None) or ProviderMode.DETERMINISTIC
        self.mode = _parse_mode(configured_mode)
        self.registry = registry
        self._provider_planner: StructuredProviderPlanner | None = None
        self._last_mode: ProviderMode | None = None
        self._last_status: ProviderStatus | None = None
        if provider is not None:
            if registry is None:
                raise ValueError("registry is required for a provider planner")
            self._provider_planner = StructuredProviderPlanner(
                provider,
                registry,
                deterministic_planner=self.deterministic_planner,
            )

    @property
    def last_mode(self) -> ProviderMode | None:
        return self._last_mode

    def plan(self, goal: Mapping[str, Any]) -> tuple[PlanStep, ...]:
        if self.mode is ProviderMode.DETERMINISTIC:
            return self._deterministic(goal, "Deterministic planner selected; Gemini is disabled.")
        if self.provider is None or self._provider_planner is None:
            if self.mode is ProviderMode.AUTO:
                return self._deterministic(goal, "Gemini is unavailable; using deterministic planner.", "missing_provider")
            raise PlanningError("Gemini mode requires a configured provider.")

        status = self._provider_status()
        if not status.configured:
            if self.mode is ProviderMode.AUTO:
                return self._deterministic(goal, "No Gemini API key configured; using deterministic planner.", "missing_key")
            raise PlanningError("Gemini mode requires GEMINI_API_KEY.")
        if not status.verified:
            status = self._verify_provider()
        if not status.verified:
            if self.mode is ProviderMode.AUTO:
                return self._deterministic(
                    goal,
                    f"Gemini verification failed ({status.last_error or 'unavailable'}); using deterministic planner.",
                    status.last_error or "provider_unavailable",
                )
            raise PlanningError(f"Gemini planning unavailable: {status.message}")
        try:
            plan = self._provider_planner.plan(goal)
        except PlanningError as exc:
            if self.mode is ProviderMode.AUTO:
                return self._deterministic(goal, "Gemini plan was rejected; using deterministic planner.", "invalid_response")
            raise PlanningError("Gemini planning failed: provider plan was rejected.") from exc
        self._last_mode = ProviderMode.GEMINI
        self._last_status = replace(status, mode=ProviderMode.GEMINI, last_mode=ProviderMode.GEMINI, message="Gemini plan accepted after provider verification.")
        return plan

    def verify_provider(self) -> ProviderStatus:
        return self._verify_provider()

    def status(self) -> ProviderStatus:
        base = self._provider_status()
        if self._last_status is not None:
            return self._last_status
        if self.mode is ProviderMode.DETERMINISTIC:
            return replace(base, mode=ProviderMode.DETERMINISTIC, configured_mode=self.mode, last_mode=ProviderMode.DETERMINISTIC)
        return base

    def _provider_status(self) -> ProviderStatus:
        if self.provider is None:
            return ProviderStatus(
                mode=ProviderMode.DETERMINISTIC if self.mode is ProviderMode.AUTO else self.mode,
                configured=False,
                verified=False,
                model=DEFAULT_GEMINI_MODEL,
                message="Gemini provider is not configured; using deterministic planner.",
                last_error="missing_provider",
                last_mode=ProviderMode.DETERMINISTIC if self.mode is ProviderMode.AUTO else None,
                configured_mode=self.mode,
            )
        value = self.provider.status() if callable(getattr(self.provider, "status", None)) else getattr(self.provider, "status", None)
        config = getattr(self.provider, "config", None)
        if value is None and isinstance(getattr(self.provider, "verified", None), bool):
            verified = bool(self.provider.verified)
            value = ProviderStatus(
                mode=ProviderMode.GEMINI if verified else self.mode,
                configured=bool(getattr(config, "api_key", None)) if config is not None else False,
                verified=verified,
                model=getattr(config, "model", DEFAULT_GEMINI_MODEL),
                message="Gemini provider verified." if verified else "Gemini provider has not been verified.",
                last_mode=ProviderMode.GEMINI if verified else None,
                configured_mode=getattr(config, "mode", self.mode),
            )
        return _coerce_status(
            value,
            config_mode=getattr(config, "mode", self.mode),
            model=getattr(config, "model", DEFAULT_GEMINI_MODEL),
        )

    def _verify_provider(self) -> ProviderStatus:
        verify = getattr(self.provider, "verify", None)
        if not callable(verify):
            status = replace(self._provider_status(), verified=False, last_error="invalid_provider", message="Provider cannot be verified.")
        else:
            try:
                result = verify()
                if result is True:
                    result = replace(
                        self._provider_status(),
                        mode=ProviderMode.GEMINI,
                        verified=True,
                        last_error=None,
                        last_mode=ProviderMode.GEMINI,
                        message="Gemini provider verified.",
                    )
                elif result is False:
                    result = replace(
                        self._provider_status(),
                        verified=False,
                        last_error="provider_unavailable",
                        message="Gemini provider could not be verified.",
                    )
                status = _coerce_status(
                    result,
                    config_mode=getattr(getattr(self.provider, "config", None), "mode", self.mode),
                    model=getattr(getattr(self.provider, "config", None), "model", DEFAULT_GEMINI_MODEL),
                )
            except ProviderError as exc:
                status = replace(self._provider_status(), verified=False, last_error=exc.category, message=exc.safe_message)
            except Exception:
                status = replace(
                    self._provider_status(),
                    verified=False,
                    last_error="provider_error",
                    message="Gemini provider verification failed.",
                )
        self._last_status = status
        return status

    def _deterministic(self, goal: Mapping[str, Any], message: str, error: str | None = None) -> tuple[PlanStep, ...]:
        self._last_mode = ProviderMode.DETERMINISTIC
        base = self._provider_status()
        self._last_status = replace(
            base,
            mode=ProviderMode.DETERMINISTIC,
            last_mode=ProviderMode.DETERMINISTIC,
            message=message,
            last_error=error or base.last_error,
        )
        return self.deterministic_planner.plan(goal)


# Names that make the two boundaries easy to discover for callers.
GeminiPlanner = StructuredProviderPlanner
ProviderPlanner = StructuredProviderPlanner


__all__ = [
    "DEFAULT_GEMINI_MODEL",
    "GEMINI_API_ORIGIN",
    "GeminiPlanner",
    "GeminiProvider",
    "HybridPlanner",
    "HttpResponse",
    "ModelInfo",
    "ProviderConfig",
    "ProviderConfigError",
    "ProviderError",
    "ProviderMode",
    "ProviderPlanner",
    "ProviderStatus",
    "ProviderTransport",
    "StructuredProviderPlanner",
    "UrllibTransport",
]
