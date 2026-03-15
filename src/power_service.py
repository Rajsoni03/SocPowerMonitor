import datetime as dt
import json
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

    # ---------- session control ----------
    def start_session(self, metadata: Optional[Dict] = None, sample_count: Optional[int] = None, delay_ms: Optional[int] = None) -> Dict:
        if self.capture_thread and self.capture_thread.is_alive():
            raise RuntimeError('Capture already running')
        if not self.selected_port:
            raise RuntimeError('UART port not selected')
        if not self.active_config:
            raise RuntimeError('Config not activated')

        cfg = self.active_config
        samples = sample_count or cfg.get('default_sample_count', 20)
        delay = delay_ms or cfg.get('default_delay_ms', 20)
        self.current_samples_per_command = samples
        self.current_delay_ms = delay
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
        self.capture_thread = threading.Thread(target=self._capture_loop, args=(samples, delay), daemon=True)
        self.capture_thread.start()
        return {'session_id': self.active_session_id, 'samples_per_command': samples, 'delay_ms': delay}

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

    # ---------- capture loop ----------
    def _capture_loop(self, samples: int, delay_ms: int):
        uart = Uart(self.selected_port, log_level=LOG_NONE)
        try:
            uart.connect()
            uart.consume_pending(PROMPT, timeout=1)
            dut_name = (
                self.active_config.get('dut_name')
                or self.active_config.get('dut')
                or self.active_config.get('soc_name')
                or self.active_config.get('config_id')
                or self.active_config.get('name')
            )
            uart.run_command(f"auto set dut {dut_name}", PROMPT, timeout=5)
            while not self.stop_event.is_set():
                timeout = max(5, int(samples * delay_ms / 1000) + 5)
                raw = uart.run_command(f"auto measure power {samples} {delay_ms}", PROMPT, timeout=timeout)
                readings = parse_measurement(raw)
                if readings:
                    self._persist_samples(readings)
                    self._push_stream(readings)
                elif raw.strip():
                    self._push_stream([], error=f'No measurements parsed from device output: {raw.strip()[:240]}')
                    break
                # throttle slightly; measurement command already delays by design
                time.sleep(0.01)
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
                (item.get('power_mw') or 0.0)
                for item in readings
                if item.get('rail') not in ignored_rails
            )

        return {
            'selected_port': self.selected_port,
            'active_config': self.active_config['name'] if self.active_config else None,
            'active_config_id': self.active_config.get('config_id') if self.active_config else None,
            'active_session_id': self.active_session_id,
            'is_monitoring': bool(self.capture_thread and self.capture_thread.is_alive()),
            'samples_per_command': self.current_samples_per_command,
            'delay_ms': self.current_delay_ms,
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
