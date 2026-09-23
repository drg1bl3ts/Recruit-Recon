#!/usr/bin/env bash
# One-time setup for RecruitRecon: creates .venv, installs Python deps, and
# initializes the database. Re-run any time (e.g. after pulling changes that
# add a new dependency) — it's idempotent.
#
# After this, just run `python3 recon.py` directly — it auto-detects and
# re-execs itself under .venv, so you never need to `source .venv/bin/activate`.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

echo "==> Python environment (.venv)"
if [ ! -x .venv/bin/python3 ]; then
    rm -rf .venv
    python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q
.venv/bin/python3 recon.py --init-db

echo
echo "==> Backend ready. Run it with: python3 recon.py"
echo "    (no need to activate .venv yourself — recon.py finds it automatically)"

if command -v npm >/dev/null 2>&1; then
    echo
    echo "==> Frontend (frontend/node_modules)"
    (cd frontend && npm install --no-fund --no-audit --silent)
    echo "==> Frontend ready. Start it with: cd frontend && npm run dev"
else
    echo
    echo "==> npm not found — skipping the frontend. Install Node.js if you want the web board."
fi

echo
echo "==> Setup complete. Try: python3 recon.py --company praetorian -v"
