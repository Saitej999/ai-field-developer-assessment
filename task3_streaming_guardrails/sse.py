"""Incremental SSE parsing and encoding for the streaming gateway.

The parser keeps only the current line and event in memory. Its UTF-8 decoder
handles characters and CRLF delimiters split across arbitrary byte chunks.
"""

import codecs
from collections.abc import AsyncIterable, AsyncIterator, Iterator
from dataclasses import dataclass


MAX_SSE_LINE_CHARS = 16_384
MAX_SSE_EVENT_CHARS = 65_536


@dataclass(frozen=True)
class SSEEvent:
    event: str
    data: str


def encode_sse(event: SSEEvent) -> bytes:
    """Encode one event, including multi-line data, as a complete SSE frame."""
    lines = [f"event: {event.event}"]
    lines.extend(f"data: {line}" for line in event.data.split("\n"))
    return ("\n".join(lines) + "\n\n").encode("utf-8")


async def parse_sse(chunks: AsyncIterable[bytes]) -> AsyncIterator[SSEEvent]:
    """Yield complete SSE events without buffering the upstream response."""
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    line_chars: list[str] = []
    data_lines: list[str] = []
    event_data_chars = 0
    event_name = "message"
    skip_next_lf = False
    first_character = True

    def process_line(line: str) -> SSEEvent | None:
        nonlocal event_name, data_lines, event_data_chars
        if not line:
            if not data_lines:
                event_name = "message"
                return None
            event = SSEEvent(event=event_name, data="\n".join(data_lines))
            event_name = "message"
            data_lines = []
            event_data_chars = 0
            return event
        if line.startswith(":"):
            return None
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "data":
            event_data_chars += len(value) + (1 if data_lines else 0)
            if event_data_chars > MAX_SSE_EVENT_CHARS:
                raise ValueError("SSE event exceeds size limit")
            data_lines.append(value)
        elif field == "event":
            event_name = value
        return None

    def process_text(text: str) -> Iterator[SSEEvent]:
        nonlocal skip_next_lf, first_character
        for character in text:
            if first_character:
                first_character = False
                if character == "\ufeff":
                    continue
            if skip_next_lf:
                skip_next_lf = False
                if character == "\n":
                    continue
            if character in ("\r", "\n"):
                event = process_line("".join(line_chars))
                line_chars.clear()
                if event is not None:
                    yield event
                if character == "\r":
                    skip_next_lf = True
            else:
                if len(line_chars) >= MAX_SSE_LINE_CHARS:
                    raise ValueError("SSE line exceeds size limit")
                line_chars.append(character)

    async for chunk in chunks:
        for event in process_text(decoder.decode(chunk)):
            yield event

    # An incomplete final frame is deliberately discarded; only a blank line
    # dispatches an SSE event.
    for event in process_text(decoder.decode(b"", final=True)):
        yield event
