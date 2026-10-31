# Task 4 — Rate Limiting and Model Fallback Router

This independent package implements an on-disk SQLite token limiter, an asynchronous primary/backup model router, local synthetic providers, and a FastAPI `POST /completions` gateway. No external API credentials, paid provider, streaming generation, or real tokenizer are used.

## Policy and schema

Each tenant has a **50,000-token limit** over the preceding **60 seconds**. A reservation with timestamp `t` counts while `t > now - 60`; it expires exactly 60 seconds after creation. Requests for zero, negative, non-integer, or Boolean token budgets are rejected. Tenant identifiers must be nonempty strings. The caller supplies an estimated input-token count plus the maximum allowed output-token budget as one positive integer.

`database.py` creates a file-backed SQLite database automatically. Its `token_reservations` table stores a random reservation ID, a SHA-256 tenant identifier hash, an optional opaque request ID, creation time, reserved tokens, currently charged tokens, optional actual tokens, a status (`active`, `settled`, or `cancelled`), an execution claim state, and a request fingerprint. Existing Stage 1 databases are migrated to add the two claim columns. The `(tenant_hash, request_id)` pair is unique. An index on `(tenant_hash, created_at)` supports window queries; a timestamp index supports cleanup. Raw tenant API keys are never stored. Use high-entropy API key identifiers because an unsalted hash of a guessable identifier is not a strong secret vault.

SQLite uses WAL mode, a five-second busy timeout by default, and a separate connection for each operation. Every reserve, reconcile, release, and usage query runs under `BEGIN IMMEDIATE`. The transaction deletes expired rows, reads the current tenant total, and writes the reservation before another writer can pass the same quota check. A database failure raises a generic `RateLimiterStorageError`; a caller must fail closed rather than send a provider request. The SQLite file and WAL sidecars are excluded by the root `.gitignore`.

## Reservation lifecycle

1. The gateway computes `input_tokens + maximum_output_tokens` using the deterministic mock estimator described below; this package does not implement real tokenization. It calls `reserve(tenant_identifier, budget, request_id=opaque_id)` before sending a provider request. A returned decision includes `allowed`, `used_tokens`, `remaining_tokens`, and, when allowed, a `reservation_id`. The router checks this active reservation's tenant, request ID, and exact budget before contacting either provider.
2. Keep **the same reservation** through model fallback attempts. Repeating `reserve` with the same request ID and budget within the window returns the existing reservation with `reused=True`; it does not charge again. The router rejects a settled reservation rather than reexecuting it and blocks overlapping attempts for the same reservation within one router process. The gateway also atomically claims the reservation in SQLite before calling a provider, preventing duplicate execution by separate gateway instances using the same database.
3. On completion, call `reconcile(tenant_identifier, reservation_id, actual_tokens)` once. The reservation is reduced to actual input-plus-output usage. A repeat with the same actual count is idempotent; a conflicting repeat is rejected. Actual usage may be zero but must never exceed the reservation. An over-budget report raises and leaves the full reserved charge in place. The router passes the output cap to both providers and validates reported usage; this cannot undo tokens already consumed externally by a noncompliant provider.
4. If all attempts fail **before any tokens are consumed**, call `release`. This sets charged usage to zero and is idempotent. If a failed attempt consumed tokens, reconcile those tokens instead. Settled reservations cannot be released.

Request IDs give idempotency **within the 60-second window**; expired rows are removed and no longer deduplicate later requests. Reservations are timestamped when admitted, including long-running requests. Reconciliation after expiry is rejected because the row has been cleaned up. A process crash after the claim leaves it marked in progress and charged until expiry; a duplicate is rejected rather than automatically replayed. Longer-lived durable request records would be needed to prevent reuse after the 60-second retention window.

## Async provider routing

`mock_providers.py` defines independent FastAPI primary and backup test apps. Configurable scenarios return synthetic HTTP 200, 429, 400, 500, malformed JSON structure, or delayed responses. The mocks report input/output token usage and cap generated output at `max_output_tokens`. They need no paid API or secret.

`providers.py` sends a structured JSON completion request using a short-lived HTTPX `AsyncClient`, validates strict response fields and request IDs, and classifies rate limits, timeouts, connection failures, malformed data, and output-budget violations. Each client closes its connections on completion or cancellation. Provider URLs and test transports are injectable. The module does not log prompts, tenant identifiers, or credentials.

`router.py` checks an existing reservation, calls primary, and awaits its response with a **3.0-second default deadline**. Only primary HTTP 429 or timeout triggers backup. Backup is invoked once, after the primary attempt has returned or been cancelled; it also has a bounded timeout. Primary HTTP 400, HTTP 500, malformed responses, connection failures, and invalid token usage do **not** trigger fallback. SQLite work runs in worker threads so a busy database does not block the async event loop. Local cancellation does not prove that the upstream provider stopped processing or billing.

The router uses one logical reservation for both attempts. A successful primary, or backup after a definite 429 rejection, is reconciled to its reported input-plus-output tokens. If primary times out, its consumption is unknown: a successful backup reconciles the **full reserved budget** even when backup reports less. If all attempts fail, a reservation is released only when all outcomes are definite pre-generation rejections (HTTP 429 or 400). HTTP 500, connection failures, malformed responses, and timeouts keep the full reservation charged. This policy prevents a failed attempt from being silently treated as free; it is conservative tenant quota accounting, **not an exact record of provider billing**. In the worst case a timed-out primary and successful backup could both bill tokens, exceeding this single reservation's charge. A later production design would need provider idempotency or a larger worst-case reservation for strict aggregate-billing accounting.

Routing results contain fixed error codes: `PRIMARY_PROVIDER_FAILED`, `BACKUP_PROVIDER_FAILED`, `ALL_PROVIDERS_UNAVAILABLE`, `PROVIDER_TIMEOUT`, `INVALID_PROVIDER_RESPONSE`, `TOKEN_BUDGET_EXCEEDED`, and `ACCOUNTING_UNAVAILABLE`. Messages are generic and omit provider bodies, stack traces, internal URLs, and prompts. The gateway translates these to standardized HTTP errors.

## FastAPI gateway

`gateway.py` accepts `POST /completions` with `X-API-Key`. The public demo keys `tenant-a-demo-key` and `tenant-b-demo-key` map to separate tenant identifiers. They are **local assessment credentials only**, not real authentication. The keys are never forwarded to providers, written to SQLite, or returned in responses. The database stores only SHA-256 tenant identifier hashes.

The request body contains an opaque `request_id` matching `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`, a nonblank string `prompt` of at most 1,000 characters, and a positive integer `max_output_tokens`. Extra fields, including client-supplied token counts, are rejected. The mock input estimate is `ceil(len(prompt.encode("utf-8")) / 4)`, with a minimum of one token. This is deterministic but **not a real tokenizer**. The gateway reserves the estimate plus the output cap before either provider is called. A budget over 50,000 is an invalid request; a valid request that exceeds remaining tenant quota gets HTTP 429 locally and never triggers provider fallback.

The gateway reserves with the request ID, then uses a SQLite `BEGIN IMMEDIATE` transaction to claim that tenant's reservation. The claim stores a SHA-256 fingerprint of prompt and budget, not raw prompt text. A duplicate in progress or already completed returns HTTP 409. Reusing an ID with different prompt or budget also returns 409. Claims survive process loss; they are not retried automatically while the record remains. The fingerprint is deterministic and could reveal low-entropy prompts through guessing, so production systems would need a keyed digest or a different privacy design.

Success returns HTTP 200 with a body like:

```json
{"request_id":"req-1","provider":"primary","text":"Synthetic primary completion.","usage":{"input_tokens":4,"output_tokens":5,"charged_tokens":9}}
```

Errors use `{"request_id":"req-1","error":{"code":"RATE_LIMIT_EXCEEDED","message":"Tenant token limit exceeded"}}`. Public codes are `INVALID_REQUEST` (422), `UNAUTHORIZED` (401), `RATE_LIMIT_EXCEEDED` (429), `DUPLICATE_REQUEST` (409), `PROVIDER_TIMEOUT` (504), `ALL_PROVIDERS_UNAVAILABLE` (503), `INVALID_PROVIDER_RESPONSE` (502), and `INTERNAL_GATEWAY_ERROR` (500). Validation errors have a null request ID because the body is untrusted. Errors omit prompts, keys, provider URLs, stack traces, and database details.

The gateway's completion handler is async. Blocking SQLite operations run in worker threads; provider calls use HTTPX async clients. A locally cancelled provider request may still incur upstream billing. If a gateway process dies after claiming a request, its reservation stays charged until expiry rather than treating uncertain consumption as zero.

## Use locally

From the repository root, use Python 3.11 or newer and an active virtual environment:

```powershell
python -m pip install -r task4_model_router/requirements.txt
python -m pytest task4_model_router/tests/ -v
```

Example:

```python
from task4_model_router.rate_limiter import SlidingWindowRateLimiter

limiter = SlidingWindowRateLimiter("task4_usage.sqlite3")
decision = limiter.reserve("opaque-tenant-key-id", 15000, request_id="completion-1")
if decision.allowed:
    # The router can use this reservation; direct accounting is shown here.
    limiter.reconcile("opaque-tenant-key-id", decision.reservation_id, 12000)
```

The database path is local and should not be committed. The tests use temporary on-disk databases and a controllable clock. From the root, `python -m pytest -q` runs all tasks. This package imports no application code from Tasks 1–3.

For a live localhost demo, start these commands in separate terminals from the repository root:

```powershell
python -m uvicorn task4_model_router.mock_providers:primary_app --host 127.0.0.1 --port 8201
python -m uvicorn task4_model_router.mock_providers:backup_app --host 127.0.0.1 --port 8202
python -m uvicorn task4_model_router.gateway:app --host 127.0.0.1 --port 8200
```

Send the example JSON body to `http://127.0.0.1:8200/completions` with `X-API-Key: tenant-a-demo-key`:

```json
{"request_id":"req-1","prompt":"Synthetic prompt","max_output_tokens":20}
```

The provider mock exposes `/stats` on localhost for test-only call counts. Set `TASK4_PRIMARY_SCENARIO=429` (or `slow`, `400`, `500`, `malformed`) before starting the primary mock to exercise other paths. `slow` waits 3.1 seconds, past the gateway's default 3.0-second primary deadline. The gateway database path can be set with `TASK4_DATABASE_PATH`; its default is an ignored `usage.sqlite3` file in this package.
