"""Local LLM client (Ollama chat API with tool calling). No hosted APIs."""

from __future__ import annotations

from typing import Any

from oncall.config import get_settings
from oncall.http import post_json


def chat(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Send one chat turn and return the assistant message (may contain tool_calls)."""
    s = get_settings()
    body: dict[str, Any] = {
        "model": s.ollama_model,
        "messages": messages,
        "stream": False,
        "think": s.ollama_think,
        "options": {"temperature": 0, "num_ctx": s.ollama_num_ctx},
    }
    if tools:
        body["tools"] = tools
    return post_json(f"{s.ollama_url}/api/chat", body)["message"]
