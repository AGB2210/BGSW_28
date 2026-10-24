"""Safety controls that live in code, not in the prompt.

- Only allowlisted actions can ever run.
- Nothing runs without an explicit human "yes".
- Dry-run mode shows the plan without executing it.
- The kill switch (a file) stops every action immediately.
"""

from __future__ import annotations

from dataclasses import dataclass

from oncall.config import get_settings

ALLOWED_ACTIONS = frozenset({"restart_service", "toggle_feature_flag", "throttle_load_generator", "remove_cpu_limit",
                             "scale_service"})


@dataclass(frozen=True)
class ProposedAction:
    action: str
    target: str
    reason: str


class ActionBlocked(RuntimeError):
    pass


def kill_switch_engaged() -> bool:
    return get_settings().kill_switch_file.exists()


def check_allowed(proposal: ProposedAction) -> None:
    if proposal.action not in ALLOWED_ACTIONS:
        raise ActionBlocked(f"{proposal.action!r} is not an allowlisted action")
    if kill_switch_engaged():
        raise ActionBlocked("Kill switch is engaged")


def ask_approval(proposal: ProposedAction, ask=input) -> bool:
    """Show the plan and wait for an explicit 'yes'. Anything else is a no."""
    check_allowed(proposal)
    print(f"\nProposed action: {proposal.action} -> {proposal.target}\nReason: {proposal.reason}")
    if get_settings().dry_run:
        print("DRY RUN: nothing will be executed.")
    return ask("Type 'yes' to approve: ").strip().lower() == "yes"
