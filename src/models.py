import datetime as dt
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Index
from sqlalchemy.dialects.sqlite import JSON

# Global SQLAlchemy instance
# Initialized in app factory

db = SQLAlchemy()


class Rail(db.Model):
    __tablename__ = 'rail'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True, nullable=False)
    ina_addr = db.Column(db.String(8), nullable=True)
    channel = db.Column(db.Integer, nullable=True)
    shunt_milliohm = db.Column(db.Float, nullable=True)
    nominal_volts = db.Column(db.Float, nullable=True)
    enabled = db.Column(db.Boolean, default=True)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'ina_addr': self.ina_addr,
            'channel': self.channel,
            'shunt_milliohm': self.shunt_milliohm,
            'nominal_volts': self.nominal_volts,
            'enabled': self.enabled,
        }


class Session(db.Model):
    __tablename__ = 'session'
    id = db.Column(db.Integer, primary_key=True)
    started_at = db.Column(db.DateTime, default=dt.datetime.utcnow, nullable=False)
    ended_at = db.Column(db.DateTime, nullable=True)
    config_name = db.Column(db.String(128), nullable=False)
    config_hash = db.Column(db.String(64), nullable=False)
    session_metadata = db.Column('metadata', JSON, nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'started_at': self.started_at.isoformat() + 'Z',
            'ended_at': self.ended_at.isoformat() + 'Z' if self.ended_at else None,
            'config_name': self.config_name,
            'config_hash': self.config_hash,
            'metadata': self.session_metadata,
        }


class Sample(db.Model):
    __tablename__ = 'sample'
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('session.id'), nullable=False)
    rail_id = db.Column(db.Integer, db.ForeignKey('rail.id'), nullable=False)
    ts = db.Column(db.DateTime, default=dt.datetime.utcnow, nullable=False)
    voltage_v = db.Column(db.Float, nullable=True)
    current_ma = db.Column(db.Float, nullable=True)
    power_mw = db.Column(db.Float, nullable=True)
    raw_payload = db.Column(db.Text, nullable=True)

    session = db.relationship('Session', backref=db.backref('samples', lazy='dynamic'))
    rail = db.relationship('Rail', backref=db.backref('samples', lazy='dynamic'))

    __table_args__ = (
        Index('idx_sample_session_ts', 'session_id', 'ts'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'session_id': self.session_id,
            'rail_id': self.rail_id,
            'rail': self.rail.name if self.rail else None,
            'ts': self.ts.isoformat() + 'Z',
            'voltage_v': self.voltage_v,
            'current_ma': self.current_ma,
            'power_mw': self.power_mw,
            'raw_payload': self.raw_payload,
        }


def init_db(app):
    db.init_app(app)
    with app.app_context():
        db.create_all()
