"""Local HTTP/JSON-RPC proxy with tool-level authorization.

Run the mock server on 127.0.0.1:8001 and this gateway on 127.0.0.1:8000.
Tool discovery is open; admin_ tool calls require a recognized admin identity.
"""

import json
import math
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response

from task2_security_gateway.auth import authenticate_request
from task2_security_gateway.authorization import may_call_tool


DEFAULT_DOWNSTREAM_URL = "http://127.0.0.1:8001/mcp"


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def _valid_id(value: Any) -> bool:
    return (
        value is None
        or isinstance(value, str)
        or (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value))
    )


def _error(request_id: str | int | float | None, code: int, message: str, status_code: int = 200) -> Response:
    payload = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    return Response(content=json.dumps(payload), status_code=status_code, media_type="application/json")


def _valid_response(content: bytes, request_id: str | int | float | None) -> bool:
    try:
        message: Any = json.loads(content, parse_constant=_reject_non_json_constant)
    except (ValueError, UnicodeDecodeError):
        return False
    return (
        isinstance(message, dict)
        and message.get("jsonrpc") == "2.0"
        and type(message.get("id")) is type(request_id)
        and message.get("id") == request_id
        and ("result" in message) != ("error" in message)
    )


def create_app(
    downstream_url: str = DEFAULT_DOWNSTREAM_URL,
    downstream_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Create a gateway; the injectable transport keeps tests off the network."""
    app = FastAPI(title="MCP Security Gateway")

    @app.post("/mcp")
    async def handle_mcp(request: Request) -> Response:
        # Never trust a client-supplied role header or put identity in the response.
        request.state.auth_context = authenticate_request(request)
        raw_body = await request.body()
        try:
            message: Any = json.loads(raw_body, parse_constant=_reject_non_json_constant)
        except (ValueError, UnicodeDecodeError):
            return _error(None, -32700, "Parse error")

        if not isinstance(message, dict):
            return _error(None, -32600, "Invalid Request")

        request_id = message.get("id")
        if not _valid_id(request_id):
            return _error(None, -32600, "Invalid Request")

        if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return _error(request_id, -32600, "Invalid Request")

        if message["method"] == "tools/call":
            # Preserve notification behavior: no response and no execution.
            if "id" not in message:
                return Response(status_code=204)
            params = message.get("params")
            if not isinstance(params, dict):
                return _error(request_id, -32602, "Invalid params")
            tool_name = params.get("name")
            arguments = params.get("arguments", {})
            if (
                not isinstance(tool_name, str)
                or not tool_name
                or tool_name.strip() != tool_name
                or not isinstance(arguments, dict)
            ):
                return _error(request_id, -32602, "Invalid params")
            if not may_call_tool(tool_name, request.state.auth_context):
                return _error(request_id, -32001, "Unauthorized Tool Call")
        elif message["method"] != "tools/list":
            if "id" not in message:
                return Response(status_code=204)
            return _error(request_id, -32601, "Method not found")

        try:
            async with httpx.AsyncClient(transport=downstream_transport, timeout=5.0) as client:
                downstream = await client.post(
                    downstream_url,
                    content=raw_body,
                    headers={"content-type": "application/json"},
                )
        except httpx.TimeoutException:
            return _error(request_id, -32001, "Downstream server timed out", 504)
        except httpx.RequestError:
            return _error(request_id, -32000, "Downstream server unavailable", 502)
        except Exception:
            # A failing downstream ASGI app can raise outside HTTPX's network
            # exception hierarchy. Do not expose its exception text or traceback.
            return _error(request_id, -32000, "Downstream server unavailable", 502)

        if "id" not in message and downstream.status_code == 204:
            return Response(status_code=204)

        if downstream.status_code != 200 or not _valid_response(downstream.content, request_id):
            return _error(request_id, -32000, "Invalid downstream response", 502)

        # Preserve the downstream JSON-RPC body byte for byte, including its ID.
        return Response(content=downstream.content, media_type="application/json")

    return app


app = create_app()
