"""Task 4 Stage 1 on-disk, token-aware sliding-window accounting."""

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from task4_model_router.database import RateLimiterStorageError
from task4_model_router.rate_limiter import SlidingWindowRateLimiter


class Clock:
    def __init__(self, now=1_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def setup_limiter(tmp_path):
    clock = Clock()
    path = tmp_path / "usage.sqlite3"
    return SlidingWindowRateLimiter(path, clock=clock), path, clock


def test_first_request_and_sequential_requests(setup_limiter):
    limiter, _, _ = setup_limiter
    decisions = [limiter.reserve("tenant-a", amount) for amount in (15_000, 20_000, 10_000)]
    assert all(result.allowed and result.reservation_id for result in decisions)
    assert [result.remaining_tokens for result in decisions] == [35_000, 15_000, 5_000]
    rejected = limiter.reserve("tenant-a", 10_000)
    assert not rejected.allowed
    assert rejected.reason == "limit_exceeded"
    assert rejected.remaining_tokens == 5_000
    assert limiter.usage("tenant-a") == 45_000


def test_exact_limit_and_one_over(setup_limiter):
    limiter, _, _ = setup_limiter
    assert limiter.reserve("tenant-a", 50_000).remaining_tokens == 0
    result = limiter.reserve("tenant-a", 1)
    assert not result.allowed and result.remaining_tokens == 0
    assert limiter.usage("tenant-a") == 50_000


def test_separate_tenants_and_hashed_identifiers(setup_limiter):
    limiter, path, _ = setup_limiter
    assert limiter.reserve("tenant-a", 50_000).allowed
    assert limiter.reserve("tenant-b", 50_000).allowed
    assert not limiter.reserve("tenant-a", 1).allowed
    with sqlite3.connect(path) as connection:
        hashes = {row[0] for row in connection.execute("SELECT tenant_hash FROM token_reservations")}
    assert hashes == {
        hashlib.sha256(name.encode()).hexdigest() for name in ("tenant-a", "tenant-b")
    }


def test_exact_sixty_second_expiry_and_cleanup(setup_limiter):
    limiter, path, clock = setup_limiter
    assert limiter.reserve("tenant-a", 50_000).allowed
    clock.now += 59.999
    assert not limiter.reserve("tenant-a", 1).allowed
    clock.now += 0.001
    assert limiter.reserve("tenant-a", 50_000).allowed
    with sqlite3.connect(path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM token_reservations").fetchone()[0]
    assert count == 1


def test_sliding_window_across_minute_boundary(setup_limiter):
    limiter, _, clock = setup_limiter
    clock.now = 3_659.0
    assert limiter.reserve("tenant-a", 30_000).allowed
    clock.now = 3_661.0
    assert limiter.reserve("tenant-a", 20_000).allowed
    assert not limiter.reserve("tenant-a", 1).allowed
    clock.now = 3_719.0
    assert limiter.usage("tenant-a") == 20_000
    assert limiter.reserve("tenant-a", 30_000).allowed


@pytest.mark.parametrize("value", [0, -1, -50_000])
def test_zero_and_negative_token_budgets_are_rejected(setup_limiter, value):
    limiter, _, _ = setup_limiter
    with pytest.raises(ValueError):
        limiter.reserve("tenant-a", value)
    assert limiter.usage("tenant-a") == 0


@pytest.mark.parametrize("value", [1.5, "10", None, True])
def test_non_integer_token_budgets_are_rejected(setup_limiter, value):
    limiter, _, _ = setup_limiter
    with pytest.raises(TypeError):
        limiter.reserve("tenant-a", value)
    assert limiter.usage("tenant-a") == 0


@pytest.mark.parametrize("tenant", ["", "   ", None, 123])
def test_invalid_tenant_identifiers_are_rejected(setup_limiter, tenant):
    limiter, _, _ = setup_limiter
    with pytest.raises(ValueError):
        limiter.reserve(tenant, 1)


def test_two_simultaneous_reservations_cannot_exceed_limit(setup_limiter):
    limiter, path, clock = setup_limiter
    assert limiter.reserve("tenant-a", 45_000).allowed
    other_instance = SlidingWindowRateLimiter(path, clock=clock)
    barrier = Barrier(2)

    def attempt(instance):
        barrier.wait(timeout=5)
        return instance.reserve("tenant-a", 4_000)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt, instance) for instance in (limiter, other_instance)]
        decisions = [future.result(timeout=10) for future in futures]
    assert sorted(result.allowed for result in decisions) == [False, True]
    assert limiter.usage("tenant-a") == 49_000


def test_persistence_across_instances_and_connections(setup_limiter):
    limiter, path, clock = setup_limiter
    assert limiter.reserve("tenant-a", 12_345).allowed
    new_instance = SlidingWindowRateLimiter(path, clock=clock)
    assert new_instance.usage("tenant-a") == 12_345
    assert new_instance.reserve("tenant-a", 37_655).allowed
    assert not limiter.reserve("tenant-a", 1).allowed


def test_denials_and_invalid_requests_do_not_change_usage(setup_limiter):
    limiter, _, _ = setup_limiter
    assert limiter.reserve("tenant-a", 40_000).allowed
    assert not limiter.reserve("tenant-a", 20_000).allowed
    with pytest.raises(ValueError):
        limiter.reserve("tenant-a", -1)
    assert limiter.usage("tenant-a") == 40_000
    assert limiter.reserve("tenant-a", 10_000).allowed


def test_reconciliation_refunds_unused_budget_and_is_idempotent(setup_limiter):
    limiter, _, _ = setup_limiter
    reservation = limiter.reserve("tenant-a", 12_000, request_id="completion-1")
    result = limiter.reconcile("tenant-a", reservation.reservation_id, 8_000)
    assert result.charged_tokens == 8_000
    assert result.refunded_tokens == 4_000
    assert not result.already_reconciled
    replay = limiter.reconcile("tenant-a", reservation.reservation_id, 8_000)
    assert replay.already_reconciled
    assert limiter.usage("tenant-a") == 8_000
    with pytest.raises(ValueError):
        limiter.reconcile("tenant-a", reservation.reservation_id, 9_000)
    assert limiter.usage("tenant-a") == 8_000


def test_actual_above_reservation_is_rejected_without_silent_undercharge(setup_limiter):
    limiter, _, _ = setup_limiter
    reservation = limiter.reserve("tenant-a", 5_000)
    with pytest.raises(ValueError, match="exceeds reserved"):
        limiter.reconcile("tenant-a", reservation.reservation_id, 5_001)
    assert limiter.usage("tenant-a") == 5_000


def test_zero_actual_usage_and_release_after_failure(setup_limiter):
    limiter, _, _ = setup_limiter
    zero = limiter.reserve("tenant-a", 1_000)
    assert limiter.reconcile("tenant-a", zero.reservation_id, 0).charged_tokens == 0
    failed = limiter.reserve("tenant-a", 3_000)
    assert limiter.release("tenant-a", failed.reservation_id)
    assert not limiter.release("tenant-a", failed.reservation_id)
    assert limiter.usage("tenant-a") == 0
    with pytest.raises(ValueError):
        limiter.release("tenant-a", zero.reservation_id)


def test_request_id_retries_and_fallback_share_one_reservation(setup_limiter):
    limiter, _, _ = setup_limiter
    original = limiter.reserve("tenant-a", 20_000, request_id="completion-1")
    repeated = limiter.reserve("tenant-a", 20_000, request_id="completion-1")
    assert repeated.allowed and repeated.reused
    assert repeated.reservation_id == original.reservation_id
    assert limiter.usage("tenant-a") == 20_000
    with pytest.raises(ValueError):
        limiter.reserve("tenant-a", 21_000, request_id="completion-1")
    limiter.reconcile("tenant-a", original.reservation_id, 15_000)
    assert limiter.reserve("tenant-a", 20_000, request_id="completion-1").reused
    assert limiter.usage("tenant-a") == 15_000


def test_cancelled_request_id_cannot_be_reused_inside_window(setup_limiter):
    limiter, _, _ = setup_limiter
    original = limiter.reserve("tenant-a", 2_000, request_id="completion-1")
    limiter.release("tenant-a", original.reservation_id)
    replay = limiter.reserve("tenant-a", 2_000, request_id="completion-1")
    assert not replay.allowed and replay.reason == "cancelled_request"
    assert limiter.usage("tenant-a") == 0


def test_cross_tenant_reconciliation_and_release_are_blocked(setup_limiter):
    limiter, _, _ = setup_limiter
    original = limiter.reserve("tenant-a", 2_000)
    with pytest.raises(ValueError):
        limiter.reconcile("tenant-b", original.reservation_id, 1_000)
    with pytest.raises(ValueError):
        limiter.release("tenant-b", original.reservation_id)
    assert limiter.usage("tenant-a") == 2_000


def test_storage_errors_are_generic_and_fail_closed(tmp_path):
    path = tmp_path / "usage.sqlite3"
    limiter = SlidingWindowRateLimiter(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE token_reservations")
    with pytest.raises(RateLimiterStorageError, match="storage unavailable") as error:
        limiter.reserve("tenant-a", 1)
    assert str(path) not in str(error.value)


def test_failed_insert_rolls_back_expiry_cleanup(setup_limiter):
    limiter, path, clock = setup_limiter
    assert limiter.reserve("tenant-a", 100).allowed
    clock.now += 60
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TRIGGER reject_insert BEFORE INSERT ON token_reservations
               BEGIN SELECT RAISE(ABORT, 'private synthetic storage error'); END"""
        )
    with pytest.raises(RateLimiterStorageError, match="storage unavailable") as error:
        limiter.reserve("tenant-a", 1)
    assert "private synthetic" not in str(error.value)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM token_reservations").fetchone()[0] == 1
        connection.execute("DROP TRIGGER reject_insert")
    assert limiter.reserve("tenant-a", 1).allowed
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM token_reservations").fetchone()[0] == 1


def test_missing_database_directory_is_reported_safely(tmp_path):
    with pytest.raises(RateLimiterStorageError, match="storage unavailable"):
        SlidingWindowRateLimiter(tmp_path / "missing" / "usage.sqlite3")


def test_database_is_on_disk_and_wal_enabled(setup_limiter):
    _, path, _ = setup_limiter
    assert path.is_file()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
