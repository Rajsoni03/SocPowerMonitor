import datetime as dt
import getpass
import json
import logging
import math
import queue
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from serial.tools import list_ports

from .models import (
    CONFIG_SNAPSHOT_KEY,
    SYSTEM_METADATA_KEY,
    db,
    Rail,
    Session,
    Sample,
    _utcnow,
)
from .parser import parse_measurement
from .uart import LOG_NONE, Uart

log = logging.getLogger(__name__)

PROMPT = '=>'


def list_uart_ports() -> List[Dict]:
    ports = []
    for p in sorted(list_ports.comports(), key=lambda p: p.device):
        name = p.device.split('/')[-1]
        if not (name.startswith('ttyUSB') or name.startswith('ttyACM')):
            continue
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

        # T2: single lock protecting all mutable shared state
        self._state_lock = threading.Lock()
        self.active_session_id: Optional[int] = None
        self.capture_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.stream_queue: queue.Queue = queue.Queue(maxsize=100)
        self.current_samples_per_command: Optional[int] = None
        self.current_delay_ms: Optional[int] = None
        self.current_command_interval: Optional[float] = None
        self.last_stream_payload: Optional[Dict] = None
        self.last_error: Optional[str] = None

        # BK1: rail name → rail id cache; warmed once per session start
        self._rail_id_cache: Dict[str, int] = {}

        # DB capture: when False, samples are streamed but not written to DB
        self.persist_to_db: bool = False

        # Log dump: raw UART output written to file when active
        self.log_dump_path: Optional[str] = None
        self._log_dump_file = None
        self._log_dump_lock = threading.Lock()
        self._log_dump_max_samples: Optional[int] = None
        self._log_dump_sample_count: int = 0

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
            keys = [rail.get('name'), *(rail.get('aliases') or [])]
            for key in keys:
                if key:
                    rail_map[str(key).strip().lower()] = rail
        return rail_map

    @staticmethod
    def _clean_config_snapshot(cfg: Dict) -> Dict:
        snapshot = {key: value for key, value in cfg.items() if not str(key).startswith('__')}
        return json.loads(json.dumps(snapshot))

    def _build_session_metadata(self, metadata: Optional[Dict], cfg: Dict) -> Dict:
        session_metadata = dict(metadata or {})
        raw_system_metadata = session_metadata.get(SYSTEM_METADATA_KEY)
        system_metadata = dict(raw_system_metadata) if isinstance(raw_system_metadata, dict) else {}
        system_metadata[CONFIG_SNAPSHOT_KEY] = self._clean_config_snapshot(cfg)
        session_metadata[SYSTEM_METADATA_KEY] = system_metadata
        return session_metadata

    def _config_for_session(self, session: Optional[Session]) -> Optional[Dict]:
        if not session:
            return None

        metadata = session.session_metadata if isinstance(session.session_metadata, dict) else {}
        system_metadata = metadata.get(SYSTEM_METADATA_KEY)
        if isinstance(system_metadata, dict):
            config_snapshot = system_metadata.get(CONFIG_SNAPSHOT_KEY)
            if isinstance(config_snapshot, dict):
                return config_snapshot

        try:
            return self.config_loader.load_config(session.config_name)
        except FileNotFoundError:
            return None

    def _mark_session_ended(self, session_id: Optional[int]):
        if not session_id:
            return
        with self.app.app_context():
            session = Session.query.get(session_id)
            if session and not session.ended_at:
                session.ended_at = _utcnow()
                db.session.commit()

    # T2/T3: all shared-state mutations go through the lock
    def _clear_capture_state(self, thread: threading.Thread, session_id: Optional[int]):
        with self._state_lock:
            if self.capture_thread is thread:
                self.capture_thread = None
            if self.active_session_id == session_id:
                self.active_session_id = None
                self.current_command_interval = None

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
        cfg = self._config_for_session(rows[0].session)
        return self.annotate_readings([row.to_dict() for row in rows], cfg)

    def set_persist_to_db(self, enabled: bool):
        with self._state_lock:
            if self.capture_thread and self.capture_thread.is_alive():
                raise RuntimeError('Cannot change DB capture while monitoring is active')
            self.persist_to_db = enabled

    # ---------- log dump ----------
    def _resolve_dump_path(self, file_path: str) -> str:
        """Return file_path, or a counter-suffixed variant if the file already exists."""
        p = Path(file_path)
        if not p.exists():
            return str(p)
        stem = p.stem
        suffix = p.suffix or '.txt'
        parent = p.parent
        counter = 1
        while True:
            candidate = parent / f'{stem}_{counter}{suffix}'
            if not candidate.exists():
                return str(candidate)
            counter += 1

    def start_log_dump(self, file_path: str, max_samples: Optional[int] = None) -> str:
        """Start writing raw UART output to *file_path*. Returns the actual path used."""
        with self._log_dump_lock:
            if self._log_dump_file is not None:
                raise RuntimeError('Log dump already active')
            resolved = self._resolve_dump_path(file_path)
            Path(resolved).parent.mkdir(parents=True, exist_ok=True)
            self._log_dump_file = open(resolved, 'w', encoding='utf-8')  # noqa: WPS515
            self.log_dump_path = resolved
            self._log_dump_max_samples = max_samples
            self._log_dump_sample_count = 0
            return resolved

    def stop_log_dump(self):
        """Stop writing raw UART output to file."""
        with self._log_dump_lock:
            if self._log_dump_file is not None:
                try:
                    self._log_dump_file.close()
                except Exception:
                    pass
                self._log_dump_file = None
            self.log_dump_path = None
            self._log_dump_max_samples = None
            self._log_dump_sample_count = 0

    # ---------- session control ----------
    def start_session(
        self,
        metadata: Optional[Dict] = None,
        sample_count: Optional[int] = None,
        delay_ms: Optional[int] = None,
        command_interval: Optional[int] = None,
    ) -> Dict:
        with self._state_lock:
            running = self.capture_thread and self.capture_thread.is_alive()
        if running:
            raise RuntimeError('Capture already running')
        if not self.selected_port:
            raise RuntimeError('UART port not selected')
        if not self.active_config:
            raise RuntimeError('Config not activated')

        cfg = self.active_config
        port = self.selected_port

        samples = int(sample_count) if sample_count is not None else int(cfg.get('default_sample_count', 20))
        delay = int(delay_ms) if delay_ms is not None else int(cfg.get('default_delay_ms', 20))
        command_interval = (
            float(command_interval)
            if command_interval is not None
            else float(cfg.get('default_command_interval', 0))
        )
        if samples < 1:
            raise RuntimeError('samples_per_command must be >= 1')
        if delay < 1:
            raise RuntimeError('delay_ms must be >= 1')
        if command_interval < 0:
            raise RuntimeError('command_interval must be >= 0')

        self.current_samples_per_command = samples
        self.current_delay_ms = delay
        self.current_command_interval = command_interval
        self.last_error = None
        persist_to_db = self.persist_to_db

        # BK1: clear rail cache so it's re-warmed for the new session
        self._rail_id_cache.clear()

        session_id = None
        if persist_to_db:
            with self.app.app_context():
                session = Session(
                    config_name=cfg['name'],
                    config_hash=cfg['__hash__'],
                    session_metadata=self._build_session_metadata(metadata, cfg),
                )
                db.session.add(session)
                db.session.commit()
                session_id = session.id

        with self._state_lock:
            self.active_session_id = session_id

        self.stop_event.clear()
        thread = threading.Thread(
            target=self._capture_loop,
            args=(session_id, port, self._clean_config_snapshot(cfg), samples, delay, self.current_command_interval, persist_to_db),
            daemon=True,
        )
        with self._state_lock:
            self.capture_thread = thread
        thread.start()
        return {
            'session_id': session_id,
            'samples_per_command': samples,
            'delay_ms': delay,
            'command_interval': self.current_command_interval,
            'persist_to_db': persist_to_db,
        }

    def stop_session(self):
        with self._state_lock:
            thread = self.capture_thread
            session_id = self.active_session_id
        if not thread:
            return
        self.stop_event.set()
        thread.join(timeout=10)
        # T1: whether or not the thread stopped in time, always clean up state.
        # If still alive, the thread will finish on its own (stop_event is set)
        # and its finally block will call _clear_capture_state again (idempotent).
        if thread.is_alive():
            log.warning(
                'Capture thread did not stop within 10s for session %s; '
                'forcing state clear. Thread will finish on its own.',
                session_id,
            )
        self._mark_session_ended(session_id)
        self._clear_capture_state(thread, session_id)

    # ---------- capture loop ----------
    def _capture_loop(
        self,
        session_id: Optional[int],
        port: str,
        cfg: Dict,
        samples: int,
        delay_ms: int,
        command_interval: float,
        persist_to_db: bool = False,
    ):
        thread = threading.current_thread()
        uart = Uart(port, log_level=LOG_NONE)
        try:
            uart.connect()
            uart.consume_pending(PROMPT, timeout=1)
            dut_name = (
                cfg.get('dut_name')
                or cfg.get('name')
                or cfg.get('soc_name')
                or cfg.get('config_id')
            )
            uart.run_command(f"auto set dut {dut_name}", PROMPT, timeout=5)
            while not self.stop_event.is_set():
                timeout = max(5, int(samples * delay_ms / 1000) + 5)
                raw = uart.run_command(f"auto measure power {samples} {delay_ms}", PROMPT, timeout=timeout)
                if raw.strip():
                    with self._log_dump_lock:
                        if self._log_dump_file is not None:
                            try:
                                ts_str = dt.datetime.utcnow().isoformat() + 'Z'
                                self._log_dump_file.write(f'# {ts_str}\n{raw}\n---\n')
                                self._log_dump_file.flush()
                                self._log_dump_sample_count += 1
                                if (
                                    self._log_dump_max_samples is not None
                                    and self._log_dump_sample_count >= self._log_dump_max_samples
                                ):
                                    try:
                                        self._log_dump_file.close()
                                    except Exception:
                                        pass
                                    self._log_dump_file = None
                                    self.log_dump_path = None
                                    log.info('Log dump auto-stopped after %d samples', self._log_dump_sample_count)
                            except Exception as exc:
                                log.warning('Failed to write to log dump file: %s', exc)
                readings = self.annotate_readings(parse_measurement(raw), cfg)
                if readings:
                    if persist_to_db and session_id is not None:
                        self._persist_samples(session_id, readings)
                    self._push_stream(readings)
                elif raw.strip():
                    self._push_stream([], error=f'No measurements parsed from device output: {raw.strip()[:240]}')
                    break
                if command_interval > 0 and not self.stop_event.is_set():
                    remaining = command_interval
                    while remaining > 0 and not self.stop_event.is_set():
                        sleep_for = min(0.1, remaining)
                        time.sleep(sleep_for)
                        remaining -= sleep_for
        except Exception as exc:
            self._push_stream([], error=str(exc))
        finally:
            uart.disconnect()
            self._mark_session_ended(session_id)
            self._clear_capture_state(thread, session_id)

    def _persist_samples(self, session_id: int, readings: List[Dict]):
        ts = _utcnow()
        with self.app.app_context():
            # BK1: warm rail cache once (on first call or after cache was cleared)
            if not self._rail_id_cache:
                self._rail_id_cache = {r.name: r.id for r in Rail.query.all()}

            for r in readings:
                rail_id = self._rail_id_cache.get(r['rail'])
                if rail_id is None:
                    # New rail not yet in DB: create it
                    rail = Rail(name=r['rail'], enabled=True)
                    db.session.add(rail)
                    db.session.flush()
                    self._rail_id_cache[rail.name] = rail.id
                    rail_id = rail.id
                sample = Sample(
                    session_id=session_id,
                    rail_id=rail_id,
                    ts=ts,
                    voltage_v=r.get('voltage_v'),
                    current_ma=r.get('current_ma'),
                    power_mw=r.get('power_mw'),
                    raw_payload=r.get('raw'),
                )
                db.session.add(sample)
            db.session.commit()

    def _push_stream(self, readings: List[Dict], error: Optional[str] = None):
        with self._log_dump_lock:
            log_dump_active = self._log_dump_file is not None
            log_dump_path = self.log_dump_path
            log_dump_sample_count = self._log_dump_sample_count
            log_dump_max_samples = self._log_dump_max_samples
        payload = {
            'ts': _utcnow().isoformat() + 'Z',
            'readings': readings,
            'error': error,
            'log_dump_active': log_dump_active,
            'log_dump_path': log_dump_path,
            'log_dump_sample_count': log_dump_sample_count,
            'log_dump_max_samples': log_dump_max_samples,
        }
        # T2: atomic update of shared state
        with self._state_lock:
            self.last_stream_payload = payload
            self.last_error = error
        try:
            self.stream_queue.put_nowait(payload)
        except queue.Full:
            # BK4: log when data is dropped so operators know backpressure is occurring
            try:
                _ = self.stream_queue.get_nowait()
                self.stream_queue.put_nowait(payload)
                log.warning('Stream queue was full; oldest payload dropped (session %s)', self.active_session_id)
            except queue.Empty:
                pass

    # ---------- state setters ----------
    def set_port(self, port: str):
        self.selected_port = port

    def status(self) -> Dict:
        # T2: read shared state under lock to avoid torn reads
        with self._state_lock:
            last_payload = self.last_stream_payload
            active_session = self.active_session_id
            is_monitoring = bool(self.capture_thread and self.capture_thread.is_alive())
            last_error = self.last_error
        with self._log_dump_lock:
            log_dump_active = self._log_dump_file is not None
            log_dump_path = self.log_dump_path
            log_dump_sample_count = self._log_dump_sample_count
            log_dump_max_samples = self._log_dump_max_samples

        readings = []
        updated_at = None
        total_power_mw = 0.0
        ignored_rails = {
            rail['name']
            for rail in (self.active_config or {}).get('rails', [])
                if rail.get('ignore_for_soc_total')
        }
        if last_payload:
            readings = last_payload.get('readings', [])
            updated_at = last_payload.get('ts')
            total_power_mw = sum(
                (item.get('actual_power_mw') or item.get('power_mw') or 0.0)
                for item in readings
                if not item.get('ignore_for_soc_total') and item.get('rail') not in ignored_rails
            )

        return {
            'selected_port': self.selected_port,
            'persist_to_db': self.persist_to_db,
            'active_config': self.active_config['name'] if self.active_config else None,
            'active_config_id': self.active_config.get('config_id') if self.active_config else None,
            'active_session_id': active_session,
            'is_monitoring': is_monitoring,
            'samples_per_command': self.current_samples_per_command,
            'delay_ms': self.current_delay_ms,
            'command_interval': self.current_command_interval,
            'last_error': last_error,
            'last_update_ts': updated_at,
            'rail_count': len(readings),
            'total_power_mw': total_power_mw,
            'latest_readings': readings,
            'log_dump_active': log_dump_active,
            'log_dump_path': log_dump_path,
            'log_dump_sample_count': log_dump_sample_count,
            'log_dump_max_samples': log_dump_max_samples,
            'system_user': getpass.getuser(),
        }

    # ---------- streaming helpers ----------
    def stream_generator(self):
        while True:
            item = self.stream_queue.get()
            yield f"data: {json.dumps(item)}\n\n"
