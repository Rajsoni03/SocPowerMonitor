import datetime as dt
import json
import math
import queue
import threading
import time
from typing import Dict, List, Optional

from serial.tools import list_ports

from .models import db, Rail, Session, Sample
from .parser import parse_measurement
from .uart import LOG_NONE, Uart

PROMPT = '=>'


def list_uart_ports() -> List[Dict]:
    ports = []
    for p in list_ports.comports():
        ports.append({
            'device': p.device,
            'description': p.description,
            'hwid': p.hwid,
            'vid': p.vid,
            'pid': p.pid,
            'serial_number': p.serial_number,
        })
    return ports


class PowerService:
    def __init__(self, app, config_loader):
        self.app = app
        self.config_loader = config_loader
        self.selected_port: Optional[str] = None
        self.active_config: Optional[Dict] = None
        self.active_session_id: Optional[int] = None
        self.capture_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.stream_queue: queue.Queue = queue.Queue(maxsize=100)
        self.current_samples_per_command: Optional[int] = None
        self.current_delay_ms: Optional[int] = None
        self.current_command_interval: Optional[int] = None
        self.last_stream_payload: Optional[Dict] = None
        self.last_error: Optional[str] = None

    # ---------- configuration ----------
    def activate_config(self, config_name: str) -> Dict:
        cfg = self.config_loader.load_config(config_name)
        with self.app.app_context():
            self._sync_rails(cfg)
        self.active_config = cfg
        return cfg

    def _sync_rails(self, cfg: Dict):
        existing = {r.name: r for r in Rail.query.all()}
        for rail in cfg.get('rails', []):
            row = existing.get(rail['name'])
            if not row:
                row = Rail(name=rail['name'])
            row.enabled = bool(rail.get('enabled', True))
            db.session.add(row)
        db.session.commit()

    def _rail_map(self, cfg: Optional[Dict] = None) -> Dict[str, Dict]:
        config = cfg or self.active_config or {}
        rail_map = {}
        for rail in config.get('rails', []):
            name = rail.get('name')
            if name:
                rail_map[str(name).strip().lower()] = rail
        return rail_map

    @staticmethod
    def _is_number(value) -> bool:
        return isinstance(value, (int, float)) and math.isfinite(value)

    def _annotate_reading(self, reading: Dict, cfg: Optional[Dict] = None) -> Dict:
        annotated = dict(reading)
        rail_name = str(reading.get('rail') or '').strip()
        rail_cfg = self._rail_map(cfg).get(rail_name.lower())
        if not rail_cfg:
            return annotated

        input_voltage_v = reading.get('voltage_v')
        input_current_ma = reading.get('current_ma')
        raw_power_mw = reading.get('power_mw')
        out_v = rail_cfg.get('out_v')
        eff_ratio = rail_cfg.get('eff_ratio', 1)
        calculation_mode = rail_cfg.get('calculation_mode', 'direct')

        actual_current_ma = input_current_ma
        actual_power_mw = raw_power_mw
        display_voltage_v = input_voltage_v

        if (
            calculation_mode == 'custom'
            and self._is_number(input_voltage_v)
            and self._is_number(input_current_ma)
            and self._is_number(out_v)
            and self._is_number(eff_ratio)
            and out_v
        ):
            # for custom mode, we calculate the actual current and power based on input voltage/current, output voltage, and efficiency ratio
            actual_current_ma = ((input_current_ma * input_voltage_v) / out_v) * eff_ratio
            actual_power_mw = actual_current_ma * out_v
            display_voltage_v = out_v
        elif self._is_number(input_voltage_v) and self._is_number(input_current_ma) and not self._is_number(actual_power_mw):
            actual_power_mw = input_current_ma * input_voltage_v

        annotated.update({
            'rail': rail_cfg['name'],
            'group': rail_cfg.get('group'),
            'out_v': out_v,
            'eff_ratio': eff_ratio,
            'calculation_mode': calculation_mode,
            'ignore_for_soc_total': bool(rail_cfg.get('ignore_for_soc_total', False)),
            'display_voltage_v': display_voltage_v,
            'actual_current_ma': actual_current_ma,
            'actual_power_mw': actual_power_mw,
        })
        return annotated

    def annotate_readings(self, readings: List[Dict], cfg: Optional[Dict] = None) -> List[Dict]:
        return [self._annotate_reading(reading, cfg) for reading in readings]

    def serialize_sample_rows(self, rows: List[Sample]) -> List[Dict]:
        if not rows:
            return []
        session_name = rows[0].session.config_name if rows[0].session else None
        cfg = None
        if session_name:
            try:
                cfg = self.config_loader.load_config(session_name)
            except FileNotFoundError:
                cfg = self.active_config
        return self.annotate_readings([row.to_dict() for row in rows], cfg)

    # ---------- session control ----------
    def start_session(
        self,
        metadata: Optional[Dict] = None,
        sample_count: Optional[int] = None,
        delay_ms: Optional[int] = None,
        command_interval: Optional[int] = None,
    ) -> Dict:
        if self.capture_thread and self.capture_thread.is_alive():
            raise RuntimeError('Capture already running')
        if not self.selected_port:
            raise RuntimeError('UART port not selected')
        if not self.active_config:
            raise RuntimeError('Config not activated')

        cfg = self.active_config
        samples = sample_count or cfg.get('default_sample_count', 20)
        delay = delay_ms or cfg.get('default_delay_ms', 20)
        command_interval = command_interval if command_interval is not None else cfg.get('default_command_interval', 0)
        self.current_samples_per_command = samples
        self.current_delay_ms = delay
        self.current_command_interval = max(0, int(command_interval))
        self.last_error = None

        with self.app.app_context():
            session = Session(
                config_name=cfg['name'],
                config_hash=cfg['__hash__'],
                session_metadata=metadata or {},
            )
            db.session.add(session)
            db.session.commit()
            self.active_session_id = session.id

        self.stop_event.clear()
        self.capture_thread = threading.Thread(
            target=self._capture_loop,
            args=(samples, delay, self.current_command_interval),
            daemon=True,
        )
        self.capture_thread.start()
        return {
            'session_id': self.active_session_id,
            'samples_per_command': samples,
            'delay_ms': delay,
            'command_interval': self.current_command_interval,
        }

    def stop_session(self):
        if not self.capture_thread:
            return
        self.stop_event.set()
        self.capture_thread.join(timeout=5)
        with self.app.app_context():
            session = Session.query.get(self.active_session_id)
            if session and not session.ended_at:
                session.ended_at = dt.datetime.utcnow()
                db.session.commit()
        self.capture_thread = None
        self.active_session_id = None
        self.current_command_interval = None

    # ---------- capture loop ----------
    def _capture_loop(self, samples: int, delay_ms: int, command_interval: int):
        uart = Uart(self.selected_port, log_level=LOG_NONE)
        try:
            uart.connect()
            uart.consume_pending(PROMPT, timeout=1)
            dut_name = (
                self.active_config.get('name')
                or self.active_config.get('dut_name')
                or self.active_config.get('soc_name')
                or self.active_config.get('config_id')
            )
            uart.run_command(f"auto set dut {dut_name}", PROMPT, timeout=5)
            while not self.stop_event.is_set():
                timeout = max(5, int(samples * delay_ms / 1000) + 5)
                raw = uart.run_command(f"auto measure power {samples} {delay_ms}", PROMPT, timeout=timeout)
                readings = self.annotate_readings(parse_measurement(raw))
                if readings:
                    self._persist_samples(readings)
                    self._push_stream(readings)
                elif raw.strip():
                    self._push_stream([], error=f'No measurements parsed from device output: {raw.strip()[:240]}')
                    break
                if command_interval > 0 and not self.stop_event.is_set():
                    remaining = command_interval / 1000
                    while remaining > 0 and not self.stop_event.is_set():
                        sleep_for = min(0.1, remaining)
                        time.sleep(sleep_for)
                        remaining -= sleep_for
        except Exception as exc:
            # push error into stream for UI consumption
            self._push_stream([], error=str(exc))
        finally:
            uart.disconnect()
            with self.app.app_context():
                if self.active_session_id:
                    session = Session.query.get(self.active_session_id)
                    if session and not session.ended_at:
                        session.ended_at = dt.datetime.utcnow()
                        db.session.commit()
            self.capture_thread = None
            self.active_session_id = None
            self.current_command_interval = None

    def _persist_samples(self, readings: List[Dict]):
        ts = dt.datetime.utcnow()
        with self.app.app_context():
            rail_lookup = {r.name: r for r in Rail.query.all()}
            for r in readings:
                rail = rail_lookup.get(r['rail'])
                if not rail:
                    rail = Rail(name=r['rail'], enabled=True)
                    db.session.add(rail)
                    db.session.flush()
                    rail_lookup[rail.name] = rail
                sample = Sample(
                    session_id=self.active_session_id,
                    rail_id=rail.id,
                    ts=ts,
                    voltage_v=r.get('voltage_v'),
                    current_ma=r.get('current_ma'),
                    power_mw=r.get('power_mw'),
                    raw_payload=r.get('raw'),
                )
                db.session.add(sample)
            db.session.commit()

    def _push_stream(self, readings: List[Dict], error: Optional[str] = None):
        payload = {'ts': dt.datetime.utcnow().isoformat() + 'Z', 'readings': readings, 'error': error}
        self.last_stream_payload = payload
        self.last_error = error
        try:
            self.stream_queue.put_nowait(payload)
        except queue.Full:
            # drop oldest to make room
            try:
                _ = self.stream_queue.get_nowait()
                self.stream_queue.put_nowait(payload)
            except queue.Empty:
                pass

    # ---------- state setters ----------
    def set_port(self, port: str):
        self.selected_port = port

    def status(self) -> Dict:
        readings = []
        updated_at = None
        total_power_mw = 0.0
        ignored_rails = {
            rail['name']
            for rail in (self.active_config or {}).get('rails', [])
                if rail.get('ignore_for_soc_total')
        }
        if self.last_stream_payload:
            readings = self.last_stream_payload.get('readings', [])
            updated_at = self.last_stream_payload.get('ts')
            total_power_mw = sum(
                (item.get('actual_power_mw') or item.get('power_mw') or 0.0)
                for item in readings
                if not item.get('ignore_for_soc_total') and item.get('rail') not in ignored_rails
            )

        return {
            'selected_port': self.selected_port,
            'active_config': self.active_config['name'] if self.active_config else None,
            'active_config_id': self.active_config.get('config_id') if self.active_config else None,
            'active_session_id': self.active_session_id,
            'is_monitoring': bool(self.capture_thread and self.capture_thread.is_alive()),
            'samples_per_command': self.current_samples_per_command,
            'delay_ms': self.current_delay_ms,
            'command_interval': self.current_command_interval,
            'last_error': self.last_error,
            'last_update_ts': updated_at,
            'rail_count': len(readings),
            'total_power_mw': total_power_mw,
            'latest_readings': readings,
        }

    # ---------- streaming helpers ----------
    def stream_generator(self):
        while True:
            item = self.stream_queue.get()
            yield f"data: {json.dumps(item)}\n\n"
