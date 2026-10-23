"""Settings, read from environment variables and an optional .env file."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Load KEY=VALUE lines into os.environ without overriding real env vars."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _env(key: str, default: str) -> str:
    return os.environ.get(key) or default


def docker_port(container: str, port: int) -> str | None:
    """Return 'localhost:<host port>' for a published container port, or None."""
    try:
        out = subprocess.run(
            ["docker", "port", container, str(port)],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    first = out.splitlines()[0] if out else ""
    return first.replace("0.0.0.0", "localhost") or None


@dataclass(frozen=True)
class Settings:
    prometheus_url: str
    jaeger_url: str
    opensearch_url: str | None
    ollama_url: str
    ollama_model: str
    ollama_num_ctx: int
    ollama_think: bool
    ollama_num_gpu: int
    ollama_num_thread: int
    max_steps: int
    min_confidence: float
    dry_run: bool
    kill_switch_file: Path


@lru_cache
def get_settings() -> Settings:
    _load_dotenv()
    opensearch = os.environ.get("OPENSEARCH_URL")
    if not opensearch:
        hostport = docker_port("opensearch", 9200)
        opensearch = f"http://{hostport}" if hostport else None
    return Settings(
        prometheus_url=_env("PROMETHEUS_URL", "http://localhost:9090"),
        jaeger_url=_env("JAEGER_URL", "http://localhost:8080/jaeger/ui/api/v3"),
        opensearch_url=opensearch,
        ollama_url=_env("OLLAMA_URL", "http://localhost:11434"),
        ollama_model=_env("OLLAMA_MODEL", "qwen3:4b"),
        ollama_num_ctx=int(_env("OLLAMA_NUM_CTX", "8192")),
        ollama_think=_env("OLLAMA_THINK", "false").lower() == "true",
        ollama_num_gpu=int(_env("OLLAMA_NUM_GPU", "99")),
        ollama_num_thread=int(_env("OLLAMA_NUM_THREAD", "4")),
        max_steps=int(_env("MAX_STEPS", "12")),
        min_confidence=float(_env("MIN_CONFIDENCE", "0.6")),
        dry_run=_env("DRY_RUN", "true").lower() != "false",
        kill_switch_file=Path(_env("KILL_SWITCH_FILE", ".killswitch")),
    )
