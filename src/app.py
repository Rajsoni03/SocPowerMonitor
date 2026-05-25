import csv
import io
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from flask import Flask, Response, jsonify, render_template, request, stream_with_context
from sqlalchemy.orm import joinedload

from .config_loader import ConfigLoader
from .models import Sample, Session, db, init_db
from .power_service import PowerService, list_uart_ports

log = logging.getLogger(__name__)


def _require_json(body):
    """Return parsed JSON body or raise ValueError with a clear message."""
    if body is None:
        raise ValueError('Request body must be JSON with Content-Type: application/json')
    return body


def create_app(test_config: Optional[dict] = None):
    app = Flask(__name__)
    data_dir = Path(os.environ.get('DATA_DIR', 'data'))
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = (data_dir / 'power.db').resolve()

    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', f'sqlite:///{db_path}')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    if test_config:
        app.config.update(test_config)

    init_db(app)

    config_loader = ConfigLoader(os.environ.get('CONFIG_DIR', 'config'))
    power_service = PowerService(app, config_loader)

    # A3: global error handler — returns JSON for all unhandled exceptions
    @app.errorhandler(Exception)
    def handle_exception(exc):
        log.exception('Unhandled exception in request %s %s', request.method, request.path)
        return jsonify({'error': str(exc) or 'Internal server error'}), 500

    # --------- routes ---------
    @app.get('/healthz')
    def healthz():
        return jsonify({'status': 'ok'})

    @app.get('/api/ports')
    def api_ports():
        return jsonify(list_uart_ports())

    @app.post('/api/ports/select')
    def api_select_port():
        # S2: removed force=True; returns 415 if Content-Type is not JSON
        body = request.get_json()
        try:
            body = _require_json(body)
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 415
        port = body.get('port')
        if not port:
            return jsonify({'error': 'port required'}), 400
        if not isinstance(port, str):
            return jsonify({'error': 'port must be a string'}), 400
        power_service.set_port(port)
        return jsonify({'selected_port': port})

    @app.get('/api/configs')
    def api_configs():
        return jsonify(config_loader.list_configs())

    @app.get('/api/status')
    def api_status():
        return jsonify(power_service.status())

    @app.post('/api/configs/activate')
    def api_activate_config():
        body = request.get_json()
        try:
            body = _require_json(body)
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 415
        name = body.get('name')
        if not name:
            return jsonify({'error': 'name required'}), 400
        if not isinstance(name, str):
            return jsonify({'error': 'name must be a string'}), 400
        try:
            cfg = power_service.activate_config(name)
        except FileNotFoundError:
            return jsonify({'error': f"Config '{name}' not found"}), 404
        return jsonify(cfg)

    @app.get('/api/sessions')
    def api_list_sessions():
        # D4: proper pagination via limit + offset query params
        limit = request.args.get('limit', type=int, default=50)
        offset = request.args.get('offset', type=int, default=0)
        limit = max(1, min(limit, 200))  # clamp: 1–200
        sessions = (
            Session.query
            .order_by(Session.started_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return jsonify([s.to_dict() for s in sessions])

    @app.post('/api/sessions')
    def api_session():
        body = request.get_json()
        try:
            body = _require_json(body)
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 415
        action = body.get('action', 'start')
        # A2: validate action field
        if action not in ('start', 'stop'):
            return jsonify({'error': "action must be 'start' or 'stop'"}), 400
        if action == 'start':
            meta = body.get('metadata') or {}
            # A2: validate numeric fields
            samples = body.get('samples_per_command')
            delay_ms = body.get('delay_ms')
            command_interval = body.get('command_interval', 0)
            if samples is not None and not isinstance(samples, int):
                return jsonify({'error': 'samples_per_command must be an integer'}), 400
            if delay_ms is not None and not isinstance(delay_ms, int):
                return jsonify({'error': 'delay_ms must be an integer'}), 400
            if not isinstance(command_interval, (int, float)):
                return jsonify({'error': 'command_interval must be a number'}), 400
            try:
                result = power_service.start_session(meta, samples, delay_ms, command_interval)
            except RuntimeError as exc:
                return jsonify({'error': str(exc)}), 400
            return jsonify(result)
        # action == 'stop'
        power_service.stop_session()
        return jsonify({'stopped': True})

    @app.get('/api/samples')
    def api_samples():
        session_id = request.args.get('session_id', type=int)
        limit = request.args.get('limit', type=int, default=500)
        offset = request.args.get('offset', type=int, default=0)
        since_ts = request.args.get('since_ts')
        order = request.args.get('order', default='desc')
        if not session_id:
            return jsonify({'error': 'session_id required'}), 400
        if order not in {'asc', 'desc'}:
            return jsonify({'error': 'order must be asc or desc'}), 400
        sort_column = Sample.ts.asc() if order == 'asc' else Sample.ts.desc()
        query = Sample.query.filter_by(session_id=session_id).order_by(sort_column)
        if since_ts:
            try:
                # B1: parse ISO-8601, normalize to naive UTC for SQLite comparison
                parsed = datetime.fromisoformat(since_ts.replace('Z', '+00:00'))
                if parsed.tzinfo is not None:
                    parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
                query = query.filter(Sample.ts >= parsed)
            except ValueError:
                return jsonify({'error': 'since_ts must be ISO-8601'}), 400
        rows = query.offset(offset).limit(limit).all()
        return jsonify(power_service.serialize_sample_rows(rows))

    @app.get('/api/export.csv')
    def api_export_csv():
        session_id = request.args.get('session_id', type=int)
        if not session_id:
            return jsonify({'error': 'session_id required'}), 400
        rails_filter = request.args.get('rails')
        rails = set(rails_filter.split(',')) if rails_filter else None

        def csv_value(value):
            return '' if value is None else value

        def generate():
            yield 'ts,rail,voltage_v,current_ma,power_mw,raw\n'
            # D3: joinedload avoids N+1 per row for row.rail.name
            query = (
                Sample.query
                .options(joinedload(Sample.rail))
                .filter_by(session_id=session_id)
                .order_by(Sample.ts)
            )
            for row in query.yield_per(500):
                rail_name = row.rail.name if row.rail else ''
                if rails and rail_name not in rails:
                    continue
                buffer = io.StringIO()
                writer = csv.writer(buffer, lineterminator='\n')
                writer.writerow([
                    f'{row.ts.isoformat()}Z',
                    rail_name,
                    csv_value(row.voltage_v),
                    csv_value(row.current_ma),
                    csv_value(row.power_mw),
                    row.raw_payload or '',
                ])
                yield buffer.getvalue()

        headers = {
            'Content-Disposition': f'attachment; filename="session_{session_id}.csv"'
        }
        return Response(stream_with_context(generate()), mimetype='text/csv', headers=headers)

    @app.delete('/api/sessions/<int:session_id>')
    def api_delete_session(session_id):
        session = Session.query.get(session_id)
        if not session:
            return jsonify({'error': f'Session {session_id} not found'}), 404
        if power_service.status().get('active_session_id') == session_id:
            return jsonify({'error': 'Cannot delete the active session'}), 409
        db.session.delete(session)
        db.session.commit()
        return jsonify({'deleted': session_id})

    @app.post('/api/db-capture')
    def api_db_capture():
        body = request.get_json()
        try:
            body = _require_json(body)
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 415
        enabled = body.get('enabled')
        if enabled is None:
            return jsonify({'error': 'enabled required'}), 400
        if not isinstance(enabled, bool):
            return jsonify({'error': 'enabled must be a boolean'}), 400
        try:
            power_service.set_persist_to_db(enabled)
        except RuntimeError as exc:
            return jsonify({'error': str(exc)}), 409
        return jsonify({'persist_to_db': enabled})

    @app.get('/api/stream')
    def api_stream():
        return Response(power_service.stream_generator(), mimetype='text/event-stream')

    @app.post('/api/log-dump')
    def api_log_dump():
        body = request.get_json()
        try:
            body = _require_json(body)
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 415
        action = body.get('action')
        if action not in ('start', 'stop'):
            return jsonify({'error': "action must be 'start' or 'stop'"}), 400
        if action == 'start':
            file_path = body.get('file_path')
            if not file_path:
                return jsonify({'error': 'file_path required'}), 400
            if not isinstance(file_path, str):
                return jsonify({'error': 'file_path must be a string'}), 400
            max_samples = body.get('max_samples')
            if max_samples is not None:
                if not isinstance(max_samples, int) or max_samples < 1:
                    return jsonify({'error': 'max_samples must be a positive integer'}), 400
            try:
                actual_path = power_service.start_log_dump(file_path, max_samples)
            except RuntimeError as exc:
                return jsonify({'error': str(exc)}), 409
            return jsonify({'log_dump_active': True, 'log_dump_path': actual_path})
        power_service.stop_log_dump()
        return jsonify({'log_dump_active': False, 'log_dump_path': None})

    @app.get('/api')
    def api_index():
        return jsonify({
            'message': 'SocPowerMonitor API',
            'endpoints': [
                '/api/ports', '/api/ports/select', '/api/configs', '/api/configs/activate',
                '/api/status', '/api/sessions', '/api/samples', '/api/export.csv',
                '/api/db-capture', '/api/log-dump', '/api/stream', '/healthz'
            ]
        })

    @app.get('/')
    def index():
        return render_template('index.html')

    # Optionally auto-activate default config if present
    try:
        power_service.activate_config('j722s')
    except FileNotFoundError:
        pass

    return app


if __name__ == '__main__':
    app = create_app()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8000)), debug=True)
