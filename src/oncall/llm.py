"""Local LLM client (Ollama chat API with tool calling). No hosted APIs."""

from __future__ import annotations

from typing import Any

from oncall.config import get_settings
from oncall.http import post_json


def chat(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send one chat turn and return the assistant message (may contain tool_calls).

    With `schema`, the reply is constrained to JSON matching that schema.
    """
    s = get_settings()
    body: dict[str, Any] = {
        "model": s.ollama_model,
        "messages": messages,
        "stream": False,
        "think": s.ollama_think,
        "options": {
            "temperature": 0,
            "num_ctx": s.ollama_num_ctx,
            "num_gpu": s.ollama_num_gpu,  # layers on the GPU; 99 = all of them
            "num_thread": s.ollama_num_thread,  # CPU threads, kept low so the demo isn't starved
        },
    }
    if tools:
        body["tools"] = tools
    if schema:
        body["format"] = schema
    body["keep_alive"] = "2m"  # unload soon after the last call even if unload() is never reached
    response = post_json(f"{s.ollama_url}/api/chat", body, timeout=300)
    LAST_STATS.update(prompt_tokens=response.get("prompt_eval_count", 0), output_tokens=response.get("eval_count", 0),
                      context=s.ollama_num_ctx)
    return response["message"]


LAST_STATS: dict[str, int] = {}  # token counts of the latest call, for diagnostics


def unload() -> None:
    """Free the model's memory. Its prompt cache grows to several GB, which starves the demo."""
    s = get_settings()
    try:
        post_json(f"{s.ollama_url}/api/generate", {"model": s.ollama_model, "keep_alive": 0}, timeout=30)
    except Exception:
        pass  # best effort: keep_alive above unloads it anyway
