import os
from pathlib import Path


# --------------------------------------------------
# Project Root
# --------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent


def _project_path(value):
    """
    An absolute path; relative settings are taken from the project
    root, so they mean the same thing whichever folder you start in.
    """

    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _env(name, default):
    """
    The environment variable `name`, or `default` when it is unset,
    empty or only whitespace (e.g. `export VAR=$UNSET_VAR`), so a
    blank value never turns into "the project root" or a crash.
    """

    value = os.environ.get(name, "")
    return value.strip() or default


# --------------------------------------------------
# Data Paths
# --------------------------------------------------

# Single-user data (raw collector output, processed dashboard data,
# Focus Assistant logs). Point WORKSPACE_DATA_DIR somewhere else (e.g.
# data/demo) to run demos without touching your own recorded activity.
DATA_DIR = _project_path(
    _env("WORKSPACE_DATA_DIR", PROJECT_ROOT / "data")
)

def _is_project_root(path):
    # samefile, not ==: it also catches other spellings of the same
    # folder (case-insensitive file systems, symlinks).
    try:
        return os.path.samefile(path, PROJECT_ROOT)
    except OSError:
        return False


# The project root holds the source packages (focus/, collector/, ...)
# and is not git-ignored: personal data must never land there.
if _is_project_root(DATA_DIR):
    raise ValueError(
        f"WORKSPACE_DATA_DIR points at the project folder ({PROJECT_ROOT}). "
        "Use a separate folder such as data/demo, or unset it."
    )

RAW_DATA_FILE = (
    DATA_DIR
    / "raw"
    / "workspace_activity.csv"
)

PROCESSED_DATA_FILE = (
    DATA_DIR
    / "processed"
    / "workspace_activity_processed.csv"
)

# Written by run_pipeline.py / live_pipeline.py after every run;
# served by /api/status.
PIPELINE_STATUS_FILE = (
    DATA_DIR
    / "processed"
    / "status.json"
)

# The behaviour clusters of finished sessions, kept between pipeline
# runs so past sessions keep their labels (see ml/clustering.py).
# run_pipeline.py keeps it next to PROCESSED_DATA_FILE.
BEHAVIOR_MODEL_FILE = (
    DATA_DIR
    / "processed"
    / "behavior_model.json"
)

FOCUS_DATA_DIR = DATA_DIR / "focus"


# --------------------------------------------------
# Collector Configuration
# --------------------------------------------------

COLLECTION_INTERVAL = 5

IDLE_THRESHOLD_SECONDS = 60

# Columns the collector writes to RAW_DATA_FILE (and, for team
# members, uploads; see TEAM_SHARED_FIELDS).
RAW_COLUMNS = [
    "timestamp",
    "application",
    "window_title",
    "activity_status",
    "idle_seconds",
    "cpu_percent",
    "memory_percent",
    "battery_percent",
    "charging",
]

# A gap between two samples longer than this (laptop asleep, collector
# paused) is not credited as active or idle time.
MAX_OBSERVATION_GAP_SECONDS = 3 * COLLECTION_INTERVAL


# --------------------------------------------------
# Required Final Dataset Columns
# --------------------------------------------------

FINAL_COLUMNS = [
    "timestamp",
    "application",
    "window_title",
    "activity_status",
    "idle_seconds",
    "cpu_percent",
    "memory_percent",
    "battery_percent",
    "charging",
    "duration_seconds",
    "previous_application",
    "application_switch",
    "previous_window",
    "window_switch",
    "active_duration_seconds",
    "idle_duration_seconds",
    "session_id",
    "total_application_switches",
    "idle_ratio",
    "switch_frequency",
    "unique_app_count",
    "avg_cpu",
    "avg_memory",
    "hour",
    "day_of_week",
    "is_weekend",
    "cluster_id",
    "behavior_label",
    "anomaly_score",
    "is_anomaly"
]


# --------------------------------------------------
# Session Configuration
# --------------------------------------------------

# If the user is inactive for this long,
# the next activity starts a new workspace session.
SESSION_IDLE_BREAK_SECONDS = 300  # 5 minutes

# Very large timestamp gaps also create a new session.
SESSION_GAP_SECONDS = 300


# --------------------------------------------------
# ML Configuration
# --------------------------------------------------

DEFAULT_CLUSTER_COUNT = 4

RANDOM_STATE = 42

# Robust distance (MAD-scaled, RMS over features) at which a session
# counts as unusual.
ANOMALY_DISTANCE = 3.5

# Sessions shorter than this say little about behaviour; they are left
# out of clustering, anomaly detection and team comparisons.
MIN_SESSION_SECONDS = 120


# --------------------------------------------------
# Near Real-Time Processing
# --------------------------------------------------

# How often live_pipeline.py reprocesses and the dashboard polls.
PIPELINE_REFRESH_SECONDS = 10

# --------------------------------------------------
# API Server
# --------------------------------------------------

# Keep 127.0.0.1 for single-user use. To let team members reach the
# server, run `start_project.py --team`. Beyond loopback, your own
# activity endpoints only answer this machine or the admin key.

API_HOST = _env("WORKSPACE_API_HOST", "127.0.0.1")

# 5050, not Flask's usual 5000: macOS AirPlay Receiver holds :5000.
API_PORT = int(_env("WORKSPACE_API_PORT", "5050"))

DASHBOARD_URL = _env("WORKSPACE_DASHBOARD_URL", "http://localhost:5173")


# --------------------------------------------------
# Team (multi-user) Collection
# --------------------------------------------------

# Team database, admin key and demo state. WORKSPACE_TEAM_DIR moves all
# of them at once (the test suite uses this to stay off real data).
TEAM_DATA_DIR = _project_path(
    _env("WORKSPACE_TEAM_DIR", PROJECT_ROOT / "data" / "team")
)

TEAM_DB_FILE = _project_path(
    _env("WORKSPACE_TEAM_DB", TEAM_DATA_DIR / "team.db")
)

# The main user's key for the Team page and admin endpoints.
# Generated on first server start unless WORKSPACE_TEAM_ADMIN_KEY is set.
TEAM_ADMIN_KEY_FILE = TEAM_DATA_DIR / "admin_key"

# A member counts as online if their last upload was this recent.
TEAM_ONLINE_SECONDS = 120

TEAM_INVITE_TTL_HOURS = 168

# Upper bound on observations accepted in one upload.
TEAM_MAX_BATCH = 500

# How often a member's collector uploads its queued observations.
TEAM_SYNC_INTERVAL = 10

# Team analysis only looks at the most recent days of observations,
# which bounds memory and rebuild time.
TEAM_ANALYSIS_DAYS = 30

# What a member agrees to share. The collector shows this list
# and the server only issues a token for exactly this scope, so
# changing it forces every member to consent again.
TEAM_CONSENT_VERSION = 1

TEAM_SHARED_FIELDS = list(RAW_COLUMNS)


# --------------------------------------------------
# Focus Sessions (scoring and the headless engine)
# --------------------------------------------------

# Every focus session starts at this score. Each deviation episode
# (switching to an application outside the declared focus zone) costs
# FOCUS_SWITCH_PENALTY points, and every minute spent outside the zone
# another FOCUS_OFF_TASK_PENALTY_PER_MINUTE (see focus/scoring.py).
FOCUS_START_SCORE = 100

FOCUS_SWITCH_PENALTY = 5

FOCUS_OFF_TASK_PENALTY_PER_MINUTE = 6

# How often the focus engine (focus/engine.py) checks the frontmost window.
FOCUS_TICK_SECONDS = 1

# The overall focus score averages the sessions of this many days.
FOCUS_SCORE_WINDOW_DAYS = 30

# The application a session is started from (the dashboard's browser,
# the member's terminal) counts as neither focus nor deviation for this
# long, so there is time to switch to an allowed application.
FOCUS_START_GRACE_SECONDS = 10
