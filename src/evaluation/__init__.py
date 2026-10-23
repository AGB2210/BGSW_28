"""Practice-incident evaluation: inject, detect, investigate, score, reset.

The expected answers below are used only for scoring. The agent package never
imports this package or faultctl.
"""

# Components a correct diagnosis may name for each incident.
EXPECTED: dict[str, set[str]] = {
    "payment-failure": {"payment"},
    "payment-unreachable": {"payment"},
    "cart-failure": {"cart"},
    "product-catalog-failure": {"product-catalog"},
    "recommendation-cache-leak": {"recommendation"},
    "ad-high-cpu": {"ad"},
    "ad-gc": {"ad"},
    "homepage-flood": {"load-generator", "frontend", "frontend-proxy"},
    "image-slow": {"image-provider", "frontend"},
    "service-down": {"checkout"},
    "cache-down": {"valkey-cart", "valkey"},
    "db-down": {"astronomy-db", "postgres", "postgresql"},
    "cpu-starve": {"product-catalog"},
}
