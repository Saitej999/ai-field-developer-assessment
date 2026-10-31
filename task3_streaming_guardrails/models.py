"""Request model shared only within Task 3."""

from typing import Literal

from pydantic import BaseModel, Field


class GenerationRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=1000)
    scenario: Literal["normal", "email", "ssn", "card", "multiple", "split", "long"] = "normal"
