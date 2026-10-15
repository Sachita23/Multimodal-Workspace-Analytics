"""
SQLite storage for team (multi-user) collection.

Tables:
    members       one row per invited / joined person
    observations  raw collector rows pushed by each member

Tokens and invite codes are stored only as SHA-256 hashes.
"""

import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import (
    TEAM_DB_FILE,
    TEAM_INVITE_TTL_HOURS,
    TEAM_SHARED_FIELDS,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    invite_hash     TEXT    UNIQUE,
    invite_expires  TEXT,
    token_hash      TEXT    UNIQUE,
    consent_version INTEGER,
    consent_fields  TEXT,
    consent_at      TEXT,
    joined_at       TEXT,
    last_seen       TEXT,
    created_at      TEXT    NOT NULL,
    simulated       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id       INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    timestamp       TEXT    NOT NULL,
    application     TEXT    NOT NULL,
    window_title    TEXT    NOT NULL,
    activity_status TEXT    NOT NULL,
    idle_seconds    REAL    NOT NULL,
    cpu_percent     REAL    NOT NULL,
    memory_percent  REAL    NOT NULL,
    battery_percent REAL    NOT NULL,
    charging        INTEGER NOT NULL,
    received_at     TEXT    NOT NULL,
    UNIQUE (member_id, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_observations_member_time
    ON observations (member_id, timestamp);
"""

# Columns added after the first release. init_db() adds any that
# an older database is missing.
MIGRATIONS = {
    "members": {
        "simulated": "INTEGER NOT NULL DEFAULT 0",
    },
}

# PRAGMA user_version after init_db(). Version 1 flags the demo
# members written before the `simulated` column existed.
SCHEMA_VERSION = 1

# Names given to simulated members before they were flagged: the
# team.simulate profiles (an earlier README ran it against the real
# team.db) and the first live demo's "Demo · " copies of them.
# Fixed on purpose: renaming a profile later must not widen this.
_LEGACY_PROFILE_NAMES = (
    "Asha (focused dev)",
    "Ben (focused dev)",
    "Chen (analyst)",
    "Dana (analyst)",
    "Eli (context switcher)",
)
LEGACY_SIMULATED_NAMES = frozenset(
    _LEGACY_PROFILE_NAMES
    + tuple("Demo · " + name for name in _LEGACY_PROFILE_NAMES)
)

# The simulators redeem their invite the moment they create it. A
# real person needs far longer to pass the code to their machine.
LEGACY_SIMULATED_MAX_JOIN_SECONDS = 5

# Member columns plus per-member observation stats. Correlated
# subqueries use idx_observations_member_time, so each one only
# reads that member's slice of the index.
MEMBER_STATS_SQL = """
    SELECT m.*,
           (SELECT COUNT(*) FROM observations o
             WHERE o.member_id = m.id)       AS observation_count,
           (SELECT MIN(o.timestamp) FROM observations o
             WHERE o.member_id = m.id)       AS first_observation,
           (SELECT MAX(o.timestamp) FROM observations o
             WHERE o.member_id = m.id)       AS last_observation
    FROM members m
"""


def utc_now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.isoformat(timespec="seconds")


def hash_secret(secret):
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def new_invite_code():
    # Hex so a code can never start with "-" and be read as a
    # command-line flag by team_sync.py.
    return secrets.token_hex(8)


@contextmanager
def connect(db_file=None):
    db_file = Path(db_file or TEAM_DB_FILE)
    db_file.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_file, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")

    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_file=None):
    with connect(db_file) as conn:
        conn.executescript(SCHEMA)

        for table, columns in MIGRATIONS.items():
            existing = {
                row["name"]
                for row in conn.execute(f"PRAGMA table_info({table})")
            }

            for column, definition in columns.items():
                if column not in existing:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                    )

        version = conn.execute("PRAGMA user_version").fetchone()[0]

        if version < 1:
            _flag_legacy_simulated(conn)

        if version < SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")


def _flag_legacy_simulated(conn):
    """
    Mark simulated members created before the `simulated` column
    existed: a known simulator name AND an invite redeemed within
    seconds of being created. Returns how many were flagged.
    """

    flagged = 0

    rows = conn.execute(
        "SELECT id, name, created_at, joined_at FROM members "
        "WHERE simulated = 0 AND joined_at IS NOT NULL"
    ).fetchall()

    for row in rows:
        if row["name"] not in LEGACY_SIMULATED_NAMES:
            continue

        try:
            gap = (
                datetime.fromisoformat(row["joined_at"])
                - datetime.fromisoformat(row["created_at"])
            ).total_seconds()
        except (TypeError, ValueError):
            continue

        if 0 <= gap <= LEGACY_SIMULATED_MAX_JOIN_SECONDS:
            conn.execute(
                "UPDATE members SET simulated = 1 WHERE id = ?", (row["id"],)
            )
            flagged += 1

    return flagged


# --------------------------------------------------
# Members
# --------------------------------------------------

def create_invite(name, db_file=None, simulated=False):
    """
    Add a pending member and return (member_id, invite_code).

    The code is shown once to the main user, who passes it to
    the member. It expires and can only be redeemed once.
    `simulated` marks members created by a demo or simulator.
    """

    invite_code = new_invite_code()
    now = utc_now()

    with connect(db_file) as conn:
        cursor = conn.execute(
            """
            INSERT INTO members (
                name, invite_hash, invite_expires, created_at, simulated
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                name,
                hash_secret(invite_code),
                _iso(now + timedelta(hours=TEAM_INVITE_TTL_HOURS)),
                _iso(now),
                int(bool(simulated)),
            ),
        )

    return cursor.lastrowid, invite_code


def reissue_invite(member_id, db_file=None):
    """
    Give a member who has not joined yet (pending or expired
    invite) a fresh code and expiry on the same row. Returns the
    new code, or None when the member has already joined or does
    not exist. The old code stops working.
    """

    invite_code = new_invite_code()
    now = utc_now()

    with connect(db_file) as conn:
        cursor = conn.execute(
            """
            UPDATE members
            SET invite_hash = ?, invite_expires = ?
            WHERE id = ? AND token_hash IS NULL
            """,
            (
                hash_secret(invite_code),
                _iso(now + timedelta(hours=TEAM_INVITE_TTL_HOURS)),
                member_id,
            ),
        )

    return invite_code if cursor.rowcount > 0 else None


def redeem_invite(invite_code, consent_version, consent_fields, db_file=None):
    """
    Exchange an invite code plus recorded consent for a member
    token. Returns (member_id, name, token) or None if the code
    is unknown, used or expired.
    """

    now = utc_now()
    token = secrets.token_urlsafe(32)
    invite_hash = hash_secret(invite_code)

    with connect(db_file) as conn:
        row = conn.execute(
            "SELECT id, name, invite_expires FROM members WHERE invite_hash = ?",
            (invite_hash,),
        ).fetchone()

        if row is None:
            return None

        if datetime.fromisoformat(row["invite_expires"]) < now:
            return None

        # Re-check the code in the UPDATE itself: if two requests
        # redeem the same code at once, only one of them matches.
        cursor = conn.execute(
            """
            UPDATE members
            SET invite_hash = NULL,
                invite_expires = NULL,
                token_hash = ?,
                consent_version = ?,
                consent_fields = ?,
                consent_at = ?,
                joined_at = ?
            WHERE id = ? AND invite_hash = ?
            """,
            (
                hash_secret(token),
                consent_version,
                json.dumps(consent_fields),
                _iso(now),
                _iso(now),
                row["id"],
                invite_hash,
            ),
        )

        if cursor.rowcount == 0:
            return None

    return row["id"], row["name"], token


def member_for_token(token, db_file=None):
    if not token:
        return None

    with connect(db_file) as conn:
        row = conn.execute(
            "SELECT * FROM members WHERE token_hash = ?",
            (hash_secret(token),),
        ).fetchone()

    return dict(row) if row else None


def get_member(member_id, db_file=None):
    with connect(db_file) as conn:
        row = conn.execute(
            "SELECT * FROM members WHERE id = ?",
            (member_id,),
        ).fetchone()

    return dict(row) if row else None


def list_members(db_file=None):
    with connect(db_file) as conn:
        rows = conn.execute(
            MEMBER_STATS_SQL + " ORDER BY m.name COLLATE NOCASE, m.id"
        ).fetchall()

    return [dict(row) for row in rows]


def member_with_stats(member_id, db_file=None):
    """
    One member with the same observation stats as list_members().
    """

    with connect(db_file) as conn:
        row = conn.execute(
            MEMBER_STATS_SQL + " WHERE m.id = ?",
            (member_id,),
        ).fetchone()

    return dict(row) if row else None


def member_ids(db_file=None):
    with connect(db_file) as conn:
        rows = conn.execute("SELECT id FROM members ORDER BY id").fetchall()

    return [row["id"] for row in rows]


def member_cohorts(db_file=None):
    """
    {member_id: simulated (bool)} for every member, by id.
    """

    with connect(db_file) as conn:
        rows = conn.execute(
            "SELECT id, simulated FROM members ORDER BY id"
        ).fetchall()

    return {row["id"]: bool(row["simulated"]) for row in rows}


def delete_member(member_id, db_file=None):
    """
    Remove a member and every observation they shared.
    """

    with connect(db_file) as conn:
        conn.execute(
            "DELETE FROM observations WHERE member_id = ?",
            (member_id,),
        )
        cursor = conn.execute(
            "DELETE FROM members WHERE id = ?",
            (member_id,),
        )

    return cursor.rowcount > 0


# --------------------------------------------------
# Observations
# --------------------------------------------------

def insert_observations(member_id, rows, db_file=None):
    """
    Store validated observations. Rows already received (same
    member and timestamp) are ignored, so a collector can safely
    retry an upload. Returns the number of new rows.

    last_seen (what "online" is based on) only moves when the
    batch held at least one valid row, new or already stored; an
    empty or fully rejected upload does not make a member online.
    """

    if not rows:
        return 0

    now = _iso(utc_now())

    with connect(db_file) as conn:
        before = conn.total_changes

        conn.executemany(
            """
            INSERT OR IGNORE INTO observations (
                member_id, timestamp, application, window_title,
                activity_status, idle_seconds, cpu_percent,
                memory_percent, battery_percent, charging, received_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    member_id,
                    row["timestamp"],
                    row["application"],
                    row["window_title"],
                    row["activity_status"],
                    row["idle_seconds"],
                    row["cpu_percent"],
                    row["memory_percent"],
                    row["battery_percent"],
                    int(row["charging"]),
                    now,
                )
                for row in rows
            ],
        )

        inserted = conn.total_changes - before

        conn.execute(
            "UPDATE members SET last_seen = ? WHERE id = ?",
            (now, member_id),
        )

    return inserted


def load_observations(member_id, db_file=None, since=None):
    """
    Return a member's observations in the raw collector CSV
    layout so the existing ETL can process them unchanged.

    `since` ("YYYY-MM-DD HH:MM:SS") keeps only rows at or after
    that time.
    """

    import pandas as pd

    columns = ", ".join(TEAM_SHARED_FIELDS)
    sql = f"SELECT {columns} FROM observations WHERE member_id = ?"
    params = [member_id]

    if since is not None:
        sql += " AND timestamp >= ?"
        params.append(since)

    with connect(db_file) as conn:
        df = pd.read_sql_query(
            sql + " ORDER BY timestamp",
            conn,
            params=params,
        )

    df["charging"] = df["charging"].astype(bool)

    return df


def data_version(db_file=None):
    """
    Changes whenever observations or members change; used to
    cache processed results.
    """

    with connect(db_file) as conn:
        row = conn.execute(
            """
            SELECT (SELECT COALESCE(MAX(id), 0) FROM observations),
                   (SELECT COUNT(*) FROM observations),
                   (SELECT COALESCE(GROUP_CONCAT(id), '')
                      FROM (SELECT id FROM members ORDER BY id))
            """
        ).fetchone()

    return tuple(row)


def member_data_version(member_id, db_file=None, since=None):
    """
    (max id, row count, newest timestamp) of one member's
    observations at or after `since`. Changes when that member
    uploads, or when old rows slide out of the window, and not
    when anyone else uploads.
    """

    sql = (
        "SELECT COALESCE(MAX(id), 0), COUNT(*), MAX(timestamp) "
        "FROM observations WHERE member_id = ?"
    )
    params = [member_id]

    if since is not None:
        sql += " AND timestamp >= ?"
        params.append(since)

    with connect(db_file) as conn:
        row = conn.execute(sql, params).fetchone()

    return tuple(row)


def recent_observations(
    member_id, limit=100, offset=0, db_file=None, *, before=None, after=None
):
    """
    A member's raw observations, newest first. Returns
    (rows, total, has_more), where total counts all of the
    member's rows.

    before / after ("YYYY-MM-DD HH:MM:SS", strict) page by
    timestamp, which stays stable while new rows arrive:
      - before: the `limit` newest rows older than it; has_more
        means older rows remain.
      - after: the `limit` oldest rows newer than it (still
        returned newest first); has_more means newer rows remain.
    offset is still applied for older clients.
    """

    columns = ", ".join(TEAM_SHARED_FIELDS)
    where = "member_id = ?"
    params = [member_id]

    if before is not None:
        where += " AND timestamp < ?"
        params.append(before)

    if after is not None:
        where += " AND timestamp > ?"
        params.append(after)

    # With only `after`, walk forward from it so a client catching
    # up never skips rows.
    order = "ASC" if after is not None and before is None else "DESC"

    with connect(db_file) as conn:
        rows = conn.execute(
            f"SELECT {columns} FROM observations WHERE {where} "
            f"ORDER BY timestamp {order} LIMIT ? OFFSET ?",
            (*params, limit + 1, offset),
        ).fetchall()

        total = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE member_id = ?",
            (member_id,),
        ).fetchone()[0]

    has_more = len(rows) > limit
    rows = rows[:limit]

    if order == "ASC":
        rows.reverse()

    return [
        {**dict(row), "charging": bool(row["charging"])} for row in rows
    ], total, has_more


# --------------------------------------------------
# Live view
# --------------------------------------------------

LIVE_COLUMNS = (
    "timestamp", "application", "window_title", "activity_status",
    "idle_seconds", "cpu_percent", "battery_percent",
)


def live_members(db_file=None):
    """
    Members (no observation stats) plus the newest observation id,
    for the cheap /live freshness check.
    """

    with connect(db_file) as conn:
        members = conn.execute(
            """
            SELECT id, name, invite_expires, token_hash, last_seen, simulated
            FROM members
            ORDER BY name COLLATE NOCASE, id
            """
        ).fetchall()

        newest = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM observations"
        ).fetchone()[0]

    return [dict(row) for row in members], newest


def latest_observations(db_file=None, window_seconds=600):
    """
    For every member with data: their newest observation and
    every observation in the `window_seconds` before it (oldest
    first, newest included). Returns {member_id: (latest, rows)}.

    SQL only, each lookup on idx_observations_member_time.
    """

    columns = ", ".join(LIVE_COLUMNS)
    result = {}

    with connect(db_file) as conn:
        latest_rows = conn.execute(
            f"""
            SELECT m.id AS member_id, {", ".join("o." + c for c in LIVE_COLUMNS)}
            FROM members m
            JOIN observations o ON o.id = (
                SELECT id FROM observations
                WHERE member_id = m.id
                ORDER BY timestamp DESC
                LIMIT 1
            )
            """
        ).fetchall()

        for latest in latest_rows:
            newest = datetime.strptime(latest["timestamp"], "%Y-%m-%d %H:%M:%S")
            start = (newest - timedelta(seconds=window_seconds)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )

            rows = conn.execute(
                f"SELECT {columns} FROM observations "
                "WHERE member_id = ? AND timestamp >= ? "
                "ORDER BY timestamp",
                (latest["member_id"], start),
            ).fetchall()

            latest = dict(latest)
            member_id = latest.pop("member_id")
            result[member_id] = (latest, [dict(row) for row in rows])

    return result
