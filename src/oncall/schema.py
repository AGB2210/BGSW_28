"""Agent output models.

If the organisers publish an official JSON schema, align these models to it
and keep `Diagnosis.model_json_schema()` matching their file.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Diagnosis(BaseModel):
    component: str | None = Field(description="Failing service or container, or null if unknown")
    cause: str = Field(description="Most likely root cause in one or two sentences")
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list, description="Ledger IDs, e.g. ev-003")
    escalate: bool = Field(description="True when not confident enough to act")
    next_steps: list[str] = Field(default_factory=list, description="What a human should check next")
