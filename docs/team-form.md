# Team Form

## Team

| | |
|---|---|
| Team | Team 28 |
| Members | Akash, Hrushik, Sriram, Veerendra |
| Repository | `AGB2210/BGSW_28` |

## Who built which part

| Part | Built by |
|---|---|
| AI agent: the agent loop, the five read-only tools, the evidence checks, the diagnosis and remedy rules | Akash |
| Setup: Docker in WSL, the demo in a lean setup, the local model, `demo.bat` and `start.bat` | Hrushik |
| Fault injection: faultctl and its 14 practice incidents | Hrushik |
| Fault detection: the alert detector that compares live metrics and containers with a healthy baseline | Sriram |
| Kill switch, dry run, incident reports and automatic runs (auto-detect) | Veerendra |

## Tools and AI models

| Tool or model | Used for |
|---|---|
| qwen3:4b, run locally with Ollama | The agent's model: chooses tools and writes the diagnosis. No hosted AI service is used at run time. |
| Claude Code (Anthropic), an AI coding assistant | Used throughout development: it wrote most of the code, tests and documentation under our direction, ran the evaluations and live tests, tested the console in a browser and helped prepare the slides. We set the goals and constraints, made the design decisions, reviewed the results and pushed the code. |
| OpenTelemetry Demo 3.1.0 | The shop being monitored, with its own telemetry stack: OpenTelemetry Collector, Prometheus, Jaeger, OpenSearch, flagd, Envoy, Locust, PostgreSQL and Valkey |
| Docker Engine and Docker Compose, in WSL2 Ubuntu 24.04 | Run the demo, the model and the agent on one laptop |
| nicolaka/netshoot container image | Adds the network delay for one practice incident, and reads delay rules for the container check |
| Git and GitHub | Version control |

## Third-party libraries

| Library | Used for |
|---|---|
| httpx | HTTP calls to Prometheus, Jaeger, OpenSearch, Ollama and the Action API |
| pydantic | The diagnosis and incident report models, and the JSON schema |
| pytest | Unit tests (development only) |

Everything else uses the Python 3.12 standard library, including the console's web server and the
Action API. The console page uses no outside scripts, styles or fonts.
