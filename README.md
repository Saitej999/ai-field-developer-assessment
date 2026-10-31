# AI Field Developer / Solutions Engineer Assessment

Four independent Python projects for an AI Field Developer / Solutions Engineer technical assessment. All four tasks are implemented and verified with local mock data and services.

| Task | Status | Independent guide |
| --- | --- | --- |
| 1 — Customer support MCP server | Complete | [Task 1 README](task1_mcp_server/README.md) |
| 2 — MCP security gateway proxy | Complete | [Task 2 README](task2_security_gateway/README.md) |
| 3 — Streaming LLM PII redaction | Complete | [Task 3 README](task3_streaming_guardrails/README.md) |
| 4 — Rate limiting, provider fallback, and SQLite | Complete | [Task 4 README](task4_model_router/README.md) |

Each task has its own Python package, requirements file, entry points, guide, and tests. No task imports another task's application code. The architecture progresses from a stdio MCP server (Task 1), to an HTTP/JSON-RPC security proxy (Task 2), to an SSE redaction gateway (Task 3), and finally to a SQLite-backed completion gateway with provider fallback (Task 4).

**Stack:** Python 3.11+, official MCP Python SDK, Pydantic, FastAPI, HTTPX, Uvicorn, standard-library SQLite, and Pytest. All providers, identities, customer records, and administrative actions are local simulations; no paid API or external credential is required.

```text
ai-field-developer-assessment/
├── task1_mcp_server/          # stdio MCP server, schemas, demo, README, requirements
├── task2_security_gateway/   # HTTP gateway, auth, authorization, mock, README, requirements
├── task3_streaming_guardrails/ # SSE gateway, mock provider, parser, README, requirements
├── task4_model_router/         # SQLite limiter, async router, FastAPI gateway, mocks
├── tests/                    # independent task test modules
├── requirements.txt          # install all tasks together
├── README.md
├── pytest.ini
└── .gitignore
```

From the repository root, install all implemented tasks and run the full suite with an active Python 3.11+ virtual environment:

```powershell
python -m pip install -r requirements.txt
python -m pytest -q
```

Each linked task guide has independent installation, local execution, and test commands. From the repository root, the final verified suite has **213 passed, 0 failed**: Task 1 (18), Task 2 (53), Task 3 (65), and Task 4 (77).

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
- [x] Task 3 — Streaming LLM PII redaction (streaming gateway and stateful redaction)
- [x] Task 4 — SQLite token limiter, provider fallback, and FastAPI gateway

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

Send `tools/list` requests to `http://127.0.0.1:8000/mcp`. The gateway forwards to `http://127.0.0.1:8001/mcp` by default. Both addresses bind only to the local computer. The Task 2 tests use in-memory HTTP transports and do not start listening services.

## Task 3: Streaming LLM PII guardrails

The [Task 3 guide](task3_streaming_guardrails/README.md) runs a local mock LLM provider and a separate FastAPI SSE gateway. The gateway parses streamed events and redacts supported email, US SSN, and Luhn-valid card patterns before sending text to the client. Stateful buffering detects values split across events while bounding candidate memory. Run `python -m pytest -q tests/test_task3.py tests/test_task3_pii.py tests/test_task3_streaming_redaction.py tests/test_task3_final_verification.py` to test this task independently. The guide documents localhost run commands, measured latency, and detection limits.

## Task 4: Rate limiting and model fallback

The [Task 4 guide](task4_model_router/README.md) runs a local FastAPI completion gateway with mock primary and backup providers. An on-disk SQLite database enforces 50,000 reserved tokens per tenant over a 60-second sliding window. Primary HTTP 429 or a three-second timeout triggers one backup attempt; tenant quota rejection occurs before any provider call. Run `python -m pytest -q tests/test_task4_rate_limiter.py tests/test_task4_router.py tests/test_task4_gateway.py` to test this task independently. The guide covers setup, localhost commands, accounting and concurrency, error formats, and mock-security limits.

## Important

This repository is intended as an assessment demonstration. Before submission, run tests locally, verify the execution environment, and be prepared to explain the code and its assumptions.
