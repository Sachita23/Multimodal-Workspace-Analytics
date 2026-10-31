from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

import run_pipeline
from collector import demo_feed
from config import (
    FINAL_COLUMNS,
    MAX_OBSERVATION_GAP_SECONDS,
    RAW_COLUMNS,
)
from etl import session_processor
from etl.session_processor import (
    calculate_activity_duration,
    calculate_application_switches,
    calculate_behavior_features,
    calculate_time_difference,
    calculate_total_switches,
    calculate_window_switches,
    clean_data,
    create_sessions,
)
from ml.anomaly_detection import run_anomaly_detection
from ml.clustering import assign_labels, run_clustering


NOW = datetime(2026, 10, 1, 18, 0, 0)
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


# --------------------------------------------------
# Helpers
# --------------------------------------------------

def _row(t, application="Visual Studio Code", idle=1.0, **overrides):
    return {
        "timestamp": t.strftime(TIMESTAMP_FORMAT),
        "application": application,
        "window_title": f"{application} window",
        "activity_status": "Idle" if idle >= 60 else "Active",
        "idle_seconds": float(idle),
        "cpu_percent": 20.0,
        "memory_percent": 50.0,
        "battery_percent": 80.0,
        "charging": True,
        **overrides,
    }


def _active(start, minutes, apps=("Visual Studio Code", "Terminal"), every=12):
    """
    Active rows every 5 s, switching app every `every` samples.
    """
    return [
        _row(start + timedelta(seconds=5 * i), apps[(i // every) % len(apps)])
        for i in range(minutes * 12)
    ]


def _idle(start, minutes, application="Visual Studio Code"):
    """
    The collector keeps running while the user is away.
    """
    return [
        _row(start + timedelta(seconds=5 * i), application, idle=5.0 * (i + 1))
        for i in range(minutes * 12)
    ]


def _etl(rows):
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)

    df = clean_data(df)
    df = calculate_time_difference(df)
    df = calculate_application_switches(df)
    df = calculate_window_switches(df)
    df = calculate_activity_duration(df)
    df = create_sessions(df)
    df = calculate_total_switches(df)
    return calculate_behavior_features(df)


def _demo_rows(start, end, seed=7):
    user = demo_feed.DemoUser(start, seed=seed)
    return user, demo_feed.backfill(user, start, end)


@pytest.fixture
def paths(tmp_path, monkeypatch):
    raw = tmp_path / "raw" / "workspace_activity.csv"
    processed = tmp_path / "processed" / "workspace_activity_processed.csv"
    raw.parent.mkdir()

    monkeypatch.setattr(session_processor, "RAW_DATA_FILE", raw)
    monkeypatch.setattr(run_pipeline, "PROCESSED_DATA_FILE", processed)
    monkeypatch.setattr(
        run_pipeline, "PIPELINE_STATUS_FILE", processed.parent / "status.json"
    )

    # The behaviour model follows the processed file.
    assert run_pipeline.behavior_model_file().parent == processed.parent

    return raw, processed


def _write_raw(raw, rows):
    pd.DataFrame(rows, columns=RAW_COLUMNS).to_csv(raw, index=False)


# --------------------------------------------------
# Sessions
# --------------------------------------------------

def test_one_long_idle_break_gives_exactly_two_sessions():
    start = datetime(2026, 10, 1, 9)
    rows = (
        _active(start, 30)
        + _idle(start + timedelta(minutes=30), 30)
        + _active(start + timedelta(minutes=60), 30)
    )

    df = _etl(rows)

    assert df["session_id"].nunique() == 2

    # The idle stretch stays in the session it ended.
    first = df[df["session_id"] == 0]
    assert first["idle_seconds"].max() == 30 * 60
    assert df.loc[df["session_id"] == 1, "idle_seconds"].max() < 60


def test_short_pauses_do_not_split_sessions():
    start = datetime(2026, 10, 1, 9)
    rows = (
        _active(start, 20)
        + _idle(start + timedelta(minutes=20), 4)
        + _active(start + timedelta(minutes=24), 20)
    )

    assert _etl(rows)["session_id"].nunique() == 1


def test_no_switch_is_counted_on_a_session_first_row():
    start = datetime(2026, 10, 1, 9)
    rows = (
        _active(start, 10, apps=("Slack", "Zoom"))
        + _idle(start + timedelta(minutes=10), 10, application="Zoom")
        # Comes back in a different app and window.
        + _active(start + timedelta(minutes=20), 10, apps=("Excel",))
    )

    df = _etl(rows)
    first_rows = df["session_id"] != df["session_id"].shift(1)

    assert df["session_id"].nunique() == 2
    assert not df.loc[first_rows, "application_switch"].any()
    assert not df.loc[first_rows, "window_switch"].any()

    # The Excel session has no switches at all.
    second = df[df["session_id"] == 1]
    assert (second["total_application_switches"] == 0).all()
    assert (second["switch_frequency"] == 0).all()


def test_unobserved_gaps_are_not_credited_as_time():
    t = datetime(2026, 10, 1, 9)
    rows = [
        _row(t),
        _row(t + timedelta(seconds=5)),
        _row(t + timedelta(seconds=125)),   # 2-minute pause of the collector
        _row(t + timedelta(seconds=725)),   # 10-minute sleep: new session
    ]

    df = _etl(rows)

    assert df["duration_seconds"].tolist() == [0, 5, MAX_OBSERVATION_GAP_SECONDS, 0]
    assert df["session_id"].tolist() == [0, 0, 0, 1]


def test_switch_frequency_of_a_short_session_is_not_inflated():
    t = datetime(2026, 10, 1, 9)
    rows = [_row(t), _row(t + timedelta(seconds=5), "Slack")]

    df = _etl(rows)

    # One switch over 5 seconds is not 12 switches a minute.
    assert df["switch_frequency"].iloc[-1] == pytest.approx(1 / 5)


# --------------------------------------------------
# Behaviour labels
# --------------------------------------------------

def test_assign_labels_gives_each_cluster_its_own_label():
    # idle_ratio, switch_frequency, unique_app_count, avg_cpu, avg_memory
    centroids = np.array([
        [0.10, 0.5, 5, 20, 50],
        [0.15, 0.1, 3, 20, 50],
        [0.12, 0.05, 1, 20, 50],
        [0.20, 0.2, 2, 20, 50],
    ])

    labels = assign_labels(centroids, mean=0, std=1)

    assert len(set(labels)) == 4
    assert labels[0] == "High-switching"
    assert labels[1] == "Multitasking"
    assert labels[2] == "Stable-activity"
    assert "Idle-heavy" not in labels

    centroids[3, 0] = 0.8
    labels = assign_labels(centroids, mean=0, std=1)
    assert labels[3] == "Idle-heavy"
    assert len(set(labels)) == 4


def test_too_few_sessions_are_not_clustered():
    start = datetime(2026, 10, 1, 9)
    df = run_clustering(_etl(_active(start, 30)), now=NOW)

    assert (df["cluster_id"] == -1).all()
    assert (df["behavior_label"] == "Not-enough-history").all()


# --------------------------------------------------
# Anomalies
# --------------------------------------------------

def _history_with_odd_last_session():
    rows = []

    for day in range(10):
        start = datetime(2026, 9, 20 + day, 9)
        rows += _active(start, 30)

    # Switching app on every sample across six apps.
    odd_start = datetime(2026, 10, 1, 9)
    apps = ("Slack", "Chrome", "Zoom", "Mail", "Word", "Excel")
    rows += _active(odd_start, 30, apps=apps, every=1)

    return _etl(rows), odd_start + timedelta(minutes=30)


def test_finished_unusual_session_is_flagged_once_per_session():
    df, _ = _history_with_odd_last_session()

    result = run_anomaly_detection(df, now=datetime(2026, 10, 2, 9))

    per_session = result.groupby("session_id").agg(
        flags=("is_anomaly", "nunique"),
        scores=("anomaly_score", "nunique"),
        flagged=("is_anomaly", "first"),
    )

    # Every row of a session shares one decision.
    assert (per_session["flags"] == 1).all()
    assert (per_session["scores"] == 1).all()
    assert per_session["flagged"].tolist() == [False] * 10 + [True]


def test_open_last_session_is_not_flagged():
    df, end = _history_with_odd_last_session()

    result = run_anomaly_detection(df, now=end)
    last = result[result["session_id"] == result["session_id"].max()]

    assert not result["is_anomaly"].any()
    assert (last["anomaly_score"] == 0).all()


# --------------------------------------------------
# Full pipeline
# --------------------------------------------------

def test_pipeline_output_contract_and_atomic_write(paths):
    raw, processed = paths
    user, rows = _demo_rows(NOW - timedelta(hours=24), NOW)
    _write_raw(raw, rows)

    final_df = run_pipeline.build_dashboard_dataset(verbose=False, now=NOW)

    assert list(final_df.columns) == FINAL_COLUMNS
    assert list(pd.read_csv(processed, nrows=1).columns) == FINAL_COLUMNS
    assert "window_title" in FINAL_COLUMNS
    assert not list(processed.parent.glob("*.tmp"))

    # Sessions follow the breaks, not the number of idle samples.
    sessions = final_df["session_id"].nunique()
    assert user.breaks >= 5
    assert sessions <= user.breaks + 2

    labels = set(final_df["behavior_label"])
    assert len(labels) > 1
    assert "Not-enough-history" not in labels

    # The still-open last session is never flagged.
    last = final_df[final_df["session_id"] == final_df["session_id"].max()]
    assert not last["is_anomaly"].any()
    assert (last["anomaly_score"] == 0).all()


def _labels_by_session(df):
    return df.groupby("session_id")[["cluster_id", "behavior_label"]].first()


def test_cluster_ids_of_finished_sessions_are_stable(paths):
    raw, _ = paths
    user, rows = _demo_rows(NOW - timedelta(hours=24), NOW, seed=7)
    _write_raw(raw, rows)

    before = run_pipeline.build_dashboard_dataset(verbose=False, now=NOW)
    model = run_pipeline.load_model(run_pipeline.behavior_model_file())

    last = before["session_id"].max()
    assert (before.loc[before["session_id"] == last, "behavior_label"]
            == "In-progress").all()

    # One more minute of live rows: nothing changes.
    more = demo_feed.backfill(user, NOW, NOW + timedelta(seconds=60))
    _write_raw(raw, rows + more)

    after = run_pipeline.build_dashboard_dataset(
        verbose=False, now=NOW + timedelta(seconds=60)
    )

    assert after["session_id"].max() == last

    pd.testing.assert_frame_equal(
        _labels_by_session(before[before["session_id"] < last]),
        _labels_by_session(after[after["session_id"] < last]),
    )

    # The user takes a 10-minute break and works for 30 more minutes,
    # so the session that was open closes and a new one starts.
    later = NOW + timedelta(seconds=60)
    rows = rows + more + _idle(later, 10) + _active(later + timedelta(minutes=10), 30)
    _write_raw(raw, rows)

    closed = run_pipeline.build_dashboard_dataset(
        verbose=False, now=later + timedelta(minutes=40)
    )

    assert closed["session_id"].max() == last + 1

    # Every session that was already labelled keeps its cluster and
    # label, and the session that just closed gets a real one.
    pd.testing.assert_frame_equal(
        _labels_by_session(before[before["session_id"] < last]),
        _labels_by_session(closed[closed["session_id"] < last]),
    )

    newly_closed = _labels_by_session(closed).loc[last]
    assert newly_closed["cluster_id"] >= 0
    assert newly_closed["behavior_label"] not in ("In-progress", "Too-short")

    stored = run_pipeline.load_model(run_pipeline.behavior_model_file())
    assert stored["fitted"] == model["fitted"]
    assert len(stored["sessions"]) == len(model["sessions"]) + 1


def test_failed_write_leaves_no_temp_file(tmp_path):
    target = tmp_path / "out.csv"

    def explode(temp_path):
        temp_path.write_text("partial")
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        run_pipeline.write_atomically(target, explode)

    assert list(tmp_path.iterdir()) == []

    run_pipeline.write_atomically(target, lambda p: p.write_text("ok"))
    assert target.read_text() == "ok"
    assert list(tmp_path.iterdir()) == [target]


# --------------------------------------------------
# Single-user demo feed
# --------------------------------------------------

def test_demo_feed_refuses_the_real_data_directory(monkeypatch, tmp_path):
    assert demo_feed.uses_default_data_dir(demo_feed.DEFAULT_DATA_DIR)
    assert not demo_feed.uses_default_data_dir(tmp_path)

    monkeypatch.setattr(demo_feed, "DATA_DIR", demo_feed.DEFAULT_DATA_DIR)
    monkeypatch.setattr(demo_feed, "RAW_DATA_FILE", tmp_path / "raw.csv")

    assert demo_feed.main(["--backfill-hours", "1", "--backfill-only"]) == 2
    assert not (tmp_path / "raw.csv").exists()


def test_demo_feed_backfill_writes_raw_rows_with_long_breaks(monkeypatch, tmp_path):
    raw = tmp_path / "raw" / "workspace_activity.csv"
    monkeypatch.setattr(demo_feed, "DATA_DIR", tmp_path)
    monkeypatch.setattr(demo_feed, "RAW_DATA_FILE", raw)

    assert demo_feed.main(["--backfill-hours", "6", "--backfill-only", "--seed", "1"]) == 0

    df = pd.read_csv(raw)
    assert list(df.columns) == RAW_COLUMNS
    assert len(df) == 6 * 60 * 12
    assert set(df["activity_status"]) == {"Active", "Idle"}

    # Breaks of 5-30 minutes: idle time climbs past 5 minutes.
    assert 300 <= df["idle_seconds"].max() <= 30 * 60 + 5

    # A second run appends to the existing rows instead of backfilling again.
    assert demo_feed.main(["--backfill-hours", "6", "--backfill-only"]) == 0
    assert len(pd.read_csv(raw)) == len(df)
