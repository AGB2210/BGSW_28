"""HTTP server for the Action API (standard library only).

    POST /actions                 {action, target, params, reason, approved_by, dry_run, incident_id}
    POST /actions/<id>/rollback   {approved_by}
    POST /actions/clear           start a fresh call log (the old one is archived, not deleted)
    GET  /actions                 call log
    GET  /health
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from actionapi import actions


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        elif self.path == "/actions":
            self._send(200, actions.history())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            body = self._body()
        except json.JSONDecodeError:
            return self._send(400, {"error": "body must be JSON"})
        parts = self.path.strip("/").split("/")
        if parts == ["actions", "clear"]:
            return self._send(200, {"archived": actions.clear()})
        if parts == ["actions"]:
            entry = actions.execute(body)
        elif len(parts) == 3 and parts[0] == "actions" and parts[2] == "rollback":
            entry = actions.rollback(parts[1], body.get("approved_by"))
        else:
            return self._send(404, {"error": "not found"})
        self._send({"refused": 403, "failed": 500}.get(entry["status"], 200), entry)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[action-api] {self.address_string()} {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="action-api")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    print(f"Action API on http://{args.host}:{args.port}  (log: {actions.LOG_FILE})")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
