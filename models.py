import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class User(UserMixin, db.Model):
    """Application user with optional Telegram notification support."""

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    telegram_chat_id = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    positions = db.relationship("Position", backref="user", lazy=True)

    def set_password(self, pw: str) -> None:
        """Hash and store password."""
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw: str) -> bool:
        """Verify a plaintext password against the stored hash."""
        return check_password_hash(self.password_hash, pw)

    def __repr__(self) -> str:
        return f"<User {self.username}>"


class ScanResult(db.Model):
    """Latest scan result for a given ticker, upserted on each full scan."""

    __tablename__ = "scan_results"

    id = db.Column(db.Integer, primary_key=True)
    ticker = db.Column(db.String(32), unique=True, nullable=False)
    resolved = db.Column(db.String(32), nullable=False)
    exchange = db.Column(db.String(16), nullable=False)  # NSE / BSE / NYSE/NASDAQ
    company_name = db.Column(db.String(256), nullable=True)
    phase = db.Column(db.String(128), nullable=False)
    signal_score = db.Column(db.Float, default=0.0)
    close = db.Column(db.Float, nullable=False)
    ma50 = db.Column(db.Float, nullable=True)
    ma200 = db.Column(db.Float, nullable=True)
    t1 = db.Column(db.Float, nullable=True)
    t2 = db.Column(db.Float, nullable=True)
    stop_loss = db.Column(db.Float, nullable=True)
    rr = db.Column(db.Float, nullable=True)
    last_scanned = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    def __repr__(self) -> str:
        return f"<ScanResult {self.ticker} score={self.signal_score}>"


class Position(db.Model):
    """A trade position belonging to a user."""

    __tablename__ = "positions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    ticker = db.Column(db.String(32), nullable=False)
    exchange = db.Column(db.String(16), nullable=False)
    company_name = db.Column(db.String(256), nullable=True)
    entry_price = db.Column(db.Float, nullable=False)
    shares = db.Column(db.Float, nullable=False)
    stop_loss = db.Column(db.Float, nullable=True)
    target1 = db.Column(db.Float, nullable=True)
    target2 = db.Column(db.Float, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    opened_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    closed_at = db.Column(db.DateTime, nullable=True)
    close_price = db.Column(db.Float, nullable=True)
    realized_pnl = db.Column(db.Float, nullable=True)

    @property
    def is_open(self) -> bool:
        """True when the position has not been closed."""
        return self.closed_at is None

    def __repr__(self) -> str:
        status = "open" if self.is_open else "closed"
        return f"<Position {self.ticker} {status}>"
