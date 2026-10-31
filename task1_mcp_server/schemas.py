"""Strict tool argument schemas.

Pydantic validates the parameters at runtime, and model_json_schema()
advertises identical constraints in the MCP tools/list response.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


CustomerId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^CUST-[0-9]{5}$"),
]

RefundReason = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=10),
]


class StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GetCustomerRecordArgs(StrictArgs):
    customer_id: CustomerId


class TriggerRefundArgs(StrictArgs):
    customer_id: CustomerId
    amount: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    reason: RefundReason
