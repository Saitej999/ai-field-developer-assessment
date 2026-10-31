"""Local HTTP/JSON-RPC and Bearer identity checks for Task 2."""

import json
import logging

import httpx
import pytest
from fastapi import Request

from task2_security_gateway.auth import authenticate_authorization_header
from task2_security_gateway.gateway import create_app
from task2_security_gateway.mock_server import app as mock_app


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_mock_server_lists_tools_and_simulates_admin_call():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app), base_url="http://mock"
    ) as client:
        response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": "list-1", "method": "tools/list"})
        admin_call = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "admin_reset_key"}},
        )

    assert response.status_code == 200
    assert response.json()["id"] == "list-1"
    assert [tool["name"] for tool in response.json()["result"]["tools"]] == [
        "get_customer_record", "admin_reset_key"
    ]
    assert admin_call.json()["id"] == 2
    assert "simulated" in admin_call.json()["result"]["content"][0]["text"]


@pytest.mark.anyio
async def test_gateway_forwards_raw_tools_list_request_and_response():
    sent = []
    downstream_body = b'{ "jsonrpc":"2.0", "id":42, "result":{"tools":[]} }'

    def downstream(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, content=downstream_body, headers={"content-type": "application/json"})

    gateway = create_app("http://mock/mcp", httpx.MockTransport(downstream))
    request_body = b'{ "jsonrpc":"2.0", "id":42, "method":"tools/list" }'
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
    ) as client:
        response = await client.post("/mcp", content=request_body, headers={"content-type": "application/json"})

    assert response.status_code == 200
    assert response.content == downstream_body
    assert len(sent) == 1
    assert sent[0].url == "http://mock/mcp"
    assert sent[0].content == request_body
    assert sent[0].headers["content-type"] == "application/json"


@pytest.mark.anyio
async def test_gateway_discovers_tools_from_real_mock_asgi_app():
    gateway = create_app("http://mock/mcp", httpx.ASGITransport(app=mock_app))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
    ) as client:
        response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 101, "method": "tools/list"})

    assert response.status_code == 200
    assert response.json()["id"] == 101
    assert {tool["name"] for tool in response.json()["result"]["tools"]} == {
        "get_customer_record", "admin_reset_key"
    }


@pytest.mark.anyio
async def test_gateway_never_forwards_unauthorized_admin_execution():
    forwarded = []

    def downstream(request: httpx.Request) -> httpx.Response:
        forwarded.append(request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 7, "result": {}})

    gateway = create_app("http://mock/mcp", httpx.MockTransport(downstream))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
    ) as client:
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "admin_reset_key"}},
            headers={"Authorization": "Bearer viewer-demo-token"},
        )

    assert response.json() == {"jsonrpc": "2.0", "id": 7, "error": {"code": -32001, "message": "Unauthorized Tool Call"}}
    assert forwarded == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        (b"{broken", -32700),
        (b'{"jsonrpc":"2.0","id":NaN,"method":"tools/list"}', -32700),
        (b"[]", -32600),
        (b'{"jsonrpc":"1.0","id":3,"method":"tools/list"}', -32600),
        (b'{"jsonrpc":"2.0","id":true,"method":"tools/list"}', -32600),
    ],
)
async def test_gateway_rejects_invalid_jsonrpc_requests(body, expected_code):
    gateway = create_app("http://mock/mcp", httpx.MockTransport(lambda _: httpx.Response(200)))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
    ) as client:
        response = await client.post("/mcp", content=body)

    assert response.status_code == 200
    assert response.json()["error"]["code"] == expected_code


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("exception_type", "http_status", "rpc_code"),
    [(httpx.ConnectError, 502, -32000), (httpx.ReadTimeout, 504, -32001)],
)
async def test_downstream_failures_are_safe(exception_type, http_status, rpc_code):
    def broken(request: httpx.Request) -> httpx.Response:
        raise exception_type("private network detail", request=request)

    gateway = create_app("http://mock/mcp", httpx.MockTransport(broken))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
    ) as client:
        response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": "req-9", "method": "tools/list"})

    assert response.status_code == http_status
    assert response.json()["id"] == "req-9"
    assert response.json()["error"]["code"] == rpc_code
    assert "private network detail" not in response.text
    assert "Traceback" not in response.text


@pytest.mark.anyio
@pytest.mark.parametrize(("request_id", "returned_id"), [("right", "wrong"), (1, True)])
async def test_mismatched_downstream_id_is_not_forwarded(request_id, returned_id):
    gateway = create_app(
        "http://mock/mcp",
        httpx.MockTransport(lambda _: httpx.Response(200, json={"jsonrpc": "2.0", "id": returned_id, "result": {"tools": []}})),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
    ) as client:
        response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": request_id, "method": "tools/list"})

    assert response.status_code == 502
    assert response.json()["id"] == request_id
    assert response.json()["error"]["code"] == -32000


@pytest.mark.anyio
async def test_fractional_numeric_id_is_preserved():
    gateway = create_app("http://mock/mcp", httpx.ASGITransport(app=mock_app))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
    ) as client:
        response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1.5, "method": "tools/list"})

    assert response.status_code == 200
    assert response.json()["id"] == 1.5


@pytest.mark.parametrize(
    ("header", "expected_role"),
    [("Bearer admin-demo-token", "admin"), ("bearer viewer-demo-token", "viewer")],
)
def test_demo_bearer_tokens_resolve_roles(header, expected_role):
    context = authenticate_authorization_header(header)
    assert context.status == "authenticated"
    assert context.role == expected_role
    assert not hasattr(context, "token")


@pytest.mark.parametrize(
    ("header", "expected_status"),
    [
        (None, "missing"),
        ("", "invalid"),
        ("Basic admin-demo-token", "invalid"),
        ("Bearer", "invalid"),
        ("Bearer ", "invalid"),
        ("Bearer unknown-demo-token", "invalid"),
        ("Bearer viewer-demo-token extra", "invalid"),
    ],
)
def test_missing_malformed_and_unknown_tokens_have_no_role(header, expected_status):
    context = authenticate_authorization_header(header)
    assert context.status == expected_status
    assert context.role is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("headers", "expected_status", "expected_role"),
    [
        ({"Authorization": "Bearer admin-demo-token"}, "authenticated", "admin"),
        ({"Authorization": "Bearer viewer-demo-token", "X-Role": "admin"}, "authenticated", "viewer"),
        ({"X-Role": "admin"}, "missing", None),
        ({"Authorization": "Bearer unknown-demo-token", "X-Role": "admin"}, "invalid", None),
    ],
)
async def test_gateway_stores_identity_without_changing_tools_list(headers, expected_status, expected_role):
    gateway = create_app("http://mock/mcp", httpx.ASGITransport(app=mock_app))
    captured = []

    @gateway.middleware("http")
    async def capture_context(request: Request, call_next):
        response = await call_next(request)
        captured.append(request.state.auth_context)
        return response

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
    ) as client:
        response = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": "auth-list-1", "method": "tools/list"}, headers=headers
        )

    assert response.status_code == 200
    assert response.json()["id"] == "auth-list-1"
    assert {tool["name"] for tool in response.json()["result"]["tools"]} == {
        "get_customer_record", "admin_reset_key"
    }
    assert captured[0].status == expected_status
    assert captured[0].role == expected_role
    assert "role" not in response.json()
    assert "auth" not in response.json()


@pytest.mark.anyio
async def test_gateway_does_not_forward_or_log_authentication_tokens(caplog):
    downstream_requests = []

    def downstream(request: httpx.Request) -> httpx.Response:
        downstream_requests.append(request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 8, "result": {"tools": []}})

    gateway = create_app("http://mock/mcp", httpx.MockTransport(downstream))
    with caplog.at_level(logging.DEBUG):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
        ) as client:
            response = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 8, "method": "tools/list"},
                headers={"Authorization": "Bearer admin-demo-token", "X-Role": "viewer"},
            )

    assert response.status_code == 200
    assert len(downstream_requests) == 1
    assert "authorization" not in downstream_requests[0].headers
    assert "x-role" not in downstream_requests[0].headers
    assert "admin-demo-token" not in caplog.text
    assert "admin-demo-token" not in response.text


@pytest.mark.anyio
async def test_admin_can_call_simulated_reset_through_gateway():
    gateway = create_app("http://mock/mcp", httpx.ASGITransport(app=mock_app))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
    ) as client:
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": "admin-42", "method": "tools/call", "params": {"name": "admin_reset_key", "arguments": {}}},
            headers={"Authorization": "Bearer admin-demo-token"},
        )

    assert response.status_code == 200
    assert response.json()["id"] == "admin-42"
    result = json.loads(response.json()["result"]["content"][0]["text"])
    assert result == {"message": "No real credential changed", "status": "simulated"}
    assert "admin-demo-token" not in response.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("authorization", "tool_name", "extra_headers"),
    [
        ("Bearer viewer-demo-token", "admin_reset_key", {}),
        (None, "admin_reset_key", {}),
        ("Bearer unknown-demo-token", "admin_reset_key", {}),
        ("Bearer", "admin_reset_key", {}),
        ("Bearer viewer-demo-token", "admin_reset_key", {"X-Role": "admin"}),
        ("Bearer viewer-demo-token", "admin_nonexistent", {}),
    ],
)
async def test_blocked_admin_calls_never_reach_downstream(authorization, tool_name, extra_headers):
    forwarded = []

    def downstream(request: httpx.Request) -> httpx.Response:
        forwarded.append(request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "blocked-9", "result": {}})

    headers = dict(extra_headers)
    if authorization is not None:
        headers["Authorization"] = authorization
    gateway = create_app("http://mock/mcp", httpx.MockTransport(downstream))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
    ) as client:
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": "blocked-9", "method": "tools/call", "params": {"name": tool_name, "arguments": {}}},
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": "blocked-9",
        "error": {"code": -32001, "message": "Unauthorized Tool Call"},
    }
    assert forwarded == []
    if authorization:
        assert authorization not in response.text


@pytest.mark.anyio
@pytest.mark.parametrize("authorization", ["Bearer viewer-demo-token", "Bearer admin-demo-token", None])
async def test_ordinary_tool_call_needs_no_admin_role(authorization):
    gateway = create_app("http://mock/mcp", httpx.ASGITransport(app=mock_app))
    headers = {"Authorization": authorization} if authorization else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
    ) as client:
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 55, "method": "tools/call", "params": {"name": "get_customer_record", "arguments": {"customer_id": "CUST-12345"}}},
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json()["id"] == 55
    customer = json.loads(response.json()["result"]["content"][0]["text"])["customer"]
    assert customer["customer_id"] == "CUST-12345"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("authorization", "tool_name", "arguments"),
    [
        ("Bearer admin-demo-token", "admin_reset_key", {}),
        ("Bearer viewer-demo-token", "get_customer_record", {"customer_id": "CUST-12345"}),
    ],
)
async def test_authorized_call_reaches_downstream_unchanged(authorization, tool_name, arguments):
    forwarded = []
    downstream_body = b'{"jsonrpc":"2.0","id":88,"result":{"content":[]}}'

    def downstream(request: httpx.Request) -> httpx.Response:
        forwarded.append(request)
        return httpx.Response(200, content=downstream_body)

    gateway = create_app("http://mock/mcp", httpx.MockTransport(downstream))
    body = json.dumps({"jsonrpc": "2.0", "id": 88, "method": "tools/call", "params": {"name": tool_name, "arguments": arguments}}).encode()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
    ) as client:
        response = await client.post(
            "/mcp", content=body, headers={"Authorization": authorization, "X-Role": "admin"}
        )

    assert response.status_code == 200
    assert response.content == downstream_body
    assert len(forwarded) == 1
    assert forwarded[0].content == body
    assert "authorization" not in forwarded[0].headers
    assert "x-role" not in forwarded[0].headers


@pytest.mark.anyio
@pytest.mark.parametrize(
    "params",
    [None, [], {}, {"name": None}, {"name": 123}, {"name": ""}, {"name": " admin_reset_key"}, {"name": "admin_reset_key", "arguments": []}],
)
async def test_malformed_tool_call_params_are_rejected_locally(params):
    forwarded = []

    def downstream(request: httpx.Request) -> httpx.Response:
        forwarded.append(request)
        return httpx.Response(200)

    gateway = create_app("http://mock/mcp", httpx.MockTransport(downstream))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
    ) as client:
        response = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 901, "method": "tools/call", "params": params},
            headers={"Authorization": "Bearer viewer-demo-token"},
        )

    assert response.status_code == 200
    assert response.json()["id"] == 901
    assert response.json()["error"]["code"] == -32602
    assert forwarded == []


@pytest.mark.anyio
async def test_unexpected_downstream_exception_has_safe_response():
    def broken(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError("internal credential store detail")

    gateway = create_app("http://mock/mcp", httpx.MockTransport(broken))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
    ) as client:
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": "failure-1", "method": "tools/call", "params": {"name": "admin_reset_key"}},
            headers={"Authorization": "Bearer admin-demo-token"},
        )

    assert response.status_code == 502
    assert response.json()["id"] == "failure-1"
    assert response.json()["error"]["code"] == -32000
    assert "internal credential store detail" not in response.text
    assert "Traceback" not in response.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool_name", "arguments", "authorization"),
    [
        ("get_customer_record", {"customer_id": "bad"}, "Bearer viewer-demo-token"),
        ("admin_reset_key", {"unexpected": "value"}, "Bearer admin-demo-token"),
        ("unknown_tool", {}, None),
    ],
)
async def test_mock_rejects_invalid_tool_arguments_and_unknown_names(tool_name, arguments, authorization):
    gateway = create_app("http://mock/mcp", httpx.ASGITransport(app=mock_app))
    headers = {"Authorization": authorization} if authorization else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
    ) as client:
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": "invalid-3", "method": "tools/call", "params": {"name": tool_name, "arguments": arguments}},
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json()["id"] == "invalid-3"
    assert response.json()["error"]["code"] == -32602
    assert "Traceback" not in response.text


@pytest.mark.anyio
async def test_tool_call_notification_has_no_response_or_downstream_execution():
    forwarded = []

    def downstream(request: httpx.Request) -> httpx.Response:
        forwarded.append(request)
        return httpx.Response(204)

    gateway = create_app("http://mock/mcp", httpx.MockTransport(downstream))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
    ) as client:
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "admin_reset_key"}},
            headers={"Authorization": "Bearer admin-demo-token"},
        )

    assert response.status_code == 204
    assert response.content == b""
    assert forwarded == []
