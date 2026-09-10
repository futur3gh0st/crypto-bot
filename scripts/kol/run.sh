#!/bin/sh
# Start (or restart) the KOL copy-trade forward test in the background.
# Safe to re-run: it stops any running copy first. Logs live in the repo,
# not in a Claude session folder, so the test outlives the session.
cd "$(dirname "$0")/../.." || exit 1
mkdir -p data/bt_cache/logs
pkill -f "scripts/kol/watch.py" 2>/dev/null
pkill -f "scripts/kol/track_prices.py" 2>/dev/null
sleep 1
nohup .venv/bin/python scripts/kol/watch.py 50 >> data/bt_cache/logs/kol_watch.log 2>&1 &
nohup .venv/bin/python scripts/kol/track_prices.py >> data/bt_cache/logs/kol_prices.log 2>&1 &
sleep 2
echo "running:"
pgrep -fl "scripts/kol/(watch|track_prices).py"
echo "logs:    data/bt_cache/logs/"
echo "results: .venv/bin/python scripts/kol/evaluate.py"
echo "stop:    pkill -f scripts/kol/"
