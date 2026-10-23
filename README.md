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
oncall check                                        # are all backends reachable?
oncall investigate "checkout error rate above 5%"   # run the agent on an alert
pytest                                              # run tests
```

## Safety
- **Approval gate:** the agent only proposes; nothing changes until a human types `yes`.
- **Dry run:** on by default (`DRY_RUN=true`).
- **Kill switch:** `touch .killswitch` blocks every action; delete the file to re-enable.
- **Allowlist:** restart a service, toggle a feature flag, throttle the load generator, scale a service.
- **No flag peeking:** tools cannot reach flagd or the flag UI; diagnosis comes from telemetry only.

See [docs/architecture.md](docs/architecture.md) for the design.

## Layout
```
src/oncall/
  __main__.py   CLI
  agent.py      investigation loop and diagnosis validation
  tools/        one module per data source, registered with @tool
  evidence.py   evidence ledger
  safety.py     allowlist, approval, dry-run, kill switch
  llm.py        Ollama client
tests/          unit tests (no live services needed)
reports/        incident reports produced by the agent
```

## Status and known limitations
- [x] Metrics tool (`service_overview`)
- [ ] Traces, logs, container health, deployment history tools
- [ ] Alert detector
- [ ] Action execution, recovery check, rollback
- [ ] Incident report generation

A small local model can still misread evidence; every diagnosis is validated against the evidence
ledger and escalates to a human when citations are missing or confidence is low.
