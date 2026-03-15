import csv
import io
import os
from pathlib import Path
from typing import Optional

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

from .config_loader import ConfigLoader
from .models import Sample, Session, init_db
from .power_service import PowerService, list_uart_ports


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

    # --------- routes ---------
    @app.get('/healthz')
    def healthz():
        return {'status': 'ok'}

    @app.get('/api/ports')
    def api_ports():
        return jsonify(list_uart_ports())

    @app.post('/api/ports/select')
    def api_select_port():
        body = request.get_json(force=True)
        port = body.get('port')
        if not port:
            return {'error': 'port required'}, 400
        power_service.set_port(port)
        return {'selected_port': port}

    @app.get('/api/configs')
    def api_configs():
        return jsonify(config_loader.list_configs())

    @app.get('/api/status')
    def api_status():
        return jsonify(power_service.status())

    @app.post('/api/configs/activate')
    def api_activate_config():
        body = request.get_json(force=True)
        name = body.get('name')
        if not name:
            return {'error': 'name required'}, 400
        try:
            cfg = power_service.activate_config(name)
        except FileNotFoundError:
            return {'error': 'config not found'}, 404
        return jsonify(cfg)

    @app.get('/api/sessions')
    def api_list_sessions():
        sessions = Session.query.order_by(Session.started_at.desc()).limit(50).all()
        return jsonify([s.to_dict() for s in sessions])

    @app.post('/api/sessions')
    def api_session():
        body = request.get_json(force=True)
        action = body.get('action', 'start')
        if action == 'start':
            meta = body.get('metadata') or {}
            samples = body.get('samples_per_command')
            delay_ms = body.get('delay_ms')
            command_interval = body.get('command_interval', 0)
            try:
                result = power_service.start_session(meta, samples, delay_ms, command_interval)
            except RuntimeError as exc:
                return {'error': str(exc)}, 400
            return result
        elif action == 'stop':
            power_service.stop_session()
            return {'stopped': True}
        return {'error': 'action must be start or stop'}, 400

    @app.get('/api/samples')
    def api_samples():
        session_id = request.args.get('session_id', type=int)
        limit = request.args.get('limit', type=int, default=500)
        offset = request.args.get('offset', type=int, default=0)
        since_ts = request.args.get('since_ts')
        order = request.args.get('order', default='desc')
        if not session_id:
            return {'error': 'session_id required'}, 400
        if order not in {'asc', 'desc'}:
            return {'error': 'order must be asc or desc'}, 400
        sort_column = Sample.ts.asc() if order == 'asc' else Sample.ts.desc()
        query = Sample.query.filter_by(session_id=session_id).order_by(sort_column)
        if since_ts:
            try:
                from datetime import datetime
                parsed = datetime.fromisoformat(since_ts.replace('Z', ''))
                query = query.filter(Sample.ts >= parsed)
            except ValueError:
                return {'error': 'since_ts must be ISO-8601'}, 400
        rows = query.offset(offset).limit(limit).all()
        return jsonify(power_service.serialize_sample_rows(rows))

    @app.get('/api/export.csv')
    def api_export_csv():
        session_id = request.args.get('session_id', type=int)
        if not session_id:
            return {'error': 'session_id required'}, 400
        rails_filter = request.args.get('rails')
        rails = set(rails_filter.split(',')) if rails_filter else None

        def csv_value(value):
            return '' if value is None else value

        def generate():
            yield 'ts,rail,voltage_v,current_ma,power_mw,raw\n'
            query = Sample.query.filter_by(session_id=session_id).order_by(Sample.ts)
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

    @app.get('/api/stream')
    def api_stream():
        return Response(power_service.stream_generator(), mimetype='text/event-stream')

    @app.get('/api')
    def api_index():
        return {
            'message': 'SocPowerMonitor API',
            'endpoints': [
                '/api/ports', '/api/ports/select', '/api/configs', '/api/configs/activate',
                '/api/status', '/api/sessions', '/api/samples', '/api/export.csv', '/api/stream', '/healthz'
            ]
        }

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
