# Architecture

```
alert ─▶ agent loop ─▶ tools ─▶ Prometheus / Jaeger / OpenSearch / Docker
            │  ▲          │
            │  └─ results ┘── evidence ledger (ev-001, ev-002 …)
            ▼
       diagnosis ─▶ validator ─▶ escalate?  ─▶ human
            │          (citations exist, confidence ≥ threshold)
            ▼
   proposed action ─▶ safety gate ─▶ human "yes" ─▶ action ─▶ verify ─▶ report
                   (allowlist, kill switch, dry-run)
```

| Module | Job |
|---|---|
| `config.py` | Settings from `.env`; finds OpenSearch's random port |
| `http.py` | Only HTTP path for tools; blocks feature-flag endpoints |
| `tools/` | One file per data source; `@tool` registers a function for the model |
| `evidence.py` | Ledger of tool results; rejects citations that don't exist |
| `llm.py` | Local model via Ollama (no hosted APIs) |
| `agent.py` | Tool-calling loop, final JSON diagnosis, escalation rules |
| `schema.py` | Diagnosis model (align with the official schema if one is published) |
| `safety.py` | Allowlist, approval prompt, dry-run, kill switch |

## Safety controls
- The model can only *propose* actions; execution goes through `safety.py`.
- Only `restart_service`, `toggle_feature_flag`, `throttle_load_generator`, `scale_service` are allowed.
- Nothing runs without the operator typing `yes`. `DRY_RUN=true` by default.
- Creating `.killswitch` in the repo root blocks every action.
- Flag settings (flagd, flag UI, flag API) are unreachable from tools by design.
