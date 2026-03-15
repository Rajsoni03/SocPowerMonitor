# SocPowerMonitor

SocPowerMonitor is a Flask-based power monitoring dashboard for TI Jacinto platforms. It connects to an XDS110 or MSP432 automation interface over UART, captures rail-level power data, stores samples in SQLite, and visualizes live and historical trends in a browser.

The project is aimed at board bring-up, lab validation, workload comparison, and quick power analysis without depending on heavyweight desktop tooling.

## Features

- Live rail-wise power monitoring from UART automation firmware
- Browser dashboard with:
  - combined SoC power trend
  - per-rail mini charts
  - recent session history
  - start/stop monitoring controls
  - UART port selection
- JSON-based SoC configuration under `config/`
- Session logging to SQLite
- CSV export for recorded sessions
- Support for viewing previous sessions from the UI

## How It Works

1. The app connects to a UART device exposed by XDS110/MSP432 automation firmware.
2. It sends commands such as:
   - `auto set dut <dut_name>`
   - `auto measure power <samples> <delay_ms>`
3. The returned rail table is parsed into structured samples.
4. Samples are stored in `data/power.db`.
5. The frontend consumes live events and renders charts for total power and individual rails.

## Quick Start

### Requirements

- Python 3.10+
- A TI board/debug probe exposing the automation UART interface
- A serial device available on your host machine

### Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m src.app
```

The server runs on `http://localhost:8000/`.

## Dashboard Workflow

1. Open `http://localhost:8000/`
2. Select the UART port
3. Select the SoC config
4. Click `Activate`
5. Choose sample count and delay
6. Click `Start monitoring`
7. View live charts or export the session as CSV

## Configuration

Configs live under `config/` and are JSON files.

Current default config:
- `config/j722s.json`

Example structure:

```json
{
  "name": "j722s-evm",
  "dut_name": "j722s-evm",
  "soc_name": "j722s",
  "default_delay_ms": 20,
  "default_sample_count": 20,
  "rails": [
    {
      "name": "vdd_core",
      "enabled": true,
      "ignore_for_soc_total": false
    }
  ]
}
```

### Config Fields

- `name`: display name for the profile
- `dut_name`: value used for `auto set dut ...`
- `soc_name`: logical SoC identifier
- `default_delay_ms`: default delay for measurement command
- `default_sample_count`: default averaging sample count
- `rails`: list of rails expected for the SoC
- `rails[].aliases`: alternate names that may appear in device output
- `rails[].ignore_for_soc_total`: excludes a rail from combined SoC total calculations

## Data Storage

- SQLite database: `data/power.db`
- Sessions are stored in the `session` table
- Rail samples are stored in the `sample` table
- Rails are stored in the `rail` table

## CSV Export

Each recorded session can be exported from the UI or via:

```bash
curl "http://localhost:8000/api/export.csv?session_id=1"
```

CSV columns:

```text
ts,rail,voltage_v,current_ma,power_mw,raw
```

## API Overview

### Health and UI

- `GET /healthz`
- `GET /`
- `GET /api`

### UART and Config

- `GET /api/ports`
- `POST /api/ports/select`
- `GET /api/configs`
- `POST /api/configs/activate`

### Monitoring and Data

- `GET /api/status`
- `POST /api/sessions`
- `GET /api/sessions`
- `GET /api/samples`
- `GET /api/export.csv`
- `GET /api/stream`

## Project Layout

```text
SocPowerMonitor/
├── config/
│   └── j722s.json
├── data/
├── src/
│   ├── app.py
│   ├── config_loader.py
│   ├── models.py
│   ├── parser.py
│   ├── power_service.py
│   ├── uart.py
│   ├── static/
│   │   ├── dashboard.css
│   │   └── dashboard.js
│   └── templates/
│       └── index.html
├── PLAN.md
└── README.md
```

## Development Notes

- Configs are loaded from JSON only
- The frontend does not auto-resync config/status continuously; use the dashboard `Sync` button when needed
- Historical sessions can be viewed from the dashboard without affecting live monitoring
- Unknown rails returned by the device are persisted so session history is not lost

## Troubleshooting

### No samples are being captured

Check:
- the UART port is correct
- the selected config matches the board
- `dut_name` matches the value expected by the automation firmware

### Exported CSV is empty

This was previously caused by streaming outside Flask application context. The current route uses a context-safe stream, so if export still fails, verify the target session actually contains samples.

### Rail names do not match config

Add aliases in the config file:

```json
{
  "name": "vdd_gpu",
  "aliases": ["vda_phy_1v8"]
}
```

### Config changes do not appear in the UI

Click `Sync` in the dashboard. If the backend has already activated an older config instance, reactivate the config or restart the app.

## Known Limitations

- No migration framework yet for schema changes in `data/power.db`
- Only JSON config files are supported
- The current UI is optimized for local lab use rather than multi-user deployment

## Reference

TI J722S power measurement interface:

- https://software-dl.ti.com/jacinto7/esd/processor-sdk-rtos-j722s/latest/exports/docs/vision_apps/docs/user_guide/group_ecu_power_measurement.html
