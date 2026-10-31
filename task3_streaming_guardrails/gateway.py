"""Async SSE gateway with stateful PII redaction for a local mock provider.

Complete token events are parsed, sanitized, then forwarded to the client.
"""

from collections.abc import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from task3_streaming_guardrails.models import GenerationRequest
from task3_streaming_guardrails.sse import SSEEvent, encode_sse, parse_sse
from task3_streaming_guardrails.stream_processor import StreamingPIIRedactor


DEFAULT_PROVIDER_URL = "http://127.0.0.1:8101/generate"


async def relay_events(
    request: Request, upstream: httpx.Response, client: httpx.AsyncClient
) -> AsyncIterator[bytes]:
    """Forward only safe text; keep incomplete PII candidates in memory."""
    processor = StreamingPIIRedactor()
    completed = False
    disconnected = False
    provider_error = False
    try:
        async for event in parse_sse(upstream.aiter_bytes()):
            if await request.is_disconnected():
                disconnected = True
                break
            if event.event == "token":
                safe_text = processor.feed(event.data)
                if safe_text:
                    yield encode_sse(SSEEvent(event="token", data=safe_text))
            elif event.event == "done":
                tail = processor.finish()
                if tail:
                    yield encode_sse(SSEEvent(event="token", data=tail))
                yield encode_sse(SSEEvent(event="done", data="[DONE]"))
                completed = True
                break
            elif event.event == "error":
                yield encode_sse(SSEEvent(event="error", data="Provider stream interrupted"))
                provider_error = True
                break
        if not (completed or disconnected or provider_error):
            # EOF without the provider's completion marker is an interrupted
            # stream. Discard the candidate instead of flushing partial text.
            yield encode_sse(SSEEvent(event="error", data="Provider stream interrupted"))
    except (httpx.HTTPError, UnicodeDecodeError, ValueError, TypeError, RuntimeError):
        if not await request.is_disconnected():
            yield encode_sse(SSEEvent(event="error", data="Provider stream interrupted"))
    finally:
        await upstream.aclose()
        await client.aclose()


def create_app(
    provider_url: str = DEFAULT_PROVIDER_URL,
    provider_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Create the gateway; injectable transport keeps unit tests local."""
    gateway = FastAPI(title="LLM Streaming Gateway")

    @gateway.post("/generate")
    async def generate(payload: GenerationRequest, request: Request):
        client = httpx.AsyncClient(
            transport=provider_transport,
            timeout=httpx.Timeout(5.0, read=10.0),
        )
        try:
            upstream_request = client.build_request("POST", provider_url, json=payload.model_dump(exclude_unset=True))
            upstream = await client.send(upstream_request, stream=True)
        except (httpx.RequestError, httpx.TimeoutException):
            await client.aclose()
            return JSONResponse(status_code=502, content={"error": "Provider unavailable"})
        except Exception:
            await client.aclose()
            return JSONResponse(status_code=502, content={"error": "Provider unavailable"})

        if upstream.status_code != 200 or "text/event-stream" not in upstream.headers.get("content-type", ""):
            await upstream.aclose()
            await client.aclose()
            return JSONResponse(status_code=502, content={"error": "Invalid provider response"})

        async def close_upstream() -> None:
            await upstream.aclose()
            await client.aclose()

        return StreamingResponse(
            relay_events(request, upstream, client),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            background=BackgroundTask(close_upstream),
        )

    return gateway


app = create_app()
