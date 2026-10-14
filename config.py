import os
from pathlib import Path


# --------------------------------------------------
# Project Root
# --------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent


# --------------------------------------------------
# Data Paths
# --------------------------------------------------

RAW_DATA_FILE = (
    PROJECT_ROOT
    / "data"
    / "raw"
    / "workspace_activity.csv"
)

PROCESSED_DATA_FILE = (
    PROJECT_ROOT
    / "data"
    / "processed"
    / "workspace_activity_processed.csv"
)


# --------------------------------------------------
# Collector Configuration
# --------------------------------------------------

COLLECTION_INTERVAL = 5

IDLE_THRESHOLD_SECONDS = 60


# --------------------------------------------------
# Required Final Dataset Columns
# --------------------------------------------------

FINAL_COLUMNS = [
    "timestamp",
    "application",
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

ANOMALY_CONTAMINATION = 0.05


# --------------------------------------------------
# Near Real-Time Processing
# --------------------------------------------------

PIPELINE_REFRESH_SECONDS = 15

# --------------------------------------------------
# API Server
# --------------------------------------------------

# Keep 127.0.0.1 for single-user use. To let team members
# reach the server, set WORKSPACE_API_HOST=0.0.0.0 (debug mode
# is then switched off automatically).

API_HOST = os.environ.get("WORKSPACE_API_HOST", "127.0.0.1")

API_PORT = int(os.environ.get("WORKSPACE_API_PORT", "5000"))


# --------------------------------------------------
# Team (multi-user) Collection
# --------------------------------------------------

TEAM_DATA_DIR = PROJECT_ROOT / "data" / "team"

TEAM_DB_FILE = Path(
    os.environ.get("WORKSPACE_TEAM_DB", TEAM_DATA_DIR / "team.db")
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
TEAM_SYNC_INTERVAL = 30

# What a member agrees to share. The collector shows this list
# and the server only issues a token for exactly this scope, so
# changing it forces every member to consent again.
TEAM_CONSENT_VERSION = 1

TEAM_SHARED_FIELDS = [
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
