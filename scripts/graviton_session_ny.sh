#!/bin/bash
# Modus (crypto | stocks) aus MODE-Datei — EIN Cron fuer beide Modi.
GV_MODE="$(tr -d '[:space:]' < /root/.hermes/workspace/graviton/MODE 2>/dev/null || true)"
export GRAVITON_MODE="${GV_MODE:-crypto}"
GV_DATA="/root/.hermes/workspace/graviton/data"
[ "$GRAVITON_MODE" = "stocks" ] && GV_DATA="$GV_DATA/stocks"
mkdir -p "$GV_DATA"

# Graviton Session Cron — NY (14:02 UTC) / Asia (00:02 UTC)
# Läuft session.py im Hintergrund — Cron-Timeout killt den Prozess nicht.
# session.py sendet Live-Updates via Telegram (tg()).
# Logs: /root/.hermes/workspace/graviton/logs/session_ny.log
cd /root/.hermes/workspace/graviton || exit 1
LOG="logs/session_${1:-ny}.log"
mkdir -p logs
echo "[$(date -u '+%H:%M UTC')] Session $1 gestartet (PID $$)" >> "$LOG"
nohup .venv/bin/python3 session.py "${1:-ny}" >> "$LOG" 2>&1 &
disown
echo "PID $! — läuft im Hintergrund. Log: $LOG"