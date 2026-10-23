"""Agent output models.

`IncidentReport` is the one JSON document the agent produces per incident.
`schemas/incident_report.schema.json` is generated from it (`oncall schema`)
and a test keeps the two in sync.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"

ActionName = Literal["restart_service", "toggle_feature_flag", "throttle_load_generator", "scale_service"]


class Diagnosis(BaseModel):
    """What the model returns at the end of an investigation."""

    component: str | None = Field(description="Failing service or container, or null if unknown")
    category: Literal["application", "infrastructure", "unknown"] = Field(
        default="unknown", description="Code/behaviour fault inside a service, or a container/resource/dependency fault")
    cause: str = Field(description="Most likely root cause in one or two sentences")
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list, description="Evidence IDs from tool calls, e.g. ev-003")
    escalate: bool = Field(description="True when not confident enough to act")
    next_steps: list[str] = Field(default_factory=list, description="What a human should check next")


class EvidenceItem(BaseModel):
    id: str = Field(description="Evidence ID, e.g. ev-003")
    tool: str = Field(description="Tool that produced it")
    args: dict[str, Any] = Field(default_factory=dict)
    summary: str = Field(description="Short excerpt of the tool result the diagnosis relies on")


class ProposedAction(BaseModel):
    action: ActionName
    target: str = Field(description="Service, container or flag the action applies to")
    params: dict[str, Any] = Field(default_factory=dict)
    reason: str


class Approval(BaseModel):
    decision: Literal["approved", "rejected", "not_requested"]
    by: str | None = None
    at: datetime | None = None


class Execution(BaseModel):
    executed: bool
    dry_run: bool
    action_id: str | None = Field(default=None, description="ID in the Action API call log")
    result: str = ""
    at: datetime | None = None


class Verification(BaseModel):
    recovered: bool | None = Field(description="Null when no action was executed")
    checks: list[str] = Field(default_factory=list, description="Health checks run after the action")
    rolled_back: bool = False


class TimelineEntry(BaseModel):
    at: datetime
    event: str


class IncidentReport(BaseModel):
    schema_version: str = SCHEMA_VERSION
    incident_id: str
    alert: str = Field(description="What triggered the investigation")
    detected_at: datetime
    status: Literal["diagnosed", "escalated", "rejected", "dry_run", "resolved", "failed", "cancelled"]
    diagnosis: Diagnosis
    evidence: list[EvidenceItem] = Field(default_factory=list, description="Only the evidence the diagnosis cites")
    proposed_action: ProposedAction | None = None
    approval: Approval = Field(default_factory=lambda: Approval(decision="not_requested"))
    execution: Execution | None = None
    verification: Verification | None = None
    timeline: list[TimelineEntry] = Field(default_factory=list)
    prevention: list[str] = Field(default_factory=list, description="Changes that would stop a repeat")
