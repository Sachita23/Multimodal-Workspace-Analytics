import argparse
import json
import os
import sys
import tempfile
import time
import traceback
from datetime import datetime
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
    BEHAVIOR_MODEL_FILE,
    PIPELINE_STATUS_FILE,
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
    cluster_with_model,
    load_model
)

from ml.anomaly_detection import (
    run_anomaly_detection
)


# ============================================================
# ATOMIC WRITES
# ============================================================

# os.replace fails on Windows while another process (e.g. the API
# sending the file) has it open; retry a few times before giving up.
REPLACE_ATTEMPTS = 5


def write_atomically(path, write):
    """
    Call write(temp_path) for a temp file next to `path`, then
    swap it into place with os.replace, so readers only ever see
    the old file or the complete new one.
    """

    path = Path(path)

    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent
    )

    os.close(fd)

    temp_path = Path(temp_name)

    try:

        # mkstemp creates the file owner-only; give it the usual
        # permissions, as a plain open() would.
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(temp_path, 0o666 & ~umask)

        write(temp_path)

        for attempt in range(REPLACE_ATTEMPTS):

            try:
                os.replace(temp_path, path)
                break

            except PermissionError:

                if attempt == REPLACE_ATTEMPTS - 1:
                    raise

                time.sleep(0.1 * (attempt + 1))

    finally:

        temp_path.unlink(missing_ok=True)


def write_json_atomically(path, data):

    write_atomically(
        path,
        lambda temp_path: temp_path.write_text(
            json.dumps(data, indent=2),
            encoding="utf-8"
        )
    )


def behavior_model_file():
    """
    Where the behaviour clusters are kept between runs: next to
    PROCESSED_DATA_FILE (config.BEHAVIOR_MODEL_FILE by default), so
    whatever moves the dashboard data moves the model with it.
    """

    return PROCESSED_DATA_FILE.parent / BEHAVIOR_MODEL_FILE.name


# ============================================================
# PIPELINE STATUS (served by /api/status)
# ============================================================

def _now_iso():

    return datetime.now().astimezone().isoformat(
        timespec="seconds"
    )


def load_status():
    """
    The last status written, so a new run keeps last_success.
    """

    try:

        status = json.loads(
            PIPELINE_STATUS_FILE.read_text(encoding="utf-8")
        )

        if isinstance(status, dict):
            return status

    except (OSError, ValueError):
        pass

    return {
        "last_success": None,
        "last_error": None,
        "error_trace": None,
        "rows": 0,
        "sessions": 0,
        "last_observation_ts": None,
        "duration_s": None,
    }


def save_status(status):

    write_json_atomically(PIPELINE_STATUS_FILE, status)


def record_success(status, df, duration):
    """
    Update `status` after a successful build of `df`.
    """

    status.update({
        "last_success": _now_iso(),
        "last_error": None,
        "error_trace": None,
        "rows": int(len(df)),
        "sessions": int(df["session_id"].nunique()),
        "last_observation_ts": (
            df["timestamp"].max().strftime("%Y-%m-%d %H:%M:%S")
            if len(df) else None
        ),
        "duration_s": round(duration, 3),
    })

    return status


def record_failure(status, error, duration):
    """
    Update `status` after a failed run; last_success is kept.
    """

    status.update({
        "last_error": f"{type(error).__name__}: {error}",
        "error_trace": "".join(traceback.format_exception(error)),
        "duration_s": round(duration, 3),
    })

    return status


# ============================================================
# MAIN PIPELINE
# ============================================================

def build_dashboard_dataset(verbose=True, now=None):
    """
    Raw collector CSV -> PROCESSED_DATA_FILE (the dashboard
    contract, FINAL_COLUMNS). Returns the final dataframe.

    `now` decides whether the last session is still open
    (defaults to the current time). The behaviour clusters are
    read from and saved to behavior_model_file(), so finished
    sessions keep their labels from one run to the next.
    """

    say = print if verbose else (lambda *args, **kwargs: None)

    say("=" * 70)
    say("WORKSPACE ANALYTICS PIPELINE")
    say("=" * 70)

    # --------------------------------------------------------
    # 1. LOAD RAW DATA
    # --------------------------------------------------------

    say("\n[1/9] Loading raw data...")

    df = load_raw_data()

    say(
        f"Loaded {len(df)} observations."
    )

    if df.empty:
        raise ValueError(
            "No valid records found in the raw dataset."
        )


    # --------------------------------------------------------
    # 2. CLEAN DATA
    # --------------------------------------------------------

    say("\n[2/9] Cleaning data...")

    df = clean_data(df)


    # --------------------------------------------------------
    # 3. CALCULATE DURATIONS
    # --------------------------------------------------------

    say("\n[3/9] Computing durations...")

    df = calculate_time_difference(df)


    # --------------------------------------------------------
    # 4. DETECT APPLICATION / WINDOW SWITCHES
    # --------------------------------------------------------

    say("\n[4/9] Detecting application and window switches...")

    df = calculate_application_switches(df)

    df = calculate_window_switches(df)


    # --------------------------------------------------------
    # 5. ACTIVITY PROCESSING
    # --------------------------------------------------------

    say("\n[5/9] Processing activity...")

    df = calculate_activity_duration(df)


    # --------------------------------------------------------
    # 6. SESSIONIZATION
    # --------------------------------------------------------

    say("\n[6/9] Creating sessions...")

    # create_sessions also clears the switch flags on each
    # session's first row, so per-session totals below never
    # count a "switch" from the previous session's last app.

    df = create_sessions(df)

    df = calculate_total_switches(df)


    # --------------------------------------------------------
    # 7. FEATURE ENGINEERING
    # --------------------------------------------------------

    say("\n[7/9] Engineering behavioral features...")

    df = calculate_behavior_features(df)


    # --------------------------------------------------------
    # 8. BEHAVIORAL ANALYTICS
    # --------------------------------------------------------

    say("\n[8/9] Running behavioral analytics...")

    say("  -> Clustering...")

    model_file = behavior_model_file()
    previous_model = load_model(model_file)

    df, model = cluster_with_model(
        df,
        now=now,
        model=previous_model
    )

    say("  -> Anomaly detection...")

    df = run_anomaly_detection(df, now=now)


    # --------------------------------------------------------
    # 9. BUILD FINAL DASHBOARD DATASET
    # --------------------------------------------------------

    say("\n[9/9] Building final dashboard dataset...")

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

    # Written to a temp file and swapped in, so the API never
    # serves a half-written CSV.

    write_atomically(
        PROCESSED_DATA_FILE,
        lambda temp_path: final_df.to_csv(
            temp_path,
            index=False
        )
    )

    # Saved after the dataset, so the stored clusters always match
    # what the dashboard shows.
    if model is not None and model != previous_model:
        write_json_atomically(model_file, model)


    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------

    say("\n" + "=" * 70)
    say("PIPELINE COMPLETED SUCCESSFULLY")
    say("=" * 70)

    say(
        f"\nRows: {len(final_df)}"
    )

    say(
        f"Sessions: "
        f"{final_df['session_id'].nunique()}"
    )

    say(
        f"Unique applications: "
        f"{final_df['application'].nunique()}"
    )

    say(
        f"Clusters: "
        f"{final_df['cluster_id'].nunique()}"
    )

    say(
        f"Anomalies: "
        f"{int(final_df['is_anomaly'].sum())}"
    )

    say(
        f"\nDashboard dataset:"
        f"\n{PROCESSED_DATA_FILE}"
    )

    say("\nFinal columns:")

    for column in final_df.columns:
        say(f"  - {column}")

    say("\n" + "=" * 70)

    return final_df


# ============================================================
# ENTRY POINT
# ============================================================

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Process the collected activity once: rebuild sessions, "
                    "behaviour clusters and anomaly flags from the raw file "
                    "into the dashboard dataset, then exit. live_pipeline.py "
                    "does this every 10 seconds.",
        epilog="Uses WORKSPACE_DATA_DIR (default: data) for the raw and "
               "processed files.",
    )

    return parser.parse_args(argv)


def main():
    """
    One manual run. Records the outcome in PIPELINE_STATUS_FILE like
    live_pipeline.py does, so /api/status never shows an old error
    after a successful manual run.
    """

    status = load_status()
    started = time.perf_counter()

    try:
        df = build_dashboard_dataset()

    except Exception as error:
        record_failure(status, error, time.perf_counter() - started)
        _save_status_quietly(status)
        raise

    record_success(status, df, time.perf_counter() - started)
    _save_status_quietly(status)


def _save_status_quietly(status):

    try:
        save_status(status)

    except OSError as error:
        print(f"Could not write {PIPELINE_STATUS_FILE}: {error}")


if __name__ == "__main__":
    # --help answers without processing anything.
    parse_args()
    main()
