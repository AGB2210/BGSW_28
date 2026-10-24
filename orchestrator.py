#!/usr/bin/env python3
"""
orchestrator.py - Main AI On-Call Engineer Pipeline

Full pipeline: Monitor → Detect → Diagnose → Propose → Approve → Fix → Verify → Report

Usage:
  # One-shot investigation (when you know there's a fault):
  python orchestrator.py investigate

  # Continuous monitoring (watches for anomalies):
  python orchestrator.py monitor

  # Investigate a specific alert:
  python orchestrator.py investigate --alert "Checkout service returning 500 errors"
"""

import json
import sys
import time
import datetime
import argparse
import os

from telemetry_collector import TelemetryCollector
from detection_agent import DetectionAgent
from software_dev_agent import SoftwareDevAgent, present_diagnosis_to_user
from action_api import execute_action, get_action_log

# ═══════════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════════

REPO_ROOT = os.environ.get("OTEL_DEMO_ROOT", ".")
MONITOR_INTERVAL = 30  # seconds between monitoring checks
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
REPORTS_DIR = "incident_reports"


# ═══════════════════════════════════════════════════════════════════════════════
# Incident Report Generation
# ═══════════════════════════════════════════════════════════════════════════════

def generate_incident_report(
    incident_id: str,
    alert: str,
    detection_report: dict,
    diagnosis: dict,
    user_decision: dict,
    action_result: dict,
    verification: dict,
) -> dict:
    """Generate the full incident report from detection to resolution."""
    report = {
        "incident_id": incident_id,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "status": "resolved" if verification.get("recovered") else "needs_attention",

        "timeline": {
            "alert_received": alert,
            "detection_completed": detection_report.get("timestamp", ""),
            "diagnosis_completed": diagnosis.get("timestamp", ""),
            "user_decision_at": datetime.datetime.utcnow().isoformat() + "Z",
        },

        "detection": {
            "model_used": detection_report.get("model_used", ""),
            "root_cause": detection_report.get("detection", {}).get("root_cause", ""),
            "confidence": detection_report.get("detection", {}).get("confidence", ""),
            "affected_services": detection_report.get("detection", {}).get("affected_services", []),
            "evidence_summary": detection_report.get("detection", {}).get("evidence_summary", ""),
            "evidence_items": detection_report.get("evidence", []),
            "investigation_steps": detection_report.get("investigation_log", []),
        },

        "diagnosis": {
            "model_used": diagnosis.get("model_used", ""),
            "pinpoint_location": diagnosis.get("dev_diagnosis", {}).get("pinpoint_location", ""),
            "root_cause_detail": diagnosis.get("dev_diagnosis", {}).get("root_cause_detail", ""),
            "recommended_fix": diagnosis.get("dev_diagnosis", {}).get("recommended_fix", ""),
            "fix_type": diagnosis.get("dev_diagnosis", {}).get("fix_type", ""),
            "fix_command": diagnosis.get("dev_diagnosis", {}).get("fix_command", ""),
        },

        "resolution": {
            "user_approved": user_decision.get("approved", False),
            "user_modified": user_decision.get("modified", False),
            "user_input": user_decision.get("user_input", ""),
            "action_executed": user_decision.get("action", ""),
            "action_result": action_result,
            "dry_run": DRY_RUN,
        },

        "verification": verification,

        "action_log": get_action_log(),
    }

    return report


def save_report(report: dict) -> str:
    """Save the incident report to a JSON file."""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    filename = f"{report['incident_id']}_{datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
    filepath = os.path.join(REPORTS_DIR, filename)
    with open(filepath, 'w') as f:
        json.dump(report, f, indent=2, default=str)
    return filepath


def print_report_summary(report: dict):
    """Print a human-readable summary of the incident report."""
    print(f"\n{'='*70}")
    print(f"  INCIDENT REPORT: {report['incident_id']}")
    print(f"  Status: {report['status'].upper()}")
    print(f"{'='*70}")
    print()

    det = report["detection"]
    print("DETECTION PHASE:")
    print(f"  Root Cause:    {det['root_cause']}")
    print(f"  Confidence:    {det['confidence']}")
    print(f"  Affected:      {', '.join(det['affected_services'])}")
    print(f"  Evidence:      {det['evidence_summary']}")
    print(f"  Steps taken:   {len(det['investigation_steps'])}")
    print()

    diag = report["diagnosis"]
    print("DIAGNOSIS PHASE:")
    print(f"  Pinpoint:      {diag['pinpoint_location']}")
    print(f"  Detail:        {diag['root_cause_detail']}")
    print(f"  Fix:           {diag['recommended_fix']}")
    print(f"  Command:       {diag['fix_command']}")
    print()

    res = report["resolution"]
    print("RESOLUTION:")
    print(f"  Approved:      {res['user_approved']}")
    print(f"  Modified:      {res['user_modified']}")
    print(f"  Action:        {res['action_executed']}")
    print(f"  Dry Run:       {res['dry_run']}")
    print()

    ver = report["verification"]
    print("VERIFICATION:")
    print(f"  Recovered:     {ver.get('recovered', 'unknown')}")
    print(f"  Details:       {ver.get('details', 'N/A')}")
    print()


# ═══════════════════════════════════════════════════════════════════════════════
# Verification
# ═══════════════════════════════════════════════════════════════════════════════

def verify_recovery(collector: TelemetryCollector, affected_services: list,
                    wait_seconds: int = 20) -> dict:
    """
    After applying a fix, verify that the system has recovered.
    """
    print(f"\n  Waiting {wait_seconds}s for recovery...")
    time.sleep(wait_seconds)

    print("  Checking system health post-fix...")
    verification = {
        "recovered": True,
        "checks": [],
        "details": "",
    }

    # Check container status
    containers = collector.get_all_container_status()
    for c in containers:
        for svc in affected_services:
            if svc.lower() in c.get("name", "").lower():
                if c.get("status") != "running":
                    verification["recovered"] = False
                    verification["checks"].append({
                        "check": "container_status",
                        "service": c["name"],
                        "status": c["status"],
                        "pass": False,
                    })
                else:
                    verification["checks"].append({
                        "check": "container_status",
                        "service": c["name"],
                        "status": "running",
                        "pass": True,
                    })

    # Check error rates went down
    error_rates = collector.get_service_error_rates()
    for er in error_rates:
        for svc in affected_services:
            if svc.lower() in er.get("service", "").lower():
                rate = er.get("error_rate", 0)
                passed = rate < 0.05  # Less than 5% errors
                if not passed:
                    verification["recovered"] = False
                verification["checks"].append({
                    "check": "error_rate",
                    "service": er["service"],
                    "error_rate": rate,
                    "threshold": 0.05,
                    "pass": passed,
                })

    if verification["recovered"]:
        verification["details"] = "All affected services recovered. Error rates within normal range."
    else:
        failed = [c for c in verification["checks"] if not c["pass"]]
        verification["details"] = f"{len(failed)} check(s) still failing: " + \
            ", ".join(f"{c['check']}({c.get('service', '?')})" for c in failed)

    return verification


# ═══════════════════════════════════════════════════════════════════════════════
# Main Pipeline
# ═══════════════════════════════════════════════════════════════════════════════

def run_investigation(alert: str, incident_id: str = None):
    """
    Run the full investigation pipeline:
    1. Collect initial telemetry snapshot
    2. Detection agent investigates
    3. Software dev agent diagnoses and proposes fix
    4. Present to user for approval
    5. Execute approved fix
    6. Verify recovery
    7. Generate and save report
    """
    if not incident_id:
        incident_id = f"INC-{datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"

    collector = TelemetryCollector()

    print(f"\n{'#'*70}")
    print(f"#  AI ON-CALL ENGINEER")
    print(f"#  Incident: {incident_id}")
    print(f"#  Alert: {alert}")
    print(f"#  Mode: {'DRY RUN' if DRY_RUN else 'LIVE'}")
    print(f"{'#'*70}\n")

    # ── Step 1: Collect initial snapshot ────────────────────────────────────
    print("[1/7] Collecting initial telemetry snapshot...")
    snapshot = collector.collect_full_snapshot()
    print(f"  Found {len(snapshot.get('anomalies', []))} anomalies, "
          f"{len(snapshot.get('unhealthy_services', []))} unhealthy services\n")

    # ── Step 2: Detection agent investigates ────────────────────────────────
    print("[2/7] Detection agent investigating...")
    detection_agent = DetectionAgent()
    detection_report = detection_agent.investigate(
        alert=alert,
        incident_id=incident_id,
        initial_snapshot=snapshot,
    )

    detection = detection_report.get("detection", {})
    confidence = detection.get("confidence", "low")
    print(f"\n  Detection complete: {detection.get('root_cause', '?')}")
    print(f"  Confidence: {confidence}\n")

    # ── Step 3: Software dev agent diagnoses ────────────────────────────────
    print("[3/7] Software dev agent analyzing codebase...")
    dev_agent = SoftwareDevAgent(repo_root=REPO_ROOT)
    diagnosis = dev_agent.diagnose_and_propose_fix(detection_report)

    dev_diag = diagnosis.get("dev_diagnosis", {})
    print(f"\n  Diagnosis: {dev_diag.get('pinpoint_location', '?')}")
    print(f"  Fix: {dev_diag.get('recommended_fix', '?')}\n")

    # ── Step 4: Present to user for approval ────────────────────────────────
    print("[4/7] Presenting diagnosis to user...")
    user_decision = present_diagnosis_to_user(diagnosis)

    # ── Step 5: Execute approved fix ────────────────────────────────────────
    action_result = {}
    if user_decision["approved"]:
        print("[5/7] Executing approved fix...")
        action_cmd = user_decision["action"]
        if action_cmd:
            action_result = execute_action(action_cmd, dry_run=DRY_RUN, repo_root=REPO_ROOT)
            print(f"  Action result: {action_result.get('result', '?')}")
        else:
            print("  No action command provided.")
            action_result = {"result": "No action specified"}
    else:
        print("[5/7] Fix rejected by user. Skipping execution.")
        action_result = {"result": "Rejected by user", "reason": user_decision.get("user_input", "")}

    # ── Step 6: Verify recovery ─────────────────────────────────────────────
    verification = {"recovered": False, "details": "Skipped"}
    if user_decision["approved"] and not DRY_RUN:
        print("[6/7] Verifying recovery...")
        affected = detection.get("affected_services", [])
        verification = verify_recovery(collector, affected)
        print(f"  Recovery: {'YES' if verification['recovered'] else 'NO'}")
        print(f"  Details: {verification['details']}")
    else:
        print("[6/7] Skipping verification (dry run or rejected).")

    # ── Step 7: Generate report ─────────────────────────────────────────────
    print("[7/7] Generating incident report...")
    report = generate_incident_report(
        incident_id=incident_id,
        alert=alert,
        detection_report=detection_report,
        diagnosis=diagnosis,
        user_decision=user_decision,
        action_result=action_result,
        verification=verification,
    )

    filepath = save_report(report)
    print(f"  Report saved: {filepath}")

    # Print summary
    print_report_summary(report)

    return report


def run_monitor():
    """
    Continuous monitoring mode.
    Watches telemetry for anomalies and triggers investigations.
    """
    collector = TelemetryCollector()
    incident_counter = 0

    print(f"\n{'#'*70}")
    print(f"#  AI ON-CALL ENGINEER - MONITORING MODE")
    print(f"#  Checking every {MONITOR_INTERVAL}s for anomalies")
    print(f"#  Press Ctrl+C to stop")
    print(f"{'#'*70}\n")

    try:
        while True:
            timestamp = datetime.datetime.utcnow().strftime("%H:%M:%S")
            print(f"[{timestamp}] Scanning for anomalies...", end=" ")

            snapshot = collector.collect_full_snapshot()
            anomalies = snapshot.get("anomalies", [])
            unhealthy = snapshot.get("unhealthy_services", [])

            if anomalies or unhealthy:
                incident_counter += 1
                incident_id = f"INC-{datetime.datetime.utcnow().strftime('%Y%m%d')}-{incident_counter:03d}"

                # Build alert string from detected issues
                alert_parts = []
                for a in anomalies:
                    alert_parts.append(
                        f"{a['type']}: {a['service']} ({a['protocol']}) = {a['value']:.4f}"
                    )
                for u in unhealthy:
                    alert_parts.append(
                        f"unhealthy: {u['name']} (status={u['status']}, restarts={u.get('restart_count', 0)})"
                    )
                alert = "; ".join(alert_parts)

                print(f"\n  ALERT DETECTED! {alert}")
                print(f"  Starting investigation: {incident_id}\n")

                # Run the full pipeline
                run_investigation(alert=alert, incident_id=incident_id)

                print(f"\n[Monitor] Investigation complete. Resuming monitoring...\n")
            else:
                print("OK - No anomalies detected.")

            time.sleep(MONITOR_INTERVAL)

    except KeyboardInterrupt:
        print("\n\nMonitoring stopped.")


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="AI On-Call Engineer - Automated Incident Response",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # investigate
    inv = subparsers.add_parser("investigate", help="Run a one-shot investigation")
    inv.add_argument("--alert", "-a", type=str,
                     default="Anomalies detected. Investigate system health.",
                     help="Alert description")
    inv.add_argument("--id", type=str, default=None, help="Incident ID")
    inv.add_argument("--dry-run", action="store_true", help="Don't execute fixes")
    inv.add_argument("--repo", type=str, default=".", help="Path to opentelemetry-demo repo")

    # monitor
    mon = subparsers.add_parser("monitor", help="Continuous monitoring mode")
    mon.add_argument("--interval", type=int, default=30, help="Seconds between checks")
    mon.add_argument("--dry-run", action="store_true", help="Don't execute fixes")
    mon.add_argument("--repo", type=str, default=".", help="Path to opentelemetry-demo repo")

    args = parser.parse_args()

    if args.dry_run if hasattr(args, 'dry_run') else False:
        DRY_RUN = True

    if hasattr(args, 'repo'):
        REPO_ROOT = args.repo

    if args.command == "investigate":
        run_investigation(alert=args.alert, incident_id=args.id)
    elif args.command == "monitor":
        MONITOR_INTERVAL = args.interval
        run_monitor()
