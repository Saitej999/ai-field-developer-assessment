"""Async HTTPX client with strict response validation and safe failures."""

from dataclasses import dataclass
from enum import Enum

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class FailureKind(str, Enum):
    RATE_LIMITED = "rate_limited"
    BAD_REQUEST = "bad_request"
    HTTP_FAILURE = "http_failure"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    INVALID_RESPONSE = "invalid_response"
    BUDGET_EXCEEDED = "budget_exceeded"


class ProviderFailure(Exception):
    """Internal classification only; public errors are built by the router."""

    def __init__(self, kind: FailureKind):
        self.kind = kind
        super().__init__(kind.value)


class ProviderUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class ProviderResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    usage: ProviderUsage


@dataclass(frozen=True)
class CompletionPayload:
    request_id: str
    prompt: str
    input_tokens: int
    max_output_tokens: int


class ProviderClient:
    """One request per short-lived AsyncClient; cancellation closes resources."""

    def __init__(
        self,
        url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.url = url
        self.transport = transport
        self.timeout_seconds = timeout_seconds

    async def complete(self, payload: CompletionPayload) -> ProviderResponse:
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=self.timeout_seconds
            ) as client:
                response = await client.post(
                    self.url,
                    json={
                        "request_id": payload.request_id,
                        "prompt": payload.prompt,
                        "input_tokens": payload.input_tokens,
                        "max_output_tokens": payload.max_output_tokens,
                    },
                )
        except httpx.TimeoutException:
            raise ProviderFailure(FailureKind.TIMEOUT) from None
        except httpx.RequestError:
            raise ProviderFailure(FailureKind.CONNECTION) from None
        if response.status_code == 429:
            raise ProviderFailure(FailureKind.RATE_LIMITED)
        if response.status_code == 400:
            raise ProviderFailure(FailureKind.BAD_REQUEST)
        if response.status_code != 200:
            raise ProviderFailure(FailureKind.HTTP_FAILURE)
        try:
            parsed = ProviderResponse.model_validate(response.json())
        except (ValueError, ValidationError):
            raise ProviderFailure(FailureKind.INVALID_RESPONSE) from None
        if parsed.request_id != payload.request_id or parsed.usage.input_tokens != payload.input_tokens:
            raise ProviderFailure(FailureKind.INVALID_RESPONSE)
        if parsed.usage.output_tokens > payload.max_output_tokens:
            raise ProviderFailure(FailureKind.BUDGET_EXCEEDED)
        return parsed
