"""Assessment checks against the real MCP client/server protocol layer."""

import json
import sys
from pathlib import Path

import pytest
from mcp import Client, MCPError, StdioServerParameters
from mcp.types import INVALID_PARAMS

from task1_mcp_server.server import server


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_exposes_exactly_two_tools():
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
    assert {tool.name for tool in tools} == {"get_customer_record", "trigger_refund"}
    ids = next(tool for tool in tools if tool.name == "get_customer_record")
    assert "customer_id" in ids.input_schema["required"]
    assert ids.input_schema["additionalProperties"] is False


@pytest.mark.anyio
async def test_get_customer_record_success():
    async with Client(server) as client:
        result = await client.call_tool("get_customer_record", {"customer_id": "CUST-12345"})
    assert result.is_error is not True
    assert json.loads(result.content[0].text)["customer"]["name"] == "Alex Rivera"


@pytest.mark.anyio
@pytest.mark.parametrize("args", [
    {"customer_id": "12345"},
    {"customer_id": "CUST-12"},
    {"customer_id": "CUST-123456"},
    {"customer_id": 12345},
    {"customer_id": "CUST-12345", "unexpected": "field"},
    {},
])
async def test_customer_rejects_bad_arguments_as_jsonrpc_error(args):
    async with Client(server) as client:
        with pytest.raises(MCPError) as exc:
            await client.call_tool("get_customer_record", args)
    assert exc.value.code == INVALID_PARAMS


@pytest.mark.anyio
async def test_refund_success():
    async with Client(server) as client:
        result = await client.call_tool("trigger_refund", {
            "customer_id": "CUST-12345",
            "amount": 25.5,
            "reason": "Duplicate order charge",
        })
    assert result.is_error is not True
    refund = json.loads(result.content[0].text)["refund"]
    assert refund["customer_id"] == "CUST-12345"
    assert refund["amount"] == 25.5
    assert refund["status"] == "simulated"


@pytest.mark.anyio
@pytest.mark.parametrize("update", [
    {"amount": 0.0},
    {"amount": -1.0},
    {"amount": "10"},
    {"reason": "Too short"},
    {"reason": "          "},
    {"customer_id": "CUST-ABCDE"},
])
async def test_refund_validation_errors(update):
    data = {"customer_id": "CUST-12345", "amount": 12.0, "reason": "Customer requested refund"}
    data.update(update)
    async with Client(server) as client:
        with pytest.raises(MCPError) as exc:
            await client.call_tool("trigger_refund", data)
    assert exc.value.code == INVALID_PARAMS


@pytest.mark.anyio
async def test_missing_customer_is_tool_error():
    async with Client(server) as client:
        result = await client.call_tool("get_customer_record", {"customer_id": "CUST-99999"})
    assert result.is_error is True
    assert "not found" in result.content[0].text.lower()


@pytest.mark.anyio
async def test_unknown_tool_is_protocol_error():
    async with Client(server) as client:
        with pytest.raises(MCPError) as exc:
            await client.call_tool("not_a_tool", {})
    assert exc.value.code == INVALID_PARAMS


@pytest.mark.anyio
async def test_real_stdio_subprocess_transport():
    # Proves handshake and tool calls work over an actual stdin/stdout subprocess.
    # Any stray stdout print would corrupt protocol parsing and fail this test.
    root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "task1_mcp_server.server"],
        cwd=str(root),
    )
    async with Client(params) as client:
        names = [tool.name for tool in (await client.list_tools()).tools]
        assert "trigger_refund" in names
        record = await client.call_tool("get_customer_record", {"customer_id": "CUST-00001"})
        assert "Jordan Lee" in record.content[0].text
