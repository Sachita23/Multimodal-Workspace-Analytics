import pandas as pd

from config import (
    RAW_DATA_FILE,
    PROCESSED_DATA_FILE
)


# --------------------------------------------------
# Load Raw Data
# --------------------------------------------------

def load_raw_data():

    if not RAW_DATA_FILE.exists():

        raise FileNotFoundError(
            f"Raw dataset not found: "
            f"{RAW_DATA_FILE}"
        )

    df = pd.read_csv(
        RAW_DATA_FILE
    )

    if df.empty:

        raise ValueError(
            "Raw dataset is empty."
        )

    df["timestamp"] = pd.to_datetime(
        df["timestamp"],
        errors="coerce"
    )

    df = df.dropna(
        subset=["timestamp"]
    )

    df = df.sort_values(
        "timestamp"
    ).reset_index(
        drop=True
    )

    return df


# --------------------------------------------------
# Clean Data
# --------------------------------------------------

def clean_data(df):

    df = df.drop_duplicates()

    numeric_columns = [
        "cpu_percent",
        "memory_percent",
        "battery_percent",
        "idle_seconds"
    ]

    for column in numeric_columns:

        if column in df.columns:

            df[column] = pd.to_numeric(
                df[column],
                errors="coerce"
            )

    # CPU and memory
    if "cpu_percent" in df.columns:

        df["cpu_percent"] = (
            df["cpu_percent"]
            .fillna(0)
            .clip(0, 100)
        )

    if "memory_percent" in df.columns:

        df["memory_percent"] = (
            df["memory_percent"]
            .fillna(0)
            .clip(0, 100)
        )

    # Battery
    if "battery_percent" in df.columns:

        df["battery_percent"] = (
            df["battery_percent"]
            .ffill()
            .bfill()
        )

    # Charging
    if "charging" in df.columns:

        df["charging"] = (
            df["charging"]
            .fillna(False)
            .astype(bool)
        )

    # Application
    df["application"] = (
        df["application"]
        .fillna("Unknown")
        .astype(str)
    )

    # Window
    df["window_title"] = (
        df["window_title"]
        .fillna("Unknown")
        .astype(str)
    )

    # Activity
    df["activity_status"] = (
        df["activity_status"]
        .fillna("Unknown")
        .astype(str)
    )

    return df.reset_index(
        drop=True
    )


# --------------------------------------------------
# Observation Duration
# --------------------------------------------------

def calculate_time_difference(df):

    df["duration_seconds"] = (
        df["timestamp"]
        .diff()
        .dt.total_seconds()
    )

    df["duration_seconds"] = (
        df["duration_seconds"]
        .fillna(0)
        .clip(lower=0)
    )

    return df


# --------------------------------------------------
# Application Switching
# --------------------------------------------------

def calculate_application_switches(df):

    df["previous_application"] = (
        df["application"].shift(1)
    )

    df["application_switch"] = (

        (
            df["application"]
            != df["previous_application"]
        )

        &

        df["previous_application"].notna()
    )

    return df


# --------------------------------------------------
# Window Switching
# --------------------------------------------------

def calculate_window_switches(df):

    df["previous_window"] = (
        df["window_title"].shift(1)
    )

    df["window_switch"] = (

        (
            df["window_title"]
            != df["previous_window"]
        )

        &

        df["previous_window"].notna()
    )

    return df


# --------------------------------------------------
# Active / Idle Duration
# --------------------------------------------------

def calculate_activity_duration(df):

    df["active_duration_seconds"] = 0.0

    df["idle_duration_seconds"] = 0.0

    active_mask = (
        df["activity_status"]
        .str.lower()
        == "active"
    )

    idle_mask = (
        df["activity_status"]
        .str.lower()
        == "idle"
    )

    df.loc[
        active_mask,
        "active_duration_seconds"
    ] = df.loc[
        active_mask,
        "duration_seconds"
    ]

    df.loc[
        idle_mask,
        "idle_duration_seconds"
    ] = df.loc[
        idle_mask,
        "duration_seconds"
    ]

    return df


# --------------------------------------------------
# Create Sessions
# --------------------------------------------------

def create_sessions(df):

    """
    Creates an observation session whenever
    the active application changes.

    This is the initial session definition.
    Later analytics can operate on these sessions.
    """

    switch = (
        df["application_switch"]
        .fillna(False)
        .astype(int)
    )

    df["session_id"] = (
        switch.cumsum()
    )

    return df


# --------------------------------------------------
# Total Application Switches
# --------------------------------------------------

def calculate_total_switches(df):

    df["total_application_switches"] = (
        df["application_switch"]
        .astype(int)
        .cumsum()
    )

    return df


# --------------------------------------------------
# Behavioral Features
# --------------------------------------------------

def calculate_behavior_features(df):
    """
    Calculates observation-level behavioral features
    required by the analytics dashboard.
    """

    # --------------------------------------------------
    # Idle Ratio
    # --------------------------------------------------

    total_time = (
        df["active_duration_seconds"]
        + df["idle_duration_seconds"]
    )

    df["idle_ratio"] = (
        df["idle_duration_seconds"]
        / total_time.replace(0, 1)
    )

    df["idle_ratio"] = (
        df["idle_ratio"]
        .clip(0, 1)
        .round(4)
    )

    # --------------------------------------------------
    # Switching Frequency
    # --------------------------------------------------

    # Number of application switches per minute.
    elapsed_minutes = (
        df["duration_seconds"]
        .cumsum()
        / 60
    )

    df["switch_frequency"] = (
        df["application_switch"]
        .astype(int)
        .cumsum()
        / elapsed_minutes.replace(0, 1)
    )

    df["switch_frequency"] = (
        df["switch_frequency"]
        .replace([float("inf"), -float("inf")], 0)
        .fillna(0)
        .round(4)
    )

    # --------------------------------------------------
    # Unique Applications Seen So Far
    # --------------------------------------------------

    seen_apps = set()
    unique_counts = []

    for application in df["application"]:
        seen_apps.add(application)
        unique_counts.append(len(seen_apps))

    df["unique_app_count"] = unique_counts

    # --------------------------------------------------
    # Average CPU Usage
    # --------------------------------------------------

    df["avg_cpu"] = (
        df["cpu_percent"]
        .expanding()
        .mean()
        .round(2)
    )

    # --------------------------------------------------
    # Average Memory Usage
    # --------------------------------------------------

    df["avg_memory"] = (
        df["memory_percent"]
        .expanding()
        .mean()
        .round(2)
    )

    # --------------------------------------------------
    # Time Features
    # --------------------------------------------------

    df["hour"] = (
        df["timestamp"].dt.hour
    )

    df["day_of_week"] = (
        df["timestamp"].dt.day_name()
    )

    df["is_weekend"] = (
        df["timestamp"].dt.dayofweek >= 5
    )

    return df

# --------------------------------------------------
# Save
# --------------------------------------------------

def save_processed_data(df):

    PROCESSED_DATA_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    df.to_csv(
        PROCESSED_DATA_FILE,
        index=False
    )

    print(
        "\nProcessed dataset saved to:"
    )

    print(
        PROCESSED_DATA_FILE
    )


# --------------------------------------------------
# Main ETL Pipeline
# --------------------------------------------------

def main():

    print("=" * 65)
    print("WORKSPACE ANALYTICS ETL PIPELINE")
    print("=" * 65)

    print("\n[1/7] Loading raw data...")

    df = load_raw_data()

    print(
        f"Records loaded: {len(df)}"
    )

    print("\n[2/7] Cleaning data...")

    df = clean_data(df)

    print(
        "\n[3/7] Calculating durations..."
    )

    df = calculate_time_difference(df)

    print(
        "\n[4/7] Detecting application switches..."
    )

    df = calculate_application_switches(df)

    print(
        "\n[5/7] Detecting window switches..."
    )

    df = calculate_window_switches(df)

    print(
        "\n[6/7] Calculating activity features..."
    )

    df = calculate_activity_duration(df)

    df = create_sessions(df)

    df = calculate_total_switches(df)

    df = calculate_behavior_features(df)

    print(
        "\n[7/7] Saving processed data..."
    )

    save_processed_data(df)

    print("\n" + "=" * 65)
    print("ETL COMPLETED")
    print("=" * 65)

    print(
        f"\nRecords: {len(df)}"
    )

    print(
        f"Applications: "
        f"{df['application'].nunique()}"
    )

    print(
        f"Sessions: "
        f"{df['session_id'].nunique()}"
    )

    print(
        f"Application switches: "
        f"{df['application_switch'].sum()}"
    )

    print(
        f"Window switches: "
        f"{df['window_switch'].sum()}"
    )

    print("\nApplications:")

    print(
        df["application"]
        .value_counts()
    )


if __name__ == "__main__":
    main()