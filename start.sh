#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Check if something is on port 8050
PID=$(lsof -ti tcp:8050 2>/dev/null || true)

if [ -n "$PID" ]; then
    echo "Port 8050 is in use by PID $PID."
    read -rp "Stop it and continue? [y/N] " ans
    case "$ans" in
        [yY]|[yY][eE][sS]) 
            kill -9 "$PID" 2>/dev/null && echo "Stopped PID $PID." || echo "Process $PID already exited."
            ;;
        *)    
            echo "Aborted."
            exit 1 
            ;;
    esac
fi


# Backup existing log file
if [ -f logs.txt ]; then
    BACKUP="logs_$(date '+%Y%m%d_%H%M%S').txt"
    mv logs.txt "$BACKUP"
    echo "Backed up logs.txt → $BACKUP"
fi

# Start Flask server in background
nohup venv/bin/python app.py > logs.txt 2>&1 &
echo "Flask server started (PID $!). Logs → logs.txt"
