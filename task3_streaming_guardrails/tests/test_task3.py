"""Task 3 Stage 1 streaming and SSE protocol checks."""

import asyncio
import json

import httpx
import pytest

from task3_streaming_guardrails.gateway import create_app, relay_events
from task3_streaming_guardrails.mock_provider import MOCK_TOKENS, app as provider_app
from task3_streaming_guardrails.sse import SSEEvent, encode_sse, parse_sse


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _one_chunk(data: bytes):
    yield data


@pytest.mark.anyio
async def test_provider_streams_synthetic_token_events():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=provider_app), base_url="http://provider"
    ) as client:
        response = await client.post("/generate", json={"prompt": "Say hello"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [event async for event in parse_sse(_one_chunk(response.content))]
    assert [event.data for event in events if event.event == "token"] == list(MOCK_TOKENS)
    assert events[-1] == SSEEvent(event="done", data="[DONE]")
    assert "Say hello" not in response.text


@pytest.mark.anyio
async def test_provider_rejects_invalid_generation_request():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=provider_app), base_url="http://provider"
    ) as client:
        response = await client.post("/generate", json={"prompt": ""})
    assert response.status_code == 422


@pytest.mark.anyio
async def test_sse_parser_handles_split_frames_utf8_crlf_and_multiline_data():
    raw = b"\xef\xbb\xbfevent: token\r\ndata: caf\xc3\xa9\r\n\r\n: keepalive\r\nevent: token\r\ndata: first\r\ndata: second\r\n\r\n"

    async def bytes_one_at_a_time():
        for byte in raw:
            yield bytes([byte])

    events = [event async for event in parse_sse(bytes_one_at_a_time())]
    assert events == [
        SSEEvent(event="token", data="café"),
        SSEEvent(event="token", data="first\nsecond"),
    ]
    assert encode_sse(events[1]) == b"event: token\ndata: first\ndata: second\n\n"


@pytest.mark.anyio
async def test_gateway_forwards_generation_request_and_sse_events():
    upstream_requests = []
    upstream_body = encode_sse(SSEEvent("token", "First")) + encode_sse(SSEEvent("token", " second")) + encode_sse(SSEEvent("done", "[DONE]"))

    def downstream(request: httpx.Request) -> httpx.Response:
        upstream_requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=upstream_body)

    gateway = create_app("http://provider/generate", httpx.MockTransport(downstream))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
    ) as client:
        response = await client.post("/generate", json={"prompt": "Test prompt"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert len(upstream_requests) == 1
    assert upstream_requests[0].url == "http://provider/generate"
    assert json.loads(upstream_requests[0].content) == {"prompt": "Test prompt"}
    events = [event async for event in parse_sse(_one_chunk(response.content))]
    assert "".join(event.data for event in events if event.event == "token") == "First second"
    assert events[-1] == SSEEvent("done", "[DONE]")


@pytest.mark.anyio
async def test_gateway_handles_provider_connection_failure_without_traceback():
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private connection detail", request=request)

    gateway = create_app("http://provider/generate", httpx.MockTransport(broken))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
    ) as client:
        response = await client.post("/generate", json={"prompt": "Test"})

    assert response.status_code == 502
    assert response.json() == {"error": "Provider unavailable"}
    assert "private connection detail" not in response.text
    assert "Traceback" not in response.text


@pytest.mark.anyio
async def test_gateway_handles_bad_upstream_http_response():
    gateway = create_app(
        "http://provider/generate",
        httpx.MockTransport(lambda _: httpx.Response(500, text="private provider traceback")),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
    ) as client:
        response = await client.post("/generate", json={"prompt": "Test"})

    assert response.status_code == 502
    assert response.json() == {"error": "Invalid provider response"}
    assert "private provider traceback" not in response.text


@pytest.mark.anyio
async def test_gateway_turns_midstream_failure_into_safe_sse_error():
    class FailingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield encode_sse(SSEEvent("token", "partial"))
            raise httpx.ReadError("private streaming detail")

    gateway = create_app(
        "http://provider/generate",
        httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=FailingStream())
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
    ) as client:
        response = await client.post("/generate", json={"prompt": "Test"})

    events = [event async for event in parse_sse(_one_chunk(response.content))]
    assert events == [SSEEvent("error", "Provider stream interrupted")]
    assert "private streaming detail" not in response.text


class FakeRequest:
    def __init__(self, disconnect_after: int | None = None):
        self.checks = 0
        self.disconnect_after = disconnect_after

    async def is_disconnected(self):
        self.checks += 1
        return self.disconnect_after is not None and self.checks > self.disconnect_after


class FakeUpstream:
    def __init__(self, frames, gate=None):
        self.frames = frames
        self.gate = gate
        self.closed = False

    async def aiter_bytes(self):
        for index, frame in enumerate(self.frames):
            if index == 1 and self.gate is not None:
                await self.gate.wait()
            yield frame

    async def aclose(self):
        self.closed = True


class FakeClient:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


@pytest.mark.anyio
async def test_relay_yields_first_event_before_upstream_finishes():
    gate = asyncio.Event()
    upstream = FakeUpstream(
        [encode_sse(SSEEvent("token", "first ")), encode_sse(SSEEvent("done", "[DONE]"))],
        gate=gate,
    )
    client = FakeClient()
    stream = relay_events(FakeRequest(), upstream, client)

    first = await asyncio.wait_for(anext(stream), timeout=0.2)
    assert first == encode_sse(SSEEvent("token", "first "))
    assert not gate.is_set()
    assert not upstream.closed

    gate.set()
    remaining = [chunk async for chunk in stream]
    assert remaining == [encode_sse(SSEEvent("done", "[DONE]"))]
    assert upstream.closed and client.closed


@pytest.mark.anyio
async def test_relay_stops_and_closes_upstream_on_client_disconnect():
    upstream = FakeUpstream(
        [encode_sse(SSEEvent("token", "first ")), encode_sse(SSEEvent("token", "second"))]
    )
    client = FakeClient()
    chunks = [chunk async for chunk in relay_events(FakeRequest(disconnect_after=1), upstream, client)]

    assert chunks == [encode_sse(SSEEvent("token", "first "))]
    assert upstream.closed and client.closed
