# Task 3 — LLM Streaming Guardrails (Stages 1–3)

This independent assessment task streams synthetic LLM output through a local FastAPI gateway. The gateway redacts supported PII before forwarding text. No external provider, credentials, or Task 1/2 services are needed.

## Architecture

`mock_provider.py` accepts `POST /generate` and emits delayed SSE `token` events plus a final `done` event. `gateway.py` opens an asynchronous HTTPX streaming connection, parses events with `sse.py`, feeds decoded token text to `StreamingPIIRedactor`, and emits only sanitized SSE events. It does not scan serialized SSE frames. A disconnected client or provider error closes the upstream response and HTTP client. Connection failures return a generic HTTP 502; stream failures, including EOF without `done`, produce a generic SSE `error` event and discard pending candidate text. The gateway never forwards provider error or `done` payloads verbatim. The SSE parser limits each line to 16,384 characters and each event's data to 65,536 characters; an oversized frame produces a safe stream error.

`pii_detector.py` identifies complete email addresses, US SSNs in `XXX-XX-XXXX` format, and Luhn-valid 13–19 digit card numbers with optional spaces or hyphens. Markdown `mailto:` links with valid email targets are redacted as a single span in complete strings. `stream_processor.py` reuses the detector for complete candidates across SSE event boundaries. Both modules are independent of the web framework and perform no logging.

The provider has `normal`, `email`, `ssn`, `card`, `multiple`, `split`, and `long` synthetic scenarios. The `normal` scenario keeps the original nonsensitive sample. The `long` scenario emits 2,000 safe token events for memory and streaming checks. A `scenario` value outside this list is rejected by Pydantic.

## Stateful buffer and latency

The processor holds one current email-like or numeric candidate until a separating character or stream completion establishes its full value. It releases unambiguous prefixes, such as `Contact `, immediately; a partial `john.` remains in memory while subsequent chunks may complete an email. A candidate spanning several network chunks or SSE events is redacted as one value. On a clean `done` event, the processor flushes safe text and replaces recognizable incomplete values, including `john@gmail` and SSN/card-shaped numeric prefixes. On unexpected EOF, a provider error, or client disconnect it discards the pending candidate.

The retained candidate is limited to **256 characters for email-like runs** and **64 characters for numeric runs**. If a candidate reaches its limit, the processor emits one `[REDACTED]` placeholder and discards characters until the next boundary. It does not accumulate the full response or repeatedly scan it. Safe text remains incremental, though words without a boundary can be delayed until the following event or completion. Long unbroken ordinary words may also be redacted at the limit. The implementation favors withholding ambiguous text over releasing a possible secret.

Local measurements are workload and machine dependent. With the six-token `normal` scenario, the provider's median first output was about 0.094 s and the gateway's was about 0.375 s in three warmed localhost runs. The roughly 0.282 s difference includes an extra HTTP hop, connection setup, and waiting for a safe candidate boundary; it is **not** a measurement of redaction CPU time alone. First safe output still arrived before stream completion. A 2,000-event `long` stream was forwarded incrementally. The processor retains only its capped candidate; the SSE parser retains only its capped current line and event.

This is a pattern-based assessment implementation, **not a production-certified DLP system**. It does not cover all PII types, obfuscations, Unicode email forms, or context-dependent secrets. A plain name or email local part with no `@` cannot be distinguished reliably from ordinary text. The complete-string card detector requires Luhn validation; the streaming processor conservatively redacts long numeric runs even when their checksum is invalid. Do not use these demo scenarios as a substitute for production security review.

## Install and test

Use Python 3.11 or newer. From the repository root in Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r task3_streaming_guardrails/requirements.txt
python -m pytest task3_streaming_guardrails/tests/ -v
```

Run `python -m pytest -q` from the root to test all implemented tasks together. Task 3 has its own requirements file and imports no Task 1 or Task 2 application modules.

## Run locally

Start each service from the repository root in a separate terminal with the virtual environment active:

```powershell
python -m uvicorn task3_streaming_guardrails.mock_provider:app --host 127.0.0.1 --port 8101
```

```powershell
python -m uvicorn task3_streaming_guardrails.gateway:app --host 127.0.0.1 --port 8100
```

Send a JSON request to `POST http://127.0.0.1:8100/generate` with `Accept: text/event-stream`, for example `{"prompt":"synthetic","scenario":"split"}`. Token event boundaries may differ from the provider because the gateway buffers ambiguous text. The concatenated sanitized output for `split` is `Contact [REDACTED]; SSN: [REDACTED]; Card: [REDACTED]. Safe end.` The last event is `event: done` with `[DONE]`.

The tests cover the detector, split identifiers, per-chunk output, oversized candidates, long streams, SSE network fragmentation, errors, cleanup, and nonleakage. A separate localhost HTTP check verifies that the first safe output arrives before stream completion.
