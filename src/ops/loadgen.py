"""Locust load generator control through its web API."""

from __future__ import annotations

import os

import httpx

from ops import docker

BASE = os.environ.get("LOADGEN_URL", "http://localhost:8080/loadgen")
CONTAINER = "load-generator"
DEFAULT_USERS = 5  # the demo's LOCUST_USERS


def user_count() -> int:
    return int(httpx.get(f"{BASE}/stats/requests", timeout=15).json()["user_count"])


def set_users(users: int, spawn_rate: float = 1.0) -> None:
    response = httpx.post(f"{BASE}/swarm", data={"user_count": users, "spawn_rate": spawn_rate}, timeout=15)
    response.raise_for_status()


def restore(users: int = DEFAULT_USERS) -> None:
    """Put the load back to `users`.

    The demo's default load is restored by restarting the container, which starts it exactly as
    Compose defines it. Raising the count through /swarm after a throttle brings back only plain
    HTTP users: the browser user (all frontend-web traffic, ~10% of the shop's requests) never
    returns, and the stats cannot show it because they report a count, not user types.
    """
    if users == DEFAULT_USERS:
        docker.restart(CONTAINER)
    else:
        set_users(users)
