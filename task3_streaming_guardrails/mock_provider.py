"""Local mock LLM provider that streams synthetic SSE tokens."""

import asyncio

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from task3_streaming_guardrails.models import GenerationRequest
from task3_streaming_guardrails.sse import SSEEvent, encode_sse


app = FastAPI(title="Mock LLM Provider")

MOCK_TOKENS = ("Hello", " from", " a", " local", " mock", " LLM.")
TOKEN_DELAY_SECONDS = 0.08

SCENARIOS = {
    "normal": MOCK_TOKENS,
    "email": ("Contact ", "john.", "smith@example", ".com for details."),
    "ssn": ("SSN: ", "123-", "45-", "6789 is synthetic."),
    "card": ("Card: ", "4111-", "1111-1111-", "1111 is synthetic."),
    "multiple": ("Email jane@example.com; SSN 123-45-6789; card 4111 1111 1111 1111; done.",),
    "split": (
        "Contact ", "john.", "smith@gmail", ".com; SSN: ", "123-", "45-",
        "6789; Card: ", "4111-", "1111-1111-", "1111. Safe end.",
    ),
}


@app.post("/generate")
async def generate(request: GenerationRequest) -> StreamingResponse:
    async def events():
        tokens = ("safe " for _ in range(2000)) if request.scenario == "long" else SCENARIOS[request.scenario]
        for token in tokens:
            await asyncio.sleep(0 if request.scenario == "long" else TOKEN_DELAY_SECONDS)
            yield encode_sse(SSEEvent(event="token", data=token))
        yield encode_sse(SSEEvent(event="done", data="[DONE]"))

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )
