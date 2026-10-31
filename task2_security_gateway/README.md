# Task 2 — MCP Security Gateway Proxy

This independent local FastAPI gateway accepts HTTP POST requests with JSON-RPC 2.0 messages and forwards allowed requests to a separate mock downstream server. The mock is for assessment testing; it is not a complete MCP HTTP implementation.

## Architecture

- `gateway.py` handles `/mcp` requests, parses JSON-RPC, checks tool-call parameters, applies authorization, and forwards allowed request bytes with asynchronous HTTPX. Valid downstream JSON-RPC response bytes and request IDs are preserved.
- `auth.py` resolves optional Bearer tokens to `admin` or `viewer` roles. Missing, malformed, and unknown tokens receive no role. Client role headers are ignored.
- `authorization.py` allows a tool name beginning with `admin_` only when the resolved role is `admin`. Unauthorized calls receive JSON-RPC `-32001 Unauthorized Tool Call` locally and never reach downstream.
- `mock_server.py` lists `get_customer_record` and `admin_reset_key` and returns simulated results for their `tools/call` requests. Its administrative handler changes no real credentials.

`tools/list` is forwarded without an admin role. Ordinary tool calls do not require an admin role. Invalid tool-call parameters return `-32602`; downstream connection failures and timeouts return safe JSON-RPC errors. Authentication tokens and client-supplied role headers are not forwarded to the mock server or included in responses.

## Local-only security model

The public demo credentials are `admin-demo-token` (admin) and `viewer-demo-token` (viewer). They are **mock identities for local testing only**, not production secrets. No JWT verification, external identity provider, real credential reset, or production network isolation is implemented. Both services should bind to `127.0.0.1` for this assessment.

## Install and test

Use Python 3.11 or newer. Run these commands **from the repository root** in Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r task2_security_gateway/requirements.txt
python -m pytest task2_security_gateway/tests/ -v
```

The commands above were verified with Python 3.12. If `py -3.12` is unavailable, create the environment with another installed Python 3.11+ interpreter. Task 1 does not need to be installed or started to run the gateway or its tests.

## Run both HTTP services

With the Task 2 environment active, start the mock server in one terminal from the repository root:

```powershell
python -m uvicorn task2_security_gateway.mock_server:app --host 127.0.0.1 --port 8001
```

Start the gateway in a second terminal from the repository root:

```powershell
python -m uvicorn task2_security_gateway.gateway:app --host 127.0.0.1 --port 8000
```

Send JSON-RPC requests to `http://127.0.0.1:8000/mcp`. The gateway forwards allowed calls to `http://127.0.0.1:8001/mcp`. The Task 2 tests use in-memory HTTP transports; separate live localhost HTTP checks have also been completed.
