"""On-Call Console server (standard library only).

    GET  /                  the console page
    GET  /events            live event stream (Server-Sent Events)
    GET  /api/state         settings, kill switch, active incident, incident catalogue
    GET  /api/services      per-service metrics and anomalies against the baseline
    GET  /api/actions       Action API call log
    GET  /reports/<file>    incident reports (.md / .json)
    POST /api/respond       {alert?}         start an investigation (empty: use the detector)
    POST /api/cancel                         stop the running investigation after its current step
    POST /api/approve       {approved, name} answer the pending approval
    POST /api/actions/clear                  start a fresh Action API log (old one archived in runs/)
    POST /api/settings      {dry_run?, auto_detect?, kill_switch?}
    POST /api/fault         {incident?, reset?}  inject or reset a practice incident (demo)
"""

from __future__ import annotations

import argparse
import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from oncall import actions, detector
from oncall.config import get_settings
from oncall.incident import REPORT_DIR, respond
from oncall.schema import IncidentReport, ProposedAction

STATIC = Path(__file__).parent / "static"


class Hub:
    """Fan-out of events to every open browser tab, with a short history for late joiners."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.subscribers: list[queue.Queue] = []
        self.history: list[dict[str, Any]] = []

    def publish(self, kind: str, data: dict[str, Any]) -> None:
        event = {"kind": kind, "at": time.time(), **data}
        with self.lock:
            self.history = (self.history + [event])[-300:]
            for q in self.subscribers:
                q.put(event)

    def subscribe(self) -> tuple[queue.Queue, list[dict[str, Any]]]:
        q: queue.Queue = queue.Queue()
        with self.lock:
            self.subscribers.append(q)
            return q, list(self.history)

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)


class Console:
    def __init__(self) -> None:
        self.hub = Hub()
        self.dry_run = get_settings().dry_run
        self.auto_detect = False
        self.busy = threading.Lock()
        self.cancel_requested = threading.Event()
        self.pending: dict[str, Any] | None = None  # {"event": Event, "approved": bool, "name": str}

    # --- approval gate -------------------------------------------------
    def approver(self, proposal: ProposedAction, report: IncidentReport) -> tuple[bool, str]:
        gate = {"event": threading.Event(), "approved": False, "name": ""}
        self.pending = gate
        self.hub.publish("awaiting_approval", {"incident_id": report.incident_id, "proposal": proposal.model_dump(),
                                               "dry_run": self.dry_run})
        deadline = time.time() + 600
        while not gate["event"].wait(timeout=1):  # wake every second so Cancel works here too
            if self.cancel_requested.is_set() or time.time() > deadline:
                break
        self.pending = None
        if not gate["event"].is_set():
            return False, "cancelled" if self.cancel_requested.is_set() else "timeout"
        return gate["approved"], gate["name"] or "operator"

    def cancel(self) -> str:
        if not self.busy.locked():
            return "nothing is running"
        self.cancel_requested.set()
        self.hub.publish("cancelling", {"event": "Cancel requested; stopping after the current step"})
        return "cancelling"

    def answer(self, approved: bool, name: str) -> bool:
        gate = self.pending
        if not gate:
            return False
        gate["approved"], gate["name"] = approved, name
        gate["event"].set()
        return True

    # --- incidents -----------------------------------------------------
    def start(self, alert: str | None) -> str:
        if not self.busy.acquire(blocking=False):
            return "an incident is already being handled"
        try:
            alert = alert or detector.check()
        except detector.MetricsUnavailable as exc:
            self.busy.release()
            return f"cannot read metrics right now ({exc}); type the alert text to investigate anyway"
        if not alert:
            self.busy.release()
            return "no anomaly against the baseline; nothing to investigate"

        self.cancel_requested.clear()

        def run() -> None:
            try:
                respond(alert, self.approver, self.hub.publish, dry_run=self.dry_run,
                        should_stop=self.cancel_requested.is_set)
            except Exception as exc:  # surface failures in the UI instead of a silent thread death
                self.hub.publish("error", {"event": f"{type(exc).__name__}: {exc}"})
            finally:
                self.busy.release()

        threading.Thread(target=run, daemon=True).start()
        return "started"

    def watch_loop(self) -> None:
        while True:
            time.sleep(20)
            if self.auto_detect and not self.busy.locked():
                try:
                    alert = detector.check()
                except detector.MetricsUnavailable as exc:
                    self.hub.publish("error", {"event": f"Auto-detect skipped a check: {exc}"})
                    continue
                if alert:
                    self.start(alert)

    def state(self) -> dict[str, Any]:
        from faultctl import incidents

        return {
            "dry_run": self.dry_run,
            "auto_detect": self.auto_detect,
            "kill_switch": get_settings().kill_switch_file.exists(),
            "busy": self.busy.locked(),
            "awaiting_approval": self.pending is not None,
            "action_api": actions.available(),
            "baseline": detector.BASELINE_FILE.exists(),
            "injected": [a["incident"] for a in incidents.load_state()["active"]],
            "catalogue": [{"id": i.id, "kind": i.kind, "description": i.description}
                          for i in incidents.CATALOGUE.values()],
        }


CONSOLE = Console()


class Handler(BaseHTTPRequestHandler):
    def _json(self, body: Any, code: int = 200) -> None:
        data = json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            data = (STATIC / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/events":
            self._stream()
        elif self.path == "/api/state":
            self._json(CONSOLE.state())
        elif self.path == "/api/services":
            try:
                current = detector.snapshot()
                self._json({"services": list(current.values()), "anomalies": detector.current_anomalies()})
            except detector.MetricsUnavailable as exc:
                self._json({"services": [], "anomalies": [], "error": str(exc)})
        elif self.path == "/api/actions":
            try:
                import httpx
                self._json(httpx.get(f"{actions.URL}/actions", timeout=5).json()[-50:])
            except Exception:
                self._json([])
        elif self.path.startswith("/reports/"):
            path = REPORT_DIR / Path(self.path).name
            if path.suffix in (".md", ".json") and path.exists():
                data = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(data)
            else:
                self._json({"error": "not found"}, 404)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        body = self._body()
        if self.path == "/api/respond":
            self._json({"result": CONSOLE.start((body.get("alert") or "").strip() or None)})
        elif self.path == "/api/cancel":
            self._json({"result": CONSOLE.cancel()})
        elif self.path == "/api/actions/clear":
            if CONSOLE.busy.locked():  # a running incident may still need its entries for rollback
                return self._json({"result": "an incident is being handled; clear the log afterwards"})
            try:
                import httpx
                archived = httpx.post(f"{actions.URL}/actions/clear", timeout=5).json()["archived"]
                self._json({"result": f"Action log cleared (kept as runs/{archived})" if archived else "Action log is already empty"})
            except Exception as exc:
                self._json({"result": f"Action API unavailable: {type(exc).__name__}"})
        elif self.path == "/api/approve":
            ok = CONSOLE.answer(bool(body.get("approved")), str(body.get("name", "")).strip()[:40])
            self._json({"result": "recorded" if ok else "nothing is waiting for approval"})
        elif self.path == "/api/settings":
            if "dry_run" in body:
                CONSOLE.dry_run = bool(body["dry_run"])
            if "auto_detect" in body:
                CONSOLE.auto_detect = bool(body["auto_detect"])
            if "kill_switch" in body:
                ks = get_settings().kill_switch_file
                ks.touch() if body["kill_switch"] else ks.unlink(missing_ok=True)
                CONSOLE.hub.publish("kill_switch", {"event": f"Kill switch {'ENGAGED' if body['kill_switch'] else 'released'}"})
            self._json(CONSOLE.state())
        elif self.path == "/api/fault":
            from faultctl import incidents
            try:
                if body.get("reset"):
                    incidents.reset()
                    incidents.baseline_load()
                    CONSOLE.hub.publish("fault", {"event": "Practice incidents reset"})
                else:
                    incidents.inject(body["incident"])
                    CONSOLE.hub.publish("fault", {"event": f"Injected practice incident: {body['incident']}"})
                self._json(CONSOLE.state())
            except Exception as exc:
                self._json({"error": str(exc)}, 400)
        else:
            self._json({"error": "not found"}, 404)

    def _stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        q, history = CONSOLE.hub.subscribe()
        try:
            for event in history:
                self.wfile.write(f"data: {json.dumps(event, default=str)}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    event = q.get(timeout=15)
                    self.wfile.write(f"data: {json.dumps(event, default=str)}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            CONSOLE.hub.unsubscribe(q)

    def log_message(self, fmt: str, *args: object) -> None:
        pass  # quiet: the event stream polls constantly


def main() -> None:
    parser = argparse.ArgumentParser(prog="console")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8800)
    args = parser.parse_args()
    threading.Thread(target=CONSOLE.watch_loop, daemon=True).start()
    print(f"On-Call Console on http://{args.host}:{args.port}")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
