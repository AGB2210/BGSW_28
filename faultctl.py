#!/usr/bin/env python3
"""
faultctl.py - Fault injection and reset tool for OpenTelemetry Demo.
Supports all application faults (feature flags) and infrastructure faults (Docker ops).
Run from the opentelemetry-demo directory.
"""

import argparse
import subprocess
import sys
import json
import os
import time

import pathlib

# Resolve repo root: pass via --repo or OTEL_DEMO_ROOT env var
_DEFAULT_REPO = os.environ.get("OTEL_DEMO_ROOT", "")

def _flag_file_path(repo_root: str) -> str:
    return os.path.join(repo_root, "src", "flagd", "demo.flagd.json")

# ── All available faults ─────────────────────────────────────────────────────

FAULTS = {
    # Application faults (feature flags)
    "app-payment":       {"type": "flag", "flag": "paymentFailure",            "variant": "100%",  "desc": "Payment service fails when charging"},
    "app-payment-50":    {"type": "flag", "flag": "paymentFailure",            "variant": "50%",   "desc": "Payment service fails 50% of the time"},
    "app-cart":          {"type": "flag", "flag": "cartFailure",               "variant": "100%",  "desc": "Cart service fails intermittently (100%)"},
    "app-cart-50":       {"type": "flag", "flag": "cartFailure",               "variant": "50%",   "desc": "Cart service fails 50% of the time"},
    "app-product":       {"type": "flag", "flag": "productCatalogFailure",     "variant": "on",    "desc": "Product catalog fails for one specific product"},
    "app-product-lock":  {"type": "flag", "flag": "productCatalogLockContention", "variant": "on", "desc": "Lock contention on product catalog DB"},
    "app-leak":          {"type": "flag", "flag": "recommendationCacheFailure","variant": "on",    "desc": "Memory leak in recommendation service (runaway cache)"},
    "app-ad-cpu":        {"type": "flag", "flag": "adHighCpu",                 "variant": "on",    "desc": "High CPU / heavy GC in ad service"},
    "app-ad-gc":         {"type": "flag", "flag": "adManualGc",                "variant": "on",    "desc": "Manual garbage collection in ad service"},
    "app-ad-fail":       {"type": "flag", "flag": "adFailure",                 "variant": "on",    "desc": "Ad service fails completely"},
    "app-flood":         {"type": "flag", "flag": "loadGeneratorFloodHomepage","variant": "on",    "desc": "Traffic flood on the homepage"},
    "app-email-leak":    {"type": "flag", "flag": "emailMemoryLeak",           "variant": "1000x", "desc": "Memory leak in email service"},
    "app-image-slow":    {"type": "flag", "flag": "imageSlowLoad",             "variant": "5sec",  "desc": "Slow image loading in frontend (5s)"},
    "app-kafka-lag":     {"type": "flag", "flag": "kafkaQueueProblems",        "variant": "3sec",  "desc": "Kafka consumer lag (3s per-record delay)"},
    "app-kafka-dead":    {"type": "flag", "flag": "kafkaConsumerDead",         "variant": "on",    "desc": "Kafka consumer stops polling (dead consumer group)"},
    "app-shipping-slow": {"type": "flag", "flag": "intlShippingSlowdown",      "variant": "5sec",  "desc": "International shipping slowdown (5s delay)"},
    "app-payment-unreach":{"type": "flag","flag": "paymentUnreachable",        "variant": "on",    "desc": "Payment service is unreachable"},
    "app-readiness":     {"type": "flag", "flag": "failedReadinessProbe",      "variant": "on",    "desc": "Cart service readiness probe fails"},

    # Infrastructure faults (Docker operations)
    "infra-stop-checkout":    {"type": "infra", "action": "stop",    "services": ["checkout"],                "desc": "Stop the checkout container"},
    "infra-stop-payment":     {"type": "infra", "action": "stop",    "services": ["payment"],                 "desc": "Stop the payment container"},
    "infra-stop-cart":        {"type": "infra", "action": "stop",    "services": ["cart"],                    "desc": "Stop the cart container"},
    "infra-stop-frontend":    {"type": "infra", "action": "stop",    "services": ["frontend"],                "desc": "Stop the frontend container"},
    "infra-db-down":          {"type": "infra", "action": "stop",    "services": ["valkey-cart"],              "desc": "Cache (Valkey/Redis) becomes unavailable"},
    "infra-postgres-down":    {"type": "infra", "action": "stop",    "services": ["postgresql"],               "desc": "PostgreSQL database becomes unavailable"},
    "infra-all-db-down":      {"type": "infra", "action": "stop",    "services": ["valkey-cart", "postgresql"],"desc": "All databases/caches unavailable"},
    "infra-latency-payment":  {"type": "infra", "action": "pause",   "services": ["payment"],                 "desc": "Payment container paused (simulates network latency)"},
    "infra-latency-cart":     {"type": "infra", "action": "pause",   "services": ["cart"],                    "desc": "Cart container paused (simulates network latency)"},
    "infra-starve-ad":        {"type": "infra", "action": "starve",  "services": ["ad"],       "cpu": "0.05", "mem": "40m", "desc": "Ad service starved of CPU and memory"},
    "infra-starve-checkout":  {"type": "infra", "action": "starve",  "services": ["checkout"], "cpu": "0.05", "mem": "40m", "desc": "Checkout service starved of CPU and memory"},
    "infra-restart-loop":     {"type": "infra", "action": "restart_loop", "services": ["recommendation"],     "desc": "Recommendation service keeps restarting"},
    "infra-collector-down":   {"type": "infra", "action": "stop",    "services": ["otel-collector"],           "desc": "OpenTelemetry Collector stopped (telemetry blackout)"},
    "infra-network-partition":{"type": "infra", "action": "network", "services": ["payment"],                 "desc": "Network partition: payment isolated from other services"},
}


def run_cmd(cmd, ignore_errors=False, capture=False):
    """Run a shell command."""
    try:
        result = subprocess.run(
            cmd, shell=True, check=True,
            capture_output=capture, text=True
        )
        return result.stdout.strip() if capture else None
    except subprocess.CalledProcessError as e:
        if not ignore_errors:
            print(f"  Command failed: {e}")
        return None


def _dc(repo_root: str, cmd: str) -> str:
    """Build a docker compose command with --project-directory."""
    return f"docker compose --project-directory {repo_root} {cmd}"


def get_container_id(service_name, repo_root: str = ""):
    """Get Docker container ID for a compose service."""
    return run_cmd(_dc(repo_root, f"ps -q {service_name}"), capture=True) or ""


def toggle_feature_flag(flag_name, variant_value, repo_root: str = ""):
    """Set a feature flag to a specific variant."""
    flag_file = _flag_file_path(repo_root)
    if not os.path.exists(flag_file):
        print(f"  Error: {flag_file} not found. Pass --repo /path/to/opentelemetry-demo")
        return False

    with open(flag_file, 'r') as f:
        data = json.load(f)

    if "flags" in data and flag_name in data["flags"]:
        data["flags"][flag_name]["defaultVariant"] = variant_value
        with open(flag_file, 'w') as f:
            json.dump(data, f, indent=2)
        print(f"  [OK] Flag '{flag_name}' -> '{variant_value}'")
        return True
    else:
        print(f"  [FAIL] Flag '{flag_name}' not found in {flag_file}")
        return False


def inject(fault_name, repo_root: str = ""):
    """Inject a single fault."""
    if fault_name not in FAULTS:
        print(f"Unknown fault: '{fault_name}'")
        print(f"Valid faults: {', '.join(sorted(FAULTS.keys()))}")
        sys.exit(1)

    fault = FAULTS[fault_name]
    print(f"\n{'='*60}")
    print(f"INJECTING: {fault_name}")
    print(f"  {fault['desc']}")
    print(f"{'='*60}")

    if fault["type"] == "flag":
        toggle_feature_flag(fault["flag"], fault["variant"], repo_root)

    elif fault["type"] == "infra":
        action = fault["action"]

        if action == "stop":
            for svc in fault["services"]:
                run_cmd(_dc(repo_root, f"stop {svc}"))
                print(f"  [OK] '{svc}' stopped")

        elif action == "pause":
            for svc in fault["services"]:
                run_cmd(_dc(repo_root, f"pause {svc}"))
                print(f"  [OK] '{svc}' paused (network latency simulation)")

        elif action == "starve":
            for svc in fault["services"]:
                cid = get_container_id(svc, repo_root)
                if cid:
                    cpu = fault.get("cpu", "0.05")
                    mem = fault.get("mem", "40m")
                    run_cmd(f'docker update --cpus="{cpu}" --memory="{mem}" {cid}')
                    print(f"  [OK] '{svc}' starved: cpu={cpu}, mem={mem}")
                else:
                    print(f"  [FAIL] Container for '{svc}' not found")

        elif action == "restart_loop":
            for svc in fault["services"]:
                # Kill the main process repeatedly to simulate crash loop
                cid = get_container_id(svc, repo_root)
                if cid:
                    # Set restart policy to always, then kill the process
                    run_cmd(f"docker update --restart=always {cid}")
                    run_cmd(f"docker kill {cid}", ignore_errors=True)
                    print(f"  [OK] '{svc}' entering restart loop")

        elif action == "network":
            for svc in fault["services"]:
                cid = get_container_id(svc, repo_root)
                if cid:
                    run_cmd(f"docker network disconnect opentelemetry-demo {cid}", ignore_errors=True)
                    print(f"  [OK] '{svc}' disconnected from network (partition)")

    print(f"\nFault '{fault_name}' injected successfully.\n")


def reset(repo_root: str = ""):
    """Reset ALL faults - restore system to healthy state."""
    print(f"\n{'='*60}")
    print("RESETTING ALL FAULTS")
    print(f"{'='*60}\n")

    # 1. Reset all feature flags to 'off'
    flag_file = _flag_file_path(repo_root)
    if os.path.exists(flag_file):
        with open(flag_file, 'r') as f:
            data = json.load(f)
        if "flags" in data:
            for flag_name, flag_data in data["flags"].items():
                variants = flag_data.get("variants", {})
                if "off" in variants:
                    flag_data["defaultVariant"] = "off"
                elif 0 in variants.values():
                    for k, v in variants.items():
                        if v == 0:
                            flag_data["defaultVariant"] = k
                            break
            with open(flag_file, 'w') as f:
                json.dump(data, f, indent=2)
        print("[OK] All feature flags reset to 'off'")
    else:
        print(f"[WARN] {flag_file} not found, skipping flag reset")

    # 2. Unpause any paused containers
    run_cmd(_dc(repo_root, "unpause"), ignore_errors=True)
    print("[OK] All containers unpaused")

    # 3. Reconnect any network-partitioned containers
    result = run_cmd(_dc(repo_root, "ps -q"), capture=True)
    if result:
        for cid in result.strip().split('\n'):
            if cid:
                run_cmd(
                    f"docker network connect opentelemetry-demo {cid}",
                    ignore_errors=True
                )
    print("[OK] Network reconnected for all containers")

    # 4. Reset resource limits on all containers
    result = run_cmd(_dc(repo_root, "ps -q"), capture=True)
    if result:
        for cid in result.strip().split('\n'):
            if cid:
                run_cmd(f'docker update --cpus="0" --memory="0" {cid}', ignore_errors=True)
    print("[OK] Resource limits cleared on all containers")

    # 5. Reset restart policies
    if result:
        for cid in result.strip().split('\n'):
            if cid:
                run_cmd(f'docker update --restart=unless-stopped {cid}', ignore_errors=True)
    print("[OK] Restart policies restored")

    # 6. Restart ALL app services to clear stale log buffers
    #    This is critical — Docker keeps logs from the container's entire lifetime,
    #    so old error logs from a previous fault would still show up in detection.
    app_services = [
        "ad", "cart", "checkout", "currency", "email", "frontend",
        "payment", "product-catalog", "quote", "recommendation", "shipping",
        "load-generator", "image-provider", "accounting", "fraud-detection",
    ]
    print("Restarting app services to clear log buffers...")
    svc_list = " ".join(app_services)
    run_cmd(_dc(repo_root, f"restart {svc_list}"), ignore_errors=True)
    print("[OK] All app services restarted (log buffers cleared)")

    # 7. Ensure all containers are up
    run_cmd(_dc(repo_root, "up -d"), ignore_errors=True)
    print("[OK] All containers started")

    # 8. Wait for services to stabilize
    print("\nWaiting 15s for services to stabilize...")
    time.sleep(15)

    # 9. Health check
    health = run_cmd(_dc(repo_root, "ps --format json"), capture=True)
    if health:
        try:
            containers = [json.loads(line) for line in health.strip().split('\n') if line]
            unhealthy = [c for c in containers if c.get("State") != "running"]
            if unhealthy:
                print(f"\n[WARN] {len(unhealthy)} container(s) not running:")
                for c in unhealthy:
                    print(f"  - {c.get('Name', '?')}: {c.get('State', '?')}")
            else:
                print(f"\n[OK] All {len(containers)} containers running")
        except json.JSONDecodeError:
            pass

    print(f"\n{'='*60}")
    print("RESET COMPLETE")
    print(f"{'='*60}\n")


def list_faults():
    """List all available faults with descriptions."""
    print(f"\n{'='*60}")
    print("AVAILABLE FAULTS")
    print(f"{'='*60}\n")

    print("APPLICATION FAULTS (feature flags):")
    print("-" * 50)
    for name, f in sorted(FAULTS.items()):
        if f["type"] == "flag":
            print(f"  {name:<25} {f['desc']}")

    print(f"\nINFRASTRUCTURE FAULTS (docker operations):")
    print("-" * 50)
    for name, f in sorted(FAULTS.items()):
        if f["type"] == "infra":
            print(f"  {name:<25} {f['desc']}")
    print()


def status(repo_root: str = ""):
    """Show current fault/system status."""
    print(f"\n{'='*60}")
    print("SYSTEM STATUS")
    print(f"{'='*60}\n")

    # Check feature flags
    flag_file = _flag_file_path(repo_root)
    if os.path.exists(flag_file):
        with open(flag_file, 'r') as f:
            data = json.load(f)
        active_flags = []
        if "flags" in data:
            for flag_name, flag_data in data["flags"].items():
                variant = flag_data.get("defaultVariant", "off")
                if variant != "off" and variant != 0:
                    active_flags.append(f"  {flag_name}: {variant}")
        if active_flags:
            print("ACTIVE FEATURE FLAGS:")
            for fl in active_flags:
                print(fl)
        else:
            print("FEATURE FLAGS: All off (healthy)")

    # Check containers
    print("\nCONTAINER STATUS:")
    health = run_cmd(_dc(repo_root, "ps --format '{{.Name}}\t{{.State}}\t{{.Status}}'"), capture=True)
    if health:
        for line in health.strip().split('\n'):
            if line:
                parts = line.split('\t')
                name = parts[0] if len(parts) > 0 else "?"
                state = parts[1] if len(parts) > 1 else "?"
                if state != "running":
                    print(f"  [ISSUE] {name}: {state}")
        print("  (only non-running containers shown above)")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="faultctl - Inject and reset faults in OpenTelemetry Demo",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""
Examples:
  python faultctl.py --repo ~/opentelemetry-demo inject app-payment
  python faultctl.py --repo ~/opentelemetry-demo inject infra-stop-checkout
  python faultctl.py --repo ~/opentelemetry-demo list
  python faultctl.py --repo ~/opentelemetry-demo status
  python faultctl.py --repo ~/opentelemetry-demo reset
        """
    )
    parser.add_argument("--repo", type=str, default=_DEFAULT_REPO,
                        help="Path to the opentelemetry-demo repo (or set OTEL_DEMO_ROOT env var)")

    subparsers = parser.add_subparsers(dest="command", required=True)

    inject_parser = subparsers.add_parser("inject", help="Inject a fault")
    inject_parser.add_argument("fault", choices=sorted(FAULTS.keys()), help="Which fault to inject")

    subparsers.add_parser("reset", help="Reset all faults and restore system")
    subparsers.add_parser("list", help="List all available faults")
    subparsers.add_parser("status", help="Show current system status")

    args = parser.parse_args()

    repo = args.repo
    if not repo:
        print("ERROR: Pass --repo /path/to/opentelemetry-demo or set OTEL_DEMO_ROOT env var")
        sys.exit(1)

    if args.command == "inject":
        inject(args.fault, repo)
    elif args.command == "reset":
        reset(repo)
    elif args.command == "list":
        list_faults()
    elif args.command == "status":
        status(repo)
