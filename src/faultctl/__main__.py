"""faultctl command line.

    faultctl list                     show available incidents
    faultctl inject <id> [--target X] inject one (infra faults can pick a container)
    faultctl status                   show what is currently injected
    faultctl reset                    undo everything and restore baseline load
"""

from __future__ import annotations

import argparse
import json
import sys

from faultctl import incidents


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="faultctl", description="Inject and reset practice incidents")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    inj = sub.add_parser("inject")
    inj.add_argument("incident")
    inj.add_argument("--target", help="container for service-down / cpu-starve")
    sub.add_parser("status")
    sub.add_parser("reset")
    args = parser.parse_args(argv)

    if args.command == "list":
        for i in incidents.CATALOGUE.values():
            target = f" (default target: {i.default_target})" if i.default_target else ""
            print(f"  {i.id:<26} {i.kind:<15} {i.description}{target}")
    elif args.command == "inject":
        try:
            record = incidents.inject(args.incident, args.target)
        except KeyError as exc:
            print(exc.args[0], file=sys.stderr)
            return 2
        print(f"injected {record['incident']} at {record['at']}")
    elif args.command == "status":
        active = incidents.load_state()["active"]
        print(json.dumps(active, indent=2) if active else "no incident injected")
    elif args.command == "reset":
        for line in incidents.reset() or ["nothing to undo"]:
            print(f"  {line}")
        incidents.baseline_load()
        print("reset complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
