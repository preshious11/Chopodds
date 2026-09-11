"""SQLite-backed prediction audit log and settlement statistics."""

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)
DB_PATH = Path(__file__).resolve().parent / "prediction_tracking.db"
_DB_LOCK = Lock()
_MEMORY_CONNECTION: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    global _MEMORY_CONNECTION
    if str(DB_PATH) == ":memory:":
        if _MEMORY_CONNECTION is None:
            _MEMORY_CONNECTION = sqlite3.connect(":memory:")
            _MEMORY_CONNECTION.row_factory = sqlite3.Row
        return _MEMORY_CONNECTION
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def initialize() -> None:
    """Create the prediction audit schema if it does not exist."""
    with _DB_LOCK, _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS predictions (
                prediction_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL UNIQUE,
                match_name TEXT NOT NULL,
                sport_key TEXT NOT NULL,
                kickoff_time TEXT,
                selection TEXT NOT NULL,
                confidence_score REAL NOT NULL,
                odds REAL NOT NULL,
                delivered_to_users TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'PENDING',
                created_timestamp TEXT NOT NULL,
                settled_timestamp TEXT,
                settlement_result TEXT
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                prediction_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                delivered_timestamp TEXT NOT NULL,
                PRIMARY KEY (prediction_id, user_id),
                FOREIGN KEY (prediction_id) REFERENCES predictions(prediction_id)
            );
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )


def _prediction_id(event_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sportsbot:{event_id}"))


def record_predictions(predictions: list[dict], user_id: int | None = None) -> None:
    """Insert immutable prediction records and optionally record delivery."""
    initialize()
    now = datetime.now(timezone.utc).isoformat()
    with _DB_LOCK, _connect() as connection:
        for prediction in predictions:
            event_id = str(prediction.get("event_id") or prediction.get("match"))
            if not event_id:
                continue
            prediction_id = _prediction_id(event_id)
            connection.execute(
                """
                INSERT OR IGNORE INTO predictions (
                    prediction_id, event_id, match_name, sport_key, kickoff_time,
                    selection, confidence_score, odds, created_timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prediction_id,
                    event_id,
                    prediction.get("match", "Unknown match"),
                    prediction.get("sport_key", prediction.get("league", "")),
                    prediction.get("kickoff_utc") or prediction.get("match_time"),
                    prediction.get("pick", ""),
                    prediction.get("confidence", 0),
                    prediction.get("odds", 0),
                    now,
                ),
            )
            if user_id is None:
                continue
            connection.execute(
                "INSERT OR IGNORE INTO deliveries VALUES (?, ?, ?)",
                (prediction_id, user_id, now),
            )
            recipients = connection.execute(
                "SELECT delivered_to_users FROM predictions WHERE prediction_id = ?",
                (prediction_id,),
            ).fetchone()
            users = json.loads(recipients[0]) if recipients else []
            if user_id not in users:
                users.append(user_id)
                connection.execute(
                    "UPDATE predictions SET delivered_to_users = ? WHERE prediction_id = ?",
                    (json.dumps(users), prediction_id),
                )


def get_pending_predictions() -> list[dict]:
    """Return pending predictions with their stored delivery recipients."""
    initialize()
    with _DB_LOCK, _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM predictions WHERE status = 'PENDING'"
        ).fetchall()
    return [dict(row) for row in rows]


def settle_prediction(prediction_id: str, status: str) -> bool:
    """Settle a pending record exactly once."""
    if status not in {"SETTLED_WIN", "SETTLED_LOSS", "VOID"}:
        return False
    initialize()
    with _DB_LOCK, _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE predictions
            SET status = ?, settlement_result = ?, settled_timestamp = ?
            WHERE prediction_id = ? AND status = 'PENDING'
            """,
            (status, status.removeprefix("SETTLED_").lower(),
             datetime.now(timezone.utc).isoformat(), prediction_id),
        )
        return cursor.rowcount == 1


def get_global_stats() -> dict:
    """Return settled global totals from SQLite."""
    initialize()
    with _DB_LOCK, _connect() as connection:
        row = connection.execute(
            """
            SELECT
                COUNT(*) AS settled,
                SUM(status = 'SETTLED_WIN') AS wins,
                SUM(status = 'SETTLED_LOSS') AS losses,
                SUM(status = 'VOID') AS voids,
                (SELECT COUNT(*) FROM predictions) AS generated
            FROM predictions
            WHERE status != 'PENDING'
            """
        ).fetchone()
    wins = row["wins"] or 0
    losses = row["losses"] or 0
    return {
        "generated": row["generated"] or 0,
        "settled": row["settled"] or 0,
        "wins": wins,
        "losses": losses,
        "voids": row["voids"] or 0,
        "win_rate": round(wins / (wins + losses) * 100, 1) if wins + losses else 0,
    }


def get_user_stats(user_id: int, joined_at: datetime | None = None) -> dict:
    """Return settled stats for deliveries made on/after the user's join time."""
    initialize()
    joined_iso = joined_at.astimezone(timezone.utc).isoformat() if joined_at else ""
    with _DB_LOCK, _connect() as connection:
        row = connection.execute(
            """
            SELECT
                COUNT(DISTINCT d.prediction_id) AS generated,
                SUM(p.status = 'SETTLED_WIN') AS wins,
                SUM(p.status = 'SETTLED_LOSS') AS losses
            FROM deliveries d
            JOIN predictions p ON p.prediction_id = d.prediction_id
            WHERE d.user_id = ? AND d.delivered_timestamp >= ?
            """,
            (user_id, joined_iso),
        ).fetchone()
    wins = row["wins"] or 0
    losses = row["losses"] or 0
    settled = wins + losses
    return {
        "generated": row["generated"] or 0,
        "wins": wins,
        "losses": losses,
        "settled": settled,
        "win_rate": round(wins / settled * 100, 1) if settled else 0,
    }


def get_health_metrics() -> dict:
    """Return pending/settled counts and the last score-fetch timestamp."""
    initialize()
    with _DB_LOCK, _connect() as connection:
        pending = connection.execute(
            "SELECT COUNT(*) FROM predictions WHERE status = 'PENDING'"
        ).fetchone()[0]
        settled = connection.execute(
            "SELECT COUNT(*) FROM predictions WHERE status != 'PENDING'"
        ).fetchone()[0]
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'last_score_fetch'"
        ).fetchone()
    return {"pending": pending, "settled": settled, "last_score_fetch": row[0] if row else "never"}


def set_last_score_fetch() -> None:
    """Record a successful score-fetch completion timestamp."""
    initialize()
    with _DB_LOCK, _connect() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES ('last_score_fetch', ?)",
            (datetime.now(timezone.utc).isoformat(),),
        )
