#!/usr/bin/env python3
"""
telemetry_collector.py - Collects logs, metrics, traces, and container health
from the OpenTelemetry Demo stack.

Actual label names (verified live):
  HTTP:  service_name, http_response_status_code  (metric: http_server_request_duration_seconds_*)
  gRPC:  service_name, rpc_response_status_code   (metric: rpc_server_call_duration_seconds_*)
  Spans: service_name, status_code                (metric: traces_span_metrics_calls_total)
  Jobs:  "opentelemetry-demo/<svc>" format         (label: job)
"""

import json
import time
import datetime
import requests
import docker
from typing import Optional


class TelemetryCollector:

    def __init__(
        self,
        prometheus_url: str = "http://localhost:9090",
        jaeger_url: str = None,
        docker_socket: Optional[str] = None,
    ):
        self._prometheus_override = prometheus_url

        self._noise_services = {
            "astronomy-db", "postgres", "postgresql",
            "prometheus", "grafana", "jaeger",
            "otel-collector", "opensearch",
            "valkey-cart", "kafka", "opamp-server",
            "telemetry-docs", "image-provider", "frontend-proxy",
        }

        try:
            self.docker = docker.from_env() if not docker_socket else docker.DockerClient(base_url=docker_socket)
        except Exception as e:
            print(f"[WARN] Docker not available: {e}")
            self.docker = None

        self.prom_url = self._discover_prometheus(self._prometheus_override)
        self.jaeger_url = self._discover_jaeger(jaeger_url)
        self._prom_alive = self._check_prom_alive()

    # ─── INIT HELPERS ────────────────────────────────────────────────────────

    def _discover_prometheus(self, override: str) -> str:
        """Find Prometheus's mapped host port (like Jaeger, it may be random)."""
        if self.docker:
            try:
                for c in self.docker.containers.list():
                    if c.labels.get("com.docker.compose.service") == "prometheus":
                        for cp, bindings in (c.ports or {}).items():
                            if "9090" in cp and bindings:
                                port = bindings[0]["HostPort"]
                                url = f"http://localhost:{port}"
                                print(f"[INFO] Prometheus discovered at: {url}")
                                return url
            except Exception:
                pass
        return override.rstrip("/")

    def _check_prom_alive(self) -> bool:
        try:
            r = requests.get(f"{self.prom_url}/api/v1/query",
                             params={"query": "up or vector(1)"}, timeout=5)
            return bool(r.json().get("data", {}).get("result"))
        except Exception:
            return False

    def _discover_jaeger(self, override: str = None) -> str:
        # Find Jaeger's mapped host port
        jaeger_port = None
        if self.docker:
            try:
                for c in self.docker.containers.list():
                    if c.labels.get("com.docker.compose.service") == "jaeger":
                        for cp, bindings in (c.ports or {}).items():
                            if "16686" in cp and bindings:
                                jaeger_port = bindings[0]["HostPort"]
                        break
            except Exception:
                pass

        candidates = []
        if override:
            candidates.append(override.rstrip("/"))
        if jaeger_port:
            # Jaeger v2 uses /api/v3 under the base_path
            candidates.append(f"http://localhost:{jaeger_port}/jaeger/ui/api/v3")
            candidates.append(f"http://localhost:{jaeger_port}/api/v3")
            candidates.append(f"http://localhost:{jaeger_port}/api")
        candidates += [
            "http://localhost:8080/jaeger/ui/api/v3",
            "http://localhost:16686/jaeger/ui/api/v3",
            "http://localhost:16686/api",
        ]
        seen = set()
        unique = [c for c in candidates if c not in seen and not seen.add(c)]

        for url in unique:
            try:
                r = requests.get(f"{url}/services", timeout=3)
                if r.status_code == 200:
                    body = r.json()
                    if "data" in body or "services" in body:
                        print(f"[INFO] Jaeger API: {url}")
                        self._jaeger_v3 = "/v3" in url
                        return url
            except Exception:
                continue

        best = f"http://localhost:{jaeger_port}/jaeger/ui/api/v3" if jaeger_port else "http://localhost:16686/api"
        print(f"[WARN] Jaeger discovery failed, guessing: {best}")
        self._jaeger_v3 = True
        return best

    # ═══════════════════════════════════════════════════════════════════════════
    # CONTAINER HEALTH
    # ═══════════════════════════════════════════════════════════════════════════

    def get_all_container_status(self) -> list[dict]:
        if not self.docker:
            return [{"error": "Docker not available"}]
        containers = []
        for c in self.docker.containers.list(all=True):
            labels = c.labels or {}
            project = labels.get("com.docker.compose.project", "")
            service = labels.get("com.docker.compose.service", "")
            if not ("opentelemetry" in project.lower() or "otel" in project.lower()):
                continue
            if "flag" in (service or c.name).lower():   # never expose to the LLM
                continue
            info = {
                "name": service or c.name,
                "status": c.status,
                "health": "unknown",
            }
            try:
                hs = c.attrs.get("State", {}).get("Health", {})
                if hs:
                    info["health"] = hs.get("Status", "unknown")
                    info["failing_streak"] = hs.get("FailingStreak", 0)
            except Exception:
                pass
            info["restart_count"] = c.attrs.get("RestartCount", 0)
            containers.append(info)
        return containers

    def get_container_stats(self, service_name: str) -> dict:
        container = self._find_container(service_name)
        if not container:
            return {"error": f"Container '{service_name}' not found"}
        try:
            stats = container.stats(stream=False)
            cpu_d = stats["cpu_stats"]["cpu_usage"]["total_usage"] - stats["precpu_stats"]["cpu_usage"]["total_usage"]
            sys_d = stats["cpu_stats"]["system_cpu_usage"] - stats["precpu_stats"]["system_cpu_usage"]
            ncpu = stats["cpu_stats"].get("online_cpus", 1)
            cpu_pct = (cpu_d / sys_d) * ncpu * 100 if sys_d > 0 else 0
            mem = stats["memory_stats"].get("usage", 0)
            mem_lim = stats["memory_stats"].get("limit", 1)
            return {
                "service": service_name,
                "cpu_percent": round(cpu_pct, 2),
                "memory_usage_mb": round(mem / 1048576, 2),
                "memory_limit_mb": round(mem_lim / 1048576, 2),
                "memory_percent": round(mem / mem_lim * 100, 2) if mem_lim else 0,
            }
        except Exception as e:
            return {"error": str(e)}

    def get_service_logs(self, service_name: str, tail: int = 200, level: str = "error",
                         since_minutes: int = 3) -> list[str]:
        container = self._find_container(service_name)
        if not container:
            return [f"Container '{service_name}' not found"]
        try:
            raw = container.logs(
                tail=tail, timestamps=True
            ).decode("utf-8", errors="replace")
            lines = raw.strip().split("\n")

            # Filter to only recent logs based on the timestamp prefix
            # Docker log lines with timestamps=True look like:
            # 2026-10-24T04:06:43.765611409Z <actual log content>
            cutoff = datetime.datetime.utcnow() - datetime.timedelta(minutes=since_minutes)
            cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
            recent_lines = []
            for line in lines:
                # Timestamp is the first 19 chars (YYYY-MM-DDTHH:MM:SS)
                if len(line) >= 19:
                    ts_part = line[:19]
                    try:
                        if ts_part >= cutoff_str:
                            recent_lines.append(line)
                    except Exception:
                        recent_lines.append(line)  # keep if can't parse
                else:
                    recent_lines.append(line)

            if level == "all":
                return recent_lines[-100:]
            kw = {"error": ["ERROR", "EXCEPTION", "FATAL", "PANIC",
                          "FAILED WITH STATUS", "CODE=UNAVAILABLE",
                          "CODE=INTERNAL", "CODE=DEADLINE_EXCEEDED",
                          "CONNECTION REFUSED", "TIMED OUT"],
                  "warn": ["WARN", "WARNING", "ERROR", "EXCEPTION", "FATAL",
                           "FAILED WITH STATUS", "CODE=UNAVAILABLE"]
                  }.get(level, ["ERROR"])
            return [l for l in recent_lines if any(k in l.upper() for k in kw)][-50:]
        except Exception as e:
            return [f"Log fetch failed: {e}"]

    # ═══════════════════════════════════════════════════════════════════════════
    # PROMETHEUS
    # ═══════════════════════════════════════════════════════════════════════════

    def prom_query(self, promql: str) -> list[dict]:
        try:
            r = requests.get(f"{self.prom_url}/api/v1/query",
                             params={"query": promql}, timeout=10)
            r.raise_for_status()
            d = r.json()
            return d["data"]["result"] if d.get("status") == "success" else []
        except Exception:
            return []

    def prom_query_range(self, promql: str, duration_min: int = 15, step: str = "30s") -> list[dict]:
        try:
            end = datetime.datetime.utcnow()
            start = end - datetime.timedelta(minutes=duration_min)
            r = requests.get(f"{self.prom_url}/api/v1/query_range", params={
                "query": promql,
                "start": start.isoformat("T") + "Z",
                "end": end.isoformat("T") + "Z",
                "step": step,
            }, timeout=10)
            r.raise_for_status()
            return r.json()["data"]["result"]
        except Exception:
            return []

    def list_metric_names(self, search: str = "") -> list[str]:
        try:
            r = requests.get(f"{self.prom_url}/api/v1/label/__name__/values", timeout=10)
            names = r.json().get("data", [])
            if search:
                names = [n for n in names if search.lower() in n.lower()]
            return names
        except Exception as e:
            return [f"failed: {e}"]

    def get_service_error_rates(self) -> list[dict]:
        """Error rates from span_metrics → HTTP 5xx → gRPC errors → Docker logs."""
        results = []

        if self._prom_alive:
            # ── 1. Span metrics (best signal — generated by collector's span_metrics connector) ──
            # Probe the actual status label name first
            self._span_metric_name = "traces_span_metrics_calls_total"  # default
            status_label = self._discover_span_status_label()
            metric = self._span_metric_name
            if status_label:
                err_q = f'sum by (service_name) (rate({metric}{{{status_label}="STATUS_CODE_ERROR"}}[5m]))'
                tot_q = f'sum by (service_name) (rate({metric}[5m]))'
                results = self._ratio_query(err_q, tot_q, "service_name", "span_metrics")

            # ── 2. HTTP 5xx ──
            if not results:
                err_q = 'sum by (service_name) (rate(http_server_request_duration_seconds_count{http_response_status_code=~"5.."}[5m]))'
                tot_q = 'sum by (service_name) (rate(http_server_request_duration_seconds_count[5m]))'
                results = self._ratio_query(err_q, tot_q, "service_name", "http")

            # ── 3. gRPC errors (rpc_response_status_code != OK) ──
            if not results:
                err_q = 'sum by (service_name) (rate(rpc_server_call_duration_seconds_count{rpc_response_status_code!="OK",rpc_response_status_code!=""}[5m]))'
                tot_q = 'sum by (service_name) (rate(rpc_server_call_duration_seconds_count[5m]))'
                results = self._ratio_query(err_q, tot_q, "service_name", "grpc")

        # ── 4. Docker log fallback ──
        if not results:
            for svc in self._app_services():
                c = self._find_container(svc)
                if c and c.status == "running":
                    logs = self.get_service_logs(svc, tail=50, level="error")
                    real = [l for l in logs if len(str(l).strip()) > 10]
                    if len(real) > 5:
                        results.append({"service": svc, "protocol": "logs",
                                        "error_rate": len(real) / 50.0})
        return results

    def _discover_span_status_label(self) -> str:
        """Find which label name holds the span status code."""
        # Try the standard span_metrics name first
        for metric_name in ["traces_span_metrics_calls_total",
                            "traces_spanmetrics_calls_total",
                            "span_metrics_calls_total",
                            "spanmetrics_calls_total"]:
            r = self.prom_query(metric_name)
            if r:
                self._span_metric_name = metric_name
                sample = r[0].get("metric", {})
                print(f"[DEBUG] Span metric found: {metric_name}, labels: {list(sample.keys())}")
                for candidate in ["status_code", "span_status_code", "otel_status_code",
                                  "span.status_code", "status", "span_kind"]:
                    if candidate in sample and "STATUS" in str(self.prom_query(f'{metric_name}{{{candidate}=~".*ERROR.*"}}')):
                        return candidate
                # If none matched with ERROR filter, just return first status-like label
                for candidate in ["status_code", "span_status_code", "otel_status_code",
                                  "span.status_code", "status"]:
                    if candidate in sample:
                        return candidate
                # Return all labels for debugging
                print(f"[DEBUG] No status label found among: {list(sample.keys())}")
                return ""
        # No span metrics at all — try to find what metrics exist
        all_metrics = self.list_metric_names("span")
        if all_metrics:
            print(f"[DEBUG] Span-related metrics: {all_metrics[:10]}")
        else:
            print("[DEBUG] No span-related metrics found in Prometheus")
        return ""

    def _ratio_query(self, err_q, tot_q, label_key, protocol) -> list[dict]:
        errors = self.prom_query(err_q)
        if not errors:
            return []
        totals = {r["metric"].get(label_key, "?"): float(r["value"][1])
                  for r in self.prom_query(tot_q) if "metric" in r}
        results = []
        for r in errors:
            svc = r["metric"].get(label_key, "?")
            if any(n in svc.lower() for n in self._noise_services):
                continue
            err_rate = float(r["value"][1])
            total = totals.get(svc, err_rate)
            results.append({
                "service": svc, "protocol": protocol,
                "error_rate": err_rate / total if total > 0 else err_rate,
                "error_rps": err_rate,
            })
        return results

    def get_service_latency(self, service: str) -> dict:
        quantiles = {}
        patterns = [
            ("traces_span_metrics_duration_milliseconds_bucket", "service_name", 1),
            ("http_server_request_duration_seconds_bucket", "service_name", 1000),
            ("rpc_server_call_duration_seconds_bucket", "service_name", 1000),
        ]
        for metric, lbl, mult in patterns:
            for q in [0.5, 0.95, 0.99]:
                promql = f'histogram_quantile({q}, sum by (le) (rate({metric}{{{lbl}="{service}"}}[2m])))'
                r = self.prom_query(promql)
                if r and "value" in r[0]:
                    quantiles[f"p{int(q*100)}"] = round(float(r[0]["value"][1]) * mult, 2)
            if quantiles:
                break
        return {"service": service, "latency_ms": quantiles}

    def get_resource_metrics(self, service: str) -> dict:
        info = {"service": service}
        for metric, key, divisor in [
            (f'container_cpu_utilization_ratio{{name=~".*{service}.*"}}', "cpu_pct", 0.01),
            (f'container_memory_usage_total_bytes{{name=~".*{service}.*"}}', "mem_mb", 1048576),
        ]:
            r = self.prom_query(metric)
            if r and "value" in r[0]:
                v = float(r[0]["value"][1])
                info[key] = round(v / divisor, 2) if key == "mem_mb" else round(v * 100, 2)
        return info

    # ═══════════════════════════════════════════════════════════════════════════
    # JAEGER
    # ═══════════════════════════════════════════════════════════════════════════

    def get_services_list(self) -> list[str]:
        try:
            r = requests.get(f"{self.jaeger_url}/services", timeout=10)
            r.raise_for_status()
            body = r.json()
            # v3 returns {"services": [...]}, v1 returns {"data": [...]}
            return body.get("services", body.get("data", []))
        except Exception as e:
            return [f"Jaeger failed: {e}"]

    def _jaeger_trace_params(self, service: str, limit: int, lookback_min: int = 15):
        """Build query params for Jaeger traces, adapting to v3 vs v1 API."""
        now = datetime.datetime.utcnow()
        start_dt = now - datetime.timedelta(minutes=lookback_min)
        if getattr(self, "_jaeger_v3", False):
            # v3 uses RFC3339 timestamps and query.* prefix
            return {
                "query.service_name": service,
                "query.start_time_min": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "query.start_time_max": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "query.num_traces": limit,
            }
        else:
            end_us = int(time.time() * 1_000_000)
            start_us = end_us - lookback_min * 60_000_000
            return {
                "service": service, "start": start_us,
                "end": end_us, "limit": limit,
            }

    def _parse_trace_response(self, body: dict) -> list[dict]:
        """Parse traces from Jaeger API response (v1 or v3 format)."""
        # v3 nests under "result.resourceSpans" or just "resourceSpans"
        # v1 returns {"data": [trace1, trace2, ...]}
        raw_traces = body.get("data", [])
        if not raw_traces:
            result = body.get("result", {})
            if isinstance(result, dict):
                raw_traces = result.get("resourceSpans", result.get("traces", []))
            elif isinstance(result, list):
                raw_traces = result

        # Also check top-level resourceSpans (v3 direct)
        if not raw_traces:
            raw_traces = body.get("resourceSpans", [])

        traces = []
        for t in (raw_traces or []):
            spans = t.get("spans", [])
            if not spans:
                # v3/OTLP format: resourceSpans → scopeSpans → spans
                resource_spans = [t] if "scopeSpans" in t or "scope_spans" in t else \
                    t.get("resourceSpans", t.get("resource_spans", []))
                for rs in resource_spans:
                    for ss in rs.get("scopeSpans", rs.get("scope_spans", [])):
                        for sp in ss.get("spans", []):
                            # Extract service name from resource attributes
                            svc_name = ""
                            res_attrs = rs.get("resource", {}).get("attributes", [])
                            for attr in res_attrs:
                                if attr.get("key") == "service.name":
                                    svc_name = attr.get("value", {}).get("stringValue", "")
                            sp["_service_name"] = svc_name
                            spans.append(sp)
            if not spans:
                continue

            root = spans[0]
            err_spans = []
            for s in spans:
                is_err = False
                # v1 format: tags array with key/value
                for tag in s.get("tags", []):
                    if tag.get("key") == "error" and tag.get("value") == True:
                        is_err = True
                # v3/OTLP format: status.code == 2 (ERROR)
                status = s.get("status", {})
                if status.get("code") == 2 or status.get("code") == "STATUS_CODE_ERROR":
                    is_err = True
                if is_err:
                    err_spans.append(s)

            info = {
                "trace_id": t.get("traceID", t.get("traceId", root.get("traceId", ""))),
                "operation": root.get("operationName", root.get("name", "")),
                "span_count": len(spans),
                "error_span_count": len(err_spans),
            }
            # Duration: v1 uses microseconds, v3 OTLP may use startTimeUnixNano/endTimeUnixNano
            dur = root.get("duration", 0)
            if dur:
                info["duration_ms"] = round(dur / 1000, 2)
            elif root.get("endTimeUnixNano") and root.get("startTimeUnixNano"):
                try:
                    info["duration_ms"] = round(
                        (int(root["endTimeUnixNano"]) - int(root["startTimeUnixNano"])) / 1_000_000, 2)
                except (ValueError, TypeError):
                    pass

            if err_spans:
                details = []
                for s in err_spans[:5]:
                    d = {"operation": s.get("operationName", s.get("name", ""))}
                    svc = s.get("_service_name", "")
                    if svc:
                        d["service"] = svc
                    # Extract relevant tags (v1)
                    tags = {}
                    for tag in s.get("tags", []):
                        if tag["key"] in ("error", "error.message",
                                          "http.status_code", "otel.status_code",
                                          "rpc.grpc.status_code"):
                            tags[tag["key"]] = tag["value"]
                    # Extract status message (v3)
                    status_msg = s.get("status", {}).get("message", "")
                    if status_msg:
                        tags["status.message"] = status_msg
                    # Extract relevant attributes (v3 OTLP)
                    for attr in s.get("attributes", []):
                        k = attr.get("key", "")
                        if k in ("error.message", "exception.message", "exception.type",
                                 "rpc.grpc.status_code", "http.status_code"):
                            val = attr.get("value", {})
                            tags[k] = val.get("stringValue", val.get("intValue", str(val)))
                    if tags:
                        d["tags"] = tags
                    details.append(d)
                info["error_details"] = details
            traces.append(info)
        return traces

    def get_traces(self, service: str, limit: int = 10, lookback_min: int = 15) -> list[dict]:
        try:
            params = self._jaeger_trace_params(service, limit, lookback_min)
            r = requests.get(f"{self.jaeger_url}/traces", params=params, timeout=10)
            r.raise_for_status()
            return self._parse_trace_response(r.json())
        except Exception as e:
            return [{"error": str(e)}]

    def get_error_traces(self, service: str, limit: int = 5) -> list[dict]:
        """Get traces with errors. For v3, we fetch more traces and filter client-side."""
        try:
            params = self._jaeger_trace_params(service, limit * 3, lookback_min=15)
            # v1 supports tags filter; v3 may not — fetch more and filter
            if not getattr(self, "_jaeger_v3", False):
                params["tags"] = json.dumps({"error": "true"})
            r = requests.get(f"{self.jaeger_url}/traces", params=params, timeout=10)
            r.raise_for_status()
            all_traces = self._parse_trace_response(r.json())
            # Filter to traces that actually have errors
            error_traces = [t for t in all_traces if t.get("error_span_count", 0) > 0]
            return error_traces[:limit]
        except Exception as e:
            return [{"error": str(e)}]

    # ═══════════════════════════════════════════════════════════════════════════
    # SNAPSHOTS
    # ═══════════════════════════════════════════════════════════════════════════

    def collect_full_snapshot(self) -> dict:
        snapshot = {
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            "containers": [], "error_rates": [],
            "unhealthy_services": [], "error_logs": {}, "anomalies": [],
            "data_sources": {
                "prometheus": "live" if self._prom_alive else "unavailable",
                "jaeger": self.jaeger_url,
                "docker": "available" if self.docker else "unavailable",
            },
        }

        # Containers
        containers = self.get_all_container_status()
        snapshot["containers"] = containers

        # Unhealthy (app services only)
        for c in containers:
            name = c.get("name", "")
            if any(n in name.lower() for n in self._noise_services):
                continue
            if c.get("status") != "running" or c.get("restart_count", 0) > 2:
                snapshot["unhealthy_services"].append({
                    "name": name, "status": c.get("status"),
                    "restart_count": c.get("restart_count", 0),
                })

        # Error rates
        snapshot["error_rates"] = self.get_service_error_rates()

        # Anomalies from error rates
        for er in snapshot["error_rates"]:
            svc = er.get("service", "")
            if any(n in svc.lower() for n in self._noise_services):
                continue
            if er.get("error_rate", 0) > 0.001:
                snapshot["anomalies"].append({
                    "type": "high_error_rate", "service": svc,
                    "protocol": er.get("protocol"),
                    "value": er["error_rate"],
                })

        # Fallback: Docker log scanning for app services
        if not snapshot["anomalies"] and not snapshot["unhealthy_services"]:
            for svc in self._app_services():
                c = self._find_container(svc)
                if c and c.status == "running":
                    logs = self.get_service_logs(svc, tail=100, level="error")
                    real = [l for l in logs if len(str(l).strip()) > 10]
                    if len(real) > 3:
                        snapshot["anomalies"].append({
                            "type": "error_logs_detected", "service": svc,
                            "protocol": "logs", "value": len(real),
                        })

        # Grab logs for problem services
        problem = {a["service"] for a in snapshot["anomalies"]} | \
                  {u["name"] for u in snapshot["unhealthy_services"]}
        for svc in problem:
            logs = self.get_service_logs(svc, tail=100, level="error")
            if logs:
                snapshot["error_logs"][svc] = logs[:20]

        return snapshot

    def collect_targeted_investigation(self, service: str) -> dict:
        inv = {
            "service": service,
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            "container_status": {}, "container_stats": {},
            "error_logs": [], "metrics": {},
            "latency": {}, "recent_traces": [], "error_traces": [],
        }
        for c in self.get_all_container_status():
            if service.lower() in c.get("name", "").lower():
                inv["container_status"] = c
                break
        inv["container_stats"] = self.get_container_stats(service)
        inv["error_logs"] = self.get_service_logs(service, tail=200, level="warn")
        inv["metrics"] = self.get_resource_metrics(service)
        inv["latency"] = self.get_service_latency(service)
        inv["recent_traces"] = self.get_traces(service, limit=5)
        inv["error_traces"] = self.get_error_traces(service, limit=5)
        return inv

    # ─── HELPERS ─────────────────────────────────────────────────────────────

    def _find_container(self, service_name: str):
        if not self.docker:
            return None
        for c in self.docker.containers.list(all=True):
            if c.labels.get("com.docker.compose.service") == service_name:
                return c
        slug = service_name.lower().replace("-", "").replace("_", "")
        for c in self.docker.containers.list(all=True):
            if slug in c.name.lower().replace("-", "").replace("_", ""):
                return c
        return None

    def _app_services(self) -> set:
        return {"ad", "cart", "checkout", "currency", "email", "frontend",
                "load-generator", "payment", "product-catalog", "quote",
                "recommendation", "shipping", "accounting", "fraud-detection"}


if __name__ == "__main__":
    c = TelemetryCollector()
    print(json.dumps(c.collect_full_snapshot(), indent=2, default=str))
