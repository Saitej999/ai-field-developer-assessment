"""Async primary/backup routing with one conservative token reservation."""

import asyncio
from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from task4_model_router.database import RateLimiterStorageError
from task4_model_router.providers import (
    CompletionPayload,
    FailureKind,
    ProviderClient,
    ProviderFailure,
    ProviderResponse,
)
from task4_model_router.rate_limiter import SlidingWindowRateLimiter


class ErrorCode(str, Enum):
    PRIMARY_PROVIDER_FAILED = "PRIMARY_PROVIDER_FAILED"
    BACKUP_PROVIDER_FAILED = "BACKUP_PROVIDER_FAILED"
    ALL_PROVIDERS_UNAVAILABLE = "ALL_PROVIDERS_UNAVAILABLE"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    INVALID_PROVIDER_RESPONSE = "INVALID_PROVIDER_RESPONSE"
    TOKEN_BUDGET_EXCEEDED = "TOKEN_BUDGET_EXCEEDED"
    ACCOUNTING_UNAVAILABLE = "ACCOUNTING_UNAVAILABLE"


@dataclass(frozen=True)
class RoutingError:
    code: ErrorCode
    message: str
    primary_code: ErrorCode | None = None
    backup_code: ErrorCode | None = None


@dataclass(frozen=True)
class RoutingResult:
    ok: bool
    request_id: str
    reservation_id: str
    provider: str | None = None
    text: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    charged_tokens: int | None = None
    error: RoutingError | None = None


class CompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str = Field(min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=1000)
    input_tokens: int = Field(ge=0)
    max_output_tokens: int = Field(gt=0)


def _provider_code(kind: FailureKind, *, backup: bool) -> ErrorCode:
    if kind == FailureKind.TIMEOUT:
        return ErrorCode.PROVIDER_TIMEOUT
    if kind == FailureKind.INVALID_RESPONSE:
        return ErrorCode.INVALID_PROVIDER_RESPONSE
    if kind == FailureKind.BUDGET_EXCEEDED:
        return ErrorCode.TOKEN_BUDGET_EXCEEDED
    return ErrorCode.BACKUP_PROVIDER_FAILED if backup else ErrorCode.PRIMARY_PROVIDER_FAILED


def _consumption_uncertain(kind: FailureKind) -> bool:
    # HTTP 429 and HTTP 400 are known rejections; all other outcomes may have
    # consumed tokens despite lacking a usable completion response.
    return kind not in {FailureKind.RATE_LIMITED, FailureKind.BAD_REQUEST}


class ModelRouter:
    """Try primary once, then backup only for HTTP 429 or primary timeout."""

    def __init__(
        self,
        limiter: SlidingWindowRateLimiter,
        primary: ProviderClient,
        backup: ProviderClient,
        *,
        primary_timeout_seconds: float = 3.0,
        backup_timeout_seconds: float = 3.0,
    ) -> None:
        if primary_timeout_seconds <= 0 or backup_timeout_seconds <= 0:
            raise ValueError("Provider timeouts must be positive")
        self.limiter = limiter
        self.primary = primary
        self.backup = backup
        self.primary_timeout_seconds = primary_timeout_seconds
        self.backup_timeout_seconds = backup_timeout_seconds
        self._inflight_reservations: set[str] = set()

    async def _attempt(
        self, client: ProviderClient, payload: CompletionPayload, timeout: float
    ) -> ProviderResponse | ProviderFailure:
        try:
            return await asyncio.wait_for(client.complete(payload), timeout=timeout)
        except TimeoutError:
            return ProviderFailure(FailureKind.TIMEOUT)
        except ProviderFailure as failure:
            return failure
        except Exception:
            return ProviderFailure(FailureKind.HTTP_FAILURE)

    async def route(
        self,
        request: CompletionRequest,
        *,
        tenant_identifier: str,
        reservation_id: str,
    ) -> RoutingResult:
        """Route one already-reserved completion without charging fallback twice."""
        if reservation_id in self._inflight_reservations:
            return RoutingResult(
                False, request.request_id, reservation_id,
                error=RoutingError(ErrorCode.TOKEN_BUDGET_EXCEEDED, "Reservation already in use"),
            )
        self._inflight_reservations.add(reservation_id)
        try:
            return await self._route_once(
                request, tenant_identifier=tenant_identifier, reservation_id=reservation_id
            )
        finally:
            self._inflight_reservations.discard(reservation_id)

    async def _route_once(
        self,
        request: CompletionRequest,
        *,
        tenant_identifier: str,
        reservation_id: str,
    ) -> RoutingResult:
        budget = request.input_tokens + request.max_output_tokens
        try:
            snapshot = await asyncio.to_thread(
                self.limiter.inspect_reservation, tenant_identifier, reservation_id
            )
        except (RateLimiterStorageError, ValueError):
            return RoutingResult(
                False, request.request_id, reservation_id,
                error=RoutingError(ErrorCode.ACCOUNTING_UNAVAILABLE, "Token accounting unavailable"),
            )
        if (
            snapshot is None
            or snapshot.status != "active"
            or snapshot.request_id != request.request_id
            or snapshot.reserved_tokens != budget
        ):
            return RoutingResult(
                False, request.request_id, reservation_id,
                error=RoutingError(ErrorCode.TOKEN_BUDGET_EXCEEDED, "Invalid token reservation"),
            )

        payload = CompletionPayload(
            request.request_id, request.prompt, request.input_tokens, request.max_output_tokens
        )
        primary = await self._attempt(self.primary, payload, self.primary_timeout_seconds)
        if isinstance(primary, ProviderResponse):
            return await self._finish_success(
                request, tenant_identifier, reservation_id, "primary", primary,
                charge_full_budget=False,
            )

        if primary.kind not in {FailureKind.RATE_LIMITED, FailureKind.TIMEOUT}:
            if not await self._release_if_certain(tenant_identifier, reservation_id, primary.kind):
                return self._accounting_error(request.request_id, reservation_id)
            return RoutingResult(
                False, request.request_id, reservation_id,
                charged_tokens=self._charged_after_failure(budget, primary.kind),
                error=RoutingError(_provider_code(primary.kind, backup=False), "Primary provider failed"),
            )

        backup = await self._attempt(self.backup, payload, self.backup_timeout_seconds)
        if isinstance(backup, ProviderResponse):
            return await self._finish_success(
                request, tenant_identifier, reservation_id, "backup", backup,
                charge_full_budget=primary.kind == FailureKind.TIMEOUT,
            )

        if not (_consumption_uncertain(primary.kind) or _consumption_uncertain(backup.kind)):
            if not await self._release_if_certain(tenant_identifier, reservation_id, backup.kind):
                return self._accounting_error(request.request_id, reservation_id)
            charged = 0
        else:
            charged = budget
        return RoutingResult(
            False, request.request_id, reservation_id, charged_tokens=charged,
            error=RoutingError(
                ErrorCode.ALL_PROVIDERS_UNAVAILABLE,
                "No provider completed the request",
                primary_code=_provider_code(primary.kind, backup=False),
                backup_code=_provider_code(backup.kind, backup=True),
            ),
        )

    async def _release_if_certain(
        self, tenant_identifier: str, reservation_id: str, kind: FailureKind
    ) -> bool:
        if not _consumption_uncertain(kind):
            try:
                await asyncio.to_thread(self.limiter.release, tenant_identifier, reservation_id)
            except (RateLimiterStorageError, ValueError):
                return False
        return True

    @staticmethod
    def _charged_after_failure(budget: int, kind: FailureKind) -> int:
        return budget if _consumption_uncertain(kind) else 0

    @staticmethod
    def _accounting_error(request_id: str, reservation_id: str) -> RoutingResult:
        return RoutingResult(
            False, request_id, reservation_id,
            error=RoutingError(ErrorCode.ACCOUNTING_UNAVAILABLE, "Token accounting unavailable"),
        )

    async def _finish_success(
        self,
        request: CompletionRequest,
        tenant_identifier: str,
        reservation_id: str,
        provider: str,
        response: ProviderResponse,
        *,
        charge_full_budget: bool,
    ) -> RoutingResult:
        actual = response.usage.input_tokens + response.usage.output_tokens
        charged = request.input_tokens + request.max_output_tokens if charge_full_budget else actual
        try:
            await asyncio.to_thread(self.limiter.reconcile, tenant_identifier, reservation_id, charged)
        except (RateLimiterStorageError, ValueError):
            return self._accounting_error(request.request_id, reservation_id)
        return RoutingResult(
            True, request.request_id, reservation_id,
            provider=provider,
            text=response.text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            charged_tokens=charged,
        )
