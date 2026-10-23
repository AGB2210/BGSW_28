# On-Call Agent

An AI on-call engineer for the [OpenTelemetry Demo](https://github.com/open-telemetry/opentelemetry-demo).
It receives an alert, investigates traces, metrics, logs and container health with tools,
names the root cause with cited evidence, and proposes a safe fix that only runs after a human approves it.

Built for the Bosch *Incident Zero* hackathon. Runs fully on a laptop with a local model.

## Requirements
- WSL2 Ubuntu (or Linux) with Docker Engine + Compose
- The OpenTelemetry Demo running (full + observability stack)
- [Ollama](https://ollama.com) with `qwen3:4b` (fits a 4 GB GPU)
- Python 3.11+

## Setup
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env        # defaults work for the local demo
ollama pull qwen3:4b
```

## Usage
```bash
action-api &                                        # start the Action API (port 8765)
oncall check                                        # are all backends reachable?
oncall investigate "checkout error rate above 5%"   # run the agent on an alert
oncall schema                                       # regenerate schemas/incident_report.schema.json
pytest                                              # run tests
```

### Practice incidents (faultctl)
```bash
faultctl list                          # 14 incidents: 10 application, 4 infrastructure
faultctl inject payment-failure        # inject one
faultctl inject service-down --target cart
faultctl status                        # what is injected right now
faultctl reset                         # undo everything, restore baseline load
```

### Action API
The only way the agent changes the system. Every call, including refused ones, is appended to `runs/actions.jsonl`.

| Method | Path | Body |
|---|---|---|
| POST | `/actions` | `{action, target, params, reason, approved_by, dry_run, incident_id}` |
| POST | `/actions/<id>/rollback` | `{approved_by}` |
| GET | `/actions` | call log |
| GET | `/health` | |

Actions: `restart_service`, `toggle_feature_flag` (`params.variant`, default `off`),
`throttle_load_generator` (`params.users`), `scale_service` (refused: needs Kubernetes).
Real actions need `approved_by`; the observability stack and the flag service cannot be restarted.

## Safety
- **Approval gate:** the agent only proposes; nothing changes until a human types `yes`.
- **Dry run:** on by default (`DRY_RUN=true`).
- **Kill switch:** `touch .killswitch` blocks every action; delete the file to re-enable.
- **Allowlist:** restart a service, toggle a feature flag, throttle the load generator, scale a service.
- **No flag peeking:** tools cannot reach flagd or the flag UI; diagnosis comes from telemetry only.

See [docs/architecture.md](docs/architecture.md) for the design.

## Layout
```
src/oncall/          the agent
  __main__.py        CLI
  agent.py           investigation loop and diagnosis validation
  tools/             read-only tools: metrics, traces, logs, containers, deployment history
  evidence.py        evidence ledger
  schema.py          incident report models (JSON schema in schemas/)
  safety.py          allowlist, approval, dry-run, kill switch
  llm.py             Ollama client
src/actionapi/       Action API server and call log
src/faultctl/        practice incident injector
src/ops/             docker / flag / load-generator operations (used by actionapi and faultctl only)
tests/               unit tests (no live services needed)
reports/             incident reports produced by the agent
```

## Status and known limitations
- [x] Tools: `service_overview`, `search_traces`, `search_logs`, `container_health`, `deployment_history`
- [x] Action API, faultctl, incident report schema
- [ ] Agent proposes an action and calls the Action API after approval
- [ ] Recovery check and rollback
- [ ] Alert detector
- [ ] Incident report generation

Known limitations:
- A small local model (4B) can still misread evidence; every diagnosis is validated against the
  evidence ledger and escalates when citations are missing or confidence is low.
- Network latency/packet-loss faults are not in faultctl yet (the demo images lack `tc`).
- `scale_service` is refused because the demo runs on Docker Compose, not Kubernetes.
- Feature-flag span events and the flag service's own logs and traces are hidden from the agent,
  so flag settings can never leak into a diagnosis.
