import sys
from pathlib import Path


# ============================================================
# PROJECT PATH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# CONFIG
# ============================================================

from config import (
    PROCESSED_DATA_FILE,
    FINAL_COLUMNS
)


# ============================================================
# ETL IMPORTS
# ============================================================

from etl.session_processor import (
    load_raw_data,
    clean_data,
    calculate_time_difference,
    calculate_application_switches,
    calculate_window_switches,
    calculate_activity_duration,
    create_sessions,
    calculate_total_switches,
    calculate_behavior_features
)


# ============================================================
# MACHINE LEARNING IMPORTS
# ============================================================

from ml.clustering import (
    run_clustering
)

from ml.anomaly_detection import (
    run_anomaly_detection
)


# ============================================================
# MAIN PIPELINE
# ============================================================

def build_dashboard_dataset():

    print("=" * 70)
    print("WORKSPACE ANALYTICS PIPELINE")
    print("=" * 70)

    # --------------------------------------------------------
    # 1. LOAD RAW DATA
    # --------------------------------------------------------

    print("\n[1/9] Loading raw data...")

    df = load_raw_data()

    print(
        f"Loaded {len(df)} observations."
    )

    if df.empty:
        raise ValueError(
            "No valid records found in the raw dataset."
        )


    # --------------------------------------------------------
    # 2. CLEAN DATA
    # --------------------------------------------------------

    print("\n[2/9] Cleaning data...")

    df = clean_data(df)


    # --------------------------------------------------------
    # 3. CALCULATE DURATIONS
    # --------------------------------------------------------

    print("\n[3/9] Computing durations...")

    df = calculate_time_difference(df)


    # --------------------------------------------------------
    # 4. DETECT APPLICATION / WINDOW SWITCHES
    # --------------------------------------------------------

    print("\n[4/9] Detecting application and window switches...")

    df = calculate_application_switches(df)

    df = calculate_window_switches(df)


    # --------------------------------------------------------
    # 5. ACTIVITY PROCESSING
    # --------------------------------------------------------

    print("\n[5/9] Processing activity...")

    df = calculate_activity_duration(df)


    # --------------------------------------------------------
    # 6. SESSIONIZATION
    # --------------------------------------------------------

    print("\n[6/9] Creating sessions...")

    df = create_sessions(df)

    df = calculate_total_switches(df)


    # --------------------------------------------------------
    # 7. FEATURE ENGINEERING
    # --------------------------------------------------------

    print("\n[7/9] Engineering behavioral features...")

    df = calculate_behavior_features(df)


    # --------------------------------------------------------
    # 8. BEHAVIORAL ANALYTICS
    # --------------------------------------------------------

    print("\n[8/9] Running behavioral analytics...")

    print("  -> Clustering...")

    df = run_clustering(df)

    print("  -> Anomaly detection...")

    df = run_anomaly_detection(df)


    # --------------------------------------------------------
    # 9. BUILD FINAL DASHBOARD DATASET
    # --------------------------------------------------------

    print("\n[9/9] Building final dashboard dataset...")

    # Make sure every column required by the frontend exists.

    missing_columns = [
        column
        for column in FINAL_COLUMNS
        if column not in df.columns
    ]

    if missing_columns:

        raise ValueError(
            "Final dataset is missing required columns: "
            + ", ".join(missing_columns)
        )


    # Keep ONLY the columns defined by the dashboard contract.

    final_df = df[
        FINAL_COLUMNS
    ].copy()


    # --------------------------------------------------------
    # SAVE
    # --------------------------------------------------------

    PROCESSED_DATA_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    final_df.to_csv(
        PROCESSED_DATA_FILE,
        index=False
    )


    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------

    print("\n" + "=" * 70)
    print("PIPELINE COMPLETED SUCCESSFULLY")
    print("=" * 70)

    print(
        f"\nRows: {len(final_df)}"
    )

    print(
        f"Sessions: "
        f"{final_df['session_id'].nunique()}"
    )

    print(
        f"Unique applications: "
        f"{final_df['application'].nunique()}"
    )

    print(
        f"Clusters: "
        f"{final_df['cluster_id'].nunique()}"
    )

    print(
        f"Anomalies: "
        f"{int(final_df['is_anomaly'].sum())}"
    )

    print(
        f"\nDashboard dataset:"
        f"\n{PROCESSED_DATA_FILE}"
    )

    print("\nFinal columns:")

    for column in final_df.columns:
        print(f"  - {column}")

    print("\n" + "=" * 70)

    return final_df


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    build_dashboard_dataset()