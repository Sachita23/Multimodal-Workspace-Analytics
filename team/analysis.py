"""
Team analytics.

Each member's observations go through the existing single-user
ETL on their own, so sessions and switches never mix people.
Cross-user ML then works on the pooled session-level features:

    behaviour groups   K-Means over every member's sessions
    differs from team  each member's typical session compared
                       with everyone else's, with the features
                       driving the difference
"""

import threading
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from config import TEAM_ONLINE_SECONDS
from etl.session_processor import (
    clean_data,
    calculate_time_difference,
    calculate_application_switches,
    calculate_window_switches,
    calculate_activity_duration,
    create_sessions,
    calculate_total_switches,
    calculate_behavior_features,
)
from ml.anomaly_detection import ANOMALY_DISTANCE, robust_scale
from ml.clustering import (
    MODEL_FEATURES,
    _assign_behavior_label,
    _kmeans,
    _standardize,
)
from team import db


# Sessions shorter than this say little about behaviour and are
# left out of the cross-user models (they still count in totals).
MIN_SESSION_SECONDS = 120

MIN_SESSIONS_FOR_GROUPS = 8

# "Differs from team" needs enough history on both sides.
MIN_MEMBER_SESSIONS = 3
MIN_OTHER_SESSIONS = 5

# Robust z-score at which a feature counts as different.
DIFFERENCE_THRESHOLD = 2.0

# Behavioural features compared between people. CPU and memory
# describe the machine rather than the person, so they are used
# for grouping but not for "differs from team".
COMPARISON_FEATURES = {
    "idle_ratio": "Idle share of session",
    "switch_frequency": "App switches per minute",
    "unique_app_count": "Apps used per session",
    "duration_minutes": "Session length (min)",
}


# --------------------------------------------------
# Per-member processing
# --------------------------------------------------

def process_member_observations(raw):
    """
    Run the single-user ETL on one member's raw observations.
    """

    if raw.empty:
        return raw

    df = raw.copy()

    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)

    if df.empty:
        return df

    df = clean_data(df)
    df = calculate_time_difference(df)
    df = calculate_application_switches(df)
    df = calculate_window_switches(df)
    df = calculate_activity_duration(df)
    df = create_sessions(df)
    df = calculate_total_switches(df)
    df = calculate_behavior_features(df)

    return df


def build_sessions(df):
    """
    One row per workspace session.
    """

    if df.empty:
        return pd.DataFrame()

    grouped = df.groupby("session_id")

    sessions = pd.DataFrame({
        "start": grouped["timestamp"].min(),
        "end": grouped["timestamp"].max(),
        "duration_seconds": grouped["duration_seconds"].sum(),
        "active_seconds": grouped["active_duration_seconds"].sum(),
        "idle_seconds": grouped["idle_duration_seconds"].sum(),
        "idle_ratio": grouped["idle_ratio"].first(),
        "switch_frequency": grouped["switch_frequency"].first(),
        "unique_app_count": grouped["unique_app_count"].first(),
        "avg_cpu": grouped["avg_cpu"].first(),
        "avg_memory": grouped["avg_memory"].first(),
        "switches": grouped["total_application_switches"].first(),
        "top_application": grouped.apply(
            _top_application, include_groups=False
        ),
    }).reset_index()

    sessions["duration_minutes"] = sessions["duration_seconds"] / 60
    sessions["unusual"] = flag_unusual_sessions(sessions)

    return sessions


def flag_unusual_sessions(sessions):
    """
    Mark sessions far from the member's own typical session.

    Scored per session rather than per observation, so long
    sessions don't dominate what counts as typical.
    """

    unusual = pd.Series(False, index=sessions.index)
    qualifying = sessions["duration_seconds"] >= MIN_SESSION_SECONDS

    if qualifying.sum() < MIN_SESSIONS_FOR_GROUPS:
        return unusual

    values = sessions.loc[qualifying, list(COMPARISON_FEATURES)].to_numpy(dtype=float)
    distances = np.sqrt(np.mean(robust_scale(values) ** 2, axis=1))
    unusual[qualifying] = distances >= ANOMALY_DISTANCE

    return unusual


def _top_application(group):
    totals = group.groupby("application")["active_duration_seconds"].sum()

    if totals.empty or totals.max() <= 0:
        return group["application"].mode().iat[0]

    return totals.idxmax()


def _qualifying(sessions):
    if sessions.empty:
        return sessions

    return sessions[sessions["duration_seconds"] >= MIN_SESSION_SECONDS]


def summarize_member(df, sessions):
    if df.empty:
        return {
            "observations": 0,
            "sessions": 0,
            "active_seconds": 0.0,
            "idle_seconds": 0.0,
            "focus_score": None,
            "days_active": 0,
            "active_hours_per_day": 0.0,
            "top_application": None,
            "application_switches": 0,
            "window_switches": 0,
            "unusual_sessions": 0,
        }

    active = float(df["active_duration_seconds"].sum())
    idle = float(df["idle_duration_seconds"].sum())
    days = int(df["timestamp"].dt.date.nunique())

    by_app = df.groupby("application")["active_duration_seconds"].sum()

    return {
        "observations": int(len(df)),
        "sessions": int(len(sessions)),
        "active_seconds": round(active, 1),
        "idle_seconds": round(idle, 1),
        "focus_score": round(active / (active + idle) * 100, 1)
        if active + idle > 0 else None,
        "days_active": days,
        "active_hours_per_day": round(active / 3600 / max(days, 1), 2),
        "top_application": by_app.idxmax() if by_app.max() > 0 else None,
        "application_switches": int(df["application_switch"].sum()),
        "window_switches": int(df["window_switch"].sum()),
        "unusual_sessions": int(sessions["unusual"].sum()),
    }


# --------------------------------------------------
# Cross-user ML
# --------------------------------------------------

def assign_behavior_groups(pooled):
    """
    K-Means over every member's qualifying sessions.

    Adds behavior_group to `pooled` and returns group summaries.
    Returns [] when there are too few sessions to group.
    """

    pooled["behavior_group"] = None

    if len(pooled) < MIN_SESSIONS_FOR_GROUPS:
        return []

    values = (
        pooled[MODEL_FEATURES]
        .apply(pd.to_numeric, errors="coerce")
        .fillna(0)
        .to_numpy(dtype=float)
    )

    scaled, mean, std = _standardize(values)
    labels, centroids = _kmeans(scaled, min(4, len(values)))

    names = {
        cluster_id: _assign_behavior_label(center, mean, std)
        for cluster_id, center in enumerate(centroids)
    }

    pooled["behavior_group"] = [names[int(label)] for label in labels]

    groups = []

    for name, members in pooled.groupby("behavior_group"):
        groups.append({
            "label": name,
            "sessions": int(len(members)),
            "members": int(members["member_id"].nunique()),
            "avg_idle_ratio": round(float(members["idle_ratio"].mean()), 3),
            "avg_switch_frequency": round(
                float(members["switch_frequency"].mean()), 3
            ),
            "avg_unique_apps": round(
                float(members["unique_app_count"].mean()), 2
            ),
            "avg_duration_minutes": round(
                float(members["duration_minutes"].mean()), 1
            ),
        })

    return sorted(groups, key=lambda group: -group["sessions"])


def _robust_spread(values):
    values = np.asarray(values, dtype=float)

    q75, q25 = np.percentile(values, [75, 25])
    spread = (q75 - q25) / 1.349

    if spread <= 0:
        spread = np.median(np.abs(values - np.median(values))) * 1.4826

    if spread <= 0:
        spread = values.std()

    if spread <= 0:
        spread = max(abs(float(np.median(values))) * 0.1, 1e-6)

    return float(spread)


def compare_with_team(member_id, pooled):
    """
    Compare a member's typical (median) session with everyone
    else's sessions, feature by feature.
    """

    mine = pooled[pooled["member_id"] == member_id]
    others = pooled[pooled["member_id"] != member_id]

    if (
        len(mine) < MIN_MEMBER_SESSIONS
        or len(others) < MIN_OTHER_SESSIONS
        or others["member_id"].nunique() < 1
    ):
        return {
            "status": "insufficient_data",
            "score": None,
            "drivers": [],
            "features": [],
        }

    features = []

    for column, label in COMPARISON_FEATURES.items():
        member_value = float(mine[column].median())
        team_value = float(others[column].median())
        z = (member_value - team_value) / _robust_spread(others[column])

        features.append({
            "feature": column,
            "label": label,
            "member_value": round(member_value, 3),
            "team_value": round(team_value, 3),
            "z_score": round(float(z), 2),
            "direction": "higher" if z > 0 else "lower",
        })

    drivers = sorted(
        (f for f in features if abs(f["z_score"]) >= DIFFERENCE_THRESHOLD),
        key=lambda f: -abs(f["z_score"]),
    )

    return {
        "status": "differs" if drivers else "typical",
        "score": round(max(abs(f["z_score"]) for f in features), 2),
        "drivers": drivers,
        "features": features,
    }


def behavior_mix(member_id, pooled):
    mine = pooled[
        (pooled["member_id"] == member_id)
        & pooled["behavior_group"].notna()
    ]

    if mine.empty:
        return {}

    shares = (
        mine.groupby("behavior_group")["duration_seconds"].sum()
        / mine["duration_seconds"].sum()
    )

    return {label: round(float(share), 3) for label, share in shares.items()}


# --------------------------------------------------
# Status
# --------------------------------------------------

def member_status(member, now=None):
    if not member.get("token_hash"):
        return "invited"

    if not member.get("last_seen"):
        return "joined"

    now = now or datetime.now(timezone.utc)
    last_seen = datetime.fromisoformat(member["last_seen"])

    if (now - last_seen).total_seconds() <= TEAM_ONLINE_SECONDS:
        return "online"

    return "offline"


def public_member(member):
    """
    Member fields safe to send to the Team page.
    """

    return {
        "id": member["id"],
        "name": member["name"],
        "status": member_status(member),
        "invite_expires": member.get("invite_expires"),
        "joined_at": member.get("joined_at"),
        "consent_at": member.get("consent_at"),
        "last_seen": member.get("last_seen"),
        "observations": member.get("observation_count", 0),
        "first_observation": member.get("first_observation"),
        "last_observation": member.get("last_observation"),
    }


# --------------------------------------------------
# Team build (cached on data version)
# --------------------------------------------------

_cache = {}
_cache_lock = threading.Lock()


def _build_team(db_file):
    members = db.list_members(db_file)

    processed = {}
    member_sessions = {}

    for member in members:
        df = process_member_observations(
            db.load_observations(member["id"], db_file)
        )
        processed[member["id"]] = df
        sessions = build_sessions(df)
        if not sessions.empty:
            sessions.insert(0, "member_id", member["id"])
        member_sessions[member["id"]] = sessions

    frames = [_qualifying(s) for s in member_sessions.values() if not s.empty]
    pooled = (
        pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
            columns=["member_id", "duration_seconds", *MODEL_FEATURES,
                     *COMPARISON_FEATURES]
        )
    )

    groups = assign_behavior_groups(pooled)

    return {
        "processed": processed,
        "sessions": member_sessions,
        "pooled": pooled,
        "groups": groups,
    }


def get_team(db_file=None):
    key = (str(db_file), db.data_version(db_file))

    with _cache_lock:
        if key not in _cache:
            _cache.clear()
            _cache[key] = _build_team(db_file)

        return _cache[key]


def team_overview(db_file=None):
    team = get_team(db_file)
    members = db.list_members(db_file)
    pooled = team["pooled"]

    rows = []
    totals = {"active": 0.0, "idle": 0.0, "sessions": 0, "observations": 0}

    for member in members:
        df = team["processed"].get(member["id"], pd.DataFrame())
        sessions = team["sessions"].get(member["id"], pd.DataFrame())
        summary = summarize_member(df, sessions)

        totals["active"] += summary["active_seconds"]
        totals["idle"] += summary["idle_seconds"]
        totals["sessions"] += summary["sessions"]
        totals["observations"] += summary["observations"]

        mix = behavior_mix(member["id"], pooled)

        rows.append({
            **public_member(member),
            "summary": summary,
            "behavior_mix": mix,
            "dominant_behavior": max(mix, key=mix.get) if mix else None,
            "comparison": compare_with_team(member["id"], pooled),
        })

    statuses = [row["status"] for row in rows]
    tracked = totals["active"] + totals["idle"]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "team": {
            "members": len(rows),
            "joined": sum(s != "invited" for s in statuses),
            "online": statuses.count("online"),
            "active_seconds": round(totals["active"], 1),
            "idle_seconds": round(totals["idle"], 1),
            "focus_score": round(totals["active"] / tracked * 100, 1)
            if tracked > 0 else None,
            "sessions": totals["sessions"],
            "observations": totals["observations"],
            "differs_count": sum(
                row["comparison"]["status"] == "differs" for row in rows
            ),
        },
        "behavior_groups": team["groups"],
        "members": rows,
    }


def member_detail(member_id, db_file=None):
    member = next(
        (m for m in db.list_members(db_file) if m["id"] == member_id), None
    )

    if member is None:
        return None

    team = get_team(db_file)
    df = team["processed"].get(member_id, pd.DataFrame())
    sessions = team["sessions"].get(member_id, pd.DataFrame())
    pooled = team["pooled"]

    detail = {
        **public_member(member),
        "consent_fields": member.get("consent_fields"),
        "summary": summarize_member(df, sessions),
        "behavior_mix": behavior_mix(member_id, pooled),
        "comparison": compare_with_team(member_id, pooled),
        "hourly_activity": [],
        "top_applications": [],
        "top_windows": [],
        "sessions": [],
    }

    if df.empty:
        return detail

    hourly = (
        df.groupby("hour")[["active_duration_seconds", "idle_duration_seconds"]]
        .sum()
        .reindex(range(24), fill_value=0)
    )
    detail["hourly_activity"] = [
        {
            "hour": int(hour),
            "active_minutes": round(float(row["active_duration_seconds"]) / 60, 1),
            "idle_minutes": round(float(row["idle_duration_seconds"]) / 60, 1),
        }
        for hour, row in hourly.iterrows()
    ]

    apps = (
        df.groupby("application")
        .agg(
            active_seconds=("active_duration_seconds", "sum"),
            switches_in=("application_switch", "sum"),
        )
        .sort_values("active_seconds", ascending=False)
        .head(10)
    )
    detail["top_applications"] = [
        {
            "application": app,
            "active_minutes": round(float(row["active_seconds"]) / 60, 1),
            "switches_in": int(row["switches_in"]),
        }
        for app, row in apps.iterrows()
    ]

    windows = (
        df.groupby(["application", "window_title"])["active_duration_seconds"]
        .sum()
        .sort_values(ascending=False)
        .head(15)
    )
    detail["top_windows"] = [
        {
            "application": app,
            "window_title": title,
            "active_minutes": round(float(seconds) / 60, 1),
        }
        for (app, title), seconds in windows.items()
    ]

    pooled_mine = pooled[pooled["member_id"] == member_id].set_index("session_id")
    recent = sessions.sort_values("start", ascending=False).head(50)

    detail["sessions"] = [
        {
            "session_id": int(row["session_id"]),
            "start": row["start"].isoformat(sep=" "),
            "end": row["end"].isoformat(sep=" "),
            "duration_minutes": round(float(row["duration_minutes"]), 1),
            "active_minutes": round(float(row["active_seconds"]) / 60, 1),
            "idle_ratio": round(float(row["idle_ratio"]), 3),
            "switches": int(row["switches"]),
            "unique_apps": int(row["unique_app_count"]),
            "top_application": row["top_application"],
            "behavior_group": pooled_mine["behavior_group"].get(row["session_id"])
            if "behavior_group" in pooled_mine else None,
            "unusual": bool(row["unusual"]),
        }
        for _, row in recent.iterrows()
    ]

    return detail
