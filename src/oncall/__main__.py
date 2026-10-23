"""Command line: `python -m oncall check` and `python -m oncall investigate "<alert>"`."""

from __future__ import annotations

import argparse
import json
import sys

from oncall.config import get_settings


def cmd_check(_: argparse.Namespace) -> int:
    """Confirm every backend the agent needs is reachable."""
    import httpx

    s = get_settings()
    targets = {
        "prometheus": f"{s.prometheus_url}/-/ready",
        "jaeger": f"{s.jaeger_url}/services",
        "opensearch": s.opensearch_url,
        "ollama": f"{s.ollama_url}/api/tags",
    }
    ok = True
    for name, url in targets.items():
        if not url:
            print(f"  FAIL  {name:<11} not found (is the demo running?)")
            ok = False
            continue
        try:
            httpx.get(url, timeout=5).raise_for_status()
            print(f"  ok    {name:<11} {url}")
        except httpx.HTTPError as exc:
            print(f"  FAIL  {name:<11} {url}  ({type(exc).__name__})")
            ok = False
    return 0 if ok else 1


def cmd_investigate(args: argparse.Namespace) -> int:
    from oncall.agent import investigate

    diagnosis, ledger = investigate(args.alert, verbose=True)
    print(json.dumps(diagnosis.model_dump(), indent=2))
    if args.show_evidence:
        print(ledger.to_json())
    return 0


def cmd_baseline(_: argparse.Namespace) -> int:
    """Save current metrics as the healthy baseline (run when nothing is injected)."""
    from oncall import detector

    data = detector.save_baseline()
    print(f"saved baseline for {len(data)} services to {detector.BASELINE_FILE}")
    return 0


def cmd_detect(_: argparse.Namespace) -> int:
    """One detector check against the baseline."""
    from oncall import detector

    alert = detector.check()
    print(f"ALERT: {alert}" if alert else "no anomalies")
    return 0


def _cli_events(kind: str, data: dict) -> None:
    if kind == "tool":
        print(f"  [tool] {data['tool']}({data['args']}) -> {data.get('evidence_id') or data.get('error')}")
    elif "event" in data:
        print(f"[{kind}] {data['event']}")


def _cli_approver(proposal, report) -> tuple[bool, str]:
    answer = input(f"\nApprove {proposal.action} on {proposal.target}? Type your name to approve, "
                   "or press Enter to reject: ").strip()
    return (bool(answer), answer or "operator")


def cmd_respond(args: argparse.Namespace) -> int:
    """Full loop for one alert: investigate, propose, ask approval, act, verify, report."""
    from oncall import actions
    from oncall.incident import respond

    if not args.dry_run and not actions.available():
        print("Action API is not running. Start it with `action-api`, or add --dry-run.")
        return 1
    report = respond(args.alert, _cli_approver, _cli_events, dry_run=args.dry_run)
    print(f"\nStatus: {report.status}. Report: reports/{report.incident_id}.md")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Poll the detector; when it fires, run the full response loop."""
    import time

    from oncall import detector
    from oncall.incident import respond

    print(f"Watching every {args.every}s (Ctrl+C to stop)")
    while True:
        alert = detector.check()
        if alert:
            print(f"\nALERT: {alert}")
            report = respond(alert, _cli_approver, _cli_events, dry_run=args.dry_run)
            print(f"Status: {report.status}. Report: reports/{report.incident_id}.md")
        time.sleep(args.every)


SCHEMA_PATH = "schemas/incident_report.schema.json"


def cmd_schema(_: argparse.Namespace) -> int:
    """Write the JSON schema of the incident report."""
    from pathlib import Path

    from oncall.schema import IncidentReport

    Path(SCHEMA_PATH).parent.mkdir(exist_ok=True)
    Path(SCHEMA_PATH).write_text(json.dumps(IncidentReport.model_json_schema(), indent=2) + "\n")
    print(f"wrote {SCHEMA_PATH}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oncall", description="AI on-call engineer")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="check telemetry backends and the local LLM").set_defaults(fn=cmd_check)

    inv = sub.add_parser("investigate", help="investigate an alert and print a diagnosis")
    inv.add_argument("alert", help='e.g. "checkout error rate above 5%"')
    inv.add_argument("--show-evidence", action="store_true", help="print the evidence ledger")
    inv.set_defaults(fn=cmd_investigate)

    resp = sub.add_parser("respond", help="investigate, propose, approve, act, verify and report")
    resp.add_argument("alert")
    resp.add_argument("--dry-run", action="store_true", help="show the action without executing it")
    resp.set_defaults(fn=cmd_respond)

    watch = sub.add_parser("watch", help="run the detector and respond to alerts")
    watch.add_argument("--every", type=int, default=30, help="seconds between checks")
    watch.add_argument("--dry-run", action="store_true")
    watch.set_defaults(fn=cmd_watch)

    sub.add_parser("schema", help=f"write {SCHEMA_PATH}").set_defaults(fn=cmd_schema)
    sub.add_parser("baseline", help="save current metrics as the healthy baseline").set_defaults(fn=cmd_baseline)
    sub.add_parser("detect", help="check live metrics against the baseline").set_defaults(fn=cmd_detect)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
