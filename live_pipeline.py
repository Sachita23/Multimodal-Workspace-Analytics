import argparse
import sys
import time
from datetime import datetime
from pathlib import Path


# ============================================================
# PROJECT PATH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from config import (
    PIPELINE_REFRESH_SECONDS,
    PIPELINE_STATUS_FILE,
    RAW_DATA_FILE,
    SESSION_GAP_SECONDS
)

from run_pipeline import (
    build_dashboard_dataset,
    load_status,
    record_failure,
    record_success,
    save_status
)


# After a failed run the pipeline retries even if the raw file is
# unchanged, waiting longer each time up to this many seconds.
MAX_RETRY_SECONDS = 300


# ============================================================
# RUN ONCE
# ============================================================

def raw_signature(now=None):
    """
    What the last run depends on: the raw file's (size, mtime), and
    whether its newest sample is old enough that the last session
    can no longer be open (so one more run labels it once the
    collector stops). None when the raw file is missing.
    """

    try:
        stat = RAW_DATA_FILE.stat()

    except OSError:
        return None

    now = time.time() if now is None else now

    last_session_closed = (
        now - stat.st_mtime > SESSION_GAP_SECONDS
    )

    return (stat.st_size, stat.st_mtime_ns, last_session_closed)


def run_once(status=None):
    """
    Rebuild the dashboard dataset once and record the outcome in
    PIPELINE_STATUS_FILE. Prints one line. Returns the status.
    """

    status = dict(status or load_status())

    started = time.perf_counter()
    clock = datetime.now().strftime("%H:%M:%S")

    try:

        df = build_dashboard_dataset(verbose=False)

    except Exception as error:

        record_failure(status, error, time.perf_counter() - started)

        print(
            f"[{clock}] Pipeline error: {status['last_error']}",
            flush=True
        )

    else:

        record_success(status, df, time.perf_counter() - started)

        print(
            f"[{clock}] Updated: {status['rows']} rows, "
            f"{status['sessions']} sessions, "
            f"{int(df.loc[df['is_anomaly'], 'session_id'].nunique())} "
            "unusual sessions, "
            f"newest {status['last_observation_ts']} "
            f"({status['duration_s']:.2f}s)",
            flush=True
        )

    try:
        save_status(status)

    except OSError as error:
        print(f"[{clock}] Could not write {PIPELINE_STATUS_FILE}: {error}")

    return status


def retry_delay(failures):
    """
    Seconds to wait before retrying after `failures` failed runs in a
    row: the refresh interval, doubling up to MAX_RETRY_SECONDS.
    """

    return min(
        MAX_RETRY_SECONDS,
        PIPELINE_REFRESH_SECONDS * 2 ** max(0, failures - 1)
    )


# ============================================================
# MAIN LOOP
# ============================================================

def main():

    print("=" * 70)
    print("REAL-TIME WORKSPACE ANALYTICS PIPELINE")
    print("=" * 70)

    print(f"\nRaw data:       {RAW_DATA_FILE}")
    print(f"Status file:    {PIPELINE_STATUS_FILE}")
    print(
        f"Refresh every:  {PIPELINE_REFRESH_SECONDS} seconds "
        "(skipped while the raw file is unchanged)"
    )

    print("\nPress CTRL+C to stop.\n", flush=True)

    status = load_status()
    last_signature = object()
    failures = 0
    retry_at = 0.0

    try:

        while True:

            signature = raw_signature()

            # New data is processed right away. After a failure the
            # same data is also tried again after a back-off, so an
            # error (e.g. the CSV briefly locked on Windows) does not
            # stick while the collector is paused.
            changed = signature != last_signature
            retry_due = failures > 0 and time.monotonic() >= retry_at

            if changed or retry_due:

                status = run_once(status)
                last_signature = signature

                if status.get("last_error"):
                    failures += 1
                    retry_at = time.monotonic() + retry_delay(failures)

                else:
                    failures = 0

            time.sleep(
                PIPELINE_REFRESH_SECONDS
            )

    except KeyboardInterrupt:

        print("\n")
        print("=" * 70)
        print("LIVE PIPELINE STOPPED")
        print("=" * 70)


# ============================================================
# ENTRY POINT
# ============================================================

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Keep the dashboard dataset current: reprocess the "
                    "collected activity every "
                    f"{PIPELINE_REFRESH_SECONDS} seconds while the raw file "
                    "changes, and record each run in status.json. Stop with "
                    "Ctrl+C. Normally started by start_project.py.",
        epilog="Uses WORKSPACE_DATA_DIR (default: data) for the raw and "
               "processed files. For a single run, use run_pipeline.py.",
    )

    return parser.parse_args(argv)


if __name__ == "__main__":

    # --help answers without starting the loop.
    parse_args()
    main()
