"""
Regression tests for the pipeline review findings: sessions across
sleep, behaviour labels, the stored behaviour model, the demo feed's
data-directory guard, live_pipeline / status.json, and config values.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import live_pipeline
import run_pipeline
from collector import demo_feed
from config import RAW_COLUMNS
from etl import session_processor
from etl.session_processor import build_session_table, calculate_behavior_features
from ml import clustering
from ml.clustering import (
    IN_PROGRESS,
    MODEL_FEATURES,
    TOO_SHORT,
    assign_labels,
    cluster_sessions,
    run_clustering,
)
from tests.test_pipeline import (
    NOW,
    _active,
    _demo_rows,
    _etl,
    _idle,
    _row,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------
# Sessions across sleep (dark wakes)
# --------------------------------------------------

def _away_with_dark_wakes(start, idle, wakes=5, sleep_minutes=15, samples=6,
                          idle_counts_sleep=False):
    """
    The user is away and the laptop sleeps. Every `sleep_minutes` it
    wakes briefly (macOS dark wake, Power Nap) and the collector logs
    a few samples. Idle time keeps growing; on macOS it does not count
    the time asleep, on Windows it does.

    Returns (rows, time after the last wake, idle at that point).
    """
    rows = []
    t = start

    for _ in range(wakes):
        for _ in range(samples):
            idle += 5
            rows.append(_row(t, "Visual Studio Code", idle=idle))
            t += timedelta(seconds=5)

        t += timedelta(minutes=sleep_minutes)

        if idle_counts_sleep:
            idle += sleep_minutes * 60

    return rows, t, idle


def _sessions(df):
    return df.groupby("session_id").agg(
        rows=("timestamp", "size"),
        active_rows=("activity_status", lambda status: (status == "Active").sum()),
    )


@pytest.mark.parametrize("idle_counts_sleep", [False, True])
def test_dark_wakes_during_a_break_do_not_open_sessions(idle_counts_sleep):
    start = datetime(2026, 10, 1, 9)
    rows = _active(start, 30)

    # The user walks away; the screen idles for 2 minutes, then sleeps.
    away = _idle(start + timedelta(minutes=30), 2)
    rows += away

    wakes, t, _ = _away_with_dark_wakes(
        start + timedelta(minutes=32),
        idle=away[-1]["idle_seconds"],
        idle_counts_sleep=idle_counts_sleep,
    )
    rows += wakes + _active(t, 30)

    sessions = _sessions(_etl(rows))

    # Two work blocks, two sessions; no session without activity.
    assert len(sessions) == 2
    assert (sessions["active_rows"] > 0).all()


def test_lid_closed_while_active_continues_the_session_until_return():
    start = datetime(2026, 10, 1, 9)
    rows = _active(start, 30)

    # The lid closes mid-work (idle only a second or two), so the
    # first dark wake sees an idle time just above it.
    wakes, t, _ = _away_with_dark_wakes(
        start + timedelta(minutes=30), idle=rows[-1]["idle_seconds"]
    )
    rows += wakes + _active(t, 30)

    df = _etl(rows)
    sessions = _sessions(df)

    assert len(sessions) == 2
    assert (sessions["active_rows"] > 0).all()

    # The new session starts when the user is back.
    second = df[df["session_id"] == 1]
    assert second["timestamp"].min() == pd.Timestamp(t)


def test_gaps_split_sessions_when_the_user_was_active_across_them():
    t = datetime(2026, 10, 1, 9)

    # Laptop asleep for 20 minutes; the user is typing before and after.
    rows = (
        _active(t, 10)
        + [_row(t + timedelta(minutes=30), idle=0.4)]
        + _active(t + timedelta(minutes=30, seconds=5), 10)
    )

    assert _etl(rows)["session_id"].nunique() == 2


def test_gaps_split_sessions_when_idle_time_is_not_available():
    # A collector that cannot read idle time reports 0 throughout.
    t = datetime(2026, 10, 1, 9)
    rows = [
        _row(t + timedelta(seconds=offset), idle=0.0)
        for offset in (0, 5, 10, 900, 905, 1800)
    ]

    assert _etl(rows)["session_id"].tolist() == [0, 0, 0, 1, 1, 2]


def test_session_split_only_depends_on_earlier_samples():
    # Appending rows never moves an existing session boundary, so the
    # stored model can remember sessions by their first timestamp.
    start = datetime(2026, 10, 1, 9)
    rows = _active(start, 20)
    wakes, t, _ = _away_with_dark_wakes(start + timedelta(minutes=20), idle=90)
    rows += wakes + _active(t, 20)

    full = _etl(rows)

    for cut in (len(rows) - 200, len(rows) - 50, len(rows) - 1):
        part = _etl(rows[:cut])
        assert part["session_id"].tolist() == full["session_id"].tolist()[:cut]


# --------------------------------------------------
# Behaviour labels
# --------------------------------------------------

# idle_ratio, switch_frequency, unique_app_count, avg_cpu, avg_memory
CALM = [0.05, 0.00, 1, 25, 50]
BUSY = [0.05, 0.90, 6, 30, 50]
IDLE = [0.80, 0.05, 1, 10, 50]
JUGGLING = [0.15, 0.25, 4, 20, 50]


def test_calm_one_app_cluster_is_never_multitasking():
    assert assign_labels(np.array([CALM, BUSY]), 0, 1) == [
        "Stable-activity", "High-switching"
    ]
    assert assign_labels(np.array([CALM, BUSY, IDLE]), 0, 1) == [
        "Stable-activity", "High-switching", "Idle-heavy"
    ]


def test_labels_need_the_behaviour_they_name():
    # Nothing switches often, nothing uses many apps: no cluster may
    # be called High-switching or Multitasking, whatever its rank.
    centroids = np.array([
        [0.10, 0.30, 2, 20, 50],
        [0.20, 0.05, 1, 20, 50],
    ])

    labels = assign_labels(centroids, 0, 1)

    assert "High-switching" not in labels
    assert "Multitasking" not in labels
    assert labels[1] == "Stable-activity"
    assert labels[0] == "Mixed-activity"


def test_every_mostly_idle_cluster_is_idle_heavy():
    centroids = np.array([
        [0.62, 0.06, 1.3, 28, 81],
        [0.86, 0.00, 1.0, 24, 77],
        [0.06, 0.41, 4.0, 33, 81],
    ])

    labels = assign_labels(centroids, 0, 1)

    assert labels[:2] == ["Idle-heavy", "Idle-heavy"]
    assert labels[2] == "High-switching"


def test_similar_clusters_keep_their_names_at_a_refit():
    # Two near-identical calm clusters: without the stored names they
    # are ranked by tiny differences; with them, names stay put.
    centroids = np.array([
        [0.25, 0.13, 3.1, 25, 55],
        [0.27, 0.14, 3.0, 25, 70],
    ])

    fresh = assign_labels(centroids, 0, [0.1, 0.1, 1, 10, 10])
    kept = assign_labels(
        centroids, 0, [0.1, 0.1, 1, 10, 10], previous=fresh[::-1]
    )

    assert set(fresh) == set(kept)
    assert kept == fresh[::-1]


def test_sessions_without_measured_time_take_the_idle_share_of_samples():
    t = pd.Timestamp("2026-10-01 09:00:00")
    df = pd.DataFrame({
        "timestamp": [t + pd.Timedelta(seconds=5 * i) for i in range(6)],
        "application": ["Code"] * 6,
        "activity_status": ["Active", "Active", "Idle", "Idle", "Idle", "Active"],
        "cpu_percent": 20.0,
        "memory_percent": 50.0,
        # Session 1 is one sample, session 2 two samples right after
        # gaps: no measured duration in either.
        "session_id": [0, 0, 0, 1, 2, 2],
        "duration_seconds": [0, 5, 5, 0, 0, 0],
        "idle_duration_seconds": [0, 0, 5, 0, 0, 0],
        "application_switch": False,
    })

    ratios = calculate_behavior_features(df).groupby("session_id")["idle_ratio"].first()

    assert ratios.tolist() == [0.5, 1.0, 0.5]


def test_sessions_that_are_not_clustered_get_no_cluster():
    df, end = _history_with_short_and_open_sessions()

    result = run_clustering(df, now=end)
    by_session = result.groupby("session_id")[["cluster_id", "behavior_label"]].first()

    short = by_session.loc[1]
    open_session = by_session.iloc[-1]

    assert (short["cluster_id"], short["behavior_label"]) == (-1, TOO_SHORT)
    assert (open_session["cluster_id"], open_session["behavior_label"]) == (-1, IN_PROGRESS)

    fitted = by_session.drop(index=[1, by_session.index[-1]])
    assert (fitted["cluster_id"] >= 0).all()
    assert not fitted["behavior_label"].isin([TOO_SHORT, IN_PROGRESS]).any()


def _history_with_short_and_open_sessions():
    rows = []

    for day in range(8):
        start = datetime(2026, 9, 20 + day, 9)
        apps = ("Slack", "Zoom", "Chrome") if day % 2 else ("Visual Studio Code",)
        rows += _active(start, 30, apps=apps, every=6)

        if day == 0:
            # A one-minute session the next hour.
            rows += _active(start + timedelta(hours=1), 1)

    last = datetime(2026, 10, 1, 9)
    rows += _active(last, 20)

    return _etl(rows), last + timedelta(minutes=20)


# --------------------------------------------------
# Stored behaviour model
# --------------------------------------------------

def _session_table(groups, start=datetime(2026, 9, 1, 9)):
    """
    One row per session, like build_session_table, from a list of
    feature rows (finished, long enough).
    """
    table = pd.DataFrame(groups, columns=MODEL_FEATURES)
    table.index.name = "session_id"
    table["start"] = [start + timedelta(hours=2 * i) for i in range(len(table))]
    table["end"] = table["start"] + timedelta(minutes=30)
    table["duration_seconds"] = 1800.0
    table["is_open"] = False
    table["eligible"] = True
    return table


def _behaviours(count, rng):
    """
    Sessions cycling through four distinct behaviours, with a little
    noise on the behaviour features (CPU and memory stay per kind).
    """
    kinds = [IDLE, BUSY, CALM, JUGGLING]
    noise = np.array([0.01, 0.01, 0.01, 0, 0])
    return [
        list(np.array(kinds[i % len(kinds)]) + rng.normal(0, 1, 5) * noise)
        for i in range(count)
    ]


def test_refit_keeps_ids_and_labels_of_unchanged_behaviour():
    rng = np.random.default_rng(1)
    first = _session_table(_behaviours(24, rng))

    ids, labels, model = cluster_sessions(first)
    assert model["fitted"] == 24

    # Enough new sessions of the same kinds to force a refit.
    grown = _session_table(_behaviours(24, np.random.default_rng(1)) + _behaviours(6, rng))
    new_ids, new_labels, new_model = cluster_sessions(grown, model)

    assert new_model["fitted"] == 30
    pd.testing.assert_series_equal(new_ids.iloc[:24], ids)
    pd.testing.assert_series_equal(new_labels.iloc[:24], labels)
    assert {c["label"] for c in new_model["clusters"]} == {
        "Idle-heavy", "High-switching", "Stable-activity", "Multitasking"
    }


def test_new_sessions_join_stored_clusters_without_a_refit():
    rng = np.random.default_rng(2)
    rows = _behaviours(24, rng)
    ids, labels, model = cluster_sessions(_session_table(rows))

    # The stored model is what decides: a session closes that alone
    # would pull a fresh fit around, past sessions do not move.
    odd = [0.45, 0.45, 3.5, 20, 50]
    table = _session_table(rows + [odd])
    new_ids, new_labels, new_model = cluster_sessions(table, model)

    assert new_model["fitted"] == model["fitted"]
    pd.testing.assert_series_equal(new_ids.iloc[:24], ids)
    assert new_ids.iloc[24] >= 0
    assert new_model["clusters"] == model["clusters"]


def test_a_reopened_session_keeps_its_label():
    rows = _behaviours(12, np.random.default_rng(3))
    ids, labels, model = cluster_sessions(_session_table(rows))

    # A dark wake appends a sample to the last session: it is open
    # again for a few minutes, but keeps its cluster.
    table = _session_table(rows)
    table.iloc[-1, table.columns.get_loc("is_open")] = True
    table.iloc[-1, table.columns.get_loc("eligible")] = False

    new_ids, new_labels, _ = cluster_sessions(table, model)

    assert new_ids.iloc[-1] == ids.iloc[-1]
    assert new_labels.iloc[-1] == labels.iloc[-1] != IN_PROGRESS


def test_a_model_for_other_data_is_ignored():
    rows = _behaviours(12, np.random.default_rng(4))
    _, _, model = cluster_sessions(_session_table(rows))

    # Different history (e.g. a new raw file): the stored sessions are
    # gone, so the model starts over instead of mislabelling.
    other = _session_table(rows, start=datetime(2026, 8, 1, 9))
    ids, _, new_model = cluster_sessions(other, model)

    assert set(new_model["sessions"]) == set(
        other["start"].dt.strftime("%Y-%m-%d %H:%M:%S")
    )

    for damaged in ({}, {"version": 99}, {**model, "clusters": []},
                    {**model, "mean": [0, 1]}, "not a model"):
        assert cluster_sessions(other, damaged)[2]["fitted"] == len(other)


def test_stored_model_round_trips_through_json(tmp_path):
    _, _, model = cluster_sessions(_session_table(_behaviours(12, np.random.default_rng(5))))

    path = tmp_path / "model.json"
    run_pipeline.write_json_atomically(path, model)

    assert clustering.load_model(path) == model
    assert clustering.load_model(tmp_path / "missing.json") is None

    path.write_text("{broken")
    assert clustering.load_model(path) is None


# --------------------------------------------------
# Demo feed guard
# --------------------------------------------------

def _case_insensitive(directory):
    probe = directory / "CaseProbe"
    probe.mkdir()
    try:
        return (directory / "caseprobe").exists()
    finally:
        probe.rmdir()


def test_demo_feed_guard_sees_other_spellings_of_the_data_dir(tmp_path, monkeypatch):
    default = tmp_path / "data"
    raw = default / "raw" / "workspace_activity.csv"
    raw.parent.mkdir(parents=True)
    raw.write_text("timestamp\n2026-10-01 09:00:00\n")

    monkeypatch.setattr(demo_feed, "DEFAULT_DATA_DIR", default)
    monkeypatch.setattr(demo_feed, "DEFAULT_RAW_FILE", raw)

    assert demo_feed.uses_default_data_dir(default)
    assert demo_feed.uses_default_data_dir(tmp_path / "data" / ".." / "data")
    assert not demo_feed.uses_default_data_dir(tmp_path / "demo")

    # A folder that only differs in case is refused even before it
    # exists (on a case-insensitive disk it IS data/).
    assert demo_feed.uses_default_data_dir(tmp_path / "DATA")

    # A symlink to the real folder, or to the real raw file.
    link = tmp_path / "linked"
    try:
        link.symlink_to(default, target_is_directory=True)
    except OSError:
        pass
    else:
        assert demo_feed.uses_default_data_dir(link)

    other = tmp_path / "demo"
    (other / "raw").mkdir(parents=True)
    try:
        (other / "raw" / "workspace_activity.csv").symlink_to(raw)
    except OSError:
        pass
    else:
        assert demo_feed.uses_default_data_dir(other)


def test_demo_feed_reset_never_deletes_the_real_raw_file(tmp_path, monkeypatch):
    if not _case_insensitive(tmp_path):
        pytest.skip("needs a case-insensitive file system")

    default = tmp_path / "data"
    raw = default / "raw" / "workspace_activity.csv"
    raw.parent.mkdir(parents=True)
    raw.write_text("timestamp\n2026-10-01 09:00:00\n")

    # WORKSPACE_DATA_DIR=Data on macOS: the same folder, spelled differently.
    spelled = tmp_path / "Data"
    monkeypatch.setattr(demo_feed, "DEFAULT_DATA_DIR", default)
    monkeypatch.setattr(demo_feed, "DEFAULT_RAW_FILE", raw)
    monkeypatch.setattr(demo_feed, "DATA_DIR", spelled)
    monkeypatch.setattr(demo_feed, "RAW_DATA_FILE", spelled / "raw" / "workspace_activity.csv")

    assert demo_feed.main(["--reset", "--backfill-hours", "0.01", "--backfill-only"]) == 2
    assert raw.read_text() == "timestamp\n2026-10-01 09:00:00\n"


# --------------------------------------------------
# write_atomically on Windows (os.replace retries)
# --------------------------------------------------

def _flaky_replace(monkeypatch, failures):
    real_replace = os.replace
    calls = []

    def replace(source, target):
        calls.append(target)
        if len(calls) <= failures:
            raise PermissionError(13, "file in use", str(target))
        return real_replace(source, target)

    monkeypatch.setattr(run_pipeline.os, "replace", replace)
    monkeypatch.setattr(
        run_pipeline, "time", SimpleNamespace(sleep=lambda seconds: None)
    )
    return calls


def test_write_atomically_retries_a_locked_target(tmp_path, monkeypatch):
    target = tmp_path / "out.csv"
    calls = _flaky_replace(monkeypatch, run_pipeline.REPLACE_ATTEMPTS - 1)

    run_pipeline.write_atomically(target, lambda p: p.write_text("ok"))

    assert len(calls) == run_pipeline.REPLACE_ATTEMPTS
    assert target.read_text() == "ok"
    assert list(tmp_path.iterdir()) == [target]


def test_write_atomically_gives_up_and_cleans_up(tmp_path, monkeypatch):
    target = tmp_path / "out.csv"
    target.write_text("old")
    calls = _flaky_replace(monkeypatch, run_pipeline.REPLACE_ATTEMPTS)

    with pytest.raises(PermissionError):
        run_pipeline.write_atomically(target, lambda p: p.write_text("new"))

    assert len(calls) == run_pipeline.REPLACE_ATTEMPTS
    assert target.read_text() == "old"
    assert list(tmp_path.iterdir()) == [target]


# --------------------------------------------------
# live_pipeline and status.json
# --------------------------------------------------

CONTRACT_KEYS = {
    "last_success", "last_error", "error_trace", "rows", "sessions",
    "last_observation_ts", "duration_s",
}


@pytest.fixture
def live(tmp_path, monkeypatch):
    raw = tmp_path / "raw" / "workspace_activity.csv"
    processed = tmp_path / "processed" / "workspace_activity_processed.csv"
    status = tmp_path / "processed" / "status.json"
    raw.parent.mkdir()

    monkeypatch.setattr(session_processor, "RAW_DATA_FILE", raw)
    monkeypatch.setattr(live_pipeline, "RAW_DATA_FILE", raw)
    monkeypatch.setattr(run_pipeline, "PROCESSED_DATA_FILE", processed)
    monkeypatch.setattr(run_pipeline, "PIPELINE_STATUS_FILE", status)
    monkeypatch.setattr(live_pipeline, "PIPELINE_STATUS_FILE", status)

    _, rows = _demo_rows(NOW - timedelta(hours=3), NOW)
    pd.DataFrame(rows, columns=RAW_COLUMNS).to_csv(raw, index=False)

    return raw, processed, status


def _status(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_run_once_writes_the_status_contract(live, capsys):
    _, processed, status_file = live

    status = live_pipeline.run_once()

    assert processed.exists()
    stored = _status(status_file)
    assert set(stored) == CONTRACT_KEYS
    assert stored == status
    assert stored["last_error"] is None and stored["error_trace"] is None
    assert stored["rows"] == 3 * 60 * 12
    assert stored["sessions"] >= 1
    assert stored["last_observation_ts"] == (NOW - timedelta(seconds=5)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    assert len(capsys.readouterr().out.strip().splitlines()) == 1


def test_a_failed_run_keeps_the_last_success(live, monkeypatch):
    _, _, status_file = live
    good = live_pipeline.run_once()

    def broken(verbose=True):
        raise ValueError("Raw dataset is empty.")

    monkeypatch.setattr(live_pipeline, "build_dashboard_dataset", broken)
    bad = live_pipeline.run_once()

    stored = _status(status_file)
    assert stored["last_success"] == good["last_success"]
    assert stored["rows"] == good["rows"]
    assert stored["last_error"] == "ValueError: Raw dataset is empty."
    assert "Traceback" in stored["error_trace"]
    assert stored == bad


def test_an_unwritable_status_file_does_not_stop_the_loop(live, monkeypatch, capsys):
    _, processed, status_file = live

    def locked(status):
        raise PermissionError(13, "file in use", str(status_file))

    monkeypatch.setattr(live_pipeline, "save_status", locked)

    status = live_pipeline.run_once()

    # The dataset is still rebuilt; only the status write is skipped.
    assert processed.exists()
    assert status["last_error"] is None and status["last_success"]
    assert not status_file.exists()
    assert "Could not write" in capsys.readouterr().out


class _Stop(Exception):
    pass


def _run_main(monkeypatch, outcomes, signatures):
    """
    Run live_pipeline.main() for len(signatures) loops; run_once
    returns statuses with the given errors. Returns how many times
    run_once ran.
    """
    runs = []
    loops = iter(signatures)
    clock = [0.0]

    def run_once(status):
        runs.append(clock[0])
        error = outcomes[len(runs) - 1] if len(runs) <= len(outcomes) else None
        return {"last_error": error}

    def sleep(seconds):
        clock[0] += seconds

    def signature():
        try:
            return next(loops)
        except StopIteration:
            raise _Stop

    monkeypatch.setattr(live_pipeline, "run_once", run_once)
    monkeypatch.setattr(live_pipeline, "raw_signature", signature)
    monkeypatch.setattr(live_pipeline, "load_status", lambda: {})
    monkeypatch.setattr(
        live_pipeline, "time",
        SimpleNamespace(sleep=sleep, monotonic=lambda: clock[0])
    )

    with pytest.raises(_Stop):
        live_pipeline.main()

    return runs


def test_unchanged_raw_file_is_skipped(monkeypatch, capsys):
    runs = _run_main(monkeypatch, [], [(1, 1, False)] * 5 + [(2, 2, False)])

    assert len(runs) == 2


def test_a_failure_is_retried_while_the_raw_file_is_unchanged(monkeypatch, capsys):
    runs = _run_main(
        monkeypatch,
        ["PermissionError: file in use", None],
        [(1, 1, False)] * 30,
    )

    # Failed once, retried after the back-off, then nothing to do.
    assert len(runs) == 2
    assert runs[1] - runs[0] >= live_pipeline.retry_delay(1)


def test_retry_back_off_grows_and_is_capped():
    delays = [live_pipeline.retry_delay(n) for n in range(1, 12)]

    assert delays == sorted(delays)
    assert delays[0] == live_pipeline.PIPELINE_REFRESH_SECONDS
    assert delays[-1] == live_pipeline.MAX_RETRY_SECONDS


def test_signature_changes_once_the_last_session_can_close(live):
    raw, _, _ = live
    stat = raw.stat()

    fresh = live_pipeline.raw_signature(now=stat.st_mtime + 30)
    stale = live_pipeline.raw_signature(now=stat.st_mtime + 30 * 60)

    assert fresh[:2] == stale[:2]
    assert fresh != stale


def test_manual_run_records_status_and_clears_an_old_error(live):
    _, _, status_file = live
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(json.dumps({
        "last_success": None,
        "last_error": "PermissionError: file in use",
        "error_trace": "Traceback ...",
    }))

    run_pipeline.main()

    stored = _status(status_file)
    assert stored["last_error"] is None
    assert stored["error_trace"] is None
    assert stored["last_success"]
    assert stored["rows"] == 3 * 60 * 12


def test_manual_run_records_a_failure(live, monkeypatch):
    raw, _, status_file = live
    raw.write_text("")

    with pytest.raises(Exception):
        run_pipeline.main()

    assert _status(status_file)["last_error"]


# --------------------------------------------------
# Config values from the environment
# --------------------------------------------------

def _config_in_subprocess(env, expression):
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("WORKSPACE_")
    }
    environment.update(env)

    return subprocess.run(
        [sys.executable, "-c",
         f"import sys; sys.path.insert(0, {str(PROJECT_ROOT)!r}); "
         f"import config; print({expression})"],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(PROJECT_ROOT.parent),
        timeout=60,
    )


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_settings_fall_back_to_the_defaults(value):
    result = _config_in_subprocess(
        {
            "WORKSPACE_DATA_DIR": value,
            "WORKSPACE_TEAM_DB": value,
            "WORKSPACE_API_PORT": value,
            "WORKSPACE_API_HOST": value,
        },
        "(config.DATA_DIR, config.TEAM_DB_FILE, config.API_PORT, config.API_HOST)",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str((
        PROJECT_ROOT / "data",
        PROJECT_ROOT / "data" / "team" / "team.db",
        5050,
        "127.0.0.1",
    ))


@pytest.mark.parametrize("value", [".", str(PROJECT_ROOT)])
def test_data_dir_may_not_be_the_project_folder(value):
    result = _config_in_subprocess({"WORKSPACE_DATA_DIR": value}, "config.DATA_DIR")

    assert result.returncode != 0
    assert "WORKSPACE_DATA_DIR" in result.stderr


def test_a_separate_data_dir_is_used(tmp_path):
    result = _config_in_subprocess(
        {"WORKSPACE_DATA_DIR": str(tmp_path)}, "config.RAW_DATA_FILE"
    )

    assert result.stdout.strip() == str(tmp_path / "raw" / "workspace_activity.csv")


# --------------------------------------------------
# Whole pipeline on a realistic day
# --------------------------------------------------

def test_pipeline_on_a_day_with_sleep_has_no_empty_sessions(tmp_path, monkeypatch):
    raw = tmp_path / "raw" / "workspace_activity.csv"
    processed = tmp_path / "processed" / "workspace_activity_processed.csv"
    raw.parent.mkdir()
    monkeypatch.setattr(session_processor, "RAW_DATA_FILE", raw)
    monkeypatch.setattr(run_pipeline, "PROCESSED_DATA_FILE", processed)

    rows = []
    t = datetime(2026, 10, 1, 8)
    idle = 1.0

    for block in range(6):
        work = _active(t, 40, apps=("Slack", "Zoom", "Chrome") if block % 2 else
                       ("Visual Studio Code", "Terminal"))
        rows += work
        t += timedelta(minutes=40)

        # Every other break the laptop sleeps, with dark wakes.
        if block % 2:
            wakes, t, idle = _away_with_dark_wakes(t, idle=1.0, wakes=4)
            rows += wakes
        else:
            rows += _idle(t, 15)
            t += timedelta(minutes=15)

    pd.DataFrame(rows, columns=RAW_COLUMNS).to_csv(raw, index=False)

    df = run_pipeline.build_dashboard_dataset(verbose=False, now=t + timedelta(hours=1))
    sessions = _sessions(df)

    assert len(sessions) == 6
    assert (sessions["active_rows"] > 0).all()

    labels = df.groupby("session_id")["behavior_label"].first()
    assert not labels.isin([IN_PROGRESS, TOO_SHORT]).any()

    table = build_session_table(df, ["idle_ratio"], now=t + timedelta(hours=1))
    assert table["eligible"].all()


# --------------------------------------------------
# Lead follow-ups found on the owner's real data
# --------------------------------------------------

def test_activity_during_a_collection_gap_splits_the_session():
    # Real rows: the collector stopped at 10:11:08 and restarted at
    # 13:46:28; idle grew by 649 s across a 12,920 s gap, so the user
    # was at the computer around 13:35. That is two sessions.
    t = datetime(2026, 10, 1, 10, 5)
    rows = _active(t, 6)
    rows.append(_row(datetime(2026, 10, 1, 10, 11, 8), idle=0.62))
    rows += [
        _row(datetime(2026, 10, 1, 13, 46, 28) + timedelta(seconds=5 * i), idle=649.92 + 5 * i)
        for i in range(10)
    ]

    df = _etl(rows)

    assert df["session_id"].nunique() == 2
    second = df[df["session_id"] == df["session_id"].max()]
    assert second["timestamp"].min() == pd.Timestamp(2026, 10, 1, 13, 46, 28)


def test_application_names_that_differ_only_in_case_are_merged():
    from etl.session_processor import unify_application_case

    names = pd.Series(["Whatsapp", "Chrome", "Whatsapp", "WhatsApp", "chrome"])

    assert unify_application_case(names).tolist() == [
        "WhatsApp", "chrome", "WhatsApp", "WhatsApp", "chrome"
    ]
