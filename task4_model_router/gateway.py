"""Local FastAPI completion gateway for Task 4's limiter and model router."""

import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from task4_model_router.database import RateLimiterStorageError
from task4_model_router.providers import ProviderClient
from task4_model_router.rate_limiter import TOKEN_LIMIT, SlidingWindowRateLimiter
from task4_model_router.router import CompletionRequest, ErrorCode, ModelRouter, RoutingResult


DEFAULT_DATABASE_PATH = Path(__file__).with_name("usage.sqlite3")
DEFAULT_PRIMARY_URL = "http://127.0.0.1:8201/complete"
DEFAULT_BACKUP_URL = "http://127.0.0.1:8202/complete"

# Public demo credentials for localhost assessment testing only.
DEMO_TENANTS = {
    "tenant-a-demo-key": "tenant-a",
    "tenant-b-demo-key": "tenant-b",
}


class CompletionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    prompt: str = Field(min_length=1, max_length=1000)
    max_output_tokens: int = Field(gt=0, le=TOKEN_LIMIT)

    @field_validator("prompt")
    @classmethod
    def nonblank_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Prompt cannot be blank")
        return value


def estimate_input_tokens(prompt: str) -> int:
    """Deterministic mock estimate: ceiling of UTF-8 byte count / 4."""
    return max(1, (len(prompt.encode("utf-8")) + 3) // 4)


def _tenant_for_key(api_key: str | None) -> str | None:
    if api_key is None:
        return None
    try:
        supplied = api_key.encode("utf-8")
    except (UnicodeEncodeError, AttributeError):
        return None
    for expected, tenant in DEMO_TENANTS.items():
        if hmac.compare_digest(supplied, expected.encode("ascii")):
            return tenant
    return None


def _fingerprint(body: CompletionBody, input_tokens: int) -> str:
    encoded = json.dumps(
        {"prompt": body.prompt, "input_tokens": input_tokens,
         "max_output_tokens": body.max_output_tokens},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _error(status: int, code: str, message: str, request_id: str | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"request_id": request_id, "error": {"code": code, "message": message}},
    )


def _routing_error(result: RoutingResult) -> JSONResponse:
    error = result.error
    if error.code == ErrorCode.ACCOUNTING_UNAVAILABLE:
        return _error(500, "INTERNAL_GATEWAY_ERROR", "Gateway unavailable", result.request_id)
    if error.code == ErrorCode.INVALID_PROVIDER_RESPONSE or error.code == ErrorCode.TOKEN_BUDGET_EXCEEDED:
        return _error(502, "INVALID_PROVIDER_RESPONSE", "Invalid provider response", result.request_id)
    if error.backup_code == ErrorCode.PROVIDER_TIMEOUT or error.code == ErrorCode.PROVIDER_TIMEOUT:
        return _error(504, "PROVIDER_TIMEOUT", "Provider timed out", result.request_id)
    return _error(503, "ALL_PROVIDERS_UNAVAILABLE", "No provider completed the request", result.request_id)


def create_app(
    *,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    primary_url: str = DEFAULT_PRIMARY_URL,
    backup_url: str = DEFAULT_BACKUP_URL,
    primary_transport: httpx.AsyncBaseTransport | None = None,
    backup_transport: httpx.AsyncBaseTransport | None = None,
    primary_timeout_seconds: float = 3.0,
    backup_timeout_seconds: float = 3.0,
) -> FastAPI:
    """Create one local gateway; each process coordinates through SQLite."""
    limiter = SlidingWindowRateLimiter(database_path)
    router = ModelRouter(
        limiter,
        ProviderClient(primary_url, transport=primary_transport),
        ProviderClient(backup_url, transport=backup_transport),
        primary_timeout_seconds=primary_timeout_seconds,
        backup_timeout_seconds=backup_timeout_seconds,
    )
    gateway = FastAPI(title="Task 4 Mock LLM Gateway")
    gateway.state.limiter = limiter
    gateway.state.router = router

    @gateway.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _validation_exception: RequestValidationError):
        return _error(422, "INVALID_REQUEST", "Invalid completion request")

    @gateway.post("/completions")
    async def completions(body: CompletionBody, request: Request):
        tenant = _tenant_for_key(request.headers.get("x-api-key"))
        if tenant is None:
            return _error(401, "UNAUTHORIZED", "Invalid API key")
        input_tokens = estimate_input_tokens(body.prompt)
        budget = input_tokens + body.max_output_tokens
        if budget > TOKEN_LIMIT:
            return _error(422, "INVALID_REQUEST", "Token budget exceeds supported limit", body.request_id)
        try:
            decision = await asyncio.to_thread(
                limiter.reserve, tenant, budget, request_id=body.request_id
            )
        except ValueError:
            return _error(409, "DUPLICATE_REQUEST", "Request ID conflicts with an existing request", body.request_id)
        except RateLimiterStorageError:
            return _error(500, "INTERNAL_GATEWAY_ERROR", "Gateway unavailable", body.request_id)
        if not decision.allowed:
            if decision.reason == "limit_exceeded":
                return _error(429, "RATE_LIMIT_EXCEEDED", "Tenant token limit exceeded", body.request_id)
            return _error(409, "DUPLICATE_REQUEST", "Request ID is no longer available", body.request_id)

        try:
            claim = await asyncio.to_thread(
                limiter.claim_execution, tenant, decision.reservation_id,
                body.request_id, _fingerprint(body, input_tokens),
            )
        except RateLimiterStorageError:
            return _error(500, "INTERNAL_GATEWAY_ERROR", "Gateway unavailable", body.request_id)
        if claim != "claimed":
            return _error(409, "DUPLICATE_REQUEST", "Request ID is already in use", body.request_id)

        completion = CompletionRequest(
            request_id=body.request_id, prompt=body.prompt,
            input_tokens=input_tokens, max_output_tokens=body.max_output_tokens,
        )
        try:
            result = await router.route(
                completion, tenant_identifier=tenant, reservation_id=decision.reservation_id
            )
        except Exception:
            # A claimed reservation remains charged and in progress when the
            # provider's consumption cannot be established safely.
            return _error(500, "INTERNAL_GATEWAY_ERROR", "Gateway unavailable", body.request_id)
        if not result.ok:
            return _routing_error(result)
        return {
            "request_id": result.request_id,
            "provider": result.provider,
            "text": result.text,
            "usage": {
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "charged_tokens": result.charged_tokens,
            },
        }

    return gateway


app = create_app(database_path=os.environ.get("TASK4_DATABASE_PATH", DEFAULT_DATABASE_PATH))
