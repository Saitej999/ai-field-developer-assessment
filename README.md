# AI Field Developer / Solutions Engineer Assessment

Repository for a four-part technical assessment. Task 1 is complete. Task 2 has a local HTTP gateway, a mock downstream server, demo Bearer token authentication, and tool-level authorization.

| Task | Status | Independent guide |
| --- | --- | --- |
| 1 — Customer support MCP server | Complete | [Task 1 README](task1_mcp_server/README.md) |
| 2 — MCP security gateway proxy | Complete | [Task 2 README](task2_security_gateway/README.md) |
| 3 — Streaming LLM PII redaction | Not started | — |
| 4 — Rate limiting, provider fallback, and SQLite | Not started | — |

Each completed task has its own Python package, requirements file, entry points, and tests. Neither task imports application code from the other.

```text
ai-field-developer-assessment/
├── task1_mcp_server/          # stdio MCP server, schemas, demo, README, requirements
├── task2_security_gateway/   # HTTP gateway, auth, authorization, mock, README, requirements
├── tests/                    # test_task1.py and test_task2.py
├── requirements.txt          # install both completed tasks together
├── README.md
├── pytest.ini
└── .gitignore
```

From the repository root, install every completed task and run the full suite with an active Python 3.11+ virtual environment:

```powershell
python -m pip install -r requirements.txt
python -m pytest -q
```

For task-specific installs and commands, use the linked guides above. Tasks 3 and 4 have no implementation yet.

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
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pytest -q
```

The commands above were verified with Python 3.12. If `py -3.12` is unavailable, use `python -m venv .venv` with Python 3.11+.

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
- No HTTP listener is needed for Task 1. Task 2 implements HTTP gateway behavior separately.

## Roadmap

- [x] Task 1 — Strict MCP tools + stdio transport
- [x] Task 2 — MCP security gateway (HTTP/JSON-RPC proxy, demo authentication, and `admin_` RBAC)
- [ ] Task 3 — Streaming LLM PII redaction
- [ ] Task 4 — Rate limiting + provider fallback + SQLite

## Task 2: Local HTTP/JSON-RPC security gateway

`task2_security_gateway/mock_server.py` is a local FastAPI mock server. Its `/mcp` POST endpoint lists `get_customer_record` and `admin_reset_key` for `tools/list` and returns simulated results for `tools/call`. The administrative handler never changes a real credential. This is a test double, not a complete MCP HTTP server.

`task2_security_gateway/gateway.py` is a separate FastAPI app. Its `/mcp` POST endpoint accepts JSON-RPC 2.0 requests and forwards `tools/list` and authorized `tools/call` requests to the mock server using asynchronous HTTPX. Valid downstream JSON-RPC responses are returned byte for byte, including the original request ID. Downstream failures return safe JSON-RPC errors.

`task2_security_gateway/auth.py` reads an optional HTTP Bearer token and stores the resolved role in gateway request state. **The tokens `admin-demo-token` (admin) and `viewer-demo-token` (viewer) are public mock identities for local testing only.** Missing, malformed, and unknown credentials have no role. Client-supplied role headers are ignored. Tokens and roles are not included in JSON-RPC responses or forwarded to the mock server.

`task2_security_gateway/authorization.py` permits calls to tools whose names start with `admin_` only for a recognized admin token. The gateway returns JSON-RPC error `-32001` (`Unauthorized Tool Call`) for all other callers and does not contact the downstream server. Ordinary tool calls and `tools/list` do not require an admin role. Malformed `tools/call` parameters return `-32602` and are not forwarded. Tool-call notifications without a request ID receive no response and are not executed.

To run both services locally, open two terminals in the repository root with the virtual environment active:

```powershell
python -m uvicorn task2_security_gateway.mock_server:app --host 127.0.0.1 --port 8001
```

```powershell
python -m uvicorn task2_security_gateway.gateway:app --host 127.0.0.1 --port 8000
```

Send `tools/list` requests to `http://127.0.0.1:8000/mcp`. The gateway forwards to `http://127.0.0.1:8001/mcp` by default. Both addresses bind only to the local computer. Run `python -m pytest -q` to check Tasks 1 and 2 together; the Task 2 tests use in-memory HTTP transports and do not start listening services.

## Important

This repository is intended as an assessment demonstration. Before submission, run tests locally, verify the execution environment, and be prepared to explain the code and its assumptions.
