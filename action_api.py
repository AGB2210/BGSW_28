#!/usr/bin/env python3
"""
action_api.py - Safe remediation actions for the AI On-Call Engineer.

The ONLY allowed way for the agent to change the system.
All actions are logged. Supports dry-run mode.

Allowed actions:
  - restart_service(<service>)
  - toggle_feature_flag(<flag>, <value>)
  - throttle_load_generator(<rps>)
  - scale_service(<service>, <replicas>)
"""

import json
import os
import subprocess
import datetime
from typing import Optional

# Resolve repo root: pass via function args or OTEL_DEMO_ROOT env var
_DEFAULT_REPO = os.environ.get("OTEL_DEMO_ROOT", ".")

def _flag_file_path(repo_root: str) -> str:
    return os.path.join(repo_root, "src", "flagd", "demo.flagd.json")

def _dc(repo_root: str, cmd: str) -> str:
    """Build a docker compose command with --project-directory."""
    return f"docker compose --project-directory {repo_root} {cmd}"

# Action log
_action_log = []


def get_action_log() -> list[dict]:
    """Get the full action log."""
    return _action_log.copy()


def _log_action(action: str, params: dict, result: str, dry_run: bool):
    """Log an action execution."""
    entry = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "action": action,
        "params": params,
        "result": result,
        "dry_run": dry_run,
    }
    _action_log.append(entry)
    print(f"  [ACTION LOG] {json.dumps(entry, default=str)}")
    return entry


def _run_cmd(cmd: str, dry_run: bool = False) -> str:
    """Run a shell command, or just log it in dry-run mode."""
    if dry_run:
        return f"[DRY RUN] Would execute: {cmd}"
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            return f"[ERROR] {result.stderr.strip()}"
        return f"[OK] {result.stdout.strip()}" if result.stdout.strip() else "[OK]"
    except subprocess.TimeoutExpired:
        return "[ERROR] Command timed out"
    except Exception as e:
        return f"[ERROR] {e}"


# ═══════════════════════════════════════════════════════════════════════════════
# Actions
# ═══════════════════════════════════════════════════════════════════════════════

def restart_service(service: str, dry_run: bool = False, repo_root: str = "") -> dict:
    """Restart a Docker Compose service."""
    repo = repo_root or _DEFAULT_REPO
    print(f"\n  Restarting service: {service}")
    result = _run_cmd(_dc(repo, f"restart {service}"), dry_run)
    return _log_action("restart_service", {"service": service}, result, dry_run)


def toggle_feature_flag(flag_name: str, value: str, dry_run: bool = False,
                        repo_root: str = "") -> dict:
    """Toggle a feature flag to a specific variant value."""
    repo = repo_root or _DEFAULT_REPO
    flag_file = _flag_file_path(repo)
    print(f"\n  Toggling flag: {flag_name} -> {value}")

    if dry_run:
        result = f"[DRY RUN] Would set {flag_name} to '{value}'"
        return _log_action("toggle_feature_flag", {"flag": flag_name, "value": value}, result, dry_run)

    if not os.path.exists(flag_file):
        result = f"[ERROR] {flag_file} not found"
        return _log_action("toggle_feature_flag", {"flag": flag_name, "value": value}, result, dry_run)

    try:
        with open(flag_file, 'r') as f:
            data = json.load(f)

        if "flags" not in data or flag_name not in data["flags"]:
            result = f"[ERROR] Flag '{flag_name}' not found"
            return _log_action("toggle_feature_flag", {"flag": flag_name, "value": value}, result, dry_run)

        # Validate variant exists
        variants = data["flags"][flag_name].get("variants", {})
        if value not in variants and value not in ("on", "off"):
            result = f"[ERROR] Variant '{value}' not valid. Available: {list(variants.keys())}"
            return _log_action("toggle_feature_flag", {"flag": flag_name, "value": value}, result, dry_run)

        data["flags"][flag_name]["defaultVariant"] = value
        with open(flag_file, 'w') as f:
            json.dump(data, f, indent=2)

        result = f"[OK] {flag_name} set to '{value}'"
        return _log_action("toggle_feature_flag", {"flag": flag_name, "value": value}, result, dry_run)

    except Exception as e:
        result = f"[ERROR] {e}"
        return _log_action("toggle_feature_flag", {"flag": flag_name, "value": value}, result, dry_run)


def throttle_load_generator(rps: int, dry_run: bool = False, repo_root: str = "") -> dict:
    """Throttle the load generator to a specific RPS."""
    repo = repo_root or _DEFAULT_REPO
    print(f"\n  Throttling load generator to {rps} RPS")

    if dry_run:
        result = f"[DRY RUN] Would throttle load generator to {rps} RPS"
    else:
        if rps == 0:
            result = _run_cmd(_dc(repo, "stop load-generator"))
        else:
            result = _run_cmd(_dc(repo, "restart load-generator"))

    return _log_action("throttle_load_generator", {"rps": rps}, result, dry_run)


def scale_service(service: str, replicas: int, dry_run: bool = False,
                  repo_root: str = "") -> dict:
    """Scale a service to N replicas (Docker Compose)."""
    repo = repo_root or _DEFAULT_REPO
    print(f"\n  Scaling {service} to {replicas} replicas")
    result = _run_cmd(_dc(repo, f"up -d --scale {service}={replicas}"), dry_run)
    return _log_action("scale_service", {"service": service, "replicas": replicas}, result, dry_run)


def start_service(service: str, dry_run: bool = False, repo_root: str = "") -> dict:
    """Start a stopped service."""
    repo = repo_root or _DEFAULT_REPO
    print(f"\n  Starting service: {service}")
    result = _run_cmd(_dc(repo, f"start {service}"), dry_run)
    return _log_action("start_service", {"service": service}, result, dry_run)


def unpause_service(service: str, dry_run: bool = False, repo_root: str = "") -> dict:
    """Unpause a paused service."""
    repo = repo_root or _DEFAULT_REPO
    print(f"\n  Unpausing service: {service}")
    result = _run_cmd(_dc(repo, f"unpause {service}"), dry_run)
    return _log_action("unpause_service", {"service": service}, result, dry_run)


def reset_resource_limits(service: str, dry_run: bool = False, repo_root: str = "") -> dict:
    """Reset CPU/memory limits on a container."""
    repo = repo_root or _DEFAULT_REPO
    print(f"\n  Resetting resource limits on: {service}")
    cid_result = subprocess.run(
        _dc(repo, f"ps -q {service}"),
        shell=True, capture_output=True, text=True
    )
    cid = cid_result.stdout.strip()
    if not cid:
        result = f"[ERROR] Container for '{service}' not found"
    else:
        result = _run_cmd(f'docker update --cpus="0" --memory="0" {cid}', dry_run)
    return _log_action("reset_resource_limits", {"service": service}, result, dry_run)


# ═══════════════════════════════════════════════════════════════════════════════
# Command Parser
# ═══════════════════════════════════════════════════════════════════════════════

def execute_action(action_string: str, dry_run: bool = False,
                   repo_root: str = "") -> dict:
    """
    Parse and execute an action string like:
      restart_service(checkout)
      toggle_feature_flag(cartFailure, off)
      throttle_load_generator(10)
      scale_service(frontend, 3)
      start_service(payment)
      unpause_service(cart)
      reset_resource_limits(ad)
    """
    import re

    rr = repo_root  # shorthand for lambdas

    # Parse action_name(args)
    match = re.match(r'(\w+)\(([^)]*)\)', action_string.strip())
    if not match:
        return _log_action("unknown", {"raw": action_string},
                          f"[ERROR] Could not parse action: {action_string}", dry_run)

    action_name = match.group(1)
    args_str = match.group(2).strip()
    args = [a.strip().strip('"').strip("'") for a in args_str.split(",") if a.strip()]

    action_map = {
        "restart_service": lambda: restart_service(args[0], dry_run, repo_root=rr) if args else None,
        "toggle_feature_flag": lambda: toggle_feature_flag(args[0], args[1], dry_run, repo_root=rr) if len(args) >= 2 else None,
        "throttle_load_generator": lambda: throttle_load_generator(int(args[0]), dry_run, repo_root=rr) if args else None,
        "scale_service": lambda: scale_service(args[0], int(args[1]), dry_run, repo_root=rr) if len(args) >= 2 else None,
        "start_service": lambda: start_service(args[0], dry_run, repo_root=rr) if args else None,
        "unpause_service": lambda: unpause_service(args[0], dry_run, repo_root=rr) if args else None,
        "reset_resource_limits": lambda: reset_resource_limits(args[0], dry_run, repo_root=rr) if args else None,
        "escalate_to_human": lambda: _log_action("escalate_to_human", {"reason": args_str},
                                                  "[OK] Escalated to human", dry_run),
    }

    if action_name not in action_map:
        return _log_action("unknown", {"action": action_name, "args": args},
                          f"[ERROR] Unknown action: {action_name}", dry_run)

    result = action_map[action_name]()
    if result is None:
        return _log_action(action_name, {"args": args},
                          f"[ERROR] Invalid arguments for {action_name}", dry_run)
    return result


if __name__ == "__main__":
    # Test
    print("Testing Action API (dry run)...")
    execute_action("restart_service(cart)", dry_run=True)
    execute_action("toggle_feature_flag(cartFailure, off)", dry_run=True)
    execute_action("throttle_load_generator(10)", dry_run=True)
    execute_action("scale_service(frontend, 2)", dry_run=True)
    print("\nAction log:")
    print(json.dumps(get_action_log(), indent=2))
