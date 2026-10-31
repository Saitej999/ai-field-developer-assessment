"""Demonstrate real stdio MCP calls. Run: python -m task1_mcp_server.demo_client"""

import asyncio
import json
import sys
from pathlib import Path

from mcp import Client, MCPError, StdioServerParameters


async def main() -> None:
    root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "task1_mcp_server.server"],
        cwd=str(root),
    )
    async with Client(params) as client:
        tools = await client.list_tools()
        print("Available tools:", [tool.name for tool in tools.tools])

        record = await client.call_tool("get_customer_record", {"customer_id": "CUST-12345"})
        print("Customer record:", json.loads(record.content[0].text))

        refund = await client.call_tool("trigger_refund", {
            "customer_id": "CUST-12345",
            "amount": 49.99,
            "reason": "Duplicate subscription charge",
        })
        print("Simulated refund:", json.loads(refund.content[0].text))

        try:
            await client.call_tool("get_customer_record", {"customer_id": "BAD-ID"})
        except MCPError as exc:
            print("Expected validation failure: JSON-RPC code", exc.code)


if __name__ == "__main__":
    asyncio.run(main())
