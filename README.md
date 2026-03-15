# SocPowerMonitor

SocPowerMonitor is a lightweight toolkit for monitoring, logging, and visualizing power metrics on TI Jacinto TDA4x SoCs. It enables developers to analyze power consumption across different workloads and system states using real-time visualization and structured logging.

## Quick start (dev)

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m src.app
```

The service listens on port 8000 by default.

- Dashboard: `http://localhost:8000/`
- API index: `GET /api`

Available API endpoints:

- `GET /api/ports` — list UART devices (XDS110/MSP432)
- `POST /api/ports/select` — select UART port `{\"port\":\"/dev/ttyACM0\"}`
- `GET /api/configs` / `POST /api/configs/activate` — manage SoC profiles (default `j722s`)
- `GET /api/status` — current monitor status, active config, and latest live readings
- `POST /api/sessions` — start/stop capture (`{\"action\":\"start\"}` or `{\"action\":\"stop\"}`)
- `GET /api/samples` — fetch captured samples
- `GET /api/export.csv` — stream CSV export
- `GET /api/stream` — server-sent events for live charts

Data persists to `data/power.db` (SQLite). Config profiles live under `config/`.
