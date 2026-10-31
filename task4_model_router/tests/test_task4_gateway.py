"""Task 4 Stage 3 FastAPI integration and SQLite request coordination."""

import asyncio
import json
import sqlite3

import httpx
import pytest

from task4_model_router.gateway import _tenant_for_key, create_app, estimate_input_tokens
from task4_model_router.mock_providers import create_mock_provider


@pytest.fixture
def anyio_backend():
    return "asyncio"


def payload(request_id="req-1", prompt="Synthetic prompt", max_output_tokens=20):
    return {"request_id": request_id, "prompt": prompt, "max_output_tokens": max_output_tokens}


def build(tmp_path, *, primary="success", backup="success", timeout=0.02):
    primary_app = create_mock_provider("primary", scenario=primary, delay_seconds=0.1)
    backup_app = create_mock_provider("backup", scenario=backup, delay_seconds=0.1)
    database_path = tmp_path / "usage.sqlite3"
    gateway = create_app(
        database_path=database_path,
        primary_url="http://primary/complete",
        backup_url="http://backup/complete",
        primary_transport=httpx.ASGITransport(app=primary_app),
        backup_transport=httpx.ASGITransport(app=backup_app),
        primary_timeout_seconds=timeout,
        backup_timeout_seconds=timeout,
    )
    return gateway, primary_app, backup_app, database_path


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway")


async def post(client, body=None, key="tenant-a-demo-key"):
    headers = {} if key is None else {"X-API-Key": key}
    return await client.post("/completions", json=body or payload(), headers=headers)


@pytest.mark.anyio
async def test_primary_success_and_reconciliation(tmp_path):
    app, primary, backup, _ = build(tmp_path)
    async with client_for(app) as client:
        response = await post(client)
    assert response.status_code == 200
    data = response.json()
    assert data["request_id"] == "req-1" and data["provider"] == "primary"
    assert data["text"] == "Synthetic primary completion."
    assert data["usage"] == {"input_tokens": 4, "output_tokens": 5, "charged_tokens": 9}
    assert app.state.limiter.usage("tenant-a") == 9
    assert primary.state.calls == 1 and backup.state.calls == 0


@pytest.mark.anyio
async def test_primary_429_falls_back_without_double_charge(tmp_path):
    app, primary, backup, _ = build(tmp_path, primary="429")
    async with client_for(app) as client:
        response = await post(client)
    assert response.status_code == 200 and response.json()["provider"] == "backup"
    assert response.json()["usage"]["charged_tokens"] == 9
    assert app.state.limiter.usage("tenant-a") == 9
    assert primary.state.calls == backup.state.calls == 1


@pytest.mark.anyio
async def test_primary_timeout_falls_back_with_conservative_charge(tmp_path):
    app, primary, backup, _ = build(tmp_path, primary="slow")
    async with client_for(app) as client:
        response = await post(client)
    assert response.status_code == 200 and response.json()["provider"] == "backup"
    assert response.json()["usage"]["charged_tokens"] == 24
    assert app.state.limiter.usage("tenant-a") == 24
    assert primary.state.calls == backup.state.calls == 1


@pytest.mark.anyio
async def test_quota_exhaustion_returns_429_before_any_provider_call(tmp_path):
    app, primary, backup, _ = build(tmp_path, primary="429")
    assert app.state.limiter.reserve("tenant-a", 50_000).allowed
    async with client_for(app) as client:
        response = await post(client)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert primary.state.calls == backup.state.calls == 0


@pytest.mark.anyio
async def test_exactly_50000_budget_is_allowed_and_over_limit_is_invalid(tmp_path):
    app, primary, _, _ = build(tmp_path)
    async with client_for(app) as client:
        exact = await post(client, payload(max_output_tokens=49_999, prompt="a"))
        over = await post(client, payload(request_id="req-2", max_output_tokens=50_000, prompt="a"))
    assert exact.status_code == 200
    assert exact.json()["usage"]["charged_tokens"] == 6
    assert over.status_code == 422 and over.json()["error"]["code"] == "INVALID_REQUEST"
    assert primary.state.calls == 1


@pytest.mark.anyio
async def test_independent_tenant_quotas(tmp_path):
    app, primary, _, _ = build(tmp_path)
    assert app.state.limiter.reserve("tenant-a", 50_000).allowed
    async with client_for(app) as client:
        denied = await post(client, key="tenant-a-demo-key")
        allowed = await post(client, key="tenant-b-demo-key")
    assert denied.status_code == 429 and allowed.status_code == 200
    assert primary.state.calls == 1
    assert app.state.limiter.usage("tenant-b") == 9


@pytest.mark.anyio
async def test_same_request_id_is_scoped_by_tenant(tmp_path):
    app, primary, backup, _ = build(tmp_path)
    async with client_for(app) as client:
        tenant_a = await post(client, key="tenant-a-demo-key")
        tenant_b = await post(client, key="tenant-b-demo-key")
    assert tenant_a.status_code == tenant_b.status_code == 200
    assert primary.state.calls == 2 and backup.state.calls == 0
    assert app.state.limiter.usage("tenant-a") == 9
    assert app.state.limiter.usage("tenant-b") == 9


@pytest.mark.anyio
@pytest.mark.parametrize("key", [None, "unknown-key", "tenant-a-demo-key-forged"])
async def test_missing_and_invalid_api_keys_are_rejected(tmp_path, key):
    app, primary, backup, _ = build(tmp_path)
    async with client_for(app) as client:
        response = await post(client, key=key)
    assert response.status_code == 401 and response.json()["error"]["code"] == "UNAUTHORIZED"
    assert key not in response.text if key else True
    assert primary.state.calls == backup.state.calls == 0


@pytest.mark.anyio
@pytest.mark.parametrize("body", [
    {"request_id": "bad id", "prompt": "synthetic", "max_output_tokens": 10},
    {"request_id": "req-1", "prompt": "   ", "max_output_tokens": 10},
    {"request_id": "req-1", "prompt": 123, "max_output_tokens": 10},
    {"request_id": "req-1", "prompt": "synthetic", "max_output_tokens": 0},
    {"request_id": "req-1", "prompt": "synthetic", "max_output_tokens": 1.5},
    {"request_id": "req-1", "prompt": "synthetic", "max_output_tokens": 10, "input_tokens": 1},
])
async def test_malformed_requests_return_generic_422(tmp_path, body):
    app, primary, backup, _ = build(tmp_path)
    async with client_for(app) as client:
        response = await post(client, body)
    assert response.status_code == 422
    assert response.json() == {"request_id": None, "error": {"code": "INVALID_REQUEST", "message": "Invalid completion request"}}
    assert primary.state.calls == backup.state.calls == 0


def test_deterministic_mock_token_estimate():
    assert estimate_input_tokens("a") == 1
    assert estimate_input_tokens("abcde") == 2
    assert estimate_input_tokens("😀") == 1


def test_non_ascii_demo_api_key_is_rejected_safely():
    assert _tenant_for_key("é-not-a-demo-key") is None


@pytest.mark.anyio
async def test_completed_and_conflicting_request_ids_are_rejected(tmp_path):
    app, primary, backup, _ = build(tmp_path)
    async with client_for(app) as client:
        first = await post(client)
        repeat = await post(client)
        conflict = await post(client, payload(prompt="Different prompt", max_output_tokens=20))
    assert first.status_code == 200
    assert repeat.status_code == conflict.status_code == 409
    assert repeat.json()["error"]["code"] == "DUPLICATE_REQUEST"
    assert conflict.json()["error"]["code"] == "DUPLICATE_REQUEST"
    assert primary.state.calls == 1 and backup.state.calls == 0


@pytest.mark.anyio
async def test_concurrent_requests_cannot_exceed_tenant_quota(tmp_path):
    entered = asyncio.Event()
    finish = asyncio.Event()
    calls = 0

    async def provider_handler(http_request):
        nonlocal calls
        calls += 1
        entered.set()
        await finish.wait()
        body = json.loads(http_request.content)
        return httpx.Response(200, json={
            "request_id": body["request_id"], "text": "Synthetic result",
            "usage": {"input_tokens": body["input_tokens"], "output_tokens": 1},
        })

    app = create_app(
        database_path=tmp_path / "usage.sqlite3",
        primary_url="http://primary/complete",
        primary_transport=httpx.MockTransport(provider_handler),
        backup_transport=httpx.MockTransport(lambda _: httpx.Response(500)),
    )
    assert app.state.limiter.reserve("tenant-a", 49_000).allowed
    async with client_for(app) as client:
        first = asyncio.create_task(post(client, payload(request_id="req-1", prompt="a", max_output_tokens=500)))
        await asyncio.wait_for(entered.wait(), timeout=2)
        second = await post(client, payload(request_id="req-2", prompt="a", max_output_tokens=500))
        assert second.status_code == 429 and calls == 1
        finish.set()
        completed = await asyncio.wait_for(first, timeout=2)
    assert completed.status_code == 200
    assert app.state.limiter.usage("tenant-a") == 49_002


@pytest.mark.anyio
async def test_two_gateway_instances_share_atomic_request_claim(tmp_path):
    database_path = tmp_path / "usage.sqlite3"
    entered = asyncio.Event()
    finish = asyncio.Event()
    calls = 0

    async def primary_handler(http_request):
        nonlocal calls
        calls += 1
        entered.set()
        await finish.wait()
        body = json.loads(http_request.content)
        return httpx.Response(200, json={
            "request_id": body["request_id"], "text": "Synthetic completion",
            "usage": {"input_tokens": body["input_tokens"], "output_tokens": 1},
        })

    def instance():
        return create_app(
            database_path=database_path,
            primary_url="http://primary/complete",
            primary_transport=httpx.MockTransport(primary_handler),
            backup_transport=httpx.MockTransport(lambda _: httpx.Response(500)),
        )

    app_a, app_b = instance(), instance()
    assert app_a.state.limiter is not app_b.state.limiter
    async with client_for(app_a) as client_a, client_for(app_b) as client_b:
        first = asyncio.create_task(post(client_a))
        await asyncio.wait_for(entered.wait(), timeout=2)
        duplicate = await post(client_b)
        conflict = await post(client_b, payload(prompt="Different prompt"))
        assert duplicate.status_code == conflict.status_code == 409
        assert calls == 1
        finish.set()
        completed = await asyncio.wait_for(first, timeout=2)
        again = await post(client_b)
    assert completed.status_code == 200 and again.status_code == 409
    assert app_a.state.limiter.usage("tenant-a") == 5


@pytest.mark.anyio
@pytest.mark.parametrize(("primary_scenario", "backup_scenario", "expected_status", "expected_code"), [
    ("500", "success", 503, "ALL_PROVIDERS_UNAVAILABLE"),
    ("429", "429", 503, "ALL_PROVIDERS_UNAVAILABLE"),
    ("429", "slow", 504, "PROVIDER_TIMEOUT"),
    ("malformed", "success", 502, "INVALID_PROVIDER_RESPONSE"),
])
async def test_provider_failures_have_sanitized_gateway_errors(tmp_path, primary_scenario, backup_scenario, expected_status, expected_code):
    app, _, _, _ = build(tmp_path, primary=primary_scenario, backup=backup_scenario)
    async with client_for(app) as client:
        response = await post(client)
    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code
    assert "Traceback" not in response.text and "http://" not in response.text
    assert "Synthetic prompt" not in response.text


@pytest.mark.anyio
async def test_database_error_fails_closed_without_exposing_details(tmp_path):
    app, primary, backup, path = build(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE token_reservations")
    async with client_for(app) as client:
        response = await post(client)
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_GATEWAY_ERROR"
    assert str(path) not in response.text
    assert primary.state.calls == backup.state.calls == 0


@pytest.mark.anyio
async def test_claim_survives_lost_process_state_without_automatic_retry(tmp_path):
    app, primary, backup, path = build(tmp_path)
    limiter = app.state.limiter
    body = payload()
    budget = estimate_input_tokens(body["prompt"]) + body["max_output_tokens"]
    reservation = limiter.reserve("tenant-a", budget, request_id=body["request_id"])
    from task4_model_router.gateway import CompletionBody, _fingerprint
    fingerprint = _fingerprint(CompletionBody.model_validate(body), estimate_input_tokens(body["prompt"]))
    assert limiter.claim_execution("tenant-a", reservation.reservation_id, body["request_id"], fingerprint) == "claimed"
    independent_app = create_app(
        database_path=path,
        primary_transport=httpx.ASGITransport(app=primary),
        backup_transport=httpx.ASGITransport(app=backup),
    )
    async with client_for(independent_app) as client:
        response = await post(client)
    assert response.status_code == 409
    assert primary.state.calls == backup.state.calls == 0
    assert limiter.usage("tenant-a") == budget


def test_existing_stage1_sqlite_database_is_migrated(tmp_path):
    path = tmp_path / "usage.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE token_reservations (
                reservation_id TEXT PRIMARY KEY, tenant_hash TEXT NOT NULL,
                request_id TEXT, created_at REAL NOT NULL,
                reserved_tokens INTEGER NOT NULL, usage_tokens INTEGER NOT NULL,
                actual_tokens INTEGER, status TEXT NOT NULL,
                UNIQUE (tenant_hash, request_id)
            )"""
        )
    app = create_app(database_path=path)
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(token_reservations)")}
    assert {"execution_state", "request_fingerprint"} <= columns
    assert app.state.limiter.reserve("tenant-a", 1, request_id="req-1").allowed
