"""
Single-user ETL steps: raw collector rows -> observation-level
features grouped into workspace sessions.

run_pipeline.build_dashboard_dataset() is the only entry point that
writes the dashboard dataset; team/analysis.py reuses these steps
for each team member.
"""

from datetime import datetime

import pandas as pd

from config import (
    COLLECTION_INTERVAL,
    RAW_DATA_FILE,
    SESSION_IDLE_BREAK_SECONDS,
    SESSION_GAP_SECONDS,
    MAX_OBSERVATION_GAP_SECONDS,
    MIN_SESSION_SECONDS
)

# switch_frequency is switches per minute. A session that is only
# seconds old would report e.g. 12/min after a single switch, so the
# denominator is never smaller than this many minutes.
MIN_SWITCH_FREQUENCY_MINUTES = 5


# Idle growth across a long gap that still means "away the whole time":
# up to this much (macOS idle time does not count sleep) ...
DARK_WAKE_IDLE_GROWTH_SECONDS = 12 * COLLECTION_INTERVAL

# ... or the whole gap minus this slack (Windows counts sleep as idle).
IDLE_GAP_TOLERANCE_SECONDS = 12 * COLLECTION_INTERVAL


# --------------------------------------------------
# Application Names
# --------------------------------------------------

def unify_application_case(applications):
    """
    Map names that differ only in letter case to the spelling that
    appears last (rows are in time order), so an app is counted once.
    """

    key = applications.str.casefold()

    latest = (
        pd.DataFrame({"key": key, "name": applications})
        .drop_duplicates("key", keep="last")
        .set_index("key")["name"]
    )

    return key.map(latest)


# --------------------------------------------------
# Load Raw Data
# --------------------------------------------------

def load_raw_data():

    if not RAW_DATA_FILE.exists():

        raise FileNotFoundError(
            f"Raw dataset not found: "
            f"{RAW_DATA_FILE}"
        )

    df = pd.read_csv(
        RAW_DATA_FILE
    )

    if df.empty:

        raise ValueError(
            "Raw dataset is empty."
        )

    df["timestamp"] = pd.to_datetime(
        df["timestamp"],
        errors="coerce"
    )

    df = df.dropna(
        subset=["timestamp"]
    )

    df = df.sort_values(
        "timestamp"
    ).reset_index(
        drop=True
    )

    return df


# --------------------------------------------------
# Clean Data
# --------------------------------------------------

def clean_data(df):

    df = df.drop_duplicates()

    numeric_columns = [
        "cpu_percent",
        "memory_percent",
        "battery_percent",
        "idle_seconds"
    ]

    for column in numeric_columns:

        if column in df.columns:

            df[column] = pd.to_numeric(
                df[column],
                errors="coerce"
            )

    # CPU and memory
    if "cpu_percent" in df.columns:

        df["cpu_percent"] = (
            df["cpu_percent"]
            .fillna(0)
            .clip(0, 100)
        )

    if "memory_percent" in df.columns:

        df["memory_percent"] = (
            df["memory_percent"]
            .fillna(0)
            .clip(0, 100)
        )

    # Battery
    if "battery_percent" in df.columns:

        df["battery_percent"] = (
            df["battery_percent"]
            .ffill()
            .bfill()
        )

    # Charging
    if "charging" in df.columns:

        df["charging"] = (
            df["charging"]
            .fillna(False)
            .astype(bool)
        )

    # Application
    df["application"] = (
        df["application"]
        .fillna("Unknown")
        .astype(str)
    )

    # The collector's naming was improved (e.g. 'Whatsapp' became
    # 'WhatsApp'), so one app can appear under spellings that differ
    # only in case. Use the most recent spelling for all its rows.
    df["application"] = unify_application_case(df["application"])

    # Window
    df["window_title"] = (
        df["window_title"]
        .fillna("Unknown")
        .astype(str)
    )

    # Activity
    df["activity_status"] = (
        df["activity_status"]
        .fillna("Unknown")
        .astype(str)
    )

    return df.reset_index(
        drop=True
    )


# --------------------------------------------------
# Observation Duration
# --------------------------------------------------

def calculate_time_difference(df):

    df["duration_seconds"] = (
        df["timestamp"]
        .diff()
        .dt.total_seconds()
    )

    df["duration_seconds"] = (
        df["duration_seconds"]
        .fillna(0)
        .clip(lower=0)
    )

    # A gap long enough to start a new session (laptop asleep,
    # collector stopped) is not time spent in the next observation.
    long_gap = (
        df["duration_seconds"] > SESSION_GAP_SECONDS
    )

    # Shorter gaps (sleep, a paused collector) are only credited
    # up to a few sampling intervals: nobody observed that time.
    df["duration_seconds"] = (
        df["duration_seconds"]
        .clip(upper=MAX_OBSERVATION_GAP_SECONDS)
    )

    df.loc[
        long_gap,
        "duration_seconds"
    ] = 0

    return df


# --------------------------------------------------
# Application Switching
# --------------------------------------------------

def calculate_application_switches(df):

    df["previous_application"] = (
        df["application"].shift(1)
    )

    df["application_switch"] = (

        (
            df["application"]
            != df["previous_application"]
        )

        &

        df["previous_application"].notna()
    )

    return df


# --------------------------------------------------
# Window Switching
# --------------------------------------------------

def calculate_window_switches(df):

    df["previous_window"] = (
        df["window_title"].shift(1)
    )

    df["window_switch"] = (

        (
            df["window_title"]
            != df["previous_window"]
        )

        &

        df["previous_window"].notna()
    )

    return df


# --------------------------------------------------
# Active / Idle Duration
# --------------------------------------------------

def calculate_activity_duration(df):

    df["active_duration_seconds"] = 0.0

    df["idle_duration_seconds"] = 0.0

    active_mask = (
        df["activity_status"]
        .str.lower()
        == "active"
    )

    idle_mask = (
        df["activity_status"]
        .str.lower()
        == "idle"
    )

    df.loc[
        active_mask,
        "active_duration_seconds"
    ] = df.loc[
        active_mask,
        "duration_seconds"
    ]

    df.loc[
        idle_mask,
        "idle_duration_seconds"
    ] = df.loc[
        idle_mask,
        "duration_seconds"
    ]

    return df


# --------------------------------------------------
# Create Sessions
# --------------------------------------------------

def create_sessions(df):
    """
    Creates workspace-level sessions.

    A new session starts when:
    1. The timestamp gap becomes unusually large (laptop asleep,
       collector stopped), unless idle time kept growing across
       it (the user is still away), OR
    2. The user comes back after prolonged inactivity: idle time
       resets after an idle stretch that lasted at least
       SESSION_IDLE_BREAK_SECONDS or that had such a gap in it.

    While the user is away, a sleeping laptop still wakes up now
    and then (macOS dark wakes, Power Nap) and the collector logs
    a few samples. Idle time keeps growing across those gaps, so
    they continue the break instead of opening a new, empty
    session each time; the session splits when the user returns.

    The idle stretch itself stays in the session it ended, so a
    30-minute break gives two sessions, not one per sample.

    Application switching alone DOES NOT create
    a new workspace session.
    """

    df = df.copy()

    # Time between current and previous observation
    gap_seconds = (
        df["timestamp"]
        .diff()
        .dt.total_seconds()
        .fillna(0)
    )

    # An unreadable sample keeps the last known idle time, so it
    # neither hides nor fakes a return to the keyboard.
    idle = df["idle_seconds"].ffill()

    # Previous row's idle duration
    previous_idle = idle.shift(1)

    # The user touched the keyboard or mouse since the previous sample.
    idle_reset = idle < previous_idle

    long_gap = gap_seconds > SESSION_GAP_SECONDS

    # No input across the gap: idle time kept growing, by about the
    # whole gap (Windows counts sleep as idle) or by only a sample or
    # a few (macOS does not, so a dark wake adds ~one interval). Any
    # growth in between means the user was at the computer while no
    # samples were taken (collector stopped), so the gap splits.
    # Strictly positive, so a collector that cannot read idle time
    # (always 0) still splits on gaps.
    idle_growth = idle - previous_idle
    still_away = long_gap & (idle_growth > 0) & (
        (idle_growth <= DARK_WAKE_IDLE_GROWTH_SECONDS)
        |
        (idle_growth >= gap_seconds - IDLE_GAP_TOLERANCE_SECONDS)
    )

    # Rows between two idle resets form one idle stretch. Did the
    # stretch that ended on the previous row span a long gap?
    stretch = idle_reset.cumsum()

    break_spanned_gap = (
        still_away
        .groupby(stretch)
        .transform("any")
        .shift(1, fill_value=False)
        .astype(bool)
    )

    returned_from_break = idle_reset & (
        (previous_idle >= SESSION_IDLE_BREAK_SECONDS)
        |
        break_spanned_gap
    )

    new_session = (
        (long_gap & ~still_away)
        |
        returned_from_break
    )

    # Always treat first row as first session
    if len(new_session) > 0:
        new_session.iloc[0] = True

    df["session_id"] = (
        new_session
        .astype(int)
        .cumsum()
        - 1
    )

    df = reset_switches_at_session_start(df)

    return df


def reset_switches_at_session_start(df):
    """
    Opening a new session is not a switch: the previous app
    belongs to another session, so the first row of each
    session never counts as an application or window switch.
    """

    session_start = (
        df["session_id"]
        != df["session_id"].shift(1)
    )

    for column in ("application_switch", "window_switch"):

        if column in df.columns:

            df.loc[
                session_start,
                column
            ] = False

    return df


# --------------------------------------------------
# Total Application Switches
# --------------------------------------------------

def calculate_total_switches(df):
    """
    Total application switches within each workspace
    session (not a running count across all sessions).
    """

    df["total_application_switches"] = (
        df.groupby("session_id")["application_switch"]
        .transform("sum")
        .astype(int)
    )

    return df


# --------------------------------------------------
# Behavioral Features
# --------------------------------------------------

def calculate_behavior_features(df):
    """
    Creates session-context behavioral features.

    The final values remain observation-level so
    they match the dashboard dataset, but features
    describe the workspace session containing
    each observation.
    """

    df = df.copy()

    # --------------------------------------------------
    # Session Total Duration
    # --------------------------------------------------

    session_total_duration = (
        df.groupby("session_id")[
            "duration_seconds"
        ]
        .transform("sum")
    )

    # --------------------------------------------------
    # Session Idle Duration
    # --------------------------------------------------

    session_idle_duration = (
        df.groupby("session_id")[
            "idle_duration_seconds"
        ]
        .transform("sum")
    )

    # --------------------------------------------------
    # Idle Ratio
    # --------------------------------------------------

    safe_total_duration = (
        session_total_duration
        .replace(0, 1)
    )

    # A session with no measured time (a single sample, or samples
    # right after a gap) falls back to its share of Idle samples, so
    # an idle-only session never reports an idle ratio of 0.
    idle_sample_share = (
        df["activity_status"]
        .str.lower()
        .eq("idle")
        .groupby(df["session_id"])
        .transform("mean")
    )

    df["idle_ratio"] = (
        (session_idle_duration / safe_total_duration)
        .where(
            session_total_duration > 0,
            idle_sample_share
        )
    )

    df["idle_ratio"] = (
        df["idle_ratio"]
        .astype(float)
        .clip(0, 1)
        .round(4)
    )

    # --------------------------------------------------
    # Number of Switches Per Session
    # --------------------------------------------------

    session_switches = (
        df.groupby("session_id")[
            "application_switch"
        ]
        .transform("sum")
    )

    # --------------------------------------------------
    # Switch Frequency
    # switches per minute
    # --------------------------------------------------

    session_minutes = (
        session_total_duration / 60
    ).clip(
        lower=MIN_SWITCH_FREQUENCY_MINUTES
    )

    df["switch_frequency"] = (
        session_switches
        / session_minutes
    ).round(4)

    # --------------------------------------------------
    # Unique Applications Within Session
    # --------------------------------------------------

    df["unique_app_count"] = (
        df.groupby("session_id")[
            "application"
        ]
        .transform("nunique")
    )

    # --------------------------------------------------
    # Session Average CPU
    # --------------------------------------------------

    df["avg_cpu"] = (
        df.groupby("session_id")[
            "cpu_percent"
        ]
        .transform("mean")
        .round(2)
    )

    # --------------------------------------------------
    # Session Average Memory
    # --------------------------------------------------

    df["avg_memory"] = (
        df.groupby("session_id")[
            "memory_percent"
        ]
        .transform("mean")
        .round(2)
    )

    # --------------------------------------------------
    # Temporal Features
    # --------------------------------------------------

    df["hour"] = (
        df["timestamp"].dt.hour
    )

    df["day_of_week"] = (
        df["timestamp"].dt.day_name()
    )

    df["is_weekend"] = (
        df["timestamp"].dt.dayofweek >= 5
    )

    return df


# --------------------------------------------------
# One Row Per Session (for the ML steps)
# --------------------------------------------------

def build_session_table(df, columns, now=None):
    """
    One row per session_id with the session-level `columns`
    (constant within a session), plus:

        start, end, duration_seconds
        is_open   the last session, when a new observation
                  could still extend it
        eligible  long enough and finished, so it may be used
                  to fit models and be flagged

    `now` defaults to the current local time.
    """

    grouped = df.groupby(
        "session_id",
        sort=True
    )

    table = grouped[list(columns)].first()

    table["start"] = grouped["timestamp"].min()
    table["end"] = grouped["timestamp"].max()

    if "duration_seconds" in df.columns:

        table["duration_seconds"] = (
            grouped["duration_seconds"].sum()
        )

    else:

        table["duration_seconds"] = (
            table["end"] - table["start"]
        ).dt.total_seconds()

    table["is_open"] = False

    if len(table) > 0:

        now = pd.Timestamp(now or datetime.now())

        last_end = table["end"].iloc[-1]

        # A later sample within SESSION_GAP_SECONDS would still
        # belong to the last session, so it may keep changing.
        if (now - last_end).total_seconds() <= SESSION_GAP_SECONDS:
            table.iloc[-1, table.columns.get_loc("is_open")] = True

    table["eligible"] = (
        (table["duration_seconds"] >= MIN_SESSION_SECONDS)
        &
        ~table["is_open"]
    )

    return table
