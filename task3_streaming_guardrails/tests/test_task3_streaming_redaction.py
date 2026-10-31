"""Cross-event redaction and bounded-memory streaming checks."""

import httpx
import pytest

from task3_streaming_guardrails.gateway import create_app
from task3_streaming_guardrails.mock_provider import app as provider_app
from task3_streaming_guardrails.pii_detector import REDACTION
from task3_streaming_guardrails.sse import SSEEvent, encode_sse, parse_sse
from task3_streaming_guardrails.stream_processor import (
    MAX_EMAIL_CANDIDATE,
    MAX_NUMERIC_CANDIDATE,
    StreamingPIIRedactor,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def process(chunks):
    processor = StreamingPIIRedactor()
    pieces = [processor.feed(chunk) for chunk in chunks]
    pieces.append(processor.finish())
    return pieces, processor


@pytest.mark.parametrize(
    ("chunks", "secret"),
    [
        (["Contact john.", "smith@gmail", ".com now."], "john.smith@gmail.com"),
        (["SSN 123-", "45-", "6789 now."], "123-45-6789"),
        (["Card 4111-", "1111-1111-", "1111 now."], "4111-1111-1111-1111"),
        (["Email jane@example.com; SSN 123-45-6789; Card 4111 1111 1111 1111."], "jane@example.com"),
    ],
)
def test_split_pii_never_appears_in_emitted_pieces(chunks, secret):
    pieces, processor = process(chunks)
    text = "".join(pieces)
    assert secret not in text
    assert REDACTION in text
    assert processor.buffered_chars == 0


def test_partial_email_stays_buffered_until_complete():
    processor = StreamingPIIRedactor()
    assert processor.feed("Contact john.") == "Contact "
    assert processor.feed("smith@gmail") == ""
    assert processor.feed(".com now.") == f"{REDACTION} "
    assert processor.finish() == "now."


@pytest.mark.parametrize("candidate", ["john@gmail", "123-45-", "4111-1111-", "12345678", "123-45-x"])
def test_final_flush_redacts_incomplete_sensitive_candidates(candidate):
    pieces, _ = process(["Value: ", candidate])
    assert "".join(pieces) == f"Value: {REDACTION}"


def test_safe_text_streams_without_waiting_for_end():
    processor = StreamingPIIRedactor()
    assert processor.feed("Hello ") == "Hello "
    assert processor.feed("safe ") == "safe "
    assert processor.feed("world!") == "world!"
    assert processor.finish() == ""


def test_empty_chunks_and_lifecycle():
    processor = StreamingPIIRedactor()
    assert processor.feed("") == ""
    assert processor.finish() == ""
    with pytest.raises(RuntimeError):
        processor.feed("later")
    with pytest.raises(TypeError):
        StreamingPIIRedactor().feed(b"bytes")


def test_existing_placeholder_remains_unchanged():
    pieces, _ = process(["Already [REDACTED] ", "and safe."])
    assert "".join(pieces) == "Already [REDACTED] and safe."


def test_required_example_outputs_and_no_premature_candidate_release():
    examples = [
        (["Contact john.", "smith@gmail", ".com for details"], f"Contact {REDACTION} for details"),
        (["SSN: 123-", "45-", "6789"], f"SSN: {REDACTION}"),
        (["Card: 4111-", "1111-1111-", "1111"], f"Card: {REDACTION}"),
        (["Hello ", "how ", "are ", "you?"], "Hello how are you?"),
    ]
    for chunks, expected in examples:
        pieces, _ = process(chunks)
        assert "".join(pieces) == expected
        if REDACTION in expected:
            assert all("@" not in piece for piece in pieces)


def test_processor_does_not_log_sensitive_text(caplog):
    secret = "john.smith@gmail.com"
    pieces, _ = process(["Contact john.", "smith@gmail", ".com now."])
    assert secret not in "".join(pieces)
    assert secret not in caplog.text


@pytest.mark.parametrize(
    ("value", "limit"),
    [("a" * 500, MAX_EMAIL_CANDIDATE), ("1" * 200, MAX_NUMERIC_CANDIDATE)],
)
def test_oversized_candidate_is_replaced_and_memory_is_bounded(value, limit):
    pieces, processor = process(["Prefix ", value, "; safe."])
    result = "".join(pieces)
    assert result.startswith(f"Prefix {REDACTION};")
    assert value not in result
    assert result.count(REDACTION) == 1
    assert processor.max_buffered_chars <= limit


def test_long_safe_stream_has_bounded_state_and_incremental_output():
    processor = StreamingPIIRedactor()
    outputs = [processor.feed("safe ") for _ in range(10_000)]
    assert all(output == "safe " for output in outputs)
    assert processor.finish() == ""
    assert processor.max_buffered_chars <= MAX_EMAIL_CANDIDATE


async def one_chunk(data):
    yield data


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", ["email", "ssn", "card", "multiple", "split", "long"])
async def test_gateway_redacts_mock_provider_scenarios(scenario):
    async def downstream(request):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=provider_app), base_url="http://provider"
        ) as client:
            response = await client.post("/generate", content=request.content, headers={"content-type": "application/json"})
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    gateway = create_app("http://provider/generate", httpx.MockTransport(downstream))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), base_url="http://gateway") as client:
        response = await client.post("/generate", json={"prompt": "synthetic", "scenario": scenario})
    assert response.status_code == 200
    events = [event async for event in parse_sse(one_chunk(response.content))]
    assert events[-1] == SSEEvent("done", "[DONE]")
    output = "".join(event.data for event in events if event.event == "token")
    if scenario == "long":
        assert output == "safe " * 2000
        assert len(events) > 100
    else:
        assert REDACTION in output
        for secret in ("john.smith@example.com", "john.smith@gmail.com", "jane@example.com", "123-45-6789", "4111-1111-1111-1111", "4111 1111 1111 1111"):
            assert secret not in output


@pytest.mark.anyio
async def test_gateway_parses_network_chunks_split_at_every_byte():
    body = (
        encode_sse(SSEEvent("token", "Contact john."))
        + encode_sse(SSEEvent("token", "smith@gmail"))
        + encode_sse(SSEEvent("token", ".com now."))
        + encode_sse(SSEEvent("done", "[DONE]"))
    )

    class ByteStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for byte in body:
                yield bytes([byte])

    gateway = create_app(
        "http://provider/generate",
        httpx.MockTransport(lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ByteStream())),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), base_url="http://gateway") as client:
        response = await client.post("/generate", json={"prompt": "test"})
    events = [event async for event in parse_sse(one_chunk(response.content))]
    output = "".join(event.data for event in events if event.event == "token")
    assert output == f"Contact {REDACTION} now."


@pytest.mark.anyio
async def test_gateway_does_not_forward_provider_done_data_or_partial_pii_on_error(caplog):
    class FailingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield encode_sse(SSEEvent("token", "Contact john@gmail"))
            raise httpx.ReadError("private provider detail")

    gateway = create_app(
        "http://provider/generate",
        httpx.MockTransport(lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=FailingStream())),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), base_url="http://gateway") as client:
        response = await client.post("/generate", json={"prompt": "test"})
    assert "john@gmail" not in response.text
    assert "private provider detail" not in response.text
    assert "john@gmail" not in caplog.text
    events = [event async for event in parse_sse(one_chunk(response.content))]
    assert events[-1] == SSEEvent("error", "Provider stream interrupted")

    done_body = encode_sse(SSEEvent("done", "secret@example.com"))
    gateway = create_app(
        "http://provider/generate",
        httpx.MockTransport(lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=done_body)),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), base_url="http://gateway") as client:
        response = await client.post("/generate", json={"prompt": "test"})
    assert "secret@example.com" not in response.text
    events = [event async for event in parse_sse(one_chunk(response.content))]
    assert events == [SSEEvent("done", "[DONE]")]
