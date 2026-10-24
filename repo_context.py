#!/usr/bin/env python3
"""
repo_context.py - Provides the software dev agent with a structured overview
of the OpenTelemetry Demo repository for safe agent consumption.

This module builds a safe repo map that the dev agent can reference to
pinpoint where faults might originate in the codebase.
"""

import os
import json
from pathlib import Path

# Internal config files/dirs excluded from agent access
BLOCKED_PATTERNS = [
    "flagd",
    "demo.flagd.json",
    ".flagd",
    "flag",
    "feature_flag",
    "featureflag",
    "faultctl",
]


def is_blocked(path: str) -> bool:
    """Check if a path should be blocked from agent access."""
    path_lower = path.lower()
    for pattern in BLOCKED_PATTERNS:
        if pattern.lower() in path_lower:
            return True
    return False


def get_repo_overview(repo_root: str) -> dict:
    """
    Build a structured overview of the OpenTelemetry Demo repo.
    Excludes internal configuration files not relevant to diagnosis.
    """
    overview = {
        "project": "OpenTelemetry Demo (Astronomy Shop)",
        "description": "A microservices e-commerce demo with distributed tracing",
        "services": {},
        "infrastructure": {},
        "compose_files": [],
    }

    src_dir = os.path.join(repo_root, "src")
    if not os.path.isdir(src_dir):
        return {"error": f"src/ directory not found at {repo_root}"}

    # Map each service
    for service_name in sorted(os.listdir(src_dir)):
        service_path = os.path.join(src_dir, service_name)
        if not os.path.isdir(service_path):
            continue
        if is_blocked(service_name):
            continue

        svc_info = {
            "path": f"src/{service_name}",
            "language": _detect_language(service_path),
            "key_files": [],
            "has_dockerfile": False,
            "description": _get_service_description(service_name),
        }

        for root, dirs, files in os.walk(service_path):
            # Skip blocked dirs
            dirs[:] = [d for d in dirs if not is_blocked(d)]
            rel_root = os.path.relpath(root, repo_root)

            for f in files:
                if is_blocked(f):
                    continue
                rel_path = os.path.join(rel_root, f)
                if f == "Dockerfile":
                    svc_info["has_dockerfile"] = True
                if _is_key_file(f):
                    svc_info["key_files"].append(rel_path)

        overview["services"][service_name] = svc_info

    # Compose files
    for f in os.listdir(repo_root):
        if f.startswith("compose") and f.endswith(".yaml"):
            overview["compose_files"].append(f)

    # Infrastructure info
    overview["infrastructure"] = {
        "otel_collector": "Collects traces, metrics, logs from all services via OTLP",
        "prometheus": "Metrics storage, scraped from collector",
        "jaeger": "Distributed tracing UI and storage",
        "grafana": "Dashboards and visualization",
        "postgresql": "Database for product catalog and other services",
        "valkey_cart": "Redis-compatible cache for cart service data",
        "kafka": "Message broker for async order processing",
        "load_generator": "Locust-based load generator simulating user traffic",
    }

    return overview


def get_service_source(repo_root: str, service_name: str, max_lines: int = 500) -> dict:
    """
    Read the main source files of a specific service.
    Returns source files, excluding internal configs.
    """
    src_dir = os.path.join(repo_root, "src", service_name)
    if not os.path.isdir(src_dir):
        return {"error": f"Service '{service_name}' not found"}

    if is_blocked(service_name):
        return {"error": f"Access to '{service_name}' is restricted"}

    source_files = {}
    total_lines = 0

    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if not is_blocked(d) and d not in (
            "node_modules", "__pycache__", ".git", "vendor", "build", "dist", "target"
        )]

        for f in sorted(files):
            if is_blocked(f):
                continue
            if not _is_source_file(f):
                continue

            filepath = os.path.join(root, f)
            rel_path = os.path.relpath(filepath, repo_root)

            try:
                with open(filepath, 'r', errors='replace') as fh:
                    content = fh.read()
                lines = content.split('\n')
                # Filter out lines referencing internal configs
                safe_lines = []
                for line in lines:
                    if any(bp in line.lower() for bp in ["flagd", "feature_flag", "featureflag"]):
                        safe_lines.append("// [internal config - not relevant to diagnosis]")
                    else:
                        safe_lines.append(line)

                if total_lines + len(safe_lines) > max_lines:
                    remaining = max_lines - total_lines
                    source_files[rel_path] = "\n".join(safe_lines[:remaining]) + \
                        f"\n// ... truncated ({len(safe_lines) - remaining} more lines)"
                    break
                else:
                    source_files[rel_path] = "\n".join(safe_lines)
                    total_lines += len(safe_lines)
            except Exception:
                continue

        if total_lines >= max_lines:
            break

    return source_files


def _detect_language(service_path: str) -> str:
    """Detect the primary language of a service."""
    extensions = {}
    for root, _, files in os.walk(service_path):
        for f in files:
            ext = Path(f).suffix.lower()
            if ext in (".py", ".go", ".js", ".ts", ".java", ".kt", ".cs", ".rb", ".rs", ".cpp", ".c"):
                extensions[ext] = extensions.get(ext, 0) + 1

    lang_map = {
        ".py": "Python", ".go": "Go", ".js": "JavaScript", ".ts": "TypeScript",
        ".java": "Java", ".kt": "Kotlin", ".cs": "C#", ".rb": "Ruby",
        ".rs": "Rust", ".cpp": "C++", ".c": "C",
    }
    if extensions:
        top_ext = max(extensions, key=extensions.get)
        return lang_map.get(top_ext, "Unknown")

    # Check for specific build files
    for f in os.listdir(service_path):
        if f == "go.mod": return "Go"
        if f == "package.json": return "JavaScript/TypeScript"
        if f == "requirements.txt": return "Python"
        if f == "pom.xml" or f == "build.gradle": return "Java"
        if f == "Cargo.toml": return "Rust"
        if f == "Gemfile": return "Ruby"
    return "Unknown"


def _get_service_description(name: str) -> str:
    """Return a brief description of each OTel Demo service."""
    descriptions = {
        "ad": "Java ad service - returns context-based advertisements",
        "cart": "DotNet cart service - manages shopping cart via Valkey/Redis",
        "checkout": "Go checkout service - handles order placement and payment",
        "currency": "C++ currency conversion service",
        "email": "Ruby email confirmation service",
        "frontend": "JavaScript/TypeScript Next.js web frontend",
        "frontend-proxy": "Envoy proxy routing frontend requests",
        "image-provider": "Nginx image serving for product images",
        "kafka": "Apache Kafka message broker",
        "load-generator": "Python Locust load generator simulating users",
        "payment": "JavaScript payment processing service",
        "product-catalog": "Go product catalog with PostgreSQL backend",
        "quote": "PHP shipping quote service",
        "recommendation": "Python recommendation engine using product catalog",
        "shipping": "Rust shipping cost calculator",
        "accounting": "Go accounting service consuming Kafka order events",
        "fraud-detection": "Kotlin fraud detection consuming Kafka order events",
    }
    return descriptions.get(name, f"{name} service")


def _is_key_file(filename: str) -> bool:
    """Check if a file is a key/important file worth listing."""
    key_names = {"Dockerfile", "README.md", "go.mod", "package.json",
                 "requirements.txt", "pom.xml", "build.gradle", "Cargo.toml"}
    return filename in key_names or _is_source_file(filename)


def _is_source_file(filename: str) -> bool:
    """Check if a file is a source code file."""
    return Path(filename).suffix.lower() in {
        ".py", ".go", ".js", ".ts", ".jsx", ".tsx",
        ".java", ".kt", ".cs", ".rb", ".rs", ".cpp", ".c", ".h",
        ".proto", ".yaml", ".yml", ".toml",
    }


if __name__ == "__main__":
    import sys
    repo_root = sys.argv[1] if len(sys.argv) > 1 else "."
    overview = get_repo_overview(repo_root)
    print(json.dumps(overview, indent=2))
