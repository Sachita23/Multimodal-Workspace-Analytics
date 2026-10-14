from datetime import datetime, timedelta

import pandas as pd

from ml.anomaly_detection import run_anomaly_detection
from team import analysis, db, simulate
from tests.conftest import observation


def _rows(start, minutes, app_cycle, every_seconds=5, idle=0.0):
    t = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
    rows = []
    for i in range(minutes * 60 // every_seconds):
        rows.append(observation(
            t.strftime("%Y-%m-%d %H:%M:%S"),
            application=app_cycle[(i // 12) % len(app_cycle)],
            idle_seconds=idle,
            activity_status="Idle" if idle >= 60 else "Active",
        ))
        t += timedelta(seconds=every_seconds)
    return rows


def _simulated_team(db_file, profiles=None):
    ids = {}
    for seed, (name, profile) in enumerate((profiles or simulate.PROFILES).items()):
        ids[name], _ = simulate.add_simulated_member(
            name, profile, days=2, seed=seed, db_file=db_file
        )
    return ids


# --------------------------------------------------
# Per-member processing
# --------------------------------------------------

def test_members_with_overlapping_times_are_processed_separately(db_file):
    a, _ = simulate.add_simulated_member("A", simulate.PROFILES["Asha (focused dev)"],
                                         days=1, seed=1, db_file=db_file)
    b, _ = simulate.add_simulated_member("B", simulate.PROFILES["Eli (context switcher)"],
                                         days=1, seed=2, db_file=db_file)

    team = analysis.get_team(db_file)
    a_df, b_df = team["processed"][a], team["processed"][b]

    # Same clock times, but each member's switches come only from
    # their own apps: no switch between A's and B's apps.
    assert set(a_df["application"]) & set(b_df["application"]) <= {"Google Chrome"}
    assert a_df["application_switch"].sum() < b_df["application_switch"].sum()

    # Per-member durations are the 5s sampling interval, never 0
    # (which is what interleaving two people's rows would produce).
    assert (a_df["duration_seconds"].iloc[1:] > 0).mean() > 0.99


def test_overnight_gap_is_not_counted_as_active_time(db_file):
    member_id, _ = simulate.add_simulated_member(
        "A", simulate.PROFILES["Asha (focused dev)"], days=2, seed=1, db_file=db_file
    )
    df = analysis.get_team(db_file)["processed"][member_id]

    assert df["duration_seconds"].max() <= 300


def test_total_switches_restart_each_session():
    from etl.session_processor import (
        calculate_application_switches, calculate_total_switches,
    )

    df = pd.DataFrame({
        "application": ["A", "B", "A", "A", "B", "B"],
        "session_id": [0, 0, 0, 1, 1, 1],
    })
    df = calculate_total_switches(calculate_application_switches(df))

    # Session 0 has 2 switches; session 1 has the A->A boundary (0)
    # plus one A->B switch.
    assert df["total_application_switches"].tolist() == [2, 2, 2, 1, 1, 1]


# --------------------------------------------------
# Cross-user ML
# --------------------------------------------------

def test_context_switcher_differs_from_team(db_file):
    ids = _simulated_team(db_file)
    overview = analysis.team_overview(db_file)
    by_name = {m["name"]: m for m in overview["members"]}

    switcher = by_name["Eli (context switcher)"]["comparison"]
    assert switcher["status"] == "differs"
    assert switcher["drivers"][0]["feature"] in {"switch_frequency", "unique_app_count"}
    assert switcher["drivers"][0]["direction"] == "higher"

    for name in ["Asha (focused dev)", "Chen (analyst)", "Dana (analyst)"]:
        assert by_name[name]["comparison"]["status"] == "typical", name

    assert overview["team"]["differs_count"] >= 1
    assert len(ids) == overview["team"]["members"]


def test_behavior_groups_separate_switcher(db_file):
    _simulated_team(db_file)
    overview = analysis.team_overview(db_file)
    by_name = {m["name"]: m for m in overview["members"]}

    assert overview["behavior_groups"]
    assert by_name["Eli (context switcher)"]["dominant_behavior"] == "High-switching"
    assert by_name["Asha (focused dev)"]["dominant_behavior"] != "High-switching"

    for member in overview["members"]:
        if member["behavior_mix"]:
            assert abs(sum(member["behavior_mix"].values()) - 1) < 0.01


def test_too_little_data_is_reported_not_guessed(db_file):
    simulate.add_simulated_member(
        "Only", simulate.PROFILES["Asha (focused dev)"], days=1, seed=1, db_file=db_file
    )
    overview = analysis.team_overview(db_file)

    assert overview["members"][0]["comparison"]["status"] == "insufficient_data"
    assert overview["behavior_groups"] == []


def test_member_with_no_data(db_file):
    db.create_invite("Pending", db_file)
    overview = analysis.team_overview(db_file)

    member = overview["members"][0]
    assert member["status"] == "invited"
    assert member["summary"]["observations"] == 0
    assert member["summary"]["focus_score"] is None


def test_unusual_session_is_flagged():
    rows = []
    day = datetime(2026, 10, 1, 9)
    for d in range(10):
        start = (day + timedelta(days=d)).strftime("%Y-%m-%d %H:%M:%S")
        rows += _rows(start, 60, ["VS Code", "Terminal"])

    # One session switching every sample across many apps.
    apps = ["Slack", "Chrome", "Zoom", "Mail", "Word", "Excel"]
    odd = _rows("2026-10-12 09:00:00", 60, apps)
    for i, row in enumerate(odd):
        row["application"] = apps[i % len(apps)]
    rows += odd

    df = analysis.process_member_observations(pd.DataFrame(rows))
    sessions = analysis.build_sessions(df)

    assert sessions["unusual"].sum() == 1
    assert sessions.loc[sessions["unusual"], "start"].iloc[0].day == 12


def test_member_detail(db_file):
    ids = _simulated_team(db_file)
    detail = analysis.member_detail(ids["Chen (analyst)"], db_file)

    assert detail["name"] == "Chen (analyst)"
    assert len(detail["hourly_activity"]) == 24
    assert detail["top_applications"][0]["application"] == "Microsoft Excel"
    assert detail["top_windows"][0]["window_title"]
    assert detail["sessions"]
    assert all("behavior_group" in s for s in detail["sessions"])

    assert analysis.member_detail(999, db_file) is None


def test_results_are_cached_until_data_changes(db_file):
    ids = _simulated_team(db_file)
    first = analysis.get_team(db_file)
    assert analysis.get_team(db_file) is first

    db.insert_observations(
        ids["Asha (focused dev)"], [observation("2030-01-01 09:00:00")], db_file
    )
    assert analysis.get_team(db_file) is not first


def test_online_status_from_last_seen():
    now = datetime.now().astimezone()
    member = {"token_hash": "x"}

    assert analysis.member_status({}, now) == "invited"
    assert analysis.member_status(member, now) == "joined"
    assert analysis.member_status(
        {**member, "last_seen": (now - timedelta(seconds=30)).isoformat()}, now
    ) == "online"
    assert analysis.member_status(
        {**member, "last_seen": (now - timedelta(hours=1)).isoformat()}, now
    ) == "offline"


# --------------------------------------------------
# Single-user anomaly detection
# --------------------------------------------------

def _feature_frame(n, **columns):
    base = {
        "idle_ratio": [0.1] * n,
        "switch_frequency": [0.2] * n,
        "unique_app_count": [3] * n,
        "avg_cpu": [20.0] * n,
        "avg_memory": [50.0] * n,
    }
    base.update(columns)
    return pd.DataFrame(base)


def test_uniform_behaviour_flags_nothing():
    df = run_anomaly_detection(_feature_frame(100))
    assert not df["is_anomaly"].any()


def test_typical_noise_flags_little_and_outlier_is_flagged():
    import numpy as np

    rng = np.random.default_rng(0)
    n = 300
    df = _feature_frame(
        n,
        idle_ratio=rng.normal(0.15, 0.03, n).clip(0, 1),
        switch_frequency=rng.normal(0.3, 0.05, n),
        unique_app_count=rng.integers(2, 5, n),
        avg_cpu=rng.normal(25, 4, n),
        avg_memory=rng.normal(55, 4, n),
    )
    df.loc[0, ["idle_ratio", "switch_frequency", "unique_app_count"]] = [0.9, 3.0, 15]

    result = run_anomaly_detection(df)

    assert bool(result.loc[0, "is_anomaly"])
    assert result.loc[0, "anomaly_score"] == result["anomaly_score"].max()
    assert result["is_anomaly"].mean() < 0.03
