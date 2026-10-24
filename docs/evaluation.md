# Evaluation Note

Which practice incidents the agent got right and wrong in our own testing, and why.

## How we tested

- **14 practice incidents**, injected with faultctl: 9 application faults (the demo's failure flags)
  and 5 infrastructure faults.
- For each one, `python -m evaluation` resets everything, waits 5 minutes and until the system is
  calm, injects the fault, waits up to 5 minutes for the detector's alert, runs the investigation and
  compares the agent's answer with the expected component.
- **Exact names only.** An answer is right only when the component name matches one we accept for
  that incident.
- **Confident** means a confidence of at least 0.6 without the model asking to escalate. Only a
  confident diagnosis goes on to propose an action.
- The results below are from run 8, the final run with every fix. Four incidents were re-run on a
  clean start: two had started while the previous incident's failures were still visible, one was
  interrupted by a manual reset, and one started before the system had settled. The raw results,
  with every piece of evidence, are in `runs/eval/` on the test laptop.

## Results

| Incident | Expected | Agent's answer | Confidence | Result | Why |
|---|---|---|---|---|---|
| payment-failure | payment | payment | 0.80 | Right | The alert showed errors in checkout and payment; traces showed the failures start in payment |
| payment-unreachable | payment | payment | 0.90 | Right | Checkout's calls to payment got no answer; the rule for unanswered calls blames the service that does not answer, not the caller |
| cart-failure | cart | cart | 0.95 | Right | Checkout slowed down waiting for cart; traces and logs pointed at cart |
| product-catalog-failure | product-catalog | load-generator, escalated | 0.50 | Escalated | Only one product fails, so too few requests fail to raise an alert or point at product-catalog; the agent handed over instead of guessing |
| recommendation-cache-leak | recommendation | recommendation | 0.50 | Right, not confident | The leak builds up slowly and no alert fired within 5 minutes, so only one source pointed at recommendation |
| ad-high-cpu | ad | ad | 0.50 | Right, not confident | The alert did not point at ad, so only one source agreed |
| ad-gc | ad | ad | 0.95 | Right | The alert showed ad's latency jump; traces agreed |
| homepage-flood | frontend, frontend-proxy or load-generator | load-generator | 0.50 | Right, not confident | It named the true source, but the load generator never counts as evidence on its own (its errors are usually symptoms), so confidence was capped |
| image-slow | frontend-proxy, frontend or image-provider | frontend-proxy | 0.80 | Right, not confident | Right component, but the model itself asked to escalate |
| service-down | checkout | checkout | 1.00 | Right | The container alert and the container check both showed checkout stopped |
| cache-down | valkey-cart | valkey-cart | 1.00 | Right | It blamed the stopped cache, not the cart service that reported the errors |
| db-down | astronomy-db | astronomy-db | 1.00 | Right | The container alert and the container check both showed the database stopped |
| cpu-starve | product-catalog | product-catalog | 1.00 | Right | The container alert showed a CPU limit of 0.05 cores; the container check agreed |
| network-latency | cart | cart | 0.95 | Right | Cart's latency jumped while its container looked healthy; the container check found a 2-second network delay rule |

**Totals:** 13 of 14 right (9 confident, 4 not confident enough to act), 1 escalated, 0 wrong.

## Actions in live tests

Diagnosis is half the job. These incidents were also run end to end, with approval and real actions:

| Incident | Action | Outcome |
|---|---|---|
| service-down, cache-down, db-down | Restart the stopped container | Verified recovered, in 1–3 minutes |
| network-latency | Restart cart, which rebuilds its network without the delay | Verified recovered |
| cpu-starve | Lift the CPU limit | Verified recovered |
| homepage-flood | Throttle the load generator | Traffic back to normal; the flood setting itself stays on until reset |
| image-slow | None | Handed over, correctly not throttled |
| payment-failure | None | Diagnosed as payment, then handed over: an application bug that only a feature-flag change fixes, and the agent may not read flags |

The eight flag-driven application faults are handed over by design for the same reason.

## Wrong answers we saw while testing, and what fixed them

| What went wrong | Fix |
|---|---|
| The first run found the right component in only 2 of 14 and named a wrong one in 3 | Schema-limited answers, evidence checks, cause-before-symptom rules, an automatic container check |
| ad-high-cpu was blamed on recommendation, a neighbour that slowed down | Rules that look for where failures start and which container is in trouble |
| cart-failure was blamed on checkout, because the previous incident's failures were still in the 5-minute trace window | The harness waits 5 minutes after each reset |
| Our early scoring counted "load-generator" as a match for "ad" | Exact names only |

## What it still gets wrong or misses

- A single failing product (product-catalog-failure) is too faint a signal to pin down.
- Faults that build up slowly, or whose alert points elsewhere, are diagnosed correctly but not
  confidently enough to act.
- Incidents started back to back can confuse it, because its tools look back 5 minutes.
