from datetime import datetime, timedelta

import pandas as pd

from config import TEAM_CONSENT_VERSION, TEAM_SHARED_FIELDS
from ml.anomaly_detection import run_anomaly_detection
from ml.clustering import label_fits
from team import analysis, db, simulate
from tests.conftest import observation


# Labels (ml.clustering.assign_labels) that describe a switcher's
# sessions: many apps and frequent switching fit both, and K-Means
# may split one person's near-identical sessions between them.
SWITCHING_LABELS = {"High-switching", "Multitasking"}


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
    assert overview["behavior_groups_cohort"] == "demo"
    labels = [group["label"] for group in overview["behavior_groups"]]
    assert len(labels) == len(set(labels))

    eli = by_name["Eli (context switcher)"]
    assert eli["dominant_behavior"] in SWITCHING_LABELS
    assert set(eli["behavior_mix"]) <= SWITCHING_LABELS
    assert "High-switching" in eli["behavior_mix"]
    assert not set(by_name["Asha (focused dev)"]["behavior_mix"]) & SWITCHING_LABELS

    # Same labelling as the single-user pipeline: every group shows
    # the behaviour its name describes.
    for group in overview["behavior_groups"]:
        assert label_fits(
            group["label"],
            group["avg_idle_ratio"],
            group["avg_switch_frequency"],
            group["avg_unique_apps"],
        ), group

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


def test_upload_reprocesses_only_that_member(db_file, monkeypatch):
    ids = _simulated_team(db_file)
    calls = []
    real = analysis.process_member_observations

    def spy(raw):
        calls.append(len(raw))
        return real(raw)

    monkeypatch.setattr(analysis, "process_member_observations", spy)

    analysis.team_overview(db_file)
    assert len(calls) == len(ids)

    calls.clear()
    analysis.team_overview(db_file)
    analysis.member_detail(ids["Chen (analyst)"], db_file)
    assert calls == []

    asha = ids["Asha (focused dev)"]
    db.insert_observations(asha, [observation(
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )], db_file)

    overview = analysis.team_overview(db_file)
    assert len(calls) == 1
    by_id = {m["id"]: m for m in overview["members"]}
    assert by_id[asha]["summary"]["observations"] == calls[0]

    # The groups are rebuilt from the cached sessions of everyone.
    assert overview["behavior_groups"]
    assert by_name_dominant(overview, "Eli (context switcher)") in SWITCHING_LABELS

    # A removed member is dropped from the next build.
    calls.clear()
    db.delete_member(ids["Dana (analyst)"], db_file)
    overview = analysis.team_overview(db_file)
    assert calls == []
    assert len(overview["members"]) == len(ids) - 1


def by_name_dominant(overview, name):
    return {m["name"]: m["dominant_behavior"] for m in overview["members"]}[name]


def test_days_window_limits_the_analysis(db_file):
    member_id, _ = db.create_invite("A", db_file)
    now = datetime.now().replace(microsecond=0)
    old = _rows((now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S"), 10, ["A", "B"])
    recent = _rows((now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"), 10, ["A", "B"])
    db.insert_observations(member_id, old + recent, db_file)

    everything = analysis.team_overview(db_file, days=None)
    last_day = analysis.team_overview(db_file, days=1)

    assert everything["days"] == "all"
    assert last_day["days"] == 1
    assert everything["team"]["observations"] == len(old) + len(recent)
    assert last_day["team"]["observations"] == len(recent)
    assert last_day["data_as_of"] == everything["data_as_of"] == recent[-1]["timestamp"]

    since = analysis.analysis_since(1)
    df = analysis.get_team(db_file, days=1)["processed"][member_id]
    assert df["timestamp"].min() >= pd.Timestamp(since)

    detail = analysis.member_detail(member_id, db_file, days=1)
    assert detail["summary"]["observations"] == len(recent)
    assert detail["data_as_of"] == recent[-1]["timestamp"]


def _spy_on_rebuilds(monkeypatch):
    calls = []
    real = analysis.process_member_observations

    def spy(raw):
        calls.append(len(raw))
        return real(raw)

    monkeypatch.setattr(analysis, "process_member_observations", spy)
    return calls


def test_window_start_moves_once_an_hour():
    now = datetime(2026, 10, 15, 14, 37, 12)

    assert analysis.analysis_since(1, now) == "2026-10-14 14:00:00"
    assert analysis.analysis_since(7, now) == "2026-10-08 14:00:00"
    assert analysis.analysis_since(None, now) is None
    assert analysis.analysis_since(1, now + timedelta(minutes=20)) == "2026-10-14 14:00:00"


def test_polls_do_not_rebuild_members_with_data_at_the_window_edge(db_file, monkeypatch):
    # Someone who was working exactly a day ago: with a window that
    # starts `days` ago to the second, a row slid out on every poll
    # and every poll rebuilt them.
    t0 = datetime.now().replace(minute=10, second=0, microsecond=0)
    edge = t0 - timedelta(days=1)
    member_id, _ = db.create_invite("Edge", db_file)
    db.insert_observations(member_id, _rows(
        (edge - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"), 60, ["A", "B"]
    ), db_file)

    clock = [t0]
    monkeypatch.setattr(analysis, "_local_now", lambda: clock[0])
    calls = _spy_on_rebuilds(monkeypatch)

    analysis.team_overview(db_file, days=1)
    assert len(calls) == 1

    for seconds in (6, 12, 45 * 60):
        clock[0] = t0 + timedelta(seconds=seconds)
        calls.clear()
        analysis.team_overview(db_file, days=1)
        analysis.member_detail(member_id, db_file, days=1)
        assert calls == [], f"rebuilt {seconds} s later"

    # The next hour the window moves on once: one rebuild, then cached.
    clock[0] = t0 + timedelta(hours=1)
    calls.clear()
    analysis.team_overview(db_file, days=1)
    analysis.member_detail(member_id, db_file, days=1)
    assert len(calls) == 1


# --------------------------------------------------
# Real and demo (simulated) cohorts
# --------------------------------------------------

def _add_real_member(db_file, name, profile_name, seed, days=2):
    """
    A member who joined through a normal (not simulated) invite.
    """

    _, code = db.create_invite(name, db_file)
    member_id, _, _ = db.redeem_invite(
        code, TEAM_CONSENT_VERSION, TEAM_SHARED_FIELDS, db_file
    )
    db.insert_observations(member_id, simulate.generate_member_rows(
        simulate.PROFILES[profile_name], days=days, seed=seed
    ), db_file)
    return member_id


def _real_view(overview):
    """
    Everything the overview says about real members and the team.
    """

    return {
        "team": {k: v for k, v in overview["team"].items() if k != "demo_members"},
        "behavior_groups": overview["behavior_groups"],
        "behavior_groups_cohort": overview["behavior_groups_cohort"],
        "members": {
            m["name"]: (m["comparison"], m["behavior_mix"], m["dominant_behavior"],
                        m["summary"])
            for m in overview["members"] if not m["simulated"]
        },
    }


def test_demo_members_do_not_change_real_members_analysis(db_file):
    real = {
        "Real A": _add_real_member(db_file, "Real A", "Asha (focused dev)", seed=11),
        "Real B": _add_real_member(db_file, "Real B", "Chen (analyst)", seed=12),
        "Real C": _add_real_member(db_file, "Real C", "Dana (analyst)", seed=13),
    }
    before = analysis.team_overview(db_file)
    detail_before = analysis.member_detail(real["Real A"], db_file)

    assert before["team"]["real_members"] == 3
    assert before["team"]["demo_members"] == 0
    assert before["behavior_groups"], "the real cohort has enough sessions to group"
    assert before["behavior_groups_cohort"] == "real"

    _simulated_team(db_file)  # five simulated members, including a context switcher
    after = analysis.team_overview(db_file)
    detail_after = analysis.member_detail(real["Real A"], db_file)

    assert after["team"]["demo_members"] == len(simulate.PROFILES)
    assert len(after["members"]) == 3 + len(simulate.PROFILES)
    assert _real_view(after) == _real_view(before)
    assert detail_after["comparison"] == detail_before["comparison"]
    assert detail_after["behavior_mix"] == detail_before["behavior_mix"]
    assert [s["behavior_group"] for s in detail_after["sessions"]] == [
        s["behavior_group"] for s in detail_before["sessions"]
    ]

    # Demo members are compared and grouped among themselves.
    by_name = {m["name"]: m for m in after["members"]}
    assert by_name["Eli (context switcher)"]["comparison"]["status"] == "differs"
    assert set(by_name["Eli (context switcher)"]["behavior_mix"]) <= SWITCHING_LABELS


def test_real_member_is_not_called_unusual_because_of_a_demo_member(db_file):
    real_id = _add_real_member(db_file, "Real Rita", "Asha (focused dev)", seed=9, days=3)
    simulate.add_simulated_member(
        "Eli (context switcher)", simulate.PROFILES["Eli (context switcher)"],
        days=3, seed=4, db_file=db_file,
    )

    overview = analysis.team_overview(db_file)
    rita = {m["id"]: m for m in overview["members"]}[real_id]

    # No other real member to compare with: not "differs".
    assert rita["comparison"]["status"] == "insufficient_data"
    assert analysis.member_detail(real_id, db_file)["comparison"]["status"] == (
        "insufficient_data"
    )
    assert overview["team"]["members"] == 1
    assert overview["team"]["sharing"] == 1
    assert overview["team"]["differs_count"] == 0
    assert overview["team"]["sessions"] == rita["summary"]["sessions"]
    assert overview["team"]["observations"] == rita["summary"]["observations"]
    assert (overview["team"]["real_members"], overview["team"]["demo_members"]) == (1, 1)


def test_demo_only_team_counts_demo_members(db_file):
    ids = _simulated_team(db_file)
    overview = analysis.team_overview(db_file)

    assert overview["team"]["members"] == len(ids)
    assert overview["team"]["real_members"] == 0
    assert overview["team"]["demo_members"] == len(ids)
    assert overview["behavior_groups_cohort"] == "demo"
    assert overview["behavior_groups"]


def test_groups_come_from_real_members_once_they_have_sessions(db_file):
    _simulated_team(db_file)

    # A real member who has not shared anything yet: still the demo groups.
    db.create_invite("Pending", db_file)
    overview = analysis.team_overview(db_file)
    assert overview["behavior_groups_cohort"] == "demo"
    assert overview["team"]["members"] == 1
    assert overview["team"]["sharing"] == 0

    # One real member with a few sessions: too few to group, and the
    # demo members' groups are not shown in their place.
    _add_real_member(db_file, "Real A", "Asha (focused dev)", seed=11, days=1)
    overview = analysis.team_overview(db_file)
    assert overview["behavior_groups_cohort"] == "real"
    assert overview["behavior_groups"] == []


def test_live_rollup():
    rows = [
        {"timestamp": "2026-10-01 09:00:00", "application": "A", "activity_status": "Active"},
        {"timestamp": "2026-10-01 09:00:05", "application": "A", "activity_status": "Active"},
        {"timestamp": "2026-10-01 09:00:10", "application": "B", "activity_status": "Idle"},
        # A long gap (laptop asleep) is not credited in full.
        {"timestamp": "2026-10-01 09:05:00", "application": "A", "activity_status": "Active"},
    ]

    assert analysis.live_rollup(rows) == {
        "active_seconds": 5.0 + analysis.MAX_OBSERVATION_GAP_SECONDS,
        "idle_seconds": 5.0,
        "switches": 2,
        "distinct_apps": 2,
    }
    assert analysis.live_rollup([]) == {
        "active_seconds": 0.0, "idle_seconds": 0.0, "switches": 0, "distinct_apps": 0,
    }


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


def test_expired_invite_status():
    now = datetime.now().astimezone()

    assert analysis.member_status(
        {"invite_expires": (now + timedelta(hours=1)).isoformat()}, now
    ) == "invited"
    assert analysis.member_status(
        {"invite_expires": (now - timedelta(hours=1)).isoformat()}, now
    ) == "expired"
    # Joined members are never "expired", whatever the old expiry.
    assert analysis.member_status(
        {"token_hash": "x", "invite_expires": (now - timedelta(days=9)).isoformat()},
        now,
    ) == "joined"


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
