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
    created_at      TEXT    NOT NULL
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


def utc_now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.isoformat(timespec="seconds")


def hash_secret(secret):
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


@contextmanager
def connect(db_file=None):
    db_file = db_file or TEAM_DB_FILE
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


# --------------------------------------------------
# Members
# --------------------------------------------------

def create_invite(name, db_file=None):
    """
    Add a pending member and return (member_id, invite_code).

    The code is shown once to the main user, who passes it to
    the member. It expires and can only be redeemed once.
    """

    # Hex so a code can never start with "-" and be read as a
    # command-line flag by team_sync.py.
    invite_code = secrets.token_hex(8)
    now = utc_now()

    with connect(db_file) as conn:
        cursor = conn.execute(
            """
            INSERT INTO members (name, invite_hash, invite_expires, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                name,
                hash_secret(invite_code),
                _iso(now + timedelta(hours=TEAM_INVITE_TTL_HOURS)),
                _iso(now),
            ),
        )

    return cursor.lastrowid, invite_code


def redeem_invite(invite_code, consent_version, consent_fields, db_file=None):
    """
    Exchange an invite code plus recorded consent for a member
    token. Returns (member_id, name, token) or None if the code
    is unknown, used or expired.
    """

    now = utc_now()
    token = secrets.token_urlsafe(32)

    with connect(db_file) as conn:
        row = conn.execute(
            "SELECT id, name, invite_expires FROM members WHERE invite_hash = ?",
            (hash_secret(invite_code),),
        ).fetchone()

        if row is None:
            return None

        if datetime.fromisoformat(row["invite_expires"]) < now:
            return None

        conn.execute(
            """
            UPDATE members
            SET invite_hash = NULL,
                invite_expires = NULL,
                token_hash = ?,
                consent_version = ?,
                consent_fields = ?,
                consent_at = ?,
                joined_at = ?
            WHERE id = ?
            """,
            (
                hash_secret(token),
                consent_version,
                json.dumps(consent_fields),
                _iso(now),
                _iso(now),
                row["id"],
            ),
        )

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
            """
            SELECT m.*,
                   COUNT(o.id)      AS observation_count,
                   MIN(o.timestamp) AS first_observation,
                   MAX(o.timestamp) AS last_observation
            FROM members m
            LEFT JOIN observations o ON o.member_id = m.id
            GROUP BY m.id
            ORDER BY m.name COLLATE NOCASE
            """
        ).fetchall()

    return [dict(row) for row in rows]


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
    """

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


def load_observations(member_id, db_file=None):
    """
    Return a member's observations in the raw collector CSV
    layout so the existing ETL can process them unchanged.
    """

    import pandas as pd

    columns = ", ".join(TEAM_SHARED_FIELDS)

    with connect(db_file) as conn:
        df = pd.read_sql_query(
            f"SELECT {columns} FROM observations "
            "WHERE member_id = ? ORDER BY timestamp",
            conn,
            params=(member_id,),
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


def recent_observations(member_id, limit=100, offset=0, db_file=None):
    """
    A member's raw observations, newest first.
    """

    columns = ", ".join(TEAM_SHARED_FIELDS)

    with connect(db_file) as conn:
        rows = conn.execute(
            f"SELECT {columns} FROM observations WHERE member_id = ? "
            "ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (member_id, limit, offset),
        ).fetchall()

        total = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE member_id = ?",
            (member_id,),
        ).fetchone()[0]

    return [
        {**dict(row), "charging": bool(row["charging"])} for row in rows
    ], total
