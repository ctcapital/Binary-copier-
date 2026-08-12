"""SQLite storage: settings, signals, trades and an app event log.

Every write goes through a short-lived connection so the Streamlit thread and
the background engine thread never share a connection object.
"""

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "copier.db")

# Serialises writes from the UI thread and the engine thread.
_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id        TEXT    NOT NULL,
    message_id     INTEGER NOT NULL,
    received_at    REAL    NOT NULL,
    raw_text       TEXT    NOT NULL,
    parsed         INTEGER NOT NULL DEFAULT 0,
    pair           TEXT,
    direction      TEXT,
    duration       INTEGER,
    duration_unit  TEXT,
    status         TEXT    NOT NULL,
    reason         TEXT,
    UNIQUE (chat_id, message_id)
);

CREATE TABLE IF NOT EXISTS trades (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id      INTEGER REFERENCES signals(id),
    created_at     REAL    NOT NULL,
    mode           TEXT    NOT NULL,
    deriv_symbol   TEXT    NOT NULL,
    contract_type  TEXT    NOT NULL,
    stake          REAL    NOT NULL,
    currency       TEXT    NOT NULL,
    duration       INTEGER NOT NULL,
    duration_unit  TEXT    NOT NULL,
    contract_id    TEXT,
    buy_price      REAL,
    payout         REAL,
    longcode       TEXT,
    status         TEXT    NOT NULL,
    profit         REAL,
    entry_spot     REAL,
    exit_spot      REAL,
    settled_at     REAL,
    error          TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    level   TEXT NOT NULL,
    source  TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signals_received ON signals(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_created   ON trades(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_ts        ON events(ts DESC);
"""


def connect() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with _write_lock:
        conn = connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()
    # The DB holds the Deriv token and Telegram api_hash — keep it owner-only.
    try:
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

def get_setting(key: str, default: Any = None) -> Any:
    conn = connect()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (ValueError, TypeError):
        return default


def set_setting(key: str, value: Any) -> None:
    with _write_lock:
        conn = connect()
        try:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )
            conn.commit()
        finally:
            conn.close()


def all_settings() -> Dict[str, Any]:
    conn = connect()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    finally:
        conn.close()
    out = {}
    for row in rows:
        try:
            out[row["key"]] = json.loads(row["value"])
        except (ValueError, TypeError):
            pass
    return out


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------

def log(level: str, source: str, message: str) -> None:
    with _write_lock:
        conn = connect()
        try:
            conn.execute(
                "INSERT INTO events (ts, level, source, message) VALUES (?, ?, ?, ?)",
                (time.time(), level, source, str(message)[:2000]),
            )
            conn.commit()
        finally:
            conn.close()


def recent_events(limit: int = 200) -> List[sqlite3.Row]:
    conn = connect()
    try:
        return conn.execute(
            "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# signals
# --------------------------------------------------------------------------

def insert_signal(
    chat_id: str,
    message_id: int,
    raw_text: str,
    parsed: bool,
    pair: Optional[str],
    direction: Optional[str],
    duration: Optional[int],
    duration_unit: Optional[str],
    status: str,
    reason: Optional[str] = None,
) -> Optional[int]:
    """Insert a signal. Returns None if this message was already recorded.

    The UNIQUE(chat_id, message_id) constraint is what makes the copier
    idempotent: a replayed or edited message can never trade twice.
    """
    with _write_lock:
        conn = connect()
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO signals "
                "(chat_id, message_id, received_at, raw_text, parsed, pair, direction,"
                " duration, duration_unit, status, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(chat_id), int(message_id), time.time(), raw_text,
                    1 if parsed else 0, pair, direction, duration, duration_unit,
                    status, reason,
                ),
            )
            conn.commit()
            if cur.rowcount == 0:
                return None
            return int(cur.lastrowid)
        finally:
            conn.close()


def update_signal_status(signal_id: int, status: str, reason: Optional[str] = None) -> None:
    with _write_lock:
        conn = connect()
        try:
            conn.execute(
                "UPDATE signals SET status = ?, reason = ? WHERE id = ?",
                (status, reason, signal_id),
            )
            conn.commit()
        finally:
            conn.close()


def recent_signals(limit: int = 100) -> List[sqlite3.Row]:
    conn = connect()
    try:
        return conn.execute(
            "SELECT * FROM signals ORDER BY received_at DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# trades
# --------------------------------------------------------------------------

def insert_trade(
    signal_id: Optional[int],
    mode: str,
    deriv_symbol: str,
    contract_type: str,
    stake: float,
    currency: str,
    duration: int,
    duration_unit: str,
    status: str = "pending",
) -> int:
    with _write_lock:
        conn = connect()
        try:
            cur = conn.execute(
                "INSERT INTO trades (signal_id, created_at, mode, deriv_symbol,"
                " contract_type, stake, currency, duration, duration_unit, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    signal_id, time.time(), mode, deriv_symbol, contract_type,
                    stake, currency, duration, duration_unit, status,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()


_TRADE_FIELDS = {
    "contract_id", "buy_price", "payout", "longcode", "status",
    "profit", "entry_spot", "exit_spot", "settled_at", "error",
}


def update_trade(trade_id: int, **fields: Any) -> None:
    updates = {k: v for k, v in fields.items() if k in _TRADE_FIELDS}
    if not updates:
        return
    assignments = ", ".join("{} = ?".format(k) for k in updates)
    params = list(updates.values()) + [trade_id]
    with _write_lock:
        conn = connect()
        try:
            conn.execute(
                "UPDATE trades SET {} WHERE id = ?".format(assignments), params
            )
            conn.commit()
        finally:
            conn.close()


def open_trades() -> List[sqlite3.Row]:
    conn = connect()
    try:
        return conn.execute(
            "SELECT * FROM trades WHERE status IN ('pending', 'open') ORDER BY created_at"
        ).fetchall()
    finally:
        conn.close()


def recent_trades(limit: int = 200) -> List[sqlite3.Row]:
    conn = connect()
    try:
        return conn.execute(
            "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()


def trades_since(ts: float, mode: Optional[str] = None) -> List[sqlite3.Row]:
    conn = connect()
    try:
        if mode:
            return conn.execute(
                "SELECT * FROM trades WHERE created_at >= ? AND mode = ? ORDER BY created_at",
                (ts, mode),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM trades WHERE created_at >= ? ORDER BY created_at", (ts,)
        ).fetchall()
    finally:
        conn.close()


def stats() -> Dict[str, Any]:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT "
            "  COUNT(*) AS total, "
            "  SUM(CASE WHEN status = 'won'  THEN 1 ELSE 0 END) AS wins, "
            "  SUM(CASE WHEN status = 'lost' THEN 1 ELSE 0 END) AS losses, "
            "  SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors, "
            "  COALESCE(SUM(profit), 0) AS pnl "
            "FROM trades"
        ).fetchone()
    finally:
        conn.close()
    return {
        "total": row["total"] or 0,
        "wins": row["wins"] or 0,
        "losses": row["losses"] or 0,
        "errors": row["errors"] or 0,
        "pnl": row["pnl"] or 0.0,
    }
