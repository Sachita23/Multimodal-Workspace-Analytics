"""
Focus scores (focus/scoring.py), their place in the recorded-session
accounting (focus/analytics.py) and the extended live payload.
"""

from datetime import datetime, timedelta

import pytest

import config
from focus import analytics, scoring
from focus.focus_manager import FocusManager

from tests.test_focus import make_session


T0 = datetime(2026, 10, 1, 9, 0, 0)

# Keys of the live payload (focus contract, section 3).
LIVE_CONTRACT_KEYS = {
    "running", "stale", "session_id", "source", "title", "goal", "started_at",
    "planned_s", "remaining_s", "allowed_apps", "current_app", "current_window",
    "allowed", "state", "status", "score", "deviations", "deviation_seconds",
    "last_deviation", "recent_deviations", "updated_at", "pid",
}


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "focus"


def records_for(data_dir, now=None):
    return analytics.session_records(
        data_dir, now=now or T0 + timedelta(hours=6), active_session_id=""
    )


# ---------------------------------------------------------
# session_score
# ---------------------------------------------------------

def test_config_holds_the_scoring_constants():
    assert config.FOCUS_START_SCORE == 100
    assert config.FOCUS_SWITCH_PENALTY == 5
    assert config.FOCUS_OFF_TASK_PENALTY_PER_MINUTE == 6
    assert config.FOCUS_TICK_SECONDS == 1
    assert config.FOCUS_SCORE_WINDOW_DAYS == 30


@pytest.mark.parametrize("deviations, seconds, expected", [
    (0, 0, 100),
    (1, 0, 95),
    (1, 60, 89),
    (2, 90, 81),         # 100 - 10 - 9
    (3, 10, 84),         # 100 - 15 - 1
    (0, 25, 98),         # 100 - 2.5 -> 97.5 rounds to 98 (round half even)
    (10, 600, 0),        # 100 - 50 - 60, clamped at 0
    (40, 0, 0),
])
def test_session_score_formula(deviations, seconds, expected):
    assert scoring.session_score(deviations, seconds) == expected


def test_session_score_ignores_nonsense_inputs():
    assert scoring.session_score(None, None) == 100
    assert scoring.session_score(-3, -60) == 100
    assert scoring.session_score("x", float("nan")) == 100
    assert scoring.session_score(1, float("inf")) == 95


# ---------------------------------------------------------
# overall_score
# ---------------------------------------------------------

def _record(score, start, running=False):
    return {"score": score, "start": start, "running": running}


def test_overall_score_averages_finished_sessions_in_the_window():
    now = T0
    records = [
        _record(90, now - timedelta(days=1)),
        _record(75, now - timedelta(days=29)),
        _record(40, now - timedelta(days=31)),      # outside the window
        _record(10, now - timedelta(minutes=5), running=True),  # still running
    ]

    assert scoring.overall_score(records, now=now) == 82.5
    assert scoring.overall_score(records, now=now, days=2) == 90.0
    assert scoring.overall_score([], now=now) is None
    assert scoring.overall_score(records[2:], now=now) is None


def test_overall_score_rounds_to_one_decimal():
    records = [_record(score, T0) for score in (100, 95, 89)]
    assert scoring.overall_score(records, now=T0) == 94.7


# ---------------------------------------------------------
# Recorded sessions
# ---------------------------------------------------------

def test_records_sessions_and_summary_carry_scores(data_dir):
    # One episode, Chrome then Slack without returning: 120 s off-task.
    make_session(data_dir, "a", [
        (0, "Visual Studio Code", "main.py", True),
        (60, "Google Chrome", "YouTube", False),
        (120, "Slack", "general", False),
        (180, "Visual Studio Code", "main.py", True),
    ], end_offset=300)
    # Two episodes of 30 s each.
    make_session(data_dir, "b", [
        (0, "Visual Studio Code", "main.py", True),
        (60, "Google Chrome", "YouTube", False),
        (90, "Visual Studio Code", "main.py", True),
        (150, "Google Chrome", "YouTube", False),
        (180, "Visual Studio Code", "main.py", True),
    ], end_offset=300, base=T0 + timedelta(hours=1))

    records = {record["id"]: record for record in records_for(data_dir)}

    assert records["a"]["deviations"] == 1
    assert records["a"]["deviation_seconds"] == 120
    assert records["a"]["score"] == 100 - 5 - 12
    assert records["b"]["deviations"] == 2
    assert records["b"]["score"] == 100 - 10 - 6

    assert [e["app"] for e in records["a"]["deviation_episodes"]] == ["Google Chrome"]
    assert records["a"]["deviation_episodes"][0]["seconds"] == pytest.approx(120)
    assert [e["at"] for e in records["b"]["deviation_episodes"]] == [
        T0 + timedelta(hours=1, seconds=60), T0 + timedelta(hours=1, seconds=150),
    ]

    assert analytics.session_json(records["a"])["score"] == 83
    summary = analytics.summarize(list(records.values()), now=T0 + timedelta(days=1))
    assert summary["focus_score"] == round((83 + 84) / 2, 1)
    assert analytics.summarize([], now=T0)["focus_score"] is None


def test_running_sessions_have_a_score_but_do_not_count_overall(data_dir):
    make_session(data_dir, "live", [
        (0, "Visual Studio Code", "main.py", True),
        (60, "Google Chrome", "YouTube", False),
    ])

    records = analytics.session_records(
        data_dir, now=T0 + timedelta(seconds=120), active_session_id="live"
    )

    assert records[0]["running"] is True
    assert records[0]["score"] == 100 - 5 - 6
    assert analytics.summarize(records, now=T0 + timedelta(seconds=120))["focus_score"] is None


# ---------------------------------------------------------
# The one episode rule
# ---------------------------------------------------------

def _timeline(*items):
    state = {True: analytics.ALLOWED, False: analytics.DEVIATION,
             None: analytics.NEUTRAL, "idle": analytics.IDLE}
    return [(T0 + timedelta(seconds=offset), state[kind], app) for offset, kind, app in items]


@pytest.mark.parametrize("items, expected", [
    # The first deviation of a session.
    ([(0, False, "Chrome")], True),
    ([(0, True, "Code"), (10, False, "Chrome")], True),
    # Continuing an excursion in another window or app.
    ([(0, True, "Code"), (10, False, "Chrome"), (20, False, "Slack")], False),
    # A system window (neutral) in between does not end the excursion...
    ([(0, False, "Chrome"), (10, None, "Finder"), (20, False, "Chrome")], False),
    # ...nor does a short idle pause...
    ([(0, False, "Chrome"), (10, "idle", "Chrome"), (100, False, "Chrome")], False),
    # ...but a long break does.
    ([(0, False, "Chrome"), (10, "idle", "Chrome"), (400, False, "Chrome")], True),
    # Back in the zone, then out again: a new episode.
    ([(0, False, "Chrome"), (10, True, "Code"), (20, False, "Chrome")], True),
    # A zone row that lasted no time does not end the excursion.
    ([(0, False, "Chrome"), (10, True, "Code"), (10, False, "Chrome")], False),
    # Not a deviation at all.
    ([(0, False, "Chrome"), (10, True, "Code")], False),
    ([], False),
])
def test_deviation_starts_episode_follows_account_timeline(items, expected):
    timeline = _timeline(*items)

    assert analytics.deviation_starts_episode(timeline, T0) is expected

    if timeline and timeline[-1][1] == analytics.DEVIATION:
        # Same answer as counting episodes with and without the last row.
        end = timeline[-1][0] + timedelta(seconds=5)
        before = analytics.account_timeline(timeline[:-1], T0, end)["episodes"]
        after = analytics.account_timeline(timeline, T0, end)["episodes"]
        assert (after == before + 1) is expected


def test_session_timeline_selects_one_session(data_dir):
    make_session(data_dir, "a", [(0, "Visual Studio Code", "main.py", True)], end_offset=60)
    make_session(data_dir, "b", [(0, "Slack", "general", False)], end_offset=60)
    rows = analytics.read_csv(data_dir / analytics.EVENTS_FILE_NAME)

    assert analytics.session_timeline(rows, "b") == [(T0, analytics.DEVIATION, "Slack")]
    assert analytics.session_timeline(rows, "missing") == []


# ---------------------------------------------------------
# Live payload
# ---------------------------------------------------------

def test_live_status_without_a_heartbeat_has_every_contract_key(data_dir):
    live = analytics.live_status(data_dir)

    assert set(live) == LIVE_CONTRACT_KEYS
    assert live["running"] is False and live["stale"] is True
    assert live["score"] is None and live["deviations"] == 0
    assert live["allowed_apps"] == [] and live["recent_deviations"] == []
    assert live["last_deviation"] is None


def test_tk_assistant_heartbeat_gets_defaults_for_new_fields(data_dir):
    manager = FocusManager(data_dir)
    manager.start_session("Write report", "Finish the draft", 25, ["Visual Studio Code"])
    manager.write_heartbeat("Visual Studio Code", True, "Within focus zone")

    live = analytics.live_status(data_dir)

    assert set(live) == LIVE_CONTRACT_KEYS
    assert live["running"] is True
    assert live["source"] == "assistant"
    assert live["session_id"] == manager.session_id
    assert live["allowed_apps"] == ["Visual Studio Code"]
    assert live["planned_s"] == 25 * 60
    assert live["started_at"] == manager.started_at.isoformat(timespec="seconds")
    assert live["score"] is None
    assert live["deviations"] == 0 and live["deviation_seconds"] == 0
    assert live["last_deviation"] is None and live["recent_deviations"] == []
    manager.stop_session()


def test_old_heartbeat_without_new_fields_still_reads(data_dir):
    # What the assistant wrote before this change.
    analytics.write_live({
        "running": True, "session_id": "x", "title": "T", "goal": "G",
        "remaining_s": 100, "current_app": "Slack", "allowed": False,
        "status": "Focus deviation",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }, data_dir)

    live = analytics.live_status(data_dir)

    assert set(live) == LIVE_CONTRACT_KEYS
    assert live["allowed"] is False and live["remaining_s"] == 100
    assert live["source"] is None and live["score"] is None and live["pid"] is None


def test_live_status_sanitises_heartbeat_fields(data_dir):
    analytics.write_live({
        "running": True, "source": "evil", "state": "nonsense", "score": "high",
        "deviations": -4, "allowed_apps": ["Code", 3, None],
        "last_deviation": {"app": "Slack", "penalty": "5", "at": 7},
        "recent_deviations": [{"app": "Slack", "seconds": 12.6}, "junk"] + [
            {"app": f"App {n}", "seconds": 1} for n in range(20)
        ],
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }, data_dir)

    live = analytics.live_status(data_dir)

    assert live["source"] is None and live["state"] is None and live["score"] is None
    assert live["deviations"] == 0
    assert live["allowed_apps"] == ["Code"]
    assert live["last_deviation"] == {"app": "Slack", "window_title": "", "at": None, "penalty": 5}
    assert live["recent_deviations"][0] == {"app": "Slack", "at": None, "seconds": 12}
    assert len(live["recent_deviations"]) == analytics.MAX_RECENT_DEVIATIONS

