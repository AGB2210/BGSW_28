"""Evidence ledger: every tool result gets an ID the diagnosis must cite.

A diagnosis that cites an ID not in the ledger is rejected, which stops the
model from inventing traces, log lines or metric values.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class Evidence:
    id: str
    tool: str
    args: dict[str, Any]
    summary: Any
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))


class Ledger:
    def __init__(self) -> None:
        self._items: dict[str, Evidence] = {}

    def add(self, tool: str, args: dict[str, Any], summary: Any) -> Evidence:
        item = Evidence(id=f"ev-{len(self._items) + 1:03d}", tool=tool, args=args, summary=summary)
        self._items[item.id] = item
        return item

    def get(self, evidence_id: str) -> Evidence | None:
        return self._items.get(evidence_id)

    def missing(self, cited: list[str]) -> list[str]:
        """Return cited IDs that do not exist in the ledger."""
        return [eid for eid in cited if eid not in self._items]

    def items(self) -> list[Evidence]:
        return list(self._items.values())

    def to_json(self) -> str:
        return json.dumps([asdict(e) for e in self._items.values()], indent=2, default=str)
