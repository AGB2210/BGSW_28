#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# setup.sh - Setup script for AI On-Call Engineer
# Run this ONCE before first use.
# ═══════════════════════════════════════════════════════════════════════════════

set -e

echo "═══════════════════════════════════════════════════════"
echo "  AI On-Call Engineer - Setup"
echo "═══════════════════════════════════════════════════════"
echo ""

# ── 1. Check prerequisites ──────────────────────────────────────────────────
echo "[1/4] Checking prerequisites..."

if ! command -v python3 &> /dev/null; then
    echo "  ERROR: Python 3 not found. Install Python 3.11+"
    exit 1
fi
echo "  Python: $(python3 --version)"

if ! command -v docker &> /dev/null; then
    echo "  ERROR: Docker not found."
    exit 1
fi
echo "  Docker: $(docker --version)"

if ! command -v ollama &> /dev/null; then
    echo "  ERROR: Ollama not found."
    echo "  Install: https://ollama.ai/download"
    echo "  Or: curl -fsSL https://ollama.ai/install.sh | sh"
    exit 1
fi
echo "  Ollama: $(ollama --version 2>/dev/null || echo 'installed')"

# ── 2. Install Python dependencies ──────────────────────────────────────────
echo ""
echo "[2/4] Installing Python dependencies..."
pip install -r requirements.txt --break-system-packages 2>/dev/null || pip install -r requirements.txt
echo "  Done."

# ── 3. Pull Ollama models ───────────────────────────────────────────────────
echo ""
echo "[3/4] Pulling Ollama models..."
echo "  These models are optimized for 8GB VRAM (RTX 5050):"
echo ""

# Detection agent: needs strong reasoning
echo "  Pulling qwen2.5:7b (detection agent - reasoning)..."
ollama pull qwen2.5:7b

# Dev agent: needs code understanding
echo "  Pulling qwen2.5-coder:7b (dev agent - code analysis)..."
ollama pull qwen2.5-coder:7b

echo ""
echo "  Models ready!"

# ── 4. Create directories ───────────────────────────────────────────────────
echo ""
echo "[4/4] Creating output directories..."
mkdir -p incident_reports
echo "  Created: incident_reports/"

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════"
echo "  Setup complete!"
echo "═══════════════════════════════════════════════════════"
echo ""
echo "  Models installed:"
echo "    - qwen2.5:7b       (Detection Agent - ~4.7GB)"
echo "    - qwen2.5-coder:7b (Software Dev Agent - ~4.7GB)"
echo ""
echo "  Note: Only ONE model runs at a time, so 8GB VRAM is enough."
echo "  The agents swap models between phases."
echo ""
echo "  Quick start:"
echo "    1. Start the demo:  cd ~/opentelemetry-demo && docker compose up -d"
echo "    2. Inject a fault:  python faultctl.py --repo ~/opentelemetry-demo inject app-cart"
echo "    3. Investigate:     python orchestrator.py investigate --repo ~/opentelemetry-demo --alert 'Cart errors detected'"
echo ""
echo "  Or use monitoring mode:"
echo "    python orchestrator.py monitor --repo ~/opentelemetry-demo"
echo ""
echo "  Tip: set OTEL_DEMO_ROOT=~/opentelemetry-demo to skip --repo every time"
echo ""
