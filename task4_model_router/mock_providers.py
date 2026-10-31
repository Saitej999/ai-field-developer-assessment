"""Local synthetic primary and backup completion providers for tests."""

import asyncio
import os
from typing import Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field


Scenario = Literal["success", "429", "slow", "400", "500", "malformed"]


class MockCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str = Field(min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=1000)
    input_tokens: int = Field(ge=0)
    max_output_tokens: int = Field(gt=0)


def create_mock_provider(
    provider_name: Literal["primary", "backup"],
    *,
    scenario: Scenario = "success",
    delay_seconds: float = 3.1,
    output_tokens: int = 5,
) -> FastAPI:
    """Create a configurable test app; all generated content is synthetic."""
    app = FastAPI(title=f"Mock {provider_name} LLM Provider")
    app.state.calls = 0

    @app.get("/stats")
    async def stats():
        """Local test instrumentation for forwarding assertions."""
        return {"calls": app.state.calls}

    @app.post("/complete")
    async def complete(request: MockCompletionRequest):
        app.state.calls += 1
        if scenario == "slow":
            await asyncio.sleep(delay_seconds)
        if scenario in {"429", "400", "500"}:
            return JSONResponse(status_code=int(scenario), content={"error": "synthetic failure"})
        if scenario == "malformed":
            return {"request_id": request.request_id, "text": "synthetic", "usage": {"output_tokens": "invalid"}}
        generated = min(output_tokens, request.max_output_tokens)
        return {
            "request_id": request.request_id,
            "text": f"Synthetic {provider_name} completion.",
            "usage": {"input_tokens": request.input_tokens, "output_tokens": generated},
        }

    return app


primary_app = create_mock_provider(
    "primary", scenario=os.environ.get("TASK4_PRIMARY_SCENARIO", "success")
)
backup_app = create_mock_provider(
    "backup", scenario=os.environ.get("TASK4_BACKUP_SCENARIO", "success")
)
