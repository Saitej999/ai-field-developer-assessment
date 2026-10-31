"""Two-tool MCP server with strict JSON-RPC invalid-parameters errors.

Run with: python -m task1_mcp_server.server

IMPORTANT: stdout belongs to the MCP stdio transport exclusively.
Use logging (stderr) instead of print() inside the server process.
"""

import asyncio
import json
import logging
import sys
import uuid
from typing import Any

from mcp import MCPError
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    INVALID_PARAMS,
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)
from pydantic import BaseModel, ValidationError

from task1_mcp_server.schemas import GetCustomerRecordArgs, TriggerRefundArgs

# Intentionally fake data: this assessment does NOT require a real CRM or payment API.
CUSTOMERS: dict[str, dict[str, Any]] = {
    "CUST-12345": {"customer_id": "CUST-12345", "name": "Alex Rivera", "status": "active"},
    "CUST-00001": {"customer_id": "CUST-00001", "name": "Jordan Lee", "status": "active"},
}

LOGGER = logging.getLogger("task1_mcp_server")

GET_CUSTOMER_RECORD = Tool(
    name="get_customer_record",
    description="Fetch a mock customer record by a CUST-XXXXX customer ID.",
    input_schema=GetCustomerRecordArgs.model_json_schema(),
)
TRIGGER_REFUND = Tool(
    name="trigger_refund",
    description="Simulate a refund for an existing customer (no real payment is made).",
    input_schema=TriggerRefundArgs.model_json_schema(),
)


def _validate(schema: type[BaseModel], arguments: dict[str, Any]) -> BaseModel:
    """Reject malformed arguments as a JSON-RPC -32602 error."""
    try:
        return schema.model_validate(arguments)
    except ValidationError as exc:
        # Do not include original user input or external documentation URLs in errors.
        problems = [
            {"field": ".".join(map(str, e["loc"])), "message": e["msg"]}
            for e in exc.errors(include_input=False, include_url=False)
        ]
        raise MCPError(
            code=INVALID_PARAMS,
            message="Invalid tool arguments",
            data={"issues": problems},
        ) from None


def _success(payload: dict[str, Any]) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, sort_keys=True))],
    )


def _tool_error(message: str) -> CallToolResult:
    """Expected business failures are tool-level errors, not protocol failures."""
    return CallToolResult(
        content=[TextContent(type="text", text=message)],
        is_error=True,
    )


async def list_tools(
    ctx: ServerRequestContext, params: PaginatedRequestParams | None
) -> ListToolsResult:
    return ListToolsResult(tools=[GET_CUSTOMER_RECORD, TRIGGER_REFUND])


async def call_tool(
    ctx: ServerRequestContext, params: CallToolRequestParams
) -> CallToolResult:
    arguments = params.arguments or {}

    if params.name == "get_customer_record":
        values = _validate(GetCustomerRecordArgs, arguments)
        customer = CUSTOMERS.get(values.customer_id)
        if customer is None:
            return _tool_error("Customer not found")
        return _success({"customer": customer})

    if params.name == "trigger_refund":
        values = _validate(TriggerRefundArgs, arguments)
        if values.customer_id not in CUSTOMERS:
            return _tool_error("Customer not found; refund not created")
        refund = {
            "refund_id": f"RFD-{uuid.uuid4().hex[:10].upper()}",
            "customer_id": values.customer_id,
            "amount": values.amount,
            "reason": values.reason,
            "status": "simulated",
        }
        LOGGER.info("Simulated refund created for customer %s", values.customer_id)
        return _success({"refund": refund})

    raise MCPError(
        code=INVALID_PARAMS,
        message=f"Unknown tool: {params.name}",
    )


server = Server(
    "Customer Support MCP",
    version="1.0.0",
    on_list_tools=list_tools,
    on_call_tool=call_tool,
)


async def main() -> None:
    # StreamHandler writes to stderr; never write debugging text to stdout.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    LOGGER.info("Starting customer-support MCP server over stdio")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
