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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oncall", description="AI on-call engineer")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="check telemetry backends and the local LLM").set_defaults(fn=cmd_check)

    inv = sub.add_parser("investigate", help="investigate an alert and print a diagnosis")
    inv.add_argument("alert", help='e.g. "checkout error rate above 5%"')
    inv.add_argument("--show-evidence", action="store_true", help="print the evidence ledger")
    inv.set_defaults(fn=cmd_investigate)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
