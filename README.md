# AI Field Developer / Solutions Engineer Assessment

Repository for a four-part technical assessment. **Task 1** is implemented in this initial version. Tasks 2–4 can be added as separate directories later.

## Task 1: Customer Support MCP Server

The MCP server exposes exactly two tools over **stdio** using the **official Python MCP SDK v2**:

| Tool | Input | Description |
| --- | --- | --- |
| `get_customer_record` | `customer_id` like `CUST-12345` | Returns a mock customer record. |
| `trigger_refund` | `customer_id`, positive `amount`, `reason` at least 10 characters | Simulates a refund; does not move money. |

Key behaviors:

- Pydantic schemas enforce strict types, input patterns, required fields, and reject extra keys.
- Invalid arguments return a JSON-RPC **-32602 Invalid params** error using the SDK's `MCPError`.
- A valid but missing customer returns an MCP tool result with `isError: true`.
- Only the MCP SDK uses stdout for JSON-RPC. App logs are sent to stderr.
- The repository includes automated protocol and real subprocess/stdin/stdout tests.

> **Mock/demo:** No CRM connection, real refunds, or sensitive user data are included.

## Prerequisites

- Python 3.11 or newer
- Git

## Set up (Windows PowerShell)

From the repository root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pytest -q
```

If `py -3.11` is unavailable, use `python -m venv .venv` with Python 3.11+.

## Set up (macOS/Linux)

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest -q
```

## Run the server

```bash
python -m task1_mcp_server.server
```

The process waits for MCP JSON-RPC messages on stdin. Do not type natural-language commands into this terminal. To see tool calls in action, open another terminal and run:

```bash
python -m task1_mcp_server.demo_client
```

The demo starts its own MCP server subprocess through the official client library, lists the tools, makes two valid calls, and checks a bad argument.

## Example tool arguments

`get_customer_record`:

```json
{"customer_id":"CUST-12345"}
```

`trigger_refund`:

```json
{"customer_id":"CUST-12345","amount":49.99,"reason":"Duplicate subscription charge"}
```

Expected invalid parameters: `{"customer_id":"BAD-ID"}` -> JSON-RPC error code `-32602`.

## Design choices

- Use the **low-level SDK Server** rather than high-level `MCPServer` so malformed tool inputs explicitly map to JSON-RPC `-32602` as requested by the assessment. The current SDK's high-level automatic tool validation generally returns a tool error (`isError: true`) instead.
- Validation is applied in **both** the published tool input schema and the actual tool handler.
- An unknown tool is rejected with `-32602`.
- This implementation deliberately uses mock data and a simulated refund because the assessment does not require real downstream integration.
- No HTTP listener is needed for Task 1. Task 2 will implement HTTP gateway behavior separately.

## Roadmap

- [x] Task 1 — Strict MCP tools + stdio transport
- [ ] Task 2 — MCP security gateway
- [ ] Task 3 — Streaming LLM PII redaction
- [ ] Task 4 — Rate limiting + provider fallback + SQLite

## Important

This repository is intended as an assessment demonstration. Before submission, run tests locally, verify the execution environment, and be prepared to explain the code and its assumptions.
