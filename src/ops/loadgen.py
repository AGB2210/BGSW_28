"""Locust load generator control through its web API."""

from __future__ import annotations

import os

import httpx

BASE = os.environ.get("LOADGEN_URL", "http://localhost:8080/loadgen")


def user_count() -> int:
    return int(httpx.get(f"{BASE}/stats/requests", timeout=15).json()["user_count"])


def set_users(users: int, spawn_rate: float = 1.0) -> None:
    response = httpx.post(f"{BASE}/swarm", data={"user_count": users, "spawn_rate": spawn_rate}, timeout=15)
    response.raise_for_status()
