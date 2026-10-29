"""
Focus scores.

A session starts at config.FOCUS_START_SCORE. Each deviation episode
(see focus/analytics.py: one continuous excursion outside the focus zone,
however many windows it passes through) costs FOCUS_SWITCH_PENALTY
points, and every minute outside the zone FOCUS_OFF_TASK_PENALTY_PER_MINUTE
more. Idle and neutral time cost nothing.

The overall focus score is the mean score of the finished sessions that
started in the last FOCUS_SCORE_WINDOW_DAYS days.

Recorded sessions (focus/analytics.py) and the live engine
(focus/engine.py) both score through session_score(), from deviation
counts produced by the one episode rule in analytics.account_timeline.
"""

import math
import sys

from datetime import datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402


START_SCORE = getattr(config, "FOCUS_START_SCORE", 100)
SWITCH_PENALTY = getattr(config, "FOCUS_SWITCH_PENALTY", 5)
OFF_TASK_PENALTY_PER_MINUTE = getattr(config, "FOCUS_OFF_TASK_PENALTY_PER_MINUTE", 6)
SCORE_WINDOW_DAYS = getattr(config, "FOCUS_SCORE_WINDOW_DAYS", 30)


def _non_negative(value):
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0

    return number if math.isfinite(number) and number > 0 else 0.0


def session_score(deviations, deviation_seconds):
    """
    The score (0..FOCUS_START_SCORE) of a session with `deviations`
    episodes and `deviation_seconds` outside the focus zone.
    """

    penalty = (
        SWITCH_PENALTY * _non_negative(deviations)
        + OFF_TASK_PENALTY_PER_MINUTE * _non_negative(deviation_seconds) / 60
    )

    return max(0, int(round(START_SCORE - penalty)))


def overall_score(records, now=None, days=SCORE_WINDOW_DAYS):
    """
    Mean score (one decimal) of the finished sessions in `records`
    (focus/analytics.py records) that started in the last `days` days;
    None when there are none.
    """

    now = now or datetime.now()
    since = now - timedelta(days=days)

    scores = [
        record["score"]
        for record in records
        if not record.get("running")
        and record.get("score") is not None
        and record.get("start") is not None
        and record["start"] >= since
    ]

    if not scores:
        return None

    return round(sum(scores) / len(scores), 1)
