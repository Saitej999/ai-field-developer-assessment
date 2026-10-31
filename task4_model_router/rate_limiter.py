"""Tenant-specific 60-second sliding-window token reservation limiter."""

import hashlib
import math
import time
from dataclasses import dataclass
from typing import Callable
from uuid import uuid4

from task4_model_router.database import SQLiteUsageStore


TOKEN_LIMIT = 50_000
WINDOW_SECONDS = 60.0


@dataclass(frozen=True)
class ReservationDecision:
    allowed: bool
    used_tokens: int
    remaining_tokens: int
    reservation_id: str | None = None
    reused: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class ReconciliationResult:
    reservation_id: str
    charged_tokens: int
    refunded_tokens: int
    already_reconciled: bool = False


@dataclass(frozen=True)
class ReservationSnapshot:
    reservation_id: str
    reserved_tokens: int
    status: str
    request_id: str | None


def _positive_tokens(value: int) -> int:
    if type(value) is not int:
        raise TypeError("Token budget must be an integer")
    if value <= 0:
        raise ValueError("Token budget must be positive")
    return value


def _actual_tokens(value: int) -> int:
    if type(value) is not int:
        raise TypeError("Actual token usage must be an integer")
    if value < 0:
        raise ValueError("Actual token usage cannot be negative")
    return value


def _tenant_hash(tenant_identifier: str) -> str:
    if not isinstance(tenant_identifier, str) or not tenant_identifier.strip():
        raise ValueError("A nonempty tenant identifier is required")
    return hashlib.sha256(tenant_identifier.encode("utf-8")).hexdigest()


class SlidingWindowRateLimiter:
    """Reserve before provider use; reconcile or release the same reservation."""

    def __init__(
        self,
        database_path: str,
        *,
        clock: Callable[[], float] = time.time,
        busy_timeout_seconds: float = 5.0,
    ) -> None:
        self.store = SQLiteUsageStore(database_path, busy_timeout_seconds=busy_timeout_seconds)
        self.clock = clock

    def inspect_reservation(
        self, tenant_identifier: str, reservation_id: str
    ) -> ReservationSnapshot | None:
        """Read a current tenant-owned reservation for router budget checks."""
        tenant = _tenant_hash(tenant_identifier)
        cutoff = self._now() - WINDOW_SECONDS
        with self.store.transaction() as connection:
            self.store.cleanup_expired(connection, cutoff)
            row = self.store.get_by_reservation_id(connection, tenant, reservation_id)
            if row is None:
                return None
            return ReservationSnapshot(
                row["reservation_id"], row["reserved_tokens"], row["status"], row["request_id"]
            )

    def claim_execution(
        self,
        tenant_identifier: str,
        reservation_id: str,
        request_id: str,
        request_fingerprint: str,
    ) -> str:
        """Atomically claim an active request across gateway processes.

        In-progress claims persist after a process crash; callers must not
        retry an uncertain provider operation automatically.
        """
        tenant = _tenant_hash(tenant_identifier)
        cutoff = self._now() - WINDOW_SECONDS
        with self.store.transaction() as connection:
            self.store.cleanup_expired(connection, cutoff)
            row = self.store.get_by_reservation_id(connection, tenant, reservation_id)
            if row is None:
                return "missing"
            if row["request_id"] != request_id:
                return "conflict"
            if row["request_fingerprint"] is not None and row["request_fingerprint"] != request_fingerprint:
                return "conflict"
            if row["status"] != "active":
                return "completed"
            if row["execution_state"] == "in_progress":
                return "in_progress"
            connection.execute(
                "UPDATE token_reservations SET execution_state = 'in_progress', "
                "request_fingerprint = ? WHERE reservation_id = ?",
                (request_fingerprint, reservation_id),
            )
            return "claimed"

    def _now(self) -> float:
        now = self.clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            raise ValueError("Clock must return a finite timestamp")
        return float(now)

    def reserve(
        self, tenant_identifier: str, token_budget: int, *, request_id: str | None = None
    ) -> ReservationDecision:
        """Atomically charge a positive input-plus-output budget to one tenant.

        A repeated request_id inside the window returns its original reservation
        without charging again. Zero-token requests are invalid.
        """
        tenant = _tenant_hash(tenant_identifier)
        budget = _positive_tokens(token_budget)
        if request_id is not None and (
            not isinstance(request_id, str) or not request_id or len(request_id) > 128
        ):
            raise ValueError("request_id must contain 1 to 128 characters")
        now = self._now()
        cutoff = now - WINDOW_SECONDS
        with self.store.transaction() as connection:
            self.store.cleanup_expired(connection, cutoff)
            used = self.store.usage_for_tenant(connection, tenant, cutoff)
            if request_id is not None:
                existing = self.store.get_by_request_id(connection, tenant, request_id)
                if existing is not None:
                    if existing["reserved_tokens"] != budget:
                        raise ValueError("request_id already uses a different token budget")
                    return ReservationDecision(
                        allowed=existing["status"] != "cancelled",
                        used_tokens=used,
                        remaining_tokens=TOKEN_LIMIT - used,
                        reservation_id=existing["reservation_id"],
                        reused=True,
                        reason="cancelled_request" if existing["status"] == "cancelled" else None,
                    )
            if used + budget > TOKEN_LIMIT:
                return ReservationDecision(
                    allowed=False,
                    used_tokens=used,
                    remaining_tokens=TOKEN_LIMIT - used,
                    reason="limit_exceeded",
                )
            reservation_id = uuid4().hex
            connection.execute(
                "INSERT INTO token_reservations "
                "(reservation_id, tenant_hash, request_id, created_at, reserved_tokens, "
                "usage_tokens, actual_tokens, status) VALUES (?, ?, ?, ?, ?, ?, NULL, 'active')",
                (reservation_id, tenant, request_id, now, budget, budget),
            )
            used += budget
            return ReservationDecision(True, used, TOKEN_LIMIT - used, reservation_id)

    def reconcile(
        self, tenant_identifier: str, reservation_id: str, actual_tokens: int
    ) -> ReconciliationResult:
        """Replace reserved usage with actual usage, never above the budget.

        Provider output must be capped by the caller at the reserved budget.
        Over-budget reports raise and leave the original charge in place.
        """
        tenant = _tenant_hash(tenant_identifier)
        actual = _actual_tokens(actual_tokens)
        now = self._now()
        with self.store.transaction() as connection:
            self.store.cleanup_expired(connection, now - WINDOW_SECONDS)
            row = self.store.get_by_reservation_id(connection, tenant, reservation_id)
            if row is None or row["status"] == "cancelled":
                raise ValueError("Unknown or cancelled reservation")
            if actual > row["reserved_tokens"]:
                raise ValueError("Actual usage exceeds reserved budget")
            already = row["status"] == "settled"
            if already:
                if row["actual_tokens"] != actual:
                    raise ValueError("Reservation already reconciled with different usage")
            else:
                connection.execute(
                    "UPDATE token_reservations SET usage_tokens = ?, actual_tokens = ?, "
                    "status = 'settled' WHERE reservation_id = ?",
                    (actual, actual, reservation_id),
                )
            return ReconciliationResult(
                reservation_id,
                actual,
                row["reserved_tokens"] - actual,
                already_reconciled=already,
            )

    def release(self, tenant_identifier: str, reservation_id: str) -> bool:
        """Release a reservation after a failed request with zero token use.

        Repeated releases are harmless; settled usage cannot be erased.
        Keep a reservation across fallback attempts and release only when all
        attempts fail without consuming tokens.
        """
        tenant = _tenant_hash(tenant_identifier)
        now = self._now()
        with self.store.transaction() as connection:
            self.store.cleanup_expired(connection, now - WINDOW_SECONDS)
            row = self.store.get_by_reservation_id(connection, tenant, reservation_id)
            if row is None:
                raise ValueError("Unknown reservation")
            if row["status"] == "cancelled":
                return False
            if row["status"] == "settled":
                raise ValueError("Cannot release reconciled usage")
            connection.execute(
                "UPDATE token_reservations SET usage_tokens = 0, status = 'cancelled' "
                "WHERE reservation_id = ?",
                (reservation_id,),
            )
            return True

    def usage(self, tenant_identifier: str) -> int:
        """Current charged tokens, after deleting expired records."""
        tenant = _tenant_hash(tenant_identifier)
        cutoff = self._now() - WINDOW_SECONDS
        with self.store.transaction() as connection:
            self.store.cleanup_expired(connection, cutoff)
            return self.store.usage_for_tenant(connection, tenant, cutoff)
