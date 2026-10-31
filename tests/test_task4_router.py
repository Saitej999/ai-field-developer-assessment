"""Task 4 Stage 2 provider fallback and conservative token accounting."""

import asyncio
import json

import httpx
import pytest

from task4_model_router.mock_providers import create_mock_provider
from task4_model_router.providers import FailureKind, ProviderClient, ProviderFailure
from task4_model_router.rate_limiter import SlidingWindowRateLimiter
from task4_model_router.router import CompletionRequest, ErrorCode, ModelRouter


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def setup_router(tmp_path):
    limiter = SlidingWindowRateLimiter(tmp_path / "usage.sqlite3")

    def build(primary_scenario="success", backup_scenario="success", *, timeout=0.02, primary_delay=0.1, backup_delay=0.1):
        primary_app = create_mock_provider("primary", scenario=primary_scenario, delay_seconds=primary_delay)
        backup_app = create_mock_provider("backup", scenario=backup_scenario, delay_seconds=backup_delay)
        router = ModelRouter(
            limiter,
            ProviderClient("http://primary/complete", transport=httpx.ASGITransport(app=primary_app)),
            ProviderClient("http://backup/complete", transport=httpx.ASGITransport(app=backup_app)),
            primary_timeout_seconds=timeout,
            backup_timeout_seconds=timeout,
        )
        return router, primary_app, backup_app

    return limiter, build


def request(request_id="req-1", input_tokens=10, max_output_tokens=20):
    return CompletionRequest(
        request_id=request_id, prompt="Synthetic prompt", input_tokens=input_tokens,
        max_output_tokens=max_output_tokens,
    )


def test_default_primary_timeout_is_3000_milliseconds(setup_router):
    limiter, _ = setup_router
    router = ModelRouter(
        limiter,
        ProviderClient("http://primary/complete"),
        ProviderClient("http://backup/complete"),
    )
    assert router.primary_timeout_seconds == 3.0
    assert router.backup_timeout_seconds == 3.0


async def route_once(limiter, router, completion=None, tenant="tenant-a"):
    completion = completion or request()
    reservation = limiter.reserve(
        tenant, completion.input_tokens + completion.max_output_tokens,
        request_id=completion.request_id,
    )
    assert reservation.allowed
    result = await router.route(
        completion, tenant_identifier=tenant, reservation_id=reservation.reservation_id
    )
    return result, reservation


@pytest.mark.anyio
async def test_primary_success_uses_no_backup_and_reconciles_actual(setup_router):
    limiter, build = setup_router
    router, primary, backup = build()
    assert router.primary_timeout_seconds == 0.02
    result, reservation = await route_once(limiter, router)
    assert result.ok and result.provider == "primary"
    assert result.request_id == "req-1" and result.reservation_id == reservation.reservation_id
    assert result.text == "Synthetic primary completion."
    assert (result.input_tokens, result.output_tokens, result.charged_tokens) == (10, 5, 15)
    assert limiter.usage("tenant-a") == 15
    assert primary.state.calls == 1 and backup.state.calls == 0


@pytest.mark.anyio
async def test_primary_429_falls_back_with_one_reservation_and_actual_usage(setup_router):
    limiter, build = setup_router
    router, primary, backup = build("429", "success")
    result, reservation = await route_once(limiter, router)
    assert result.ok and result.provider == "backup"
    assert result.reservation_id == reservation.reservation_id
    assert result.charged_tokens == 15 and limiter.usage("tenant-a") == 15
    assert primary.state.calls == backup.state.calls == 1


@pytest.mark.anyio
async def test_primary_timeout_falls_back_and_keeps_full_quota_charge(setup_router):
    limiter, build = setup_router
    router, primary, backup = build("slow", "success")
    result, reservation = await route_once(limiter, router)
    assert result.ok and result.provider == "backup"
    assert result.charged_tokens == 30 and limiter.usage("tenant-a") == 30
    assert result.output_tokens == 5
    assert primary.state.calls == backup.state.calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["400", "500"])
async def test_primary_non_fallback_http_errors_do_not_contact_backup(setup_router, status):
    limiter, build = setup_router
    router, primary, backup = build(status, "success")
    result, _ = await route_once(limiter, router)
    assert not result.ok and result.error.code == ErrorCode.PRIMARY_PROVIDER_FAILED
    assert primary.state.calls == 1 and backup.state.calls == 0
    assert limiter.usage("tenant-a") == (0 if status == "400" else 30)


@pytest.mark.anyio
@pytest.mark.parametrize("backup_scenario", ["429", "slow", "500", "malformed"])
async def test_backup_failures_are_controlled_and_accounted(setup_router, backup_scenario):
    limiter, build = setup_router
    router, primary, backup = build("429", backup_scenario)
    result, _ = await route_once(limiter, router)
    assert not result.ok
    assert result.error.code == ErrorCode.ALL_PROVIDERS_UNAVAILABLE
    assert result.error.primary_code == ErrorCode.PRIMARY_PROVIDER_FAILED
    assert result.error.backup_code in {
        ErrorCode.BACKUP_PROVIDER_FAILED,
        ErrorCode.PROVIDER_TIMEOUT,
        ErrorCode.INVALID_PROVIDER_RESPONSE,
    }
    assert primary.state.calls == backup.state.calls == 1
    assert limiter.usage("tenant-a") == (0 if backup_scenario == "429" else 30)
    assert "http://" not in result.error.message
    assert "Synthetic prompt" not in result.error.message


@pytest.mark.anyio
async def test_primary_timeout_and_backup_failure_preserve_full_charge(setup_router):
    limiter, build = setup_router
    router, _, _ = build("slow", "429")
    result, _ = await route_once(limiter, router)
    assert not result.ok and result.error.code == ErrorCode.ALL_PROVIDERS_UNAVAILABLE
    assert result.error.primary_code == ErrorCode.PROVIDER_TIMEOUT
    assert limiter.usage("tenant-a") == 30


@pytest.mark.anyio
async def test_primary_malformed_response_has_no_fallback(setup_router):
    limiter, build = setup_router
    router, _, backup = build("malformed", "success")
    result, _ = await route_once(limiter, router)
    assert not result.ok and result.error.code == ErrorCode.INVALID_PROVIDER_RESPONSE
    assert backup.state.calls == 0
    assert limiter.usage("tenant-a") == 30


@pytest.mark.anyio
async def test_output_cap_passed_to_both_providers_and_enforced(setup_router):
    limiter, _ = setup_router
    seen = []

    def handler(response_status):
        def respond(http_request):
            payload = json.loads(http_request.content)
            seen.append(payload)
            if response_status == 429:
                return httpx.Response(429, json={"error": "synthetic"})
            return httpx.Response(200, json={
                "request_id": payload["request_id"], "text": "Synthetic output",
                "usage": {"input_tokens": payload["input_tokens"], "output_tokens": 6},
            })
        return respond

    router = ModelRouter(
        limiter,
        ProviderClient("http://primary/complete", transport=httpx.MockTransport(handler(429))),
        ProviderClient("http://backup/complete", transport=httpx.MockTransport(handler(200))),
    )
    completion = request(max_output_tokens=5)
    result, _ = await route_once(limiter, router, completion)
    assert not result.ok and result.error.code == ErrorCode.ALL_PROVIDERS_UNAVAILABLE
    assert result.error.backup_code == ErrorCode.TOKEN_BUDGET_EXCEEDED
    assert len(seen) == 2
    assert all(item["max_output_tokens"] == 5 and item["request_id"] == "req-1" for item in seen)
    assert limiter.usage("tenant-a") == 15


@pytest.mark.anyio
async def test_mock_provider_caps_output_tokens(setup_router):
    limiter, build = setup_router
    router, _, _ = build()
    result, _ = await route_once(limiter, router, request(max_output_tokens=2))
    assert result.ok and result.output_tokens == 2
    assert limiter.usage("tenant-a") == 12


@pytest.mark.anyio
async def test_connection_and_read_timeout_errors_are_safe(setup_router):
    limiter, _ = setup_router
    for exception_type, expected in [
        (httpx.ConnectError, ErrorCode.PRIMARY_PROVIDER_FAILED),
        (httpx.ReadTimeout, ErrorCode.ALL_PROVIDERS_UNAVAILABLE),
    ]:
        def broken(http_request):
            raise exception_type("private connection detail", request=http_request)

        backup_app = create_mock_provider("backup")
        router = ModelRouter(
            limiter,
            ProviderClient("http://private.internal/complete", transport=httpx.MockTransport(broken)),
            ProviderClient("http://backup/complete", transport=httpx.ASGITransport(app=backup_app)),
        )
        completion = request(request_id=exception_type.__name__)
        result, _ = await route_once(limiter, router, completion)
        if exception_type is httpx.ConnectError:
            assert not result.ok and result.error.code == expected
            assert backup_app.state.calls == 0
        else:
            assert result.ok and result.provider == "backup"
            assert limiter.usage("tenant-a") >= 30
        assert "private" not in str(result.error) if result.error else True


@pytest.mark.anyio
async def test_timeout_cancels_primary_before_backup_begins(setup_router):
    limiter, _ = setup_router
    cancelled = asyncio.Event()
    backup_called = asyncio.Event()

    async def stalled(_):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def backup_handler(http_request):
        assert cancelled.is_set()
        backup_called.set()
        payload = json.loads(http_request.content)
        return httpx.Response(200, json={
            "request_id": payload["request_id"], "text": "Synthetic backup output",
            "usage": {"input_tokens": 10, "output_tokens": 1},
        })

    router = ModelRouter(
        limiter,
        ProviderClient("http://primary/complete", transport=httpx.MockTransport(stalled)),
        ProviderClient("http://backup/complete", transport=httpx.MockTransport(backup_handler)),
        primary_timeout_seconds=0.01,
    )
    result, _ = await route_once(limiter, router)
    assert result.ok and result.provider == "backup"
    assert cancelled.is_set() and backup_called.is_set()


@pytest.mark.anyio
async def test_invalid_reservation_never_contacts_providers(setup_router):
    limiter, build = setup_router
    router, primary, backup = build()
    completion = request()
    bad = await router.route(completion, tenant_identifier="tenant-a", reservation_id="missing")
    assert not bad.ok and bad.error.code == ErrorCode.TOKEN_BUDGET_EXCEEDED
    original = limiter.reserve("tenant-a", 30, request_id="req-1")
    mismatch = await router.route(request(max_output_tokens=21), tenant_identifier="tenant-a", reservation_id=original.reservation_id)
    assert not mismatch.ok and mismatch.error.code == ErrorCode.TOKEN_BUDGET_EXCEEDED
    assert primary.state.calls == backup.state.calls == 0


@pytest.mark.anyio
async def test_concurrent_requests_use_independent_reservations(setup_router):
    limiter, build = setup_router
    router, primary, backup = build("429", "success")
    requests = [request(request_id=f"req-{index}", input_tokens=100, max_output_tokens=400) for index in range(20)]
    reservations = [limiter.reserve("tenant-a", 500, request_id=item.request_id) for item in requests]
    results = await asyncio.gather(*(
        router.route(item, tenant_identifier="tenant-a", reservation_id=reserved.reservation_id)
        for item, reserved in zip(requests, reservations)
    ))
    assert all(result.ok and result.provider == "backup" for result in results)
    assert limiter.usage("tenant-a") == 20 * 105
    assert primary.state.calls == backup.state.calls == 20


@pytest.mark.anyio
async def test_reusing_settled_reservation_does_not_reexecute(setup_router):
    limiter, build = setup_router
    router, primary, backup = build()
    result, reservation = await route_once(limiter, router)
    repeated = await router.route(request(), tenant_identifier="tenant-a", reservation_id=reservation.reservation_id)
    assert result.ok and not repeated.ok
    assert primary.state.calls == 1 and backup.state.calls == 0
    assert limiter.usage("tenant-a") == 15


@pytest.mark.anyio
async def test_same_active_reservation_cannot_run_overlapping_attempts(setup_router):
    limiter, _ = setup_router
    started = asyncio.Event()
    finish = asyncio.Event()
    calls = 0

    async def primary_handler(http_request):
        nonlocal calls
        calls += 1
        started.set()
        await finish.wait()
        payload = json.loads(http_request.content)
        return httpx.Response(200, json={
            "request_id": payload["request_id"], "text": "Synthetic result",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })

    router = ModelRouter(
        limiter,
        ProviderClient("http://primary/complete", transport=httpx.MockTransport(primary_handler)),
        ProviderClient("http://backup/complete", transport=httpx.MockTransport(lambda _: httpx.Response(500))),
    )
    completion = request()
    reservation = limiter.reserve("tenant-a", 30, request_id=completion.request_id)
    first = asyncio.create_task(router.route(
        completion, tenant_identifier="tenant-a", reservation_id=reservation.reservation_id
    ))
    await asyncio.wait_for(started.wait(), timeout=1)
    duplicate = await router.route(
        completion, tenant_identifier="tenant-a", reservation_id=reservation.reservation_id
    )
    assert not duplicate.ok and duplicate.error.code == ErrorCode.TOKEN_BUDGET_EXCEEDED
    assert calls == 1
    finish.set()
    result = await asyncio.wait_for(first, timeout=1)
    assert result.ok and limiter.usage("tenant-a") == 15
