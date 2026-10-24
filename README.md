# On-Call Agent

An AI on-call engineer for the [OpenTelemetry Demo](https://github.com/open-telemetry/opentelemetry-demo)
(release 3.1.0). It notices an incident, investigates traces, metrics, logs and container health
with tools, names the root cause with cited evidence, proposes an allowlisted fix, waits for a
human to approve it, applies it through the Action API, checks that the system recovered, and
writes an incident report. When it is not sure, it escalates with what it found instead of acting.

Built for the Bosch *Incident Zero* hackathon. Runs entirely on a laptop with a local model
(no hosted LLM, no internet needed at runtime).

## How it works

```
detector ─▶ agent loop ─▶ read-only tools ─▶ Prometheus · Jaeger · OpenSearch · Docker
 (baseline)    │  ▲           │
               │  └─ results ─┴─ evidence ledger (ev-001, ev-002 …)
               ▼
          diagnosis ─▶ validator ─▶ escalate? ──────────────────────────▶ report
               │        (citations real, 2+ tools agree, confidence ≥ 0.6)
               ▼
          remedy rules ─▶ approval (human) ─▶ Action API ─▶ verify ─▶ rollback? ─▶ report
                          dry-run · kill switch · cancel
```

Details: [docs/architecture.md](docs/architecture.md). Results: [docs/evaluation.md](docs/evaluation.md).

## Requirements
- WSL2 Ubuntu (or Linux) with Docker Engine + Compose
- The OpenTelemetry Demo cloned at `~/opentelemetry-demo`, checked out at tag `3.1.0`
- [Ollama](https://ollama.com) with `qwen3:4b` (runs fully on a 4 GB GPU)
- Python 3.11+

## Setup
```bash
python3 -m venv ~/.venvs/oncall && source ~/.venvs/oncall/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # defaults work for the local demo
ollama pull qwen3:4b
scripts/demo.sh pull          # once: download the demo's release images
scripts/demo.sh up            # start the demo (lean setup, see below)
oncall check                  # all four backends should say ok
oncall baseline               # save healthy metrics once the demo has settled
```

## Running it

### On-Call Console (web UI)
```bash
action-api &                  # the only way to change the system (port 8765)
oncall-console                # http://localhost:8800
```
The console shows service health, a live feed of every tool the agent calls, the diagnosis with
its evidence, an **Approve / Reject** card for proposed actions, dry-run and auto-detect toggles,
a **kill switch**, a **Cancel investigation** button, the Action API log and links to each report.
It also has buttons to inject and reset practice incidents for demos.

### Command line
```bash
oncall detect                                        # one check against the baseline
oncall investigate "checkout error rate above 5%"   # diagnosis only
oncall respond "checkout error rate above 5%"       # full loop with a terminal approval prompt
oncall respond "..." --dry-run                      # same, but actions are only described
oncall watch                                         # detector loop; responds to alerts
oncall schema                                        # regenerate schemas/incident_report.schema.json
```

### Practice incidents and evaluation
```bash
faultctl list                          # 14 incidents: 9 application, 5 infrastructure
faultctl inject cache-down             # inject one
faultctl status / faultctl reset       # what is injected / undo everything
python -m evaluation                   # inject each, detect, investigate, score, reset
python -m evaluation cache-down db-down
```
Expected answers live only in `src/evaluation/`; the agent never imports it or faultctl.

## Safety
- **Human approval:** the agent only proposes. Nothing changes until a named person approves.
- **Allowlist:** restart a service, toggle a feature flag, throttle the load generator
  (scaling is refused: the demo runs on Compose, not Kubernetes). The telemetry stack and the
  flag service can never be restarted by the agent.
- **Dry run:** on by default (`DRY_RUN=true`); the console has a toggle.
- **Kill switch:** console button, or `touch .killswitch`. Blocks every action.
- **Cancel:** stops a running investigation, approval wait or recovery check; the incident is
  still reported with status `cancelled`.
- **Recovery check and rollback:** after an action the component must return to its baseline,
  otherwise the action is rolled back where possible and the incident escalates.
- **Every action call is logged**, including refused ones (`runs/actions.jsonl`).
- **No flag peeking:** diagnosis comes from telemetry only. Tools cannot reach flagd or the flag
  UI, feature-flag span events are stripped from traces, and the flag service's own logs and
  traces are hidden from the agent.
- **No invented evidence:** every diagnosis must cite evidence IDs from real tool results; the
  blamed component must appear in evidence from at least two tools, or the agent escalates.

## Environment notes (honest disclosure)
- **Lean demo setup.** `scripts/demo.sh` runs the demo's minimal mode (no Kafka, accounting,
  fraud-detection) and also leaves out grafana, flagd-ui and telemetry-docs, which the agent
  does not use. This fits the demo next to the model in 10 GB of WSL memory.
- **Memory override.** `compose.local.yaml` raises the memory caps of six services
  (product-catalog, checkout, flagd, quote, payment, ad). On this laptop they did not fit their
  shipped caps and thrashed or crash-restarted at baseline (measurements are in the file). No demo
  source or config files are edited, and incidents behave as shipped.
- **WSL settings.** `C:\Users\<you>\.wslconfig`: `memory=10GB`, `swap=0`, `processors=6`. With all
  16 cores visible, the Java/Go services sized themselves too large for their caps.

## Layout
```
src/oncall/          the agent
  __main__.py        CLI
  agent.py           investigation loop, final schema-constrained answer, validation
  tools/             read-only tools: metrics, traces, logs, containers, deployment history
  evidence.py        evidence ledger
  detector.py        baseline-relative alert detector
  remedy.py          rules that pick an allowlisted action or escalate
  incident.py        full loop: investigate → propose → approve → act → verify → report
  actions.py         Action API client
  schema.py          incident report models (JSON schema in schemas/)
  safety.py          allowlist, kill switch
  llm.py             Ollama client (local model only)
src/console/         On-Call Console web UI (standard library server + one page)
src/actionapi/       Action API server and call log
src/faultctl/        practice incident injector
src/ops/             docker / flag / load-generator operations (Action API and faultctl only)
src/evaluation/      practice-incident evaluation harness
scripts/demo.sh      start/stop the demo in the lean setup
compose.local.yaml   documented memory override
tests/               unit tests (no live services needed)
reports/             incident reports produced by the agent
```

## Known limitations
- A small local model (4B parameters) can misread evidence or name a symptom instead of the
  cause. The validator catches unsupported answers and escalates, which is safe but not always
  helpful. See the evaluation note for per-incident results.
- Application faults inside a healthy container are escalated rather than fixed: choosing which
  feature flag to turn off would require reading flag settings, which the rules forbid.
- The network-latency practice incident uses the `nicolaka/netshoot` helper image to run `tc`
  inside the target container's network (the demo images lack it).
- `scale_service` is refused because the demo runs on Docker Compose, not Kubernetes.
- A model call already in progress (up to ~20 s) finishes before Cancel takes effect.
- Thinking mode (`OLLAMA_THINK=true`) is off: on six hard practice incidents it was slower
  (~216 s vs ~130 s per investigation) and less decisive (2 vs 4 confident correct answers).
- A CPU-hungry incident can starve Prometheus on this 6-core setup; the detector then reports
  "metrics unavailable" and retries instead of failing.
