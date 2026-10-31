"""Task 3 edge cases found in final security and streaming review."""

import httpx
import pytest

from task3_streaming_guardrails.gateway import create_app, relay_events
from task3_streaming_guardrails.sse import (
    MAX_SSE_EVENT_CHARS,
    MAX_SSE_LINE_CHARS,
    SSEEvent,
    encode_sse,
    parse_sse,
)
from task3_streaming_guardrails.stream_processor import StreamingPIIRedactor


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Request:
    async def is_disconnected(self):
        return False


class Upstream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def aiter_bytes(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


class Client:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


async def events_from(chunks):
    async def source():
        for chunk in chunks:
            yield chunk
    return [event async for event in parse_sse(source())]


@pytest.mark.anyio
async def test_single_byte_network_chunks_and_multiple_events_in_one_chunk():
    raw = (
        encode_sse(SSEEvent("token", "café 😀 "))
        + encode_sse(SSEEvent("token", "john."))
        + encode_sse(SSEEvent("token", "smith@example.com"))
        + encode_sse(SSEEvent("done", "[DONE]"))
    )
    expected = [
        SSEEvent("token", "café 😀 "),
        SSEEvent("token", "john."),
        SSEEvent("token", "smith@example.com"),
        SSEEvent("done", "[DONE]"),
    ]
    assert await events_from([bytes([byte]) for byte in raw]) == expected
    assert await events_from([raw, b""]) == expected

    upstream, client = Upstream([bytes([byte]) for byte in raw]), Client()
    emitted = [chunk async for chunk in relay_events(Request(), upstream, client)]
    client_events = await events_from(emitted)
    assert client_events == [
        SSEEvent("token", "café 😀 "),
        SSEEvent("token", "[REDACTED]"),
        SSEEvent("done", "[DONE]"),
    ]
    assert upstream.closed and client.closed


@pytest.mark.anyio
async def test_pii_at_start_and_end_and_consecutive_values():
    raw = (
        encode_sse(SSEEvent("token", "john@example.com"))
        + encode_sse(SSEEvent("token", ";123-45-6789;4111-1111-1111-1111"))
        + encode_sse(SSEEvent("done", "[DONE]"))
    )
    emitted = [chunk async for chunk in relay_events(Request(), Upstream([raw]), Client())]
    events = await events_from(emitted)
    assert events == [
        SSEEvent("token", "[REDACTED];[REDACTED];"),
        SSEEvent("token", "[REDACTED]"),
        SSEEvent("done", "[DONE]"),
    ]


@pytest.mark.anyio
async def test_unexpected_eof_discards_incomplete_candidate_and_closes_resources():
    raw = encode_sse(SSEEvent("token", "Safe john@gmail"))
    upstream, client = Upstream([raw]), Client()
    emitted = [chunk async for chunk in relay_events(Request(), upstream, client)]
    assert await events_from(emitted) == [
        SSEEvent("token", "Safe "),
        SSEEvent("error", "Provider stream interrupted"),
    ]
    assert upstream.closed and client.closed


@pytest.mark.anyio
@pytest.mark.parametrize("raw", [b"event: token\ndata: \xff\n\n", b"event: token\ndata: unfinished"])
async def test_malformed_or_incomplete_sse_never_releases_raw_data(raw):
    upstream, client = Upstream([raw]), Client()
    emitted = [chunk async for chunk in relay_events(Request(), upstream, client)]
    assert await events_from(emitted) == [SSEEvent("error", "Provider stream interrupted")]
    assert upstream.closed and client.closed


@pytest.mark.anyio
async def test_sse_line_and_event_limits_fail_safely():
    with pytest.raises(ValueError, match="line exceeds"):
        await events_from([b"data: " + b"x" * (MAX_SSE_LINE_CHARS + 1)])
    rows = b"".join(b"data: " + b"x" * 10_000 + b"\n" for _ in range(8)) + b"\n"
    with pytest.raises(ValueError, match="event exceeds"):
        await events_from([rows])

    upstream, client = Upstream([b"data: " + b"x" * (MAX_SSE_LINE_CHARS + 1)]), Client()
    emitted = [chunk async for chunk in relay_events(Request(), upstream, client)]
    assert await events_from(emitted) == [SSEEvent("error", "Provider stream interrupted")]
    assert upstream.closed and client.closed


@pytest.mark.anyio
async def test_upstream_read_timeout_is_generic_and_cleans_up():
    class TimeoutStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.ReadTimeout("provider secret")
            yield b""  # pragma: no cover

    gateway = create_app(
        "http://provider/generate",
        httpx.MockTransport(lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=TimeoutStream())),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), base_url="http://gateway") as client:
        response = await client.post("/generate", json={"prompt": "test"})
    assert "provider secret" not in response.text
    assert await events_from([response.content]) == [SSEEvent("error", "Provider stream interrupted")]


def test_processor_emits_safe_unicode_incrementally():
    processor = StreamingPIIRedactor()
    assert processor.feed("Café 😀 ") == "Café 😀 "
    assert processor.feed("safe ") == "safe "
    assert processor.finish() == ""
