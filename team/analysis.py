"""
Team analytics.

Each member's observations go through the existing single-user
ETL on their own, so sessions and switches never mix people.
Cross-user ML then works on the pooled session-level features:

    behaviour groups   K-Means over every member's sessions
    differs from team  each member's typical session compared
                       with everyone else's, with the features
                       driving the difference

Real and simulated (demo) members form separate cohorts for both,
so demo members never change what is said about real people.

Focus sessions members choose to share (team_sync.py focus) are
summarised per member: an overall focus score and, while one runs,
what they are doing in it.
"""

import hashlib
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

import config
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
    _kmeans,
    _standardize,
    # The single-user pipeline's labeller (rank plus behaviour
    # floors), so one person's sessions get the same names on the
    # dashboard and the Team page. Clusters that share a label
    # (several mostly-idle ones) are merged into one group below.
    assign_labels,
)
from team import db


# Only the most recent days of observations are analysed, which
# bounds rebuild time and memory. None means all of them.
ANALYSIS_DAYS = getattr(config, "TEAM_ANALYSIS_DAYS", 30)

CLUSTER_COUNT = getattr(config, "DEFAULT_CLUSTER_COUNT", 4)

# Samples further apart than this (laptop asleep, collector
# stopped) are not credited as active or idle time.
MAX_OBSERVATION_GAP_SECONDS = getattr(config, "MAX_OBSERVATION_GAP_SECONDS", 15)

# The live view summarises this much time before each member's
# newest observation.
LIVE_WINDOW_SECONDS = 600

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# A shared focus session counts as live while its collector reported
# it running within this many seconds (it uploads every 5 s).
FOCUS_LIVE_SECONDS = 30

# A running session not heard from for this long was abandoned (the
# terminal closed, the machine slept or crashed): it is shown and
# scored as Interrupted, with its last reported numbers. A later
# upload of the real end (or of the session resuming) still wins.
FOCUS_ABANDONED_SECONDS = 120
FOCUS_INTERRUPTED = "Interrupted"

# The overall focus score averages finished sessions started within
# this many days (config.FOCUS_SCORE_WINDOW_DAYS).
FOCUS_SCORE_DAYS = getattr(config, "FOCUS_SCORE_WINDOW_DAYS", 30)

# Most focus sessions returned for one member.
FOCUS_SESSIONS_LIMIT = 100


# Sessions shorter than this say little about behaviour and are
# left out of the cross-user models (they still count in totals).
MIN_SESSION_SECONDS = getattr(config, "MIN_SESSION_SECONDS", 120)

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

# Real members and simulated (demo) members are analysed as two
# separate cohorts: groups are fitted per cohort and a member is
# only compared with others of the same cohort, so demo members
# never change what the Team page says about real people.
COHORT_REAL = "real"
COHORT_DEMO = "demo"
COHORTS = (COHORT_REAL, COHORT_DEMO)


def cohort_of(simulated):
    return COHORT_DEMO if simulated else COHORT_REAL


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

def _cohorts(pooled):
    """
    Each pooled session's cohort (all real when the frame has no
    cohort column).
    """

    if "cohort" in pooled:
        return pooled["cohort"]

    return pd.Series(COHORT_REAL, index=pooled.index, dtype=object)


def assign_behavior_groups(pooled):
    """
    K-Means over the qualifying sessions of each cohort (real and
    demo members) separately.

    Adds behavior_group to `pooled` and returns
    {cohort: group summaries}; a cohort with too few sessions to
    group gets [].
    """

    pooled["behavior_group"] = None
    cohorts = _cohorts(pooled)

    return {
        cohort: _fit_behavior_groups(pooled, (cohorts == cohort).to_numpy())
        for cohort in COHORTS
    }


def _fit_behavior_groups(pooled, mask):
    """
    Group the sessions of `pooled` selected by `mask`, writing
    their behavior_group. Returns the group summaries.
    """

    if int(mask.sum()) < MIN_SESSIONS_FOR_GROUPS:
        return []

    values = (
        pooled.loc[mask, MODEL_FEATURES]
        .apply(pd.to_numeric, errors="coerce")
        .fillna(0)
        .to_numpy(dtype=float)
    )

    scaled, mean, std = _standardize(values)
    labels, centroids = _kmeans(scaled, min(CLUSTER_COUNT, len(values)))

    names = dict(enumerate(assign_labels(centroids, mean, std)))

    pooled.loc[mask, "behavior_group"] = [names[int(label)] for label in labels]

    groups = []

    for name, members in pooled[mask].groupby("behavior_group"):
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
    Compare a member's typical (median) session with the sessions
    of everyone else in the same cohort (real members with real
    members, demo members with demo members), feature by feature.
    """

    is_mine = (pooled["member_id"] == member_id).to_numpy()
    mine = pooled[is_mine]
    peers = pooled

    if is_mine.any():
        cohorts = _cohorts(pooled).to_numpy()
        peers = pooled[cohorts == cohorts[is_mine][0]]

    others = peers[peers["member_id"] != member_id]

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
    now = now or datetime.now(timezone.utc)

    if not member.get("token_hash"):
        expires = member.get("invite_expires")

        try:
            if expires and datetime.fromisoformat(expires) < now:
                return "expired"
        except (TypeError, ValueError):
            pass

        return "invited"

    if not member.get("last_seen"):
        return "joined"

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
        "simulated": bool(member.get("simulated")),
        "invite_expires": member.get("invite_expires"),
        "joined_at": member.get("joined_at"),
        "consent_at": member.get("consent_at"),
        "last_seen": member.get("last_seen"),
        "observations": member.get("observation_count", 0),
        "first_observation": member.get("first_observation"),
        "last_observation": member.get("last_observation"),
    }


# --------------------------------------------------
# Analysis window
# --------------------------------------------------

def normalize_days(days):
    """
    A positive whole number of days, or None for all data.
    """

    if days is None or days == "all":
        return None

    days = int(days)

    if days <= 0:
        raise ValueError("days must be positive")

    return days


def _local_now():
    return datetime.now()


def analysis_since(days, now=None):
    """
    The oldest local timestamp included for a window of `days`
    (member timestamps are local clock strings), or None.

    The start is rounded down to the whole hour, so a window
    covers `days` plus up to an hour and moves once an hour. A
    start that moved every second would drop a row out of the
    window on every poll for anyone who was working `days` ago at
    this time of day, changing their data version and forcing a
    full rebuild of every such member on each request.
    """

    if days is None:
        return None

    now = now or _local_now()
    start = (now - timedelta(days=days)).replace(
        minute=0, second=0, microsecond=0
    )

    return start.strftime(TIMESTAMP_FORMAT)


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------
# Team build (cached per member)
# --------------------------------------------------

# One state per database file, most recently used last:
#   members  (member_id, days) -> per-member entry
#   teams    days -> pooled sessions and behaviour groups
_cache = OrderedDict()
_cache_lock = threading.Lock()

# Per-key build guards, so building one member never blocks
# readers of another member (or of a different window).
_build_locks = {}

MAX_CACHED_DATABASES = 4


def _db_state(db_file):
    key = str(db_file)

    state = _cache.get(key)

    if state is None:
        state = _cache[key] = {"members": {}, "teams": {}}

        while len(_cache) > MAX_CACHED_DATABASES:
            old_key, _ = _cache.popitem(last=False)
            for lock_key in [k for k in _build_locks if k[0] == old_key]:
                del _build_locks[lock_key]

    _cache.move_to_end(key)

    return key, state


def _build_member(member_id, db_file, since, version):
    df = process_member_observations(
        db.load_observations(member_id, db_file, since=since)
    )
    sessions = build_sessions(df)

    if not sessions.empty:
        sessions.insert(0, "member_id", member_id)

    return {
        "version": version,
        "processed": df,
        "sessions": sessions,
        "summary": summarize_member(df, sessions),
    }


def _member_entry(member_id, db_file, days, since):
    """
    One member's processed frame, sessions and summary, rebuilt
    only when that member's own data (within the window) changes.
    """

    version = db.member_data_version(member_id, db_file, since)
    cache_key = (member_id, days)

    with _cache_lock:
        db_key, state = _db_state(db_file)
        cached = state["members"].get(cache_key)

        if cached is not None and cached["version"] == version:
            return cached

        guard = _build_locks.setdefault((db_key, cache_key), threading.Lock())

    with guard:
        # Another request may have built it while we waited.
        with _cache_lock:
            cached = state["members"].get(cache_key)

        if cached is not None and cached["version"] == version:
            return cached

        entry = _build_member(member_id, db_file, since, version)

        with _cache_lock:
            state["members"][cache_key] = entry

    return entry


def _pool_sessions(member_sessions, simulated):
    """
    Every member's qualifying sessions in one frame, each tagged
    with its member's cohort.
    """

    frames = []

    for member_id, sessions in member_sessions.items():
        qualifying = _qualifying(sessions)

        if not qualifying.empty:
            frames.append(qualifying.assign(
                cohort=cohort_of(simulated.get(member_id))
            ))

    if frames:
        return pd.concat(frames, ignore_index=True)

    return pd.DataFrame(
        columns=["member_id", "session_id", "cohort", "duration_seconds",
                 *MODEL_FEATURES, *COMPARISON_FEATURES]
    )


def get_team(db_file=None, days=ANALYSIS_DAYS, member_ids=None, simulated=None):
    """
    Processed data for every member in the window plus the
    cross-user models (fitted per cohort). Only members whose
    data changed are reprocessed; pooling and grouping are redone
    whenever any member changed.

    `simulated` maps member id -> simulated flag; it is read from
    the database when not given.
    """

    days = normalize_days(days)
    since = analysis_since(days)

    if member_ids is None or simulated is None:
        flags = db.member_cohorts(db_file)

        if member_ids is None:
            member_ids = list(flags)

        if simulated is None:
            simulated = flags

    simulated = {
        member_id: bool(simulated.get(member_id)) for member_id in member_ids
    }

    entries = {
        member_id: _member_entry(member_id, db_file, days, since)
        for member_id in member_ids
    }
    versions = tuple(sorted(
        (member_id, simulated[member_id], entries[member_id]["version"])
        for member_id in member_ids
    ))

    with _cache_lock:
        db_key, state = _db_state(db_file)

        # Forget members that were removed.
        current = set(member_ids)
        for key in [k for k in state["members"] if k[0] not in current]:
            del state["members"][key]
            _build_locks.pop((db_key, key), None)

        cached = state["teams"].get(days)
        if cached is not None and cached["versions"] == versions:
            return cached

    member_sessions = {
        member_id: entry["sessions"] for member_id, entry in entries.items()
    }
    pooled = _pool_sessions(member_sessions, simulated)
    groups = assign_behavior_groups(pooled)

    newest = [
        entry["version"][2] for entry in entries.values() if entry["version"][2]
    ]

    team = {
        "versions": versions,
        "days": days,
        "since": since,
        "data_as_of": max(newest) if newest else None,
        "simulated": simulated,
        "processed": {
            member_id: entry["processed"] for member_id, entry in entries.items()
        },
        "sessions": member_sessions,
        "summaries": {
            member_id: entry["summary"] for member_id, entry in entries.items()
        },
        "data_as_of_by_member": {
            member_id: entry["version"][2] for member_id, entry in entries.items()
        },
        "pooled": pooled,
        # {cohort: group summaries}
        "groups": groups,
    }

    with _cache_lock:
        state["teams"][days] = team

    return team


def _team_totals(rows):
    """
    The team block's counts over the given member rows.
    """

    statuses = [row["status"] for row in rows]
    summaries = [row["summary"] for row in rows]

    active = sum(summary["active_seconds"] for summary in summaries)
    idle = sum(summary["idle_seconds"] for summary in summaries)
    tracked = active + idle

    return {
        "members": len(rows),
        "joined": sum(s not in ("invited", "expired") for s in statuses),
        "sharing": sum(summary["observations"] > 0 for summary in summaries),
        "online": statuses.count("online"),
        "active_seconds": round(active, 1),
        "idle_seconds": round(idle, 1),
        "focus_score": round(active / tracked * 100, 1)
        if tracked > 0 else None,
        "sessions": sum(summary["sessions"] for summary in summaries),
        "observations": sum(summary["observations"] for summary in summaries),
        "differs_count": sum(
            row["comparison"]["status"] == "differs" for row in rows
        ),
    }


def team_overview(db_file=None, days=ANALYSIS_DAYS):
    """
    Every member with their summary, behaviour mix and comparison,
    plus team totals and behaviour groups.

    Whenever the team has at least one real member, the totals
    count real members only and demo (simulated) members are
    reported separately in team.demo_members. The behaviour groups
    are the real members' once any of them has sessions, otherwise
    the demo members'; behavior_groups_cohort says which.
    """

    members = db.list_members(db_file)
    team = get_team(
        db_file,
        days,
        member_ids=[m["id"] for m in members],
        simulated={m["id"]: bool(m.get("simulated")) for m in members},
    )
    pooled = team["pooled"]
    focus = focus_overview(db_file)

    rows = []

    for member in members:
        summary = team["summaries"].get(member["id"]) or summarize_member(
            pd.DataFrame(), pd.DataFrame()
        )

        mix = behavior_mix(member["id"], pooled)

        rows.append({
            **public_member(member),
            "summary": dict(summary),
            "behavior_mix": mix,
            "dominant_behavior": max(mix, key=mix.get) if mix else None,
            "comparison": compare_with_team(member["id"], pooled),
            "focus": focus.get(member["id"]) or empty_focus(),
        })

    real_rows = [row for row in rows if not row["simulated"]]
    demo_rows = [row for row in rows if row["simulated"]]

    def has_sessions(cohort_rows):
        return any(row["summary"]["sessions"] > 0 for row in cohort_rows)

    groups_cohort = (
        COHORT_DEMO
        if not has_sessions(real_rows) and has_sessions(demo_rows)
        else COHORT_REAL
    )

    return {
        "generated_at": _utc_now_iso(),
        "data_as_of": team["data_as_of"],
        "days": "all" if team["days"] is None else team["days"],
        "team": {
            **_team_totals(real_rows or rows),
            "real_members": len(real_rows),
            "demo_members": len(demo_rows),
        },
        "behavior_groups": team["groups"][groups_cohort],
        "behavior_groups_cohort": groups_cohort,
        "members": rows,
    }


def member_detail(member_id, db_file=None, days=ANALYSIS_DAYS):
    member = db.member_with_stats(member_id, db_file)

    if member is None:
        return None

    team = get_team(db_file, days)
    df = team["processed"].get(member_id, pd.DataFrame())
    sessions = team["sessions"].get(member_id, pd.DataFrame())
    pooled = team["pooled"]

    detail = {
        **public_member(member),
        "generated_at": _utc_now_iso(),
        "data_as_of": team["data_as_of_by_member"].get(member_id),
        "days": "all" if team["days"] is None else team["days"],
        "consent_fields": member.get("consent_fields"),
        "summary": dict(
            team["summaries"].get(member_id)
            or summarize_member(df, sessions)
        ),
        "behavior_mix": behavior_mix(member_id, pooled),
        "comparison": compare_with_team(member_id, pooled),
        "focus": focus_overview(db_file, member_id=member_id).get(member_id)
        or empty_focus(),
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


# --------------------------------------------------
# Live view (SQL only, no ETL)
# --------------------------------------------------

def live_signature(db_file=None):
    """
    The members (without stats) and a tag that changes whenever
    the /live response would: a new observation, a member added
    or removed, or a status change such as going offline.
    """

    members, newest_id = db.live_members(db_file)
    focus = focus_overview(db_file)

    for member in members:
        member["focus"] = focus.get(member["id"]) or empty_focus()

    state = [newest_id] + [
        (
            member["id"],
            member["name"],
            member_status(member),
            member["last_seen"],
            bool(member["simulated"]),
            # Includes the live session (remaining time, current app,
            # score), so every focus upload changes the tag.
            sorted_items(member["focus"]),
        )
        for member in members
    ]

    tag = hashlib.sha1(repr(state).encode("utf-8")).hexdigest()

    return members, tag


def sorted_items(value):
    """
    A dict (nested dicts and lists too) as nested sorted tuples, for
    a stable repr.
    """

    if isinstance(value, dict):
        return tuple(sorted((k, sorted_items(v)) for k, v in value.items()))

    if isinstance(value, list):
        return tuple(sorted_items(v) for v in value)

    return value


def live_rollup(rows):
    """
    Active and idle time, app switches and distinct apps over
    observations given oldest first. Each sample is credited
    with the time since the previous one, as in the ETL.
    """

    active = idle = 0.0
    switches = 0
    apps = set()
    previous = None

    for row in rows:
        timestamp = datetime.strptime(row["timestamp"], TIMESTAMP_FORMAT)
        apps.add(row["application"])

        if previous is not None:
            seconds = (timestamp - previous[0]).total_seconds()
            seconds = min(max(seconds, 0.0), MAX_OBSERVATION_GAP_SECONDS)

            if row["activity_status"] == "Active":
                active += seconds
            elif row["activity_status"] == "Idle":
                idle += seconds

            if row["application"] != previous[1]:
                switches += 1

        previous = (timestamp, row["application"])

    return {
        "active_seconds": round(active, 1),
        "idle_seconds": round(idle, 1),
        "switches": switches,
        "distinct_apps": len(apps),
    }


def team_live(db_file=None, members=None):
    """
    What every member is doing right now. `current` is only
    filled in for members who are online; the 10-minute rollup
    is measured back from each member's own newest observation.
    `focus` is the member's focus score and live focus session.
    """

    if members is None:
        members, _ = db.live_members(db_file)

    latest = db.latest_observations(db_file, LIVE_WINDOW_SECONDS)

    # live_signature() already attached each member's focus block.
    focus = (
        None if all("focus" in member for member in members)
        else focus_overview(db_file)
    )
    rows = []

    for member in members:
        status = member_status(member)
        newest, window = latest.get(member["id"], (None, []))

        rows.append({
            "id": member["id"],
            "name": member["name"],
            "status": status,
            "simulated": bool(member["simulated"]),
            "last_seen": member["last_seen"],
            "current": dict(newest) if newest and status == "online" else None,
            "last_10_min": live_rollup(window),
            "focus": member["focus"] if focus is None
            else focus.get(member["id"]) or empty_focus(),
        })

    return {
        "generated_at": _utc_now_iso(),
        "online_window_seconds": TEAM_ONLINE_SECONDS,
        "members": rows,
    }


# --------------------------------------------------
# Focus sessions (shared by members, SQL only)
# --------------------------------------------------

def _parse_utc(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed


def focus_is_live(session, now=None):
    """
    Whether a stored focus session is running right now: reported as
    running, and received within FOCUS_LIVE_SECONDS. The server's own
    receive time is used, not the member's clock, so a member in
    another time zone (or with a wrong clock) still shows live.
    """

    if session.get("status") != db.FOCUS_RUNNING:
        return False

    received = _parse_utc(session.get("received_at"))

    if received is None:
        return False

    now = now or datetime.now(timezone.utc)
    age = (now - received).total_seconds()

    return -FOCUS_LIVE_SECONDS <= age <= FOCUS_LIVE_SECONDS


def focus_since(now=None, days=None):
    """
    Oldest started_at ("YYYY-MM-DDTHH:MM:SS", member local time)
    counted in the overall focus score.
    """

    days = FOCUS_SCORE_DAYS if days is None else days
    now = now or _local_now()

    return (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


def focus_is_abandoned(session, now=None):
    """
    Whether a stored session still reports running but its collector
    has not been heard from for FOCUS_ABANDONED_SECONDS (server time).
    """

    if session.get("status") != db.FOCUS_RUNNING:
        return False

    received = _parse_utc(session.get("received_at"))
    now = now or datetime.now(timezone.utc)

    return received is None or (
        (now - received).total_seconds() > FOCUS_ABANDONED_SECONDS
    )


def focus_status(session, now=None):
    """
    The status to show and score a stored session by: Interrupted
    for an abandoned running one, else as reported.
    """

    if focus_is_abandoned(session, now):
        return FOCUS_INTERRUPTED

    return session.get("status")


def focus_summary(sessions, since=None, now=None):
    """
    {"sessions", "focus_score", "deviation_count", "window_days"} over
    the sessions started at or after `since`: focus_score is the mean
    score (1 decimal) of the finished ones (abandoned ones included,
    see focus_status), or None.
    """

    since = since or focus_since()
    recent = [s for s in sessions if (s.get("started_at") or "") >= since]
    finished = [
        s for s in recent if focus_status(s, now) != db.FOCUS_RUNNING
    ]

    return _summary_block(
        len(recent),
        len(finished),
        sum(s["score"] for s in finished),
        sum(s.get("deviations") or 0 for s in recent),
    )


def _summary_block(sessions, finished, score_sum, deviations):
    return {
        "sessions": int(sessions),
        "focus_score": round(score_sum / finished, 1) if finished else None,
        "deviation_count": int(deviations),
        "window_days": FOCUS_SCORE_DAYS,
    }


def _stats_summary(stats):
    if not stats:
        return _summary_block(0, 0, 0, 0)

    return _summary_block(
        stats["sessions"], stats["finished"], stats["score_sum"],
        stats["deviations"],
    )


def focus_live(session):
    """
    The live block of a running session for the Team page.
    """

    return {
        "running": True,
        "session_id": session["session_id"],
        "title": session["title"],
        "goal": session["goal"],
        "started_at": session["started_at"],
        "planned_minutes": session["planned_minutes"],
        "allowed_apps": list(session["allowed_apps"]),
        "remaining_s": session["remaining_s"],
        "current_app": session["current_app"],
        "allowed": session["current_allowed"],
        "score": session["score"],
        "deviations": session["deviations"],
        "updated_at": session["updated_at"],
    }


def empty_focus():
    return {"score": None, "sessions": 0, "live": None}


def member_focus(sessions, now=None, since=None):
    """
    One member's "focus" block: overall score, number of sessions in
    the score window, and the live session (or None).
    """

    summary = focus_summary(sessions, since, now)
    live = next((s for s in sessions if focus_is_live(s, now)), None)

    return {
        "score": summary["focus_score"],
        "sessions": summary["sessions"],
        "live": focus_live(live) if live else None,
    }


def _live_sessions(db_file, now, member_id=None):
    """
    {member_id: newest live session}, from the running rows received
    within FOCUS_LIVE_SECONDS (an indexed query, not a scan).
    """

    window = timedelta(seconds=FOCUS_LIVE_SECONDS)
    live = {}

    for session in db.running_focus_sessions(
        now - window, now + window, member_id=member_id, db_file=db_file,
    ):
        if focus_is_live(session, now):
            live.setdefault(session["member_id"], session)

    return live


def focus_overview(db_file=None, now=None, member_id=None):
    """
    {member_id: focus block} for every member with focus sessions (or
    just `member_id`). Runs on every /live poll, so the score window is
    aggregated in SQL rather than loaded row by row.
    """

    now = now or datetime.now(timezone.utc)
    stats = db.focus_stats(
        focus_since(),
        now - timedelta(seconds=FOCUS_ABANDONED_SECONDS),
        member_id=member_id,
        db_file=db_file,
    )
    live = _live_sessions(db_file, now, member_id)

    overview = {}

    for mid in set(stats) | set(live):
        summary = _stats_summary(stats.get(mid))
        overview[mid] = {
            "score": summary["focus_score"],
            "sessions": summary["sessions"],
            "live": focus_live(live[mid]) if mid in live else None,
        }

    return overview


def public_focus_session(session, now=None):
    """
    A stored focus session as the member focus endpoint returns it.
    """

    return {
        "session_id": session["session_id"],
        "title": session["title"],
        "goal": session["goal"],
        "planned_minutes": session["planned_minutes"],
        "allowed_apps": list(session["allowed_apps"]),
        "started_at": session["started_at"],
        "ended_at": session["ended_at"],
        "status": focus_status(session, now),
        "live": focus_is_live(session, now),
        "deviations": session["deviations"],
        "deviation_seconds": round(float(session["deviation_seconds"]), 1),
        "allowed_seconds": round(float(session["allowed_seconds"]), 1),
        "score": session["score"],
        "current_app": session["current_app"],
        "current_allowed": session["current_allowed"],
        "remaining_s": session["remaining_s"],
        "updated_at": session["updated_at"],
        "received_at": session["received_at"],
    }


def member_focus_detail(member_id, db_file=None, now=None):
    """
    GET /api/team/members/<id>/focus: the member's newest focus
    sessions plus their summary, or None for an unknown member.
    """

    if db.get_member(member_id, db_file) is None:
        return None

    now = now or datetime.now(timezone.utc)
    sessions = db.focus_sessions(member_id, db_file, limit=FOCUS_SESSIONS_LIMIT)
    stats = db.focus_stats(
        focus_since(),
        now - timedelta(seconds=FOCUS_ABANDONED_SECONDS),
        member_id=member_id,
        db_file=db_file,
    )
    live = _live_sessions(db_file, now, member_id).get(member_id)

    return {
        "generated_at": _utc_now_iso(),
        "sessions": [public_focus_session(session, now) for session in sessions],
        "summary": _stats_summary(stats.get(member_id)),
        "live": focus_live(live) if live else None,
    }
