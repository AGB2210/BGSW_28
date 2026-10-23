"""Tool registry. Decorate a function with @tool and the agent can call it.

Each tool returns a small, summarised result (never raw dumps). The registry
records every result in the evidence ledger and hands the model its ID.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from oncall.evidence import Ledger


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., Any]


REGISTRY: dict[str, Tool] = {}


def tool(description: str, parameters: dict[str, Any] | None = None):
    """Register a function as an agent tool. `parameters` is a JSON schema of its kwargs."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        REGISTRY[fn.__name__] = Tool(
            name=fn.__name__,
            description=description,
            parameters=parameters or {"type": "object", "properties": {}},
            fn=fn,
        )
        return fn

    return decorator


def specs() -> list[dict[str, Any]]:
    """Tool definitions in the function-calling format Ollama expects."""
    return [
        {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
        for t in REGISTRY.values()
    ]


def call(name: str, args: dict[str, Any], ledger: Ledger) -> dict[str, Any]:
    """Run a tool, store its result as evidence, and return what the model sees."""
    if name not in REGISTRY:
        return {"error": f"unknown tool {name!r}", "available": sorted(REGISTRY)}
    try:
        result = REGISTRY[name].fn(**args)
    except Exception as exc:  # tool failures go back to the model, not up the stack
        return {"error": f"{type(exc).__name__}: {exc}"}
    evidence = ledger.add(name, args, result)
    return {"evidence_id": evidence.id, "result": result}


# Import tool modules so their @tool decorators register.
from oncall.tools import metrics  # noqa: E402,F401
