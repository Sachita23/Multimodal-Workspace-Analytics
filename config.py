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