"""SQLite-backed prediction audit log and settlement statistics."""

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
DB_PATH = Path(__file__).resolve().parent / "prediction_tracking.db"
_DB_LOCK = Lock()

# Default time window used by the settlement engine: only poll matches that
# kicked off at least min_elapsed_minutes ago, and stop polling a match once
# it is past the upper bound (stale matches are never polled indefinitely).
DEFAULT_MIN_ELAPSED_MINUTES = 110
DEFAULT_MAX_ELAPSED_HOURS = 14

LAGOS_TZ = ZoneInfo("Africa/Lagos")
_MEMORY_CONNECTION: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    global _MEMORY_CONNECTION
    if str(DB_PATH) == ":memory:":
        if _MEMORY_CONNECTION is None:
            _MEMORY_CONNECTION = sqlite3.connect(":memory:")
            _MEMORY_CONNECTION.row_factory = sqlite3.Row
        return _MEMORY_CONNECTION
    connection = sqlite3.connect(DB_PATH, check_same_thread=False)
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
                commence_time TEXT,
                home_team TEXT,
                away_team TEXT,
                market_type TEXT,
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
        _ensure_schema_columns(connection)
        _ensure_indexes(connection)


def _ensure_schema_columns(connection: sqlite3.Connection) -> None:
    """Add columns introduced after the initial release to pre-existing tables.

    Safe no-op when a column is already present. Keeps databases created before
    the time-aware settlement engine (and home/away/market_type storage) on the
    new schema without forcing users to wipe their tracking DB.
    """
    _ADDED_COLUMNS = (
        "commence_time TEXT",
        "home_team TEXT",
        "away_team TEXT",
        "market_type TEXT",
    )
    existing = {
        row[1] for row in connection.execute("PRAGMA table_info(predictions)").fetchall()
    }
    for column_sql in _ADDED_COLUMNS:
        name = column_sql.split(" ", 1)[0]
        if name not in existing:
            connection.execute(f"ALTER TABLE predictions ADD COLUMN {column_sql}")
            logger.info("Migration: added %s column to predictions table.", name)


def _broadcast_key(date_str: str) -> str:
    """Return the metadata key that records a completed daily broadcast run."""
    return f"daily_broadcast:{date_str}"


def has_daily_broadcast_run(date_str: str) -> bool:
    """Return True if the daily broadcast already ran on the given UTC date.

    ``date_str`` is a UTC date in ``YYYY-MM-DD`` format.
    """
    initialize()
    with _DB_LOCK, _connect() as connection:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (_broadcast_key(date_str),)
        ).fetchone()
    return row is not None


def mark_daily_broadcast_run(date_str: str) -> None:
    """Record that the daily broadcast ran on the given UTC date (``YYYY-MM-DD``)."""
    initialize()
    with _DB_LOCK, _connect() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
            (_broadcast_key(date_str), date_str),
        )


def _prediction_id(event_id: str) -> str:
    """Deterministic id: the same event always maps to one authoritative prediction_id."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sportsbot:{event_id}"))


def _dedupe_predictions_by_event(predictions: list[dict]) -> list[dict]:
    """
    Deduplicate predictions by unique event_id prior to database insertion.

    If an event appears more than once (e.g. selected for both the standard
    match list and a featured/daily slip), only one record is stored under a
    single authoritative prediction_id. The strongest candidate (highest
    confidence, then most bookmakers) is kept; first occurrence wins ties.
    """
    best_by_event: dict[str, dict] = {}
    order: list[str] = []
    for prediction in predictions:
        event_id = str(
            prediction.get("event_id") or prediction.get("match") or ""
        ).strip()
        if not event_id:
            continue
        rank = (
            prediction.get("confidence", 0),
            prediction.get("num_bookmakers", 0),
        )
        existing = best_by_event.get(event_id)
        if existing is None:
            best_by_event[event_id] = prediction
            order.append(event_id)
            continue
        existing_rank = (
            existing.get("confidence", 0),
            existing.get("num_bookmakers", 0),
        )
        if rank > existing_rank:
            best_by_event[event_id] = prediction
    return [best_by_event[event_id] for event_id in order]


def _parse_utc(value) -> datetime | None:
    """Parse an ISO-8601 timestamp (with trailing 'Z') into an aware UTC datetime."""
    if not value:
        return None
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _resolve_commence_time(prediction: dict) -> str | None:
    """Return the UTC kick-off (``commence_time``) ISO string for a prediction.

    Precedence:
      1. explicit ``commence_time`` (raw event UTC from the Odds API),
      2. ``kickoff_utc`` when it is a full UTC timestamp,
      3. reconstruct from Lagos ``match_date`` + ``match_time``.
    Returns None when no UTC kick-off can be determined.
    """
    explicit = prediction.get("commence_time") or prediction.get("kickoff_utc")
    if _parse_utc(explicit) is not None:
        return str(explicit).strip()

    match_date = prediction.get("match_date") or prediction.get("date")
    match_time = prediction.get("match_time")
    if match_date and match_time:
        try:
            local_dt = datetime.strptime(f"{match_date} {match_time}", "%Y-%m-%d %H:%M")
            return local_dt.replace(tzinfo=LAGOS_TZ).astimezone(timezone.utc).isoformat()
        except (ValueError, TypeError):
            return None
    return None


def record_predictions(predictions: list[dict], user_id: int | None = None) -> None:
    """Insert immutable prediction records and optionally record delivery.

    Predictions are deduplicated by unique event_id before insertion, so an
    event selected for both the standard match predictions and featured/daily
    slips is stored once under a single authoritative prediction_id.

    The UTC ``commence_time`` (kick-off timestamp) is stored for every pick so
    the settlement engine can time-gate when a match is eligible for polling.
    """
    initialize()
    now = datetime.now(timezone.utc).isoformat()
    deduped = _dedupe_predictions_by_event(predictions)
    with _DB_LOCK, _connect() as connection:
        for prediction in deduped:
            event_id = str(prediction.get("event_id") or prediction.get("match"))
            if not event_id:
                continue
            prediction_id = _prediction_id(event_id)
            commence_time = _resolve_commence_time(prediction)
            connection.execute(
                """
                INSERT OR IGNORE INTO predictions (
                    prediction_id, event_id, match_name, sport_key, kickoff_time,
                    commence_time, home_team, away_team, market_type, selection,
                    confidence_score, odds, created_timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prediction_id,
                    event_id,
                    prediction.get("match", "Unknown match"),
                    prediction.get("sport_key", prediction.get("league", "")),
                    prediction.get("kickoff_utc") or prediction.get("match_time"),
                    commence_time,
                    prediction.get("home_team"),
                    prediction.get("away_team"),
                    prediction.get("market_type"),
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


def get_eligible_pending_predictions(
    min_elapsed_minutes: int = DEFAULT_MIN_ELAPSED_MINUTES,
    now: datetime | None = None,
) -> list[dict]:
    """Return pending predictions whose match is inside the settlement window.

    A prediction is eligible when its UTC ``commence_time`` satisfies::

        commence + min_elapsed_minutes <= now <= commence + DEFAULT_MAX_ELAPSED_HOURS

    i.e. the match has kicked off more than ``min_elapsed_minutes`` ago (so an
    outcome is likely known) but is not so stale that polling it would waste
    Odds API credits. Pending picks without a parseable ``commence_time`` are
    excluded (and logged) because they cannot be time-gated safely.

    ``now`` is injectable for deterministic tests; defaults to UTC now.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)

    earliest = now - timedelta(hours=DEFAULT_MAX_ELAPSED_HOURS)
    latest = now - timedelta(minutes=min_elapsed_minutes)

    eligible: list[dict] = []
    missing = 0
    with _DB_LOCK, _connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM predictions
            WHERE status = 'PENDING'
              AND commence_time IS NOT NULL
            ORDER BY commence_time ASC
            """
        ).fetchall()
        for row in rows:
            commence = _parse_utc(row["commence_time"])
            if commence is None:
                missing += 1
                continue
            if earliest <= commence <= latest:
                eligible.append(dict(row))

    if missing:
        logger.warning(
            "get_eligible_pending_predictions: %d pending pick(s) have no "
            "parseable commence_time and were skipped (cannot time-gate).",
            missing,
        )
    return eligible


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
    """Return settlement status totals from SQLite.

    Counting notes:
    - ``delivered`` / ``generated`` count unique fixtures (event_id); the
      predictions table enforces one row per event_id, so events selected for
      both standard predictions and featured slips are never double-counted.
    - ``settled`` includes wins, losses and voids.
    - ``win_rate`` = wins / settled games * 100 (0.0 when nothing settled).
    """
    initialize()
    with _DB_LOCK, _connect() as connection:
        row = connection.execute(
            """
            SELECT
                COUNT(*) AS delivered,
                COUNT(DISTINCT event_id) AS generated,
                SUM(status = 'PENDING') AS pending,
                SUM(status != 'PENDING') AS settled,
                SUM(status = 'SETTLED_WIN') AS wins,
                SUM(status = 'SETTLED_LOSS') AS losses,
                SUM(status = 'VOID') AS voids
            FROM predictions
            """
        ).fetchone()
    wins = row["wins"] or 0
    losses = row["losses"] or 0
    voids = row["voids"] or 0
    settled = row["settled"] or 0
    return {
        "delivered": row["delivered"] or 0,
        "generated": row["generated"] or 0,
        "pending": row["pending"] or 0,
        "settled": settled,
        "wins": wins,
        "losses": losses,
        "voids": voids,
        "win_rate": round(wins / settled * 100, 1) if settled else 0,
    }


def get_user_stats(user_id: int, joined_at: datetime | None = None) -> dict:
    """Return settlement status totals for deliveries made on/after join time.

    Counts are per unique fixture (event_id) — a prediction delivered in both
    the standard list and a featured slip is counted once.
    """
    initialize()
    joined_iso = joined_at.astimezone(timezone.utc).isoformat() if joined_at else ""
    with _DB_LOCK, _connect() as connection:
        row = connection.execute(
            """
            SELECT
                COUNT(DISTINCT p.event_id) AS delivered,
                SUM(p.status = 'PENDING') AS pending,
                SUM(p.status != 'PENDING') AS settled,
                SUM(p.status = 'SETTLED_WIN') AS wins,
                SUM(p.status = 'SETTLED_LOSS') AS losses,
                SUM(p.status = 'VOID') AS voids
            FROM deliveries d
            JOIN predictions p ON p.prediction_id = d.prediction_id
            WHERE d.user_id = ? AND d.delivered_timestamp >= ?
            """,
            (user_id, joined_iso),
        ).fetchone()
    wins = row["wins"] or 0
    losses = row["losses"] or 0
    voids = row["voids"] or 0
    settled = row["settled"] or 0
    return {
        "delivered": row["delivered"] or 0,
        "generated": row["delivered"] or 0,
        "pending": row["pending"] or 0,
        "settled": settled,
        "wins": wins,
        "losses": losses,
        "voids": voids,
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




def _ensure_indexes(connection: sqlite3.Connection) -> None:
    """Create indexes that speed up the settlement and stats queries."""
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_predictions_status ON predictions(status);
        CREATE INDEX IF NOT EXISTS idx_predictions_event_id ON predictions(event_id);
        CREATE INDEX IF NOT EXISTS idx_predictions_prediction_id ON predictions(prediction_id);
        CREATE INDEX IF NOT EXISTS idx_deliveries_user_id ON deliveries(user_id);
        """
    )

def set_last_score_fetch() -> None:
    """Record a successful score-fetch completion timestamp."""
    initialize()
    with _DB_LOCK, _connect() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES ('last_score_fetch', ?)",
            (datetime.now(timezone.utc).isoformat(),),
        )
