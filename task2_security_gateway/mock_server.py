"""Mock downstream MCP server for local Task 2 gateway testing.

Tool calls are simulated; admin_reset_key never changes a real credential.
"""

import json
import math
import re
from typing import Any

from fastapi import FastAPI, Request, Response


app = FastAPI(title="Mock downstream MCP server")

TOOLS = [
    {
        "name": "get_customer_record",
        "description": "Look up a mock customer record.",
        "inputSchema": {
            "type": "object",
            "properties": {"customer_id": {"type": "string", "pattern": "^CUST-[0-9]{5}$"}},
            "required": ["customer_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "admin_reset_key",
        "description": "Simulate an administrative key reset without changing credentials.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]

MOCK_CUSTOMERS = {
    "CUST-12345": {"customer_id": "CUST-12345", "name": "Alex Rivera", "status": "active"},
}


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def _valid_id(value: Any) -> bool:
    return (
        value is None
        or isinstance(value, str)
        or (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value))
    )


def _reply(request_id: str | int | float | None, *, result: Any = None, error: dict | None = None) -> Response:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    return Response(content=json.dumps(payload), media_type="application/json")


def _tool_result(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}]
    }
    if is_error:
        result["isError"] = True
    return result


@app.post("/mcp")
async def handle_mcp(request: Request) -> Response:
    try:
        message = json.loads(await request.body(), parse_constant=_reject_non_json_constant)
    except (ValueError, UnicodeDecodeError):
        return _reply(None, error={"code": -32700, "message": "Parse error"})

    if not isinstance(message, dict):
        return _reply(None, error={"code": -32600, "message": "Invalid Request"})

    request_id = message.get("id")
    if not _valid_id(request_id):
        return _reply(None, error={"code": -32600, "message": "Invalid Request"})

    if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _reply(request_id, error={"code": -32600, "message": "Invalid Request"})

    if "id" not in message:
        return Response(status_code=204)

    if message["method"] == "tools/list":
        return _reply(request_id, result={"tools": TOOLS})

    if message["method"] == "tools/call":
        params = message.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return _reply(request_id, error={"code": -32602, "message": "Invalid params"})
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return _reply(request_id, error={"code": -32602, "message": "Invalid params"})

        if params["name"] == "get_customer_record":
            customer_id = arguments.get("customer_id")
            if (
                set(arguments) != {"customer_id"}
                or not isinstance(customer_id, str)
                or re.fullmatch(r"CUST-[0-9]{5}", customer_id) is None
            ):
                return _reply(request_id, error={"code": -32602, "message": "Invalid params"})
            customer = MOCK_CUSTOMERS.get(customer_id)
            if customer is None:
                return _reply(request_id, result=_tool_result({"error": "Customer not found"}, is_error=True))
            return _reply(request_id, result=_tool_result({"customer": customer}))

        if params["name"] == "admin_reset_key":
            if arguments:
                return _reply(request_id, error={"code": -32602, "message": "Invalid params"})
            return _reply(
                request_id,
                result=_tool_result({"status": "simulated", "message": "No real credential changed"}),
            )

        return _reply(request_id, error={"code": -32602, "message": "Unknown tool"})

    return _reply(request_id, error={"code": -32601, "message": "Method not found"})
