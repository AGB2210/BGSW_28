"""Run practice incidents end to end and score the agent.

    python -m evaluation                      all incidents
    python -m evaluation payment-failure cache-down
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from evaluation import EXPECTED
from faultctl import incidents
from oncall import detector
from oncall.agent import investigate

OUT_DIR = Path("runs/eval")


def wait_until(check, timeout_s: int, every_s: int = 20):
    """Poll `check()` until it returns something truthy or the timeout passes."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        result = check()
        if result:
            return result
        time.sleep(every_s)
    return None


def score(component: str | None, escalate: bool, expected: set[str]) -> str:
    hit = bool(component) and any(e in component.lower() for e in expected)
    if hit:
        return "correct"
    return "escalated" if escalate else "wrong"


def run_one(incident_id: str, detect_timeout: int, settle_timeout: int) -> dict:
    print(f"\n=== {incident_id}")
    incidents.reset()
    calm = wait_until(lambda: detector.check() is None, settle_timeout)
    if not calm:
        print("  warning: baseline not calm before injection")

    started = time.time()
    incidents.inject(incident_id)
    alert = wait_until(detector.check, detect_timeout)
    detected_s = round(time.time() - started) if alert else None
    print(f"  alert after {detected_s}s: {alert}")

    t0 = time.time()
    diagnosis, ledger = investigate(alert or "No metric alert fired, but users report problems. Check the system.")
    result = {
        "incident": incident_id,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "detected_after_s": detected_s,
        "alert": alert,
        "diagnosis": diagnosis.model_dump(),
        "tools_used": [e.tool for e in ledger.items()],
        "investigation_s": round(time.time() - t0),
        "score": score(diagnosis.component, diagnosis.escalate, EXPECTED[incident_id]),
    }
    print(f"  -> {diagnosis.component!r} conf={diagnosis.confidence} escalate={diagnosis.escalate}: {result['score']}")
    incidents.reset()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evaluation")
    parser.add_argument("incidents", nargs="*", help="default: all practice incidents")
    parser.add_argument("--detect-timeout", type=int, default=240)
    parser.add_argument("--settle-timeout", type=int, default=240)
    args = parser.parse_args(argv)

    chosen = args.incidents or list(EXPECTED)
    unknown = [i for i in chosen if i not in EXPECTED]
    if unknown:
        print(f"unknown incidents: {unknown}", file=sys.stderr)
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"eval-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    results = []
    for incident_id in chosen:
        result = run_one(incident_id, args.detect_timeout, args.settle_timeout)
        results.append(result)
        with out.open("a") as f:
            f.write(json.dumps(result) + "\n")

    print("\n| incident | detected | diagnosis | conf | result |\n|---|---|---|---|---|")
    for r in results:
        d = r["diagnosis"]
        print(f"| {r['incident']} | {r['detected_after_s'] or 'no'}s | {d['component']} | {d['confidence']} | {r['score']} |")
    totals = {s: sum(r["score"] == s for r in results) for s in ("correct", "escalated", "wrong")}
    print(f"\n{totals}  (saved to {out})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
