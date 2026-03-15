# SocPowerMonitor Technical Project Plan

## Executive Summary

SocPowerMonitor is a lightweight Flask-based toolkit for monitoring, logging, and visualizing power metrics on TI Jacinto TDA4x-class SoCs, including devices such as J722S, via the XDS110 or MSP432 automation firmware and INA219 rail monitors accessed over UART/JTAG. It enables real-time visualization and structured logging of SoC rail power consumption under different workloads and system states while remaining suitable for resource-constrained embedded environments.[web:19][web:35]

The J722S EVM integrates an XDS110 debug probe running automation firmware, which exposes commands such as `auto set dut <DUT type>` and `auto measure power <samples> <delay>` for capturing power across multiple rails, with sampling count and inter-sample delay parameters.[web:19] SocPowerMonitor builds on this infrastructure by implementing a user-space monitoring and visualization layer that can select UART ports, configure SoC-specific rail sets, stream and log data, and export CSV for offline analysis.

## Project Goals and Scope

### Goals

- Provide a real-time, browser-based dashboard to visualize power consumption across selected rails for TI Jacinto TDA4x-class SoCs.
- Enable configurable data acquisition using the XDS110/MSP432 automation firmware over UART, including selection of UART ports and sampling parameters.
- Offer robust structured logging (CSV) with on-demand download for offline analysis and reporting.
- Support SoC-specific configurations (e.g., J722S, TDA4VM) defining monitored rails, scaling factors, and default sampling rates.
- Keep the runtime footprint small enough for deployment on embedded Linux hosts attached to evaluation modules or in-vehicle test setups.

### Out of Scope (Initial Version)

- Deep integration with TI Code Composer Studio (CCS) GUIs beyond using automation firmware interfaces.
- Automated workload orchestration on the DUT; workloads are assumed to be triggered externally.
- Long-term database storage (e.g., SQL/TSDB); initial scope is log-file based.
- Complex user management; initial releases may rely on lab-network isolation or simple authentication.

## System Context and Requirements

### Hardware and Platform Context

- TI Jacinto TDA4x / J7x SoCs on their respective evaluation modules (e.g., J722S EVM) with integrated XDS110 debug probe and INA219-based power monitoring circuitry on key SoC rails.[web:19][web:37]
- XDS110 or MSP432 automation firmware supports power measurement and communication over UART/JTAG, exposed as a USB serial device on the host.[web:19][web:39]
- Host machine (embedded Linux or desktop Linux/Windows) running the SocPowerMonitor Flask application and connecting to the USB serial device.

### Functional Requirements

- Discover and list available UART interfaces suitable for connection to XDS110/MSP432 automation firmware.
- Allow users to select the active UART port at runtime via the web UI; on selection, reinitialize the acquisition backend.
- Issue appropriate commands to the automation firmware to start power measurements, including parameters such as sample count and delay (sampling interval) when supported.[web:19]
- Continuously read and parse power measurement data for multiple rails, mapping raw readings to SoC rail names via configuration.
- Maintain an in-memory sliding window of recent samples for responsive real-time visualization.
- Persist incoming samples as structured log records (CSV) with timestamps and identifiers.
- Provide a web UI to:
  - View multi-rail time-series charts (power, voltage, current).
  - Select SoC configuration and rail subsets to display.
  - Adjust sampling rate and, where applicable, measurement duration.
  - Download CSV logs for a chosen time interval or session.

### Non-Functional Requirements

- **Latency:** Real-time dashboard updates with end-to-end latency under approximately one second for standard sampling rates (for example, tens of Hz).
- **Throughput:** Support continuous acquisition for extended durations (hours) without memory leaks, relying on log rotation and bounded in-memory buffers.
- **Resource Usage:** Low CPU and memory footprint to allow deployment on small embedded Linux systems co-located with the DUT.
- **Reliability:** Graceful handling of UART disconnections, timeouts, and malformed frames; automatic reconnection where feasible.
- **Portability:** Primary implementation in Python 3 with standard libraries and widely available packages (Flask, pyserial, a lightweight JS charting library) to ease cross-platform deployment.

## High-Level Architecture

### Component Overview

SocPowerMonitor consists of the following major components:

- **UART Acquisition Service:** Opens and manages the serial connection to the XDS110/MSP432 device, sends measurement commands (if required), and continuously reads raw measurement frames.
- **Parser and Normalization Layer:** Converts raw text/binary from automation firmware into normalized sample objects with timestamps and physical units for each rail.
- **Configuration Manager:** Loads SoC-specific configuration files describing rails, scaling factors, default sampling, and UI grouping.
- **Data Store and Logger:** Maintains a ring buffer of recent samples for visualization and appends all samples to CSV logs with rotation policies.
- **Flask API Server:** Exposes REST/JSON and streaming endpoints for configuration, current metrics, and CSV download.
- **Web Frontend:** Browser-based UI for configuration, real-time charts, and data export, implemented using standard HTML/CSS/JS and a charting library.

### Data Flow

1. On startup, the Configuration Manager loads default SoC and application settings from configuration files.
2. The UART Acquisition Service enumerates serial ports, optionally auto-selects a default, and waits for user selection.
3. Once a UART port is selected, the Acquisition Service opens the device, optionally sends initialization commands (for example, `auto set dut <soc_name>` for J722S), and begins measurement (`auto measure power <samples> <delay>` when using the TI automation protocol).[web:19]
4. Raw frames received from the device are parsed into structured sample objects per rail and passed to the Data Store and Logger.
5. The Data Store updates the in-memory ring buffer for real-time charts and writes entries to CSV on disk.
6. The Flask API exposes endpoints that the Web Frontend polls or subscribes to via WebSockets/Server-Sent Events to update charts, show current status, and allow CSV download.

## Detailed Backend Design

### UART Acquisition Service

- Implemented as a dedicated Python module using `pyserial`.
- Responsibilities:
  - Discover available serial ports using `pyserial.tools.list_ports`.
  - Maintain current port selection and connection state.
  - Handle (re)opening the serial device with configured parameters (baud rate, parity, stop bits, timeout).
  - Optionally send automation commands (for example, `auto set dut j722s`) based on the active SoC configuration, aligning with the TI EVM power measurement guide.[web:19]
  - Start and stop measurement sessions via commands such as `auto measure power <samples> <delay>` when applicable, where `<samples>` is the number of samples to average (up to the tool’s maximum, such as 150) and `<delay>` is the inter-sample delay in milliseconds.[web:19]
  - Read incoming data in a non-blocking manner using a worker thread or asynchronous loop.
  - Detect and report errors (timeouts, framing errors, unexpected content) to the application.

- Connection lifecycle:
  - On port selection, close any existing connection, then open the new port.
  - On read/write errors, attempt a bounded number of automatic reconnections.
  - Expose a status object (e.g., `CONNECTED`, `DISCONNECTED`, `ERROR`) for UI display.

### Parser and Normalization

- Abstract interface `PowerDataParser` that converts raw UART lines/frames into structured samples.
- Support for at least one initial protocol format:
  - Text-based lines with comma-separated or table-like fields: index, rail identifiers, voltage, current, power.
  - Alternatively, binary frames if the automation firmware uses binary encoding; parsing logic will be encapsulated.
- For each frame:
  - Validate integrity (checksum/field count).
  - Map firmware rail identifiers to logical rail names via SoC configuration.
  - Convert raw counts to engineering units (volts, amps, watts) using INA219 scaling settings from configuration.[web:35][web:41]
  - Attach a host-side timestamp for alignment with DUT events.

### Configuration Manager

- Configuration organized as a directory of JSON/YAML files, for example:
  - `configs/tda4vm.json`
  - `configs/j722s.json`
- SoC-level configuration fields:
  - `soc_name` (e.g., `j722s`).
  - `dut_command` (string for `auto set dut <name>` for use with automation firmware).[web:19]
  - `default_sampling_delay_ms` (maps to `<delay>` in `auto measure power`).[web:19]
  - `default_sample_count` (maps to `<samples>` kept within automation limits, such as 150).[web:19]
  - List of `rails`, each with:
    - `id` (firmware identifier or channel index).
    - `name` (e.g., `vdd_core`, `vdd_ram_0v85`, `vdd_ddr_1v1`).
    - `group` (`SOC`, `SOC_RAM`, `DDR`, `VIN`, etc.).
    - `enabled_by_default` (boolean).
    - INA219 scaling details (shunt resistance, calibration constants).
    - Visualization hints (color, y-axis grouping).
    - `ignore_for_soc_total` (e.g., `vsys_3v3`, `vdd1_ddr_1v8` for J722S SoC-only power as per TI docs).[web:19]

- Application-level configuration:
  - Logging directory and rotation policy.
  - Maximum in-memory buffer duration (e.g., last 10–30 minutes).
  - Default SoC configuration.
  - Authentication and binding address/port.

- Provide a configuration API:
  - `GET /api/config/socs` to list available SoCs and metadata.
  - `GET /api/config/soc/<name>` to fetch configuration.
  - `POST /api/config/soc` to switch active SoC at runtime.

### Data Store and Logging

- In-memory store:
  - Implement a ring buffer storing the most recent N samples per rail (or for a fixed time window).
  - Data model per sample:
    - Timestamp (host time).
    - SoC identifier.
    - Rail name.
    - Voltage, current, power.
    - Sample index/frame number.
  - Provide thread-safe readers for API layers.

- CSV logging:
  - Append all parsed samples to CSV files.
  - File naming strategy: `socpower_<soc>_<YYYYMMDD>_<session_id>.csv`.
  - Schema example:
    - `timestamp_iso` (e.g., RFC3339 string).
    - `soc_name`.
    - `rail_name`.
    - `group`.
    - `voltage_v`.
    - `current_ma`.
    - `power_mw`.
    - `frame_index`.
  - Implement log rotation strategies:
    - Rotate by size (e.g., 50–100 MB) or by time (e.g., daily or per session).

- CSV download feature:
  - API endpoint `GET /api/download` with optional `from`, `to`, `soc`, and `rails` query parameters.
  - Support two modes:
    - Entire log file download for a given session.
    - Filtered export (time range and subset of rails) by streaming filtered CSV rows.

## Flask API and Web Interface Design

### API Endpoints

Representative set of endpoints:

- **UART and Acquisition Control**
  - `GET /api/serial/ports` – List available UART ports with metadata (name, description).
  - `POST /api/serial/select` – Body: `{ "port": "/dev/ttyUSB0" }`; switches acquisition to the selected port.
  - `GET /api/serial/status` – Returns connection and acquisition status information.
  - `POST /api/acquisition/start` – Optional body: sampling parameters (`samples`, `delay_ms`); triggers measurement session.
  - `POST /api/acquisition/stop` – Stops measurement.

- **Configuration and Metadata**
  - `GET /api/config/socs` – List SoC configurations.
  - `GET /api/config/active` – Return active SoC and rail configuration.
  - `POST /api/config/active` – Switch active SoC.

- **Real-time Data**
  - `GET /api/data/latest` – Return a snapshot of the latest samples for all enabled rails.
  - `GET /api/data/stream` – Streaming endpoint (WebSocket or Server-Sent Events) delivering incremental samples.

- **Logs and Export**
  - `GET /api/download` – CSV download as described in the logging section.

### Web Frontend

- UI implemented using Flask templates or a lightweight front-end framework.
- Key screens/components:
  - **Dashboard:**
    - Multi-rail line charts (power vs. time, optionally voltage/current toggles).
    - Rail selection checkboxes and SoC selector.
    - Legend showing rail colors and groups.
  - **Acquisition Control Panel:**
    - UART port dropdown populated from `/api/serial/ports`.
    - Start/stop buttons with sampling parameter fields (`samples`, `delay_ms`).
    - Connection and acquisition status indicators.
  - **Export & Logs:**
    - Date/time range pickers.
    - Rail filters.
    - "Download CSV" button that constructs the `/api/download` URL.

- Real-time update mechanism:
  - Prefer WebSockets or Server-Sent Events for pushing new samples from `/api/data/stream` to charts.
  - Fallback to short-interval polling of `/api/data/latest` in constrained environments.

## UART Interface Selection Feature

### Backend Implementation

- Use `pyserial.tools.list_ports.comports()` to enumerate all serial ports at runtime.
- Maintain a current selection stored in a configuration file or simple database.
- When a new selection is made:
  - Gracefully stop the current acquisition loop.
  - Close the existing serial port.
  - Open the newly selected port with the configured parameters.
  - Re-run `auto set dut <soc_name>` to ensure the automation firmware is configured for the current SoC.[web:19]

### Frontend UX

- Present a dropdown containing:
  - Port identifier (e.g., `/dev/ttyUSB0`, `COM3`).
  - Human-readable description (e.g., `XDS110 Class Application/User UART`).[web:39]
- Show a status banner indicating the currently selected port and connection state.
- On selection change:
  - Prompt for confirmation if acquisition is in progress.
  - Display progress and any connection errors.

## SoC Configuration and Rail Mapping

### SoC Profiles

- Each supported SoC (e.g., J722S, TDA4VM) has a profile file containing:
  - Human-readable name and description.
  - DUT configuration command string for automation firmware (for example, `auto set dut j722s`).[web:19]
  - Rail list with identifiers and scaling factors as described earlier.
  - Optional metadata linking to reference documentation or lab calibration notes.

### Rail Selection and Grouping

- Allow users to:
  - Enable/disable specific rails for visualization.
  - Filter by rail group (e.g., "Core", "DDR", "Peripheral", "VIN").
- Honor ignore lists for rails that are not relevant to pure SoC power analysis, such as `vsys_3v3` and `vdd1_ddr_1v8` when focusing on SoC-only power, reflecting TI’s guidance.[web:19]

## Performance, Reliability, and Resource Considerations

- Use buffered reads and efficient parsing to keep CPU overhead low, especially at higher sampling rates.
- Limit in-memory history to a fixed duration to bound memory usage.
- Support adjustable sampling parameters within the capabilities of the automation firmware (e.g., respecting maximum sample count such as 150 in `auto measure power`).[web:19]
- Implement down-sampling for visualization when acquisition rate is high, while logging all samples to CSV.
- Log acquisition and parsing errors with enough detail to diagnose UART or firmware issues.

## Security and Access Control

- Default deployment assumes a trusted lab or development network.
- Provide configurable options for:
  - Binding the Flask app only to localhost or a specific interface.
  - Enabling simple authentication (basic auth or token) for remote access.
- Sanitize and validate all input parameters to API endpoints (e.g., restrict UART port names to discovered devices) to avoid misuse.

## Testing and Validation Plan

### Unit and Integration Testing

- Unit tests for:
  - Parsing logic for all supported firmware message formats.
  - Configuration loader and validator.
  - CSV logging and rotation.
- Integration tests using:
  - Mock serial ports that replay recorded automation firmware output.
  - End-to-end tests that start the Flask app and verify API behavior.

### Hardware-in-the-Loop (HIL) Testing

- Use a J722S EVM or similar Jacinto platform with XDS110 and automation firmware as described in TI documentation.[web:19][web:37]
- Validate that:
  - `auto set dut` and `auto measure power` commands succeed for supported SoCs and sampling parameters.[web:19]
  - Reported rail values match expectations from TI reference measurements and/or the Excel conversion tools described in the TI guide.
  - Ignored rails (for example, `vsys_3v3` and `vdd1_ddr_1v8`) are not included in SoC-only power totals, aligning with TI’s SoC power rails definition.[web:19]

### Performance and Stability Testing

- Run long-duration acquisitions (multiple hours) at representative sampling rates to ensure:
  - No unbounded memory growth.
  - Log rotation functions correctly.
  - UART reconnections occur smoothly after cable disconnection/reconnection.

## Documentation and Usability

- Provide a README and user guide covering:
  - Supported platforms and dependencies.
  - Installation and configuration steps.
  - UART selection and troubleshooting.
  - Explanation of SoC rail naming and mapping to physical rails.
  - Example workflows for capturing data during different DUT workloads.
- Where appropriate, cross-reference TI’s J722S EVM power measurement guide for users who want to validate low-level measurement setup and signal paths.[web:19][web:37]

## Project Phases and Milestones

### Phase 0 – Requirements and Design (1 week)

- Finalize detailed requirements and refine the architecture.
- Confirm UART protocol specifics with the automation firmware and J7 EVM documentation.[web:19][web:39]
- Define SoC profile structure and initial SoC targets (e.g., J722S and one additional TDA4x SoC).

### Phase 1 – Core Acquisition and Parsing (2 weeks)

- Implement UART Acquisition Service with port enumeration and selection.
- Implement initial parser for the automation firmware output format.
- Validate end-to-end acquisition against a J7 EVM, using reference commands like `auto measure power` for controlled scenarios.[web:19]

### Phase 2 – Logging, CSV Export, and Config (2 weeks)

- Implement in-memory data store and CSV logger with rotation.
- Implement SoC configuration loading and runtime switching.
- Add CSV export endpoint and verify interoperability with external analysis tools (e.g., spreadsheets, plotting packages).

### Phase 3 – Flask API and Web UI (2 weeks)

- Implement REST/streaming endpoints for real-time data and configuration.
- Build initial dashboard UI with charts, UART selection, and acquisition controls.
- Implement CSV download flows and simple status/error reporting.

### Phase 4 – Optimization, Testing, and Documentation (1–2 weeks)

- Profile performance and optimize parsing and streaming paths.
- Implement unit, integration, and HIL tests as described.
- Complete user and developer documentation and prepare example usage scenarios.

## Future Enhancements

- Add persistent database integration (e.g., InfluxDB, SQLite) for long-term trend analysis.
- Support additional measurement hardware (e.g., external power analyzers) via plugin interfaces.
- Provide more advanced analytics such as energy per workload or automatic detection of power state transitions.
- Integrate with CI systems to automatically capture power data for regression test suites.
