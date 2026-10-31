# Task 1 — Customer Support MCP Server

This independent Python MCP server exposes two mock customer support tools over **stdio** using the official MCP SDK. It does not connect to a CRM or process payments.

## Architecture and tools

- `server.py` creates the MCP server, registers tool discovery and call handlers, and runs the SDK's stdio transport.
- `schemas.py` defines strict Pydantic input models. The models both advertise JSON schemas to clients and validate arguments when a tool is called.
- `demo_client.py` starts the server as a subprocess and calls it through the SDK's MCP client.

| Tool | Required input | Behavior |
| --- | --- | --- |
| `get_customer_record` | `customer_id` matching `CUST-XXXXX` (five digits) | Returns a record from a small in-memory mock dataset. |
| `trigger_refund` | `customer_id`, positive finite float `amount`, and `reason` of at least 10 characters after trimming | Returns a simulated refund; no money moves. |

Pydantic rejects wrong types, missing or extra fields, malformed IDs, nonpositive amounts, and short reasons. Invalid tool arguments raise an MCP JSON-RPC `-32602 Invalid params` error. A well-formed but unknown customer produces an MCP tool result with `isError: true`.

The SDK reads JSON-RPC messages from stdin and writes protocol responses to stdout. Server logs go to stderr so they cannot corrupt the stdio transport.

## Install and test

Use Python 3.11 or newer. Run these commands **from the repository root** in Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r task1_mcp_server/requirements.txt
python -m pytest task1_mcp_server/tests/ -v
```

The commands above were verified with Python 3.12. If `py -3.12` is unavailable, create the environment with another installed Python 3.11+ interpreter. The Task 1 requirements file contains only its runtime and test dependencies; Task 2 does not need to be installed or started.

## Run the server and demo

From the repository root with the Task 1 environment active:

```powershell
python -m task1_mcp_server.server
```

The server waits for MCP protocol messages on stdin. In a separate terminal, run the demo client instead of typing natural-language commands into the server terminal:

```powershell
python -m task1_mcp_server.demo_client
```

The demo starts its own server subprocess, lists both tools, calls each tool, and checks an invalid customer ID. It uses a `49.99` simulated refund with a duplicate-charge reason. The tests include a real stdio subprocess check.
